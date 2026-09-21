"""
Multi-exchange market data client with automatic failover.
Supports API keys for higher rate limits.
Priority: user-configured, with signed requests when keys available.
"""
import os
import re
import time
import logging
from typing import Dict, List, Optional, Tuple
from threading import Lock

import ccxt

logger = logging.getLogger(__name__)


# Priority: cloud-friendly first, then Binance (which needs keys to be safe)
DEFAULT_EXCHANGES = ['okx', 'bybit', 'kraken', 'binance']

RATE_LIMITS = {
    'okx': 150,
    'bybit': 150,
    'kraken': 400,
    'binance': 200,   # stricter to protect quota
}

OHLCV_CACHE_TTL = 30
TICKER_CACHE_TTL = 10

# Max ban duration we self-impose (seconds)
MAX_SELF_BAN = 6 * 3600


class ExchangeWrapper:
    """Wrapper with rate limiting, ban tracking, and API key support."""

    def __init__(self, name: str, api_key: str = '', secret: str = ''):
        self.name = name
        self.api_key = api_key
        self.secret = secret
        self.exchange = None
        self.last_call = 0.0
        self.banned_until = 0.0
        self.consecutive_errors = 0
        self.lock = Lock()
        self.authenticated = bool(api_key and secret)
        self._init_exchange()

    def _init_exchange(self):
        try:
            ex_class = getattr(ccxt, self.name)
            opts = {
                'enableRateLimit': True,
                'timeout': 15000,
                'options': {'defaultType': 'spot'},
            }
            if self.authenticated:
                opts['apiKey'] = self.api_key
                opts['secret'] = self.secret
            self.exchange = ex_class(opts)
            auth_label = "authenticated" if self.authenticated else "public"
            logger.info(f"Exchange initialized: {self.name} ({auth_label})")
        except Exception as e:
            logger.error(f"Failed to init {self.name}: {e}")
            self.exchange = None

    def is_available(self) -> bool:
        if self.exchange is None:
            return False
        if time.time() < self.banned_until:
            return False
        return True

    def _throttle(self):
        rate_limit_s = RATE_LIMITS.get(self.name, 300) / 1000.0
        with self.lock:
            elapsed = time.time() - self.last_call
            if elapsed < rate_limit_s:
                time.sleep(rate_limit_s - elapsed)
            self.last_call = time.time()

    def _handle_error(self, e: Exception, operation: str, symbol: str):
        err_str = str(e)
        # Ban detection
        if '418' in err_str or 'banned' in err_str.lower():
            ban_seconds = MAX_SELF_BAN
            m = re.search(r'banned until (\d+)', err_str)
            if m:
                try:
                    ban_ts = int(m.group(1)) / 1000.0
                    ban_seconds = max(60, ban_ts - time.time())
                except Exception:
                    pass
            ban_seconds = min(ban_seconds, MAX_SELF_BAN)
            self.banned_until = time.time() + ban_seconds
            logger.error(
                f"[{self.name}] BANNED for {ban_seconds/60:.1f} min "
                f"({operation} {symbol})"
            )
        elif '429' in err_str or 'rate limit' in err_str.lower():
            # Soft rate limit - back off but don't ban
            backoff = 60
            self.banned_until = time.time() + backoff
            logger.warning(f"[{self.name}] Rate limited - backoff {backoff}s")
        else:
            self.consecutive_errors += 1
            logger.warning(f"[{self.name}] {operation} {symbol}: {err_str[:120]}")

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> Optional[List]:
        if not self.is_available():
            return None
        self._throttle()
        try:
            data = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            self.consecutive_errors = 0
            return data
        except Exception as e:
            self._handle_error(e, "ohlcv", symbol)
            return None

    def fetch_ticker(self, symbol: str) -> Optional[Dict]:
        if not self.is_available():
            return None
        self._throttle()
        try:
            data = self.exchange.fetch_ticker(symbol)
            self.consecutive_errors = 0
            return data
        except Exception as e:
            self._handle_error(e, "ticker", symbol)
            return None


