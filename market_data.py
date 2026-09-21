"""
Multi-exchange market data client with automatic failover.
Tries exchanges in order until one succeeds.
Includes per-exchange rate limiting, caching, and ban detection.
"""
import time
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from threading import Lock

import ccxt

logger = logging.getLogger(__name__)


# Priority order - cloud-friendly exchanges first
DEFAULT_EXCHANGES = ['okx', 'bybit', 'kraken', 'binance']

# Symbol format compatibility
# Most exchanges use BTC/USDT, but Kraken uses BTC/USDT as well via ccxt.
# ccxt handles the mapping internally.

# Per-exchange rate limit (milliseconds between requests)
RATE_LIMITS = {
    'okx': 200,
    'bybit': 200,
    'kraken': 500,
    'binance': 300,
}

# Cache TTL per data type (seconds)
OHLCV_CACHE_TTL = 30
TICKER_CACHE_TTL = 10


class ExchangeWrapper:
    """Wrapper around a single ccxt exchange with rate limiting + ban tracking."""

    def __init__(self, name: str):
        self.name = name
        self.exchange = None
        self.last_call = 0.0
        self.banned_until = 0.0
        self.consecutive_errors = 0
        self.lock = Lock()
        self._init_exchange()

    def _init_exchange(self):
        try:
            ex_class = getattr(ccxt, self.name)
            self.exchange = ex_class({
                'enableRateLimit': True,
                'timeout': 15000,
                'options': {'defaultType': 'spot'},
            })
            logger.info(f"Exchange initialized: {self.name}")
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
        # Detect ban
        if '418' in err_str or '429' in err_str or 'banned' in err_str.lower():
            ban_seconds = 3600  # default 1 hour
            # Try to extract ban timestamp
            import re
            m = re.search(r'banned until (\d+)', err_str)
            if m:
                try:
                    ban_ts = int(m.group(1)) / 1000.0
                    ban_seconds = max(60, ban_ts - time.time())
                except Exception:
                    pass
            # Cap at 6 hours to avoid indefinite bans
            ban_seconds = min(ban_seconds, 21600)
            self.banned_until = time.time() + ban_seconds
            logger.error(
                f"[{self.name}] BANNED for {ban_seconds/60:.1f} min "
                f"({operation} {symbol}): {err_str[:120]}"
            )
        else:
            self.consecutive_errors += 1
            logger.warning(f"[{self.name}] {operation} {symbol} error: {err_str[:120]}")

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
    """Multi-exchange client with failover, caching, and ban awareness."""

    def __init__(self, exchange_names: Optional[List[str]] = None):
        names = exchange_names or DEFAULT_EXCHANGES
        self.wrappers: List[ExchangeWrapper] = [ExchangeWrapper(n) for n in names]
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