class MarketDataClient:
    """Multi-exchange client with failover, caching, and API key support."""

    def __init__(self, exchange_names: Optional[List[str]] = None,
                 exchange_keys: Optional[Dict[str, Dict[str, str]]] = None):
        names = exchange_names or DEFAULT_EXCHANGES
        keys = exchange_keys or {}
        self.wrappers: List[ExchangeWrapper] = []
        for n in names:
            cred = keys.get(n, {})
            self.wrappers.append(ExchangeWrapper(
                n,
                api_key=cred.get('api_key', ''),
                secret=cred.get('secret', ''),
            ))
        self._ohlcv_cache: Dict[str, Tuple[float, List]] = {}
        self._ticker_cache: Dict[str, Tuple[float, Dict]] = {}
        self._cache_lock = Lock()
        self._active_exchange = None

    def _cache_get(self, cache: Dict, key: str, ttl: float):
        with self._cache_lock:
            entry = cache.get(key)
            if entry and (time.time() - entry[0]) < ttl:
                return entry[1]
        return None

    def _cache_put(self, cache: Dict, key: str, value):
        with self._cache_lock:
            cache[key] = (time.time(), value)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> Optional[List]:
        cache_key = f"{symbol}|{timeframe}|{limit}"
        cached = self._cache_get(self._ohlcv_cache, cache_key, OHLCV_CACHE_TTL)
        if cached is not None:
            return cached

        for w in self.wrappers:
            if not w.is_available():
                continue
            data = w.fetch_ohlcv(symbol, timeframe, limit)
            if data and len(data) > 0:
                self._cache_put(self._ohlcv_cache, cache_key, data)
                self._active_exchange = w.name
                return data
        return None

    def fetch_ticker(self, symbol: str) -> Optional[Dict]:
        cached = self._cache_get(self._ticker_cache, symbol, TICKER_CACHE_TTL)
        if cached is not None:
            return cached

        for w in self.wrappers:
            if not w.is_available():
                continue
            data = w.fetch_ticker(symbol)
            if data:
                self._cache_put(self._ticker_cache, symbol, data)
                self._active_exchange = w.name
                return data
        return None

    def get_status(self) -> Dict:
        return {
            'active': self._active_exchange,
            'exchanges': [
                {
                    'name': w.name,
                    'available': w.is_available(),
                    'authenticated': w.authenticated,
                    'banned_for_seconds': max(0, int(w.banned_until - time.time())),
                    'errors': w.consecutive_errors,
                }
                for w in self.wrappers
            ]
        }

    def clear_cache(self):
        with self._cache_lock:
            self._ohlcv_cache.clear()
            self._ticker_cache.clear()


# ======================================================================
# Global registry of API keys per exchange
# ======================================================================
def get_exchange_keys() -> Dict[str, Dict[str, str]]:
    """Read API keys from environment for each supported exchange."""
    keys = {}

    # Binance
    if os.environ.get('BINANCE_API_KEY') and os.environ.get('BINANCE_SECRET_KEY'):
        keys['binance'] = {
            'api_key': os.environ['BINANCE_API_KEY'],
            'secret': os.environ['BINANCE_SECRET_KEY'],
        }

    # OKX
    if os.environ.get('OKX_API_KEY') and os.environ.get('OKX_SECRET_KEY'):
        keys['okx'] = {
            'api_key': os.environ['OKX_API_KEY'],
            'secret': os.environ['OKX_SECRET_KEY'],
        }

    # Bybit
    if os.environ.get('BYBIT_API_KEY') and os.environ.get('BYBIT_SECRET_KEY'):
        keys['bybit'] = {
            'api_key': os.environ['BYBIT_API_KEY'],
            'secret': os.environ['BYBIT_SECRET_KEY'],
        }

    # Kraken
    if os.environ.get('KRAKEN_API_KEY') and os.environ.get('KRAKEN_SECRET_KEY'):
        keys['kraken'] = {
            'api_key': os.environ['KRAKEN_API_KEY'],
            'secret': os.environ['KRAKEN_SECRET_KEY'],
        }

    return keys
