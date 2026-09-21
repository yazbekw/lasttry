"""
Crypto Signal Analyzer Bot - Discriminating Edition
Version 8.0.0 - 10 Indicators + Signal Accuracy Tracking + MTF + Dynamic Weights
All notifications in English, no emojis.
"""

import os
import json
import time
import sqlite3
import logging
import threading
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict, field
from enum import Enum
from threading import Lock
from collections import OrderedDict

from flask import Flask, render_template, jsonify, request

from config_manager import config
from settings_metadata import SETTINGS_METADATA, SETTINGS_GROUPS
from market_data import MarketDataClient, get_exchange_keys

# ======================================================================
# Logging setup
# ======================================================================
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('crypto_signal.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


# ======================================================================
# Safe config getter (handles missing keys gracefully)
# ======================================================================
def _cfg(key: str, default=None):
    """Safely get config value with default fallback."""
    try:
        v = config.get(key)
        return v if v is not None else default
    except Exception:
        return default


# ======================================================================
# Static application values
# ======================================================================
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', 'crypto_buy_alerts')
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
FGI_API_URL = "https://api.alternative.me/fng/"
SECRET_KEY = os.environ.get('SECRET_KEY', 'crypto-signal-secret-2026')
FLASK_DEBUG = os.environ.get('FLASK_DEBUG', 'false').lower() in ('true', '1', 'yes')
PORT = int(os.environ.get('PORT', 5000))

COIN_NAMES = {
    "BTC/USDT": "Bitcoin", "ETH/USDT": "Ethereum", "BNB/USDT": "Binance Coin",
    "SOL/USDT": "Solana", "XRP/USDT": "Ripple", "LTC/USDT": "Litecoin",
    "ADA/USDT": "Cardano", "DOGE/USDT": "Dogecoin", "AVAX/USDT": "Avalanche",
    "DOT/USDT": "Polkadot", "MATIC/USDT": "Polygon", "LINK/USDT": "Chainlink",
    "TRX/USDT": "Tron", "TON/USDT": "Toncoin", "ATOM/USDT": "Cosmos",
    "NEAR/USDT": "NEAR Protocol", "APT/USDT": "Aptos", "ARB/USDT": "Arbitrum",
    "OP/USDT": "Optimism", "INJ/USDT": "Injective",
}


# ======================================================================
# Data structures
# ======================================================================
class SignalType(Enum):
    STRONG_BUY = "STRONG BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG SELL"


class IndicatorType(Enum):
    TREND = "trend"
    MOMENTUM = "momentum"
    VOLUME = "volume"
    STRUCTURE = "structure"
    REL_RSI = "rel_rsi"
    REL_STRENGTH = "rel_strength"
    VOL_MOMENTUM = "vol_momentum"
    ATR_VOLATILITY = "atr_volatility"
    BB_POSITION = "bb_position"
    MACD_HISTOGRAM = "macd_histogram"


# Base maximum score per indicator (unchanged reference)
BASE_MAX_PER_INDICATOR = {
    IndicatorType.TREND.value: 1.5,
    IndicatorType.MOMENTUM.value: 1.0,
    IndicatorType.VOLUME.value: 1.0,
    IndicatorType.STRUCTURE.value: 1.0,
    IndicatorType.REL_RSI.value: 0.5,
    IndicatorType.REL_STRENGTH.value: 0.75,
    IndicatorType.VOL_MOMENTUM.value: 0.5,
    IndicatorType.ATR_VOLATILITY.value: 0.3,
    IndicatorType.BB_POSITION.value: 0.75,
    IndicatorType.MACD_HISTOGRAM.value: 0.5,
}
MAX_TOTAL_SCORE = sum(BASE_MAX_PER_INDICATOR.values())  # 7.8


@dataclass
class CoinConfig:
    symbol: str
    name: str
    base_asset: str
    quote_asset: str
    enabled: bool = True


@dataclass
class IndicatorScore:
    name: str
    raw_score: float
    weighted_score: float
    percentage: float
    weight: float
    description: str
    color: str
    direction: str


@dataclass
class CoinSignal:
    symbol: str
    name: str
    current_price: float
    price_change_24h: float
    high_24h: float
    low_24h: float
    volume_24h: float
    total_percentage: float
    total_score: float
    signal_type: SignalType
    signal_strength: str
    signal_color: str
    indicator_scores: Dict[str, IndicatorScore]
    last_updated: datetime
    fear_greed_value: int
    btc_bullish: bool
    htf_trend: str
    is_valid: bool = True
    error_message: Optional[str] = None
    # ---- New: MTF + Risk ----
    mtf_score: float = 0.0
    mtf_details: Dict[str, str] = field(default_factory=dict)
    atr_value: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    risk_reward_ratio: float = 0.0
    risk_amount_usd: float = 0.0
    suggested_position_usd: float = 0.0


@dataclass
class Notification:
    id: str
    timestamp: datetime
    coin_symbol: str
    coin_name: str
    message: str
    notification_type: str
    signal_strength: float
    price: float


# ======================================================================
# Indicator display maps
# ======================================================================
INDICATOR_COLORS = {
    IndicatorType.TREND.value: '#2E86AB',
    IndicatorType.MOMENTUM.value: '#A23B72',
    IndicatorType.VOLUME.value: '#3BB273',
    IndicatorType.STRUCTURE.value: '#8F2D56',
    IndicatorType.REL_RSI.value: '#6A4C93',
    IndicatorType.REL_STRENGTH.value: '#1982C4',
    IndicatorType.VOL_MOMENTUM.value: '#FFCA3A',
    IndicatorType.ATR_VOLATILITY.value: '#8AC926',
    IndicatorType.BB_POSITION.value: '#FF595E',
    IndicatorType.MACD_HISTOGRAM.value: '#6A994E',
}

INDICATOR_DISPLAY_NAMES = {
    IndicatorType.TREND.value: 'Trend (EMA 50/200)',
    IndicatorType.MOMENTUM.value: 'Momentum (RSI)',
    IndicatorType.VOLUME.value: 'Volume Confirmation',
    IndicatorType.STRUCTURE.value: 'Structure (Breakout)',
    IndicatorType.REL_RSI.value: 'Relative RSI (vs market)',
    IndicatorType.REL_STRENGTH.value: 'Relative Strength vs BTC',
    IndicatorType.VOL_MOMENTUM.value: 'Volume Momentum',
    IndicatorType.ATR_VOLATILITY.value: 'ATR Volatility Expansion',
    IndicatorType.BB_POSITION.value: 'Bollinger Band Position',
    IndicatorType.MACD_HISTOGRAM.value: 'MACD Histogram',
}

INDICATOR_DESCRIPTIONS = {
    IndicatorType.TREND.value: 'Price vs EMA50 and EMA200',
    IndicatorType.MOMENTUM.value: 'RSI oversold / overbought zones',
    IndicatorType.VOLUME.value: 'Volume spike with price direction',
    IndicatorType.STRUCTURE.value: 'Breakout above highs or breakdown below lows',
    IndicatorType.REL_RSI.value: 'Compares this coin RSI to market average RSI',
    IndicatorType.REL_STRENGTH.value: 'Compares 24h change to BTC 24h change',
    IndicatorType.VOL_MOMENTUM.value: 'Compares current volume to 5-candle-ago volume',
    IndicatorType.ATR_VOLATILITY.value: 'ATR expansion indicates breakout potential',
    IndicatorType.BB_POSITION.value: 'Price position inside Bollinger Bands',
    IndicatorType.MACD_HISTOGRAM.value: 'MACD histogram sign and crossover',
}


# ======================================================================
# Helper to build coin list from config
# ======================================================================
def get_coins() -> List[CoinConfig]:
    symbols_str = config.get('COINS_LIST')
    symbols = [s.strip() for s in symbols_str.split(',') if s.strip()]
    coins = []
    for s in symbols:
        base = s.split('/')[0] if '/' in s else s
        quote = s.split('/')[1] if '/' in s else 'USDT'
        coins.append(CoinConfig(
            symbol=s,
            name=COIN_NAMES.get(s, base),
            base_asset=base,
            quote_asset=quote,
        ))
    return coins


# ======================================================================
# Signal Tracker (SQLite-backed accuracy tracking)
# ======================================================================
class SignalTracker:
    """
    Persists every actionable signal and evaluates its outcome
    at 1h, 4h and 24h horizons. Tracks per-indicator accuracy
    to feed dynamic weights.
    """

    HORIZONS = [('1h', 3600), ('4h', 4 * 3600), ('24h', 24 * 3600)]

    def __init__(self, db_path: str = None):
        self.db_path = db_path or _cfg('SIGNAL_TRACKER_DB', 'signals.db')
        self._lock = Lock()
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path, timeout=10.0)

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    score REAL NOT NULL,
                    percentage REAL NOT NULL,
                    price REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    raw_scores TEXT,
                    mtf_score REAL DEFAULT 0,
                    price_1h REAL, price_4h REAL, price_24h REAL,
                    pct_1h REAL, pct_4h REAL, pct_24h REAL,
                    outcome TEXT,
                    evaluated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_signals_sym_created
                    ON signals(symbol, created_at);

                CREATE TABLE IF NOT EXISTS indicator_accuracy (
                    indicator TEXT PRIMARY KEY,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total INTEGER DEFAULT 0,
                    updated_at TEXT
                );
            """)
        logger.info(f"SignalTracker DB ready: {self.db_path}")

    # ------------------------------------------------------------------
    def record(self, signal: CoinSignal):
        """Persist an actionable (non-neutral) signal."""
        if signal.signal_type == SignalType.NEUTRAL:
            return
        try:
            raw_scores = {k: v.raw_score for k, v in signal.indicator_scores.items()}
            with self._lock, self._conn() as conn:
                conn.execute("""
                    INSERT INTO signals
                        (symbol, signal_type, score, percentage, price, created_at,
                         raw_scores, mtf_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    signal.symbol, signal.signal_type.name, signal.total_score,
                    signal.total_percentage, signal.current_price,
                    signal.last_updated.isoformat(),
                    json.dumps(raw_scores), signal.mtf_score,
                ))
        except Exception as e:
            logger.error(f"SignalTracker.record error: {e}")

    # ------------------------------------------------------------------
    def evaluate_pending(self, current_prices: Dict[str, float]):
        """
        Evaluate any signals that have reached their horizon.
        current_prices: {symbol: latest_price}
        """
        now = datetime.now()
        try:
            with self._lock, self._conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT id, symbol, price, created_at, raw_scores,
                           price_1h, price_4h, price_24h,
                           signal_type, pct_1h, pct_4h, pct_24h
                    FROM signals
                    WHERE evaluated_at IS NULL OR price_24h IS NULL
                """).fetchall()

                for row in rows:
                    created = datetime.fromisoformat(row['created_at'])
                    age = (now - created).total_seconds()
                    price_now = current_prices.get(row['symbol'])
                    if not price_now or not row['price']:
                        continue

                    updates: Dict[str, Any] = {}
                    # Check each horizon
                    for label, secs in self.HORIZONS:
                        col_price = f'price_{label}'
                        col_pct = f'pct_{label}'
                        if row[col_price] is None and age >= secs:
                            updates[col_price] = price_now
                            updates[col_pct] = (price_now - row['price']) / row['price'] * 100.0

                    if updates:
                        set_clause = ", ".join(f"{k}=?" for k in updates)
                        conn.execute(
                            f"UPDATE signals SET {set_clause} WHERE id=?",
                            list(updates.values()) + [row['id']]
                        )

                    # If 24h done, mark as evaluated and update indicator accuracy
                    if updates.get('price_24h') is not None:
                        pct_24h = updates['pct_24h']
                        outcome = 'win' if abs(pct_24h) >= 0.5 else 'neutral'
                        # Determine if signal was correct
                        if row['signal_type'] in ('BUY', 'STRONG_BUY'):
                            sig_correct = pct_24h > 0.5
                        else:
                            sig_correct = pct_24h < -0.5

                        conn.execute(
                            "UPDATE signals SET outcome=?, evaluated_at=? WHERE id=?",
                            ('win' if sig_correct else 'loss',
                             now.isoformat(), row['id'])
                        )

                        # Update per-indicator accuracy
                        if outcome != 'neutral' and row['raw_scores']:
                            self._update_indicator_accuracy(
                                conn, row['raw_scores'], pct_24h
                            )
        except Exception as e:
            logger.error(f"SignalTracker.evaluate_pending error: {e}")

    @staticmethod
    def _update_indicator_accuracy(conn, raw_scores_json: str, pct_24h: float):
        try:
            raw = json.loads(raw_scores_json)
        except Exception:
            return
        actual_dir = 1 if pct_24h > 0 else -1
        now_iso = datetime.now().isoformat()
        for ind, score in raw.items():
            if not score:
                continue
            ind_dir = 1 if score > 0 else -1
            correct = (ind_dir == actual_dir)
            conn.execute("""
                INSERT INTO indicator_accuracy (indicator, wins, losses, total, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(indicator) DO UPDATE SET
                    wins = wins + excluded.wins,
                    losses = losses + excluded.losses,
                    total = total + 1,
                    updated_at = excluded.updated_at
            """, (ind, 1 if correct else 0, 0 if correct else 1, now_iso))

    # ------------------------------------------------------------------
    def get_indicator_stats(self, indicator: str) -> Dict[str, int]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT wins, losses, total FROM indicator_accuracy WHERE indicator=?",
                    (indicator,)
                ).fetchone()
                if row:
                    return {'wins': row['wins'], 'losses': row['losses'], 'total': row['total']}
        except Exception as e:
            logger.error(f"get_indicator_stats error: {e}")
        return {'wins': 0, 'losses': 0, 'total': 0}

    def get_all_stats(self) -> List[Dict]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT indicator, wins, losses, total FROM indicator_accuracy
                """).fetchall()
                return [{
                    'indicator': r['indicator'],
                    'wins': r['wins'],
                    'losses': r['losses'],
                    'total': r['total'],
                    'win_rate': (r['wins'] / r['total'] * 100.0) if r['total'] else 0.0,
                } for r in rows]
        except Exception as e:
            logger.error(f"get_all_stats error: {e}")
            return []

    def get_signal_stats(self, days: int = 30) -> Dict:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("""
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) AS wins,
                        SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
                        AVG(pct_24h) AS avg_pct,
                        AVG(CASE WHEN outcome IS NOT NULL THEN pct_24h END) AS avg_outcome
                    FROM signals
                    WHERE created_at >= ?
                """, (since,)).fetchone()
                total = row['total'] or 0
                wins = row['wins'] or 0
                losses = row['losses'] or 0
                decided = wins + losses
                return {
                    'total_signals': total,
                    'wins': wins,
                    'losses': losses,
                    'win_rate': (wins / decided * 100.0) if decided else 0.0,
                    'avg_pct_24h': row['avg_pct'] or 0.0,
                    'avg_outcome_pct': row['avg_outcome'] or 0.0,
                    'period_days': days,
                }
        except Exception as e:
            logger.error(f"get_signal_stats error: {e}")
            return {}


# Singleton — used across classes
signal_tracker = SignalTracker()


# ======================================================================
# Fear & Greed Fetcher (thread-safe + lock)
# ======================================================================
class FearGreedFetcher:
    def __init__(self):
        self.last_value = 50
        self.last_update: Optional[datetime] = None
        self.cache_ttl = 300
        self._lock = Lock()

    def get(self) -> int:
        with self._lock:
            now = datetime.now()
            if self.last_update and (now - self.last_update).total_seconds() < self.cache_ttl:
                return self.last_value
            try:
                resp = requests.get(FGI_API_URL, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if 'data' in data and data['data']:
                        value = int(data['data'][0]['value'])
                        self.last_value = value
                        self.last_update = now
                        return value
            except Exception as e:
                logger.error(f"FGI fetch error: {e}")
            return self.last_value


# ======================================================================
# Indicator Calculators (unchanged math, kept intact)
# ======================================================================
class IndicatorCalculator:

    @staticmethod
    def ema(prices: List[float], period: int) -> List[float]:
        if not prices:
            return []
        k = 2 / (period + 1)
        ema_values = [prices[0]]
        for i in range(1, len(prices)):
            ema_values.append(prices[i] * k + ema_values[-1] * (1 - k))
        return ema_values

    @staticmethod
    def rsi(prices: List[float], period: int = 14) -> List[Optional[float]]:
        if len(prices) < period + 1:
            return [None] * len(prices)
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        rsi_values = [None] * period
        for i in range(period, len(prices)):
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
            rsi_values.append(rsi)
            if i < len(prices) - 1:
                avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        return rsi_values

    @staticmethod
    def sma(values: List[float], period: int) -> float:
        if len(values) < period:
            return 0.0
        return sum(values[-period:]) / period

    @staticmethod
    def std_dev(values: List[float], period: int) -> float:
        if len(values) < period:
            return 0.0
        mean = sum(values[-period:]) / period
        variance = sum((x - mean) ** 2 for x in values[-period:]) / period
        return variance ** 0.5

    @staticmethod
    def atr(highs: List[float], lows: List[float], closes: List[float],
            period: int = 14) -> List[float]:
        if len(closes) < period + 1:
            return []
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1]),
            )
            trs.append(tr)
        atr_values = []
        for i in range(len(trs)):
            if i + 1 < period:
                atr_values.append(0.0)
            else:
                atr_values.append(sum(trs[i+1-period:i+1]) / period)
        return atr_values

    @staticmethod
    def macd(prices: List[float], fast: int = 12, slow: int = 26,
             signal: int = 9) -> Tuple[List[float], List[float], List[float]]:
        if len(prices) < slow + signal:
            return [], [], []
        ema_fast = IndicatorCalculator.ema(prices, fast)
        ema_slow = IndicatorCalculator.ema(prices, slow)
        macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
        signal_line = IndicatorCalculator.ema(macd_line, signal)
        histogram = [m - s for m, s in zip(macd_line, signal_line)]
        return macd_line, signal_line, histogram

    # ---------------- Base indicators ----------------
    @staticmethod
    def trend_score(close_prices: List[float]) -> float:
        if len(close_prices) < 200:
            return 0.0
        try:
            ema50 = IndicatorCalculator.ema(close_prices, 50)[-1]
            ema200 = IndicatorCalculator.ema(close_prices, 200)[-1]
            price = close_prices[-1]
            if price > ema50 > ema200: return 1.5
            if price > ema50 and ema50 <= ema200: return 0.75
            if price < ema50 < ema200: return -1.5
            if price < ema50 and ema50 >= ema200: return -0.75
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def momentum_score(close_prices: List[float]) -> float:
        if len(close_prices) < 15:
            return 0.0
        try:
            rsi_vals = IndicatorCalculator.rsi(close_prices, 14)
            rsi = rsi_vals[-1] if rsi_vals and rsi_vals[-1] is not None else 50.0
            if rsi < config.get('RSI_OVERSOLD_STRONG'): return 1.0
            if rsi < config.get('RSI_OVERSOLD'): return 0.75
            if rsi < 50: return 0.25
            if rsi > config.get('RSI_OVERBOUGHT_STRONG'): return -1.0
            if rsi > config.get('RSI_OVERBOUGHT'): return -0.75
            if rsi > 50: return -0.25
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def volume_score(volumes: List[float], close_prices: List[float]) -> float:
        if len(volumes) < 20 or len(close_prices) < 2:
            return 0.0
        try:
            avg_vol = sum(volumes[-20:]) / 20
            current_vol = volumes[-1]
            if avg_vol <= 0: return 0.0
            ratio = current_vol / avg_vol
            price_rising = close_prices[-1] > close_prices[-2]
            price_falling = close_prices[-1] < close_prices[-2]
            if ratio > 1.5 and price_rising: return 1.0
            if ratio > 1.0 and price_rising: return 0.75
            if ratio > 1.5 and price_falling: return -1.0
            if ratio > 1.0 and price_falling: return -0.75
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def structure_score(highs: List[float], lows: List[float],
                        close_prices: List[float], lookback: int = 20) -> float:
        if len(highs) < lookback + 1 or len(lows) < lookback + 1:
            return 0.0
        try:
            recent_high = max(highs[-lookback-1:-1])
            recent_low = min(lows[-lookback-1:-1])
            price = close_prices[-1]
            if price > recent_high: return 1.0
            if price < recent_low: return -1.0
            if price > recent_high * 0.98: return 0.5
            if price < recent_low * 1.02: return -0.5
            return 0.0
        except Exception:
            return 0.0

    # ---------------- Relative indicators ----------------
    @staticmethod
    def relative_rsi_score(coin_rsi: Optional[float], market_avg_rsi: float) -> float:
        if coin_rsi is None or market_avg_rsi <= 0: return 0.0
        try:
            diff = market_avg_rsi - coin_rsi
            if diff > 10: return 0.5
            if diff > 5: return 0.25
            if diff < -10: return -0.5
            if diff < -5: return -0.25
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def relative_strength_score(coin_change_24h: float, btc_change_24h: float) -> float:
        if not coin_change_24h and not btc_change_24h: return 0.0
        try:
            diff = coin_change_24h - btc_change_24h
            if diff > 5: return 0.75
            if diff > 2: return 0.5
            if diff > 0.5: return 0.25
            if diff < -5: return -0.75
            if diff < -2: return -0.5
            if diff < -0.5: return -0.25
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def volume_momentum_score(volumes: List[float]) -> float:
        if len(volumes) < 6: return 0.0
        try:
            current = volumes[-1]
            past = volumes[-6]
            if past <= 0: return 0.0
            ratio = current / past
            if ratio > 2.0: return 0.5
            if ratio > 1.5: return 0.25
            if ratio < 0.5: return -0.5
            if ratio < 0.7: return -0.25
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def atr_volatility_score(highs: List[float], lows: List[float],
                             closes: List[float]) -> float:
        try:
            atr_series = IndicatorCalculator.atr(highs, lows, closes, 14)
            if len(atr_series) < 25: return 0.0
            current_atr = atr_series[-1]
            past_atr = atr_series[-21] if len(atr_series) >= 21 else atr_series[0]
            if past_atr <= 0 or current_atr <= 0: return 0.0
            ratio = current_atr / past_atr
            if ratio > 1.5: return 0.3
            if ratio > 1.2: return 0.15
            if ratio < 0.7: return -0.3
            if ratio < 0.85: return -0.15
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def bollinger_position_score(close_prices: List[float],
                                 period: int = 20, num_std: float = 2.0) -> float:
        if len(close_prices) < period: return 0.0
        try:
            middle = IndicatorCalculator.sma(close_prices, period)
            std = IndicatorCalculator.std_dev(close_prices, period)
            if std == 0: return 0.0
            upper = middle + num_std * std
            lower = middle - num_std * std
            band_range = upper - lower
            if band_range <= 0: return 0.0
            price = close_prices[-1]
            position = (price - lower) / band_range
            if position < 0.1: return 0.75
            if position < 0.25: return 0.5
            if position < 0.4: return 0.25
            if position > 0.9: return -0.5
            if position > 0.75: return -0.25
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def macd_histogram_score(close_prices: List[float]) -> float:
        try:
            _, _, hist = IndicatorCalculator.macd(close_prices)
            if not hist or len(hist) < 3: return 0.0
            current = hist[-1]
            prev = hist[-2]
            if prev <= 0 and current > 0: return 0.5
            if current > 0 and current > prev: return 0.25
            if prev >= 0 and current < 0: return -0.5
            if current < 0 and current < prev: return -0.25
            return 0.0
        except Exception:
            return 0.0


# ======================================================================
# Signal Processor (with dynamic weights)
# ======================================================================
class SignalProcessor:

    # ------------------------------------------------------------------
    @staticmethod
    def _get_weights(tracker: Optional[SignalTracker]) -> Dict[str, float]:
        """
        Return weights per indicator, adjusted by historical accuracy
        then normalized so that the maximum total score remains 7.8.
        """
        base = {k: 1.0 for k in BASE_MAX_PER_INDICATOR}

        if not _cfg('USE_DYNAMIC_WEIGHTS', True) or tracker is None:
            return base

        min_samples = _cfg('DYNAMIC_WEIGHTS_MIN_SAMPLES', 20)
        adjusted: Dict[str, float] = {}
        for name in base:
            stats = tracker.get_indicator_stats(name)
            if stats['total'] >= min_samples and stats['total'] > 0:
                wr = stats['wins'] / stats['total']          # [0..1]
                adjusted[name] = 0.5 + wr                    # [0.5..1.5]
            else:
                adjusted[name] = 1.0

        # Normalize so MAX_TOTAL_SCORE stays constant
        max_weighted = sum(BASE_MAX_PER_INDICATOR[k] * adjusted[k]
                           for k in BASE_MAX_PER_INDICATOR)
        if max_weighted <= 0:
            return base
        scale = MAX_TOTAL_SCORE / max_weighted
        return {k: v * scale for k, v in adjusted.items()}

    # ------------------------------------------------------------------
    @staticmethod
    def process(raw_scores: Dict[str, float],
                btc_bullish: bool,
                btc_label: str,
                htf_trend: str,
                mtf_score: float = 0.0,
                tracker: Optional[SignalTracker] = None) -> Dict:

        weights = SignalProcessor._get_weights(tracker)
        weighted = {k: raw_scores[k] * weights.get(k, 1.0) for k in raw_scores}
        total = sum(weighted.values())

        btc_mode = config.get('USE_BTC_FILTER')

        # ----- BTC filter -----
        if btc_mode == 'block':
            if not btc_bullish:
                return SignalProcessor._build_result(
                    0.0, weighted, weights, raw_scores,
                    forced_neutral=True, mtf_score=mtf_score
                )
        elif btc_mode == 'modify':
            if total > 0 and not btc_bullish:
                total *= (_cfg('BTC_BEARISH_DISCOUNT', 0.5)
                          if btc_label == "bearish"
                          else _cfg('BTC_NEUTRAL_DISCOUNT', 0.75))
            elif total < 0 and not btc_bullish:
                total *= 1.15

        # ----- HTF confirmation (single timeframe) -----
        if _cfg('USE_HTF_CONFIRMATION', True) and total != 0 and not _cfg('USE_MTF_CONFIRMATION', False):
            agrees = (
                (total > 0 and htf_trend == "bullish") or
                (total < 0 and htf_trend == "bearish")
            )
            disagrees = (
                (total > 0 and htf_trend == "bearish") or
                (total < 0 and htf_trend == "bullish")
            )
            if agrees:
                total *= _cfg('HTF_BONUS', 1.15)
            elif disagrees:
                total *= _cfg('HTF_PENALTY', 0.85)

        # ----- Multi-timeframe confirmation -----
        if _cfg('USE_MTF_CONFIRMATION', False) and total != 0:
            influence = _cfg('MTF_INFLUENCE', 0.3)
            # mtf_score ∈ [-1, +1]; apply multiplier ∈ [1-influence, 1+influence]
            total *= (1.0 + mtf_score * influence)

        total = max(-MAX_TOTAL_SCORE, min(MAX_TOTAL_SCORE, total))
        return SignalProcessor._build_result(
            total, weighted, weights, raw_scores,
            forced_neutral=False, mtf_score=mtf_score
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _build_result(total: float,
                      weighted: Dict[str, float],
                      weights: Dict[str, float],
                      raw_scores: Dict[str, float],
                      forced_neutral: bool = False,
                      mtf_score: float = 0.0) -> Dict:

        if forced_neutral:
            signal_type = SignalType.NEUTRAL
        elif total >= config.get('STRONG_BUY_THRESHOLD'):
            signal_type = SignalType.STRONG_BUY
        elif total >= config.get('BUY_THRESHOLD'):
            signal_type = SignalType.BUY
        elif total <= config.get('STRONG_SELL_THRESHOLD'):
            signal_type = SignalType.STRONG_SELL
        elif total <= config.get('SELL_THRESHOLD'):
            signal_type = SignalType.SELL
        else:
            signal_type = SignalType.NEUTRAL

        total_percentage = (total / MAX_TOTAL_SCORE) * 100.0
        strength = SignalProcessor.get_signal_strength(signal_type)
        color = SignalProcessor.get_signal_color(signal_type)

        indicator_scores: Dict[str, IndicatorScore] = {}
        for name, raw in raw_scores.items():
            w = weights.get(name, 1.0)
            w_score = weighted.get(name, raw)
            max_val = BASE_MAX_PER_INDICATOR.get(name, 1.0)
            norm_pct = (w_score / (max_val * w)) * 100.0 if max_val * w else 0.0
            direction = "bullish" if raw > 0 else ("bearish" if raw < 0 else "neutral")
            indicator_scores[name] = IndicatorScore(
                name=name,
                raw_score=raw,
                weighted_score=w_score,
                percentage=norm_pct,
                weight=w,
                description=INDICATOR_DESCRIPTIONS.get(name, ''),
                color=INDICATOR_COLORS.get(name, '#2E86AB'),
                direction=direction,
            )

        return {
            'total_score': total,
            'total_percentage': total_percentage,
            'indicator_scores': indicator_scores,
            'signal_type': signal_type,
            'signal_strength': strength,
            'signal_color': color,
            'mtf_score': mtf_score,
            'weights': weights,
        }

    @staticmethod
    def get_signal_strength(signal_type: SignalType) -> str:
        return {
            SignalType.STRONG_BUY: "Strong Bullish",
            SignalType.BUY: "Moderate Bullish",
            SignalType.STRONG_SELL: "Strong Bearish",
            SignalType.SELL: "Moderate Bearish",
        }.get(signal_type, "Neutral")

    @staticmethod
    def get_signal_color(signal_type: SignalType) -> str:
        return {
            SignalType.STRONG_BUY: "success",
            SignalType.BUY: "primary",
            SignalType.NEUTRAL: "secondary",
            SignalType.SELL: "warning",
            SignalType.STRONG_SELL: "danger",
        }.get(signal_type, "secondary")


# ======================================================================
# Risk Management helper
# ======================================================================
class RiskManager:
    """Compute SL / TP / R:R / position sizing from ATR."""

    @staticmethod
    def compute(signal_type: SignalType, entry_price: float,
                atr_value: float) -> Dict[str, float]:
        empty = {
            'stop_loss': 0.0, 'take_profit': 0.0,
            'risk_reward_ratio': 0.0, 'risk_amount_usd': 0.0,
            'suggested_position_usd': 0.0,
        }
        if entry_price <= 0 or atr_value <= 0:
            return empty
        if signal_type not in (SignalType.BUY, SignalType.STRONG_BUY,
                               SignalType.SELL, SignalType.STRONG_SELL):
            return empty

        stop_mult = _cfg('ATR_STOP_MULT', 1.5)
        tp_mult = _cfg('ATR_TP_MULT', 2.5)
        risk_pct = _cfg('RISK_PER_TRADE_PCT', 1.0)
        account = _cfg('ACCOUNT_SIZE', 1000.0)

        is_long = signal_type in (SignalType.BUY, SignalType.STRONG_BUY)
        if is_long:
            stop = entry_price - stop_mult * atr_value
            tp = entry_price + tp_mult * atr_value
        else:
            stop = entry_price + stop_mult * atr_value
            tp = entry_price - tp_mult * atr_value

        risk_per_unit = abs(entry_price - stop)
        reward_per_unit = abs(tp - entry_price)
        rr = reward_per_unit / risk_per_unit if risk_per_unit > 0 else 0.0

        risk_amount = account * (risk_pct / 100.0)
        position_usd = (risk_amount / risk_per_unit) * entry_price if risk_per_unit > 0 else 0.0

        return {
            'stop_loss': round(stop, 8),
            'take_profit': round(tp, 8),
            'risk_reward_ratio': round(rr, 2),
            'risk_amount_usd': round(risk_amount, 2),
            'suggested_position_usd': round(position_usd, 2),
        }


# ======================================================================
# Notification Manager
# ======================================================================
class NotificationManager:
    def __init__(self):
        self.history: List[Notification] = []
        self.max_history = 100
        self.last_notification_time: Dict[str, datetime] = {}
        self.last_signal_type: Dict[str, SignalType] = {}
        self._lock = Lock()

    def add(self, notification: Notification):
        self.history.append(notification)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

    def get_recent(self, limit: int = 10) -> List[Notification]:
        return self.history[-limit:] if self.history else []

    def clear_history(self):
        with self._lock:
            self.history = []
            self.last_notification_time = {}
            self.last_signal_type = {}
        logger.info("Notification history cleared")

    def should_send(self, coin_symbol: str, signal_type: SignalType) -> bool:
        if signal_type in (SignalType.BUY, SignalType.STRONG_BUY) and not config.get('NOTIFY_ON_BUY'):
            return False
        if signal_type in (SignalType.SELL, SignalType.STRONG_SELL) and not config.get('NOTIFY_ON_SELL'):
            return False
        if signal_type == SignalType.NEUTRAL:
            return False

        # Skip if the signal type did not change for this coin
        if self.last_signal_type.get(coin_symbol) == signal_type:
            return False

        now = datetime.now()
        min_interval = config.get('MIN_NOTIFY_INTERVAL')
        if coin_symbol in self.last_notification_time:
            delta = now - self.last_notification_time[coin_symbol]
            if delta.total_seconds() < min_interval:
                return False
        return True

    # ------------------------------------------------------------------
    def send_ntfy(self, message: str, title: str = "Crypto Signal",
                  priority: str = "3", tags: str = "chart",
                  retries: int = 3) -> bool:
        headers = {
            "Title": title,
            "Priority": priority,
            "Tags": tags,
            "Content-Type": "text/plain; charset=utf-8"
        }
        safe = message.encode('ascii', errors='replace').decode('ascii')
        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(NTFY_URL, data=safe.encode('utf-8'),
                                     headers=headers, timeout=8)
                if resp.status_code == 200:
                    return True
                logger.warning(f"NTFY attempt {attempt}/{retries} status {resp.status_code}")
            except Exception as e:
                logger.warning(f"NTFY attempt {attempt}/{retries} error: {e}")
            time.sleep(min(2 ** attempt, 10))
        logger.error("NTFY failed after retries")
        return False

    # ------------------------------------------------------------------
    def create_notification(self, coin_signal: CoinSignal) -> Optional[Notification]:
        if not self.should_send(coin_signal.symbol, coin_signal.signal_type):
            return None

        st = coin_signal.signal_type
        title = f"{st.value}: {coin_signal.name}"

        lines = [
            f"{title}",
            f"Weighted Score: {coin_signal.total_score:+.2f} / {MAX_TOTAL_SCORE}",
            f"Percentage: {coin_signal.total_percentage:+.1f}%",
            f"Price: ${coin_signal.current_price:,.6f}" if coin_signal.current_price < 1
                else f"Price: ${coin_signal.current_price:,.2f}",
            f"24h Change: {coin_signal.price_change_24h:+.2f}%",
        ]

        # MTF block
        if coin_signal.mtf_details:
            mtf_str = " | ".join(f"{tf}:{v}" for tf, v in coin_signal.mtf_details.items())
            lines.append(f"MTF Trend: {mtf_str}")

        lines.append(f"HTF Trend: {coin_signal.htf_trend}")
        lines.append(f"BTC Bullish: {coin_signal.btc_bullish}")

        # Risk block (only for actionable entries)
        if coin_signal.stop_loss > 0 and st in (
            SignalType.BUY, SignalType.STRONG_BUY,
            SignalType.SELL, SignalType.STRONG_SELL
        ):
            sl_pct = ((coin_signal.stop_loss - coin_signal.current_price)
                      / coin_signal.current_price * 100.0)
            tp_pct = ((coin_signal.take_profit - coin_signal.current_price)
                      / coin_signal.current_price * 100.0)
            lines.append("")
            lines.append("--- Risk Management ---")
            lines.append(f"ATR(14): {coin_signal.atr_value:.6f}")
            lines.append(f"Stop Loss: {coin_signal.stop_loss:,.6f} ({sl_pct:+.2f}%)")
            lines.append(f"Take Profit: {coin_signal.take_profit:,.6f} ({tp_pct:+.2f}%)")
            lines.append(f"Risk:Reward: 1:{coin_signal.risk_reward_ratio}")
            lines.append(f"Risk Amount: ${coin_signal.risk_amount_usd:,.2f}")
            lines.append(f"Suggested Size: ${coin_signal.suggested_position_usd:,.2f}")

        lines.append(f"Time: {coin_signal.last_updated.strftime('%Y-%m-%d %H:%M')}")
        message = "\n".join(lines)

        tags_map = {
            SignalType.STRONG_BUY: "heavy_plus_sign",
            SignalType.BUY: "chart_increasing",
            SignalType.SELL: "chart_decreasing",
            SignalType.STRONG_SELL: "warning",
        }
        priority_map = {
            SignalType.STRONG_BUY: "4",
            SignalType.BUY: "3",
            SignalType.SELL: "3",
            SignalType.STRONG_SELL: "4",
        }

        if self.send_ntfy(message, title,
                          priority_map.get(st, "3"),
                          tags_map.get(st, "loudspeaker")):
            notif = Notification(
                id=f"{coin_signal.symbol}_{int(datetime.now().timestamp())}",
                timestamp=datetime.now(),
                coin_symbol=coin_signal.symbol,
                coin_name=coin_signal.name,
                message=message,
                notification_type=st.name.lower(),
                signal_strength=coin_signal.total_percentage,
                price=coin_signal.current_price,
            )
            with self._lock:
                self.add(notif)
                self.last_notification_time[coin_signal.symbol] = datetime.now()
                self.last_signal_type[coin_signal.symbol] = st
            return notif
        return None


# ======================================================================
# Signal Manager (two-pass + MTF + caching + tracker)
# ======================================================================
class SignalManager:
    def __init__(self):
        self.signals: Dict[str, CoinSignal] = {}
        self.history: List[Dict] = []
        self.last_update: Optional[datetime] = None
        self.lock = Lock()
        self.notification_manager = NotificationManager()
        self.fgi_fetcher = FearGreedFetcher()
        self.fear_greed_index = 50
        self.btc_bullish = False
        self.btc_trend_label = "unknown"
        self._market: Optional[MarketDataClient] = None
        self._market_priority: str = ""
        # Cache for HTF trends keyed by (symbol, timeframe)
        self._trend_cache: Dict[Tuple[str, str], Tuple[str, datetime]] = {}
        self._trend_cache_lock = Lock()
        self.tracker = signal_tracker

    @property
    def market(self) -> MarketDataClient:
        priority = config.get('EXCHANGE_PRIORITY')
        if self._market is None or priority != self._market_priority:
            names = [n.strip() for n in priority.split(',') if n.strip()]
            keys = get_exchange_keys()
            self._market = MarketDataClient(names, keys)
            self._market_priority = priority
            authed = [n for n in names if n in keys]
            logger.info(
                f"MarketDataClient initialized with: {names} | "
                f"Authenticated: {authed or 'none'}"
            )
        return self._market

    # ------------------------------------------------------------------
    @staticmethod
    def _timeframe_seconds(tf: str) -> int:
        unit = tf[-1].lower()
        try:
            n = int(tf[:-1])
        except Exception:
            return 3600
        return n * {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}.get(unit, 3600)

    def _trend_label(self, closes: List[float]) -> str:
        if len(closes) < 200:
            return "unknown"
        try:
            ema50 = IndicatorCalculator.ema(closes, 50)[-1]
            ema200 = IndicatorCalculator.ema(closes, 200)[-1]
            price = closes[-1]
            if price > ema50 > ema200: return "bullish"
            if price < ema50 < ema200: return "bearish"
            return "neutral"
        except Exception:
            return "unknown"

    def _get_trend_cached(self, symbol: str, timeframe: str) -> str:
        """Cache HTF trend per (symbol, timeframe) with TTL based on TF size."""
        key = (symbol, timeframe)
        now = datetime.now()
        with self._trend_cache_lock:
            cached = self._trend_cache.get(key)
            if cached:
                value, expires_at = cached
                if expires_at > now:
                    return value
        # Fetch
        try:
            ohlcv = self.market.fetch_ohlcv(symbol, timeframe, 250)
            trend = "unknown"
            if ohlcv and len(ohlcv) >= 200:
                closes = [c[4] for c in ohlcv]
                trend = self._trend_label(closes)
        except Exception as e:
            logger.debug(f"HTF trend fetch failed for {symbol}/{timeframe}: {e}")
            trend = "unknown"

        ttl = max(self._timeframe_seconds(timeframe) // 2, 60)
        with self._trend_cache_lock:
            self._trend_cache[key] = (trend, now + timedelta(seconds=ttl))
        return trend

    def check_btc_trend(self) -> Tuple[bool, str]:
        try:
            label = self._get_trend_cached("BTC/USDT", config.get('TIMEFRAME'))
            return (label == "bullish"), label
        except Exception as e:
            logger.error(f"BTC trend error: {e}")
            return False, "unknown"

    def _htf_trend(self, symbol: str) -> str:
        if not _cfg('USE_HTF_CONFIRMATION', True):
            return "unknown"
        return self._get_trend_cached(symbol, config.get('HTF_TIMEFRAME'))

    # ------------------------------------------------------------------
    def _multi_timeframe_score(self, symbol: str) -> Tuple[float, Dict[str, str]]:
        """
        Evaluate 3 timeframes and return a weighted score in [-1, +1].
        Weights: primary TF 0.5, HTF 0.3, HTF2 0.2
        """
        if not _cfg('USE_MTF_CONFIRMATION', False):
            return 0.0, {}

        primary = config.get('TIMEFRAME')
        htf = config.get('HTF_TIMEFRAME')
        htf2 = _cfg('HTF2_TIMEFRAME', '1d')

        plan = [(primary, 0.5), (htf, 0.3), (htf2, 0.2)]
        total = 0.0
        details: Dict[str, str] = {}
        for tf, w in plan:
            t = self._get_trend_cached(symbol, tf)
            details[tf] = t
            v = {'bullish': 1.0, 'bearish': -1.0}.get(t, 0.0)
            total += v * w
        return total, details

    # ------------------------------------------------------------------
    def _fetch_base(self, coin: CoinConfig) -> Optional[Dict]:
        ohlcv = self.market.fetch_ohlcv(coin.symbol, config.get('TIMEFRAME'),
                                        config.get('MAX_CANDLES'))
        if not ohlcv or len(ohlcv) < 50:
            return None

        closes = [c[4] for c in ohlcv]
        highs = [c[2] for c in ohlcv]
        lows = [c[3] for c in ohlcv]
        volumes = [c[5] for c in ohlcv]

        ticker = self.market.fetch_ticker(coin.symbol)
        if not ticker:
            return None

        rsi_vals = IndicatorCalculator.rsi(closes, 14)
        coin_rsi = rsi_vals[-1] if rsi_vals and rsi_vals[-1] is not None else None

        atr_series = IndicatorCalculator.atr(highs, lows, closes, 14)
        current_atr = atr_series[-1] if atr_series else 0.0

        base_scores = {
            IndicatorType.TREND.value: IndicatorCalculator.trend_score(closes),
            IndicatorType.MOMENTUM.value: IndicatorCalculator.momentum_score(closes),
            IndicatorType.VOLUME.value: IndicatorCalculator.volume_score(volumes, closes),
            IndicatorType.STRUCTURE.value: IndicatorCalculator.structure_score(highs, lows, closes, 20),
        }

        return {
            'coin': coin,
            'closes': closes,
            'highs': highs,
            'lows': lows,
            'volumes': volumes,
            'ticker': ticker,
            'rsi_value': coin_rsi,
            'atr_value': current_atr,
            'price_change_24h': ticker.get('percentage', 0.0) or 0.0,
            'base_scores': base_scores,
        }

    # ------------------------------------------------------------------
    def _finalize_signal(self, data: Dict, market_avg_rsi: float,
                         btc_change_24h: float) -> Optional[CoinSignal]:
        coin = data['coin']
        closes = data['closes']
        highs = data['highs']
        lows = data['lows']
        volumes = data['volumes']
        ticker = data['ticker']

        raw_scores = dict(data['base_scores'])
        raw_scores[IndicatorType.REL_RSI.value] = \
            IndicatorCalculator.relative_rsi_score(data['rsi_value'], market_avg_rsi)
        raw_scores[IndicatorType.REL_STRENGTH.value] = \
            IndicatorCalculator.relative_strength_score(
                data['price_change_24h'], btc_change_24h
            )
        raw_scores[IndicatorType.VOL_MOMENTUM.value] = \
            IndicatorCalculator.volume_momentum_score(volumes)
        raw_scores[IndicatorType.ATR_VOLATILITY.value] = \
            IndicatorCalculator.atr_volatility_score(highs, lows, closes)
        raw_scores[IndicatorType.BB_POSITION.value] = \
            IndicatorCalculator.bollinger_position_score(closes)
        raw_scores[IndicatorType.MACD_HISTOGRAM.value] = \
            IndicatorCalculator.macd_histogram_score(closes)

        htf = self._htf_trend(coin.symbol)
        mtf_score, mtf_details = self._multi_timeframe_score(coin.symbol)

        result = SignalProcessor.process(
            raw_scores, self.btc_bullish, self.btc_trend_label,
            htf, mtf_score=mtf_score, tracker=self.tracker
        )

        entry_price = ticker.get('last', 0.0) or 0.0
        risk = RiskManager.compute(result['signal_type'], entry_price, data['atr_value'])

        return CoinSignal(
            symbol=coin.symbol,
            name=coin.name,
            current_price=entry_price,
            price_change_24h=ticker.get('percentage', 0.0) or 0.0,
            high_24h=ticker.get('high', 0.0) or 0.0,
            low_24h=ticker.get('low', 0.0) or 0.0,
            volume_24h=ticker.get('quoteVolume', 0.0) or 0.0,
            total_percentage=result['total_percentage'],
            total_score=result['total_score'],
            signal_type=result['signal_type'],
            signal_strength=result['signal_strength'],
            signal_color=result['signal_color'],
            indicator_scores=result['indicator_scores'],
            last_updated=datetime.now(),
            fear_greed_value=self.fear_greed_index,
            btc_bullish=self.btc_bullish,
            htf_trend=htf,
            is_valid=True,
            mtf_score=mtf_score,
            mtf_details=mtf_details,
            atr_value=data['atr_value'],
            stop_loss=risk['stop_loss'],
            take_profit=risk['take_profit'],
            risk_reward_ratio=risk['risk_reward_ratio'],
            risk_amount_usd=risk['risk_amount_usd'],
            suggested_position_usd=risk['suggested_position_usd'],
        )

    # ------------------------------------------------------------------
    def update_all(self) -> bool:
        with self.lock:
            coins = get_coins()
            logger.info(f"Updating {len(coins)} coins...")

            self.btc_bullish, self.btc_trend_label = self.check_btc_trend()
            logger.info(f"BTC trend: {self.btc_trend_label} (bullish={self.btc_bullish})")

            self.fear_greed_index = self.fgi_fetcher.get()

            # ---------- PASS 1 ----------
            intermediate: List[Dict] = []
            for coin in coins:
                if not coin.enabled:
                    continue
                try:
                    data = self._fetch_base(coin)
                    if data:
                        intermediate.append(data)
                except Exception as e:
                    logger.error(f"Pass 1 error on {coin.symbol}: {e}")

            if not intermediate:
                logger.warning("No coins fetched successfully")
                self.last_update = datetime.now()
                return False

            rsi_values = [d['rsi_value'] for d in intermediate if d['rsi_value'] is not None]
            market_avg_rsi = sum(rsi_values) / len(rsi_values) if rsi_values else 50.0

            btc_change_24h = 0.0
            for d in intermediate:
                if d['coin'].symbol == 'BTC/USDT':
                    btc_change_24h = d['price_change_24h']
                    break

            logger.info(
                f"Market context: avg_rsi={market_avg_rsi:.1f}, "
                f"btc_change_24h={btc_change_24h:+.2f}%"
            )

            # ---------- PASS 2 ----------
            success = 0
            current_prices: Dict[str, float] = {}
            for data in intermediate:
                try:
                    sig = self._finalize_signal(data, market_avg_rsi, btc_change_24h)
                    if sig and sig.is_valid:
                        self.signals[data['coin'].symbol] = sig
                        success += 1
                        current_prices[data['coin'].symbol] = sig.current_price
                        self.tracker.record(sig)
                        self.notification_manager.create_notification(sig)
                except Exception as e:
                    logger.error(f"Pass 2 error on {data['coin'].symbol}: {e}")

            # ---------- Evaluate pending signals ----------
            try:
                self.tracker.evaluate_pending(current_prices)
            except Exception as e:
                logger.error(f"Tracker evaluation error: {e}")

            self.last_update = datetime.now()
            self._save_history()
            logger.info(f"Updated {success}/{len(coins)} coins")
            return success > 0

    def _save_history(self):
        entry = {
            'timestamp': datetime.now(),
            'signals': {s: self.signals[s].total_percentage for s in self.signals},
            'fgi': self.fear_greed_index,
            'btc_bullish': self.btc_bullish,
            'btc_trend': self.btc_trend_label,
        }
        self.history.append(entry)
        if len(self.history) > 100:
            self.history = self.history[-100:]

    # ------------------------------------------------------------------
    def get_coins_data(self) -> List[Dict]:
        with self.lock:
            snapshot = dict(self.signals)
        data = []
        coins = get_coins()
        for coin in coins:
            sig = snapshot.get(coin.symbol)
            data.append(self._format_coin(sig) if sig and sig.is_valid else self._default_coin(coin))
        data.sort(key=lambda x: x['total_percentage'], reverse=True)
        return data

    def _format_coin(self, s: CoinSignal) -> Dict:
        indicators = []
        for k, v in s.indicator_scores.items():
            indicators.append({
                'name': k,
                'display_name': INDICATOR_DISPLAY_NAMES.get(k, k),
                'description': INDICATOR_DESCRIPTIONS.get(k, ''),
                'raw_score': v.raw_score,
                'weighted_score': v.weighted_score,
                'weight': round(v.weight, 3),
                'percentage': v.percentage,
                'color': v.color,
                'direction': v.direction,
            })
        return {
            'symbol': s.symbol,
            'name': s.name,
            'current_price': s.current_price,
            'formatted_price': self._format_price(s.current_price),
            'price_change_24h': s.price_change_24h,
            'formatted_24h_change': self._format_percentage(s.price_change_24h),
            'volume_24h': s.volume_24h,
            'formatted_volume_24h': self._format_volume(s.volume_24h),
            'total_percentage': s.total_percentage,
            'total_score': s.total_score,
            'signal_type': s.signal_type.value,
            'signal_strength': s.signal_strength,
            'signal_color': s.signal_color,
            'indicators': indicators,
            'last_updated_str': self._format_time_delta(s.last_updated),
            'fear_greed_value': s.fear_greed_value,
            'btc_bullish': s.btc_bullish,
            'htf_trend': s.htf_trend,
            'mtf_score': s.mtf_score,
            'mtf_details': s.mtf_details,
            'atr_value': s.atr_value,
            'stop_loss': s.stop_loss,
            'take_profit': s.take_profit,
            'risk_reward_ratio': s.risk_reward_ratio,
            'risk_amount_usd': s.risk_amount_usd,
            'suggested_position_usd': s.suggested_position_usd,
            'is_valid': True,
        }

    def _default_coin(self, coin: CoinConfig) -> Dict:
        return {
            'symbol': coin.symbol,
            'name': coin.name,
            'current_price': 0,
            'formatted_price': '0',
            'price_change_24h': 0,
            'formatted_24h_change': '0.00%',
            'volume_24h': 0,
            'formatted_volume_24h': '0',
            'total_percentage': 0,
            'total_score': 0,
            'signal_type': SignalType.NEUTRAL.value,
            'signal_strength': 'Unavailable',
            'signal_color': 'secondary',
            'indicators': [],
            'last_updated_str': 'Unknown',
            'fear_greed_value': self.fear_greed_index,
            'btc_bullish': self.btc_bullish,
            'htf_trend': 'unknown',
            'mtf_score': 0.0,
            'mtf_details': {},
            'atr_value': 0.0,
            'stop_loss': 0.0,
            'take_profit': 0.0,
            'risk_reward_ratio': 0.0,
            'risk_amount_usd': 0.0,
            'suggested_position_usd': 0.0,
            'is_valid': False,
        }

    @staticmethod
    def _format_price(v: float) -> str:
        """Always show precise price, no K/M."""
        try:
            if v >= 1000:
                return f"{v:,.2f}"
            if v >= 1:
                return f"{v:.4f}"
            if v >= 0.01:
                return f"{v:.5f}"
            return f"{v:.8f}"
        except Exception:
            return "0"

    @staticmethod
    def _format_volume(v: float) -> str:
        """Volumes use K/M suffixes for readability."""
        try:
            if v >= 1_000_000_000:
                return f"{v/1_000_000_000:.2f}B"
            if v >= 1_000_000:
                return f"{v/1_000_000:.2f}M"
            if v >= 1_000:
                return f"{v/1_000:.2f}K"
            return f"{v:.2f}"
        except Exception:
            return "0"

    @staticmethod
    def _format_percentage(v: float) -> str:
        try:
            return f"{v:+.2f}%" if v else "0.00%"
        except Exception:
            return "0.00%"

    @staticmethod
    def _format_time_delta(dt: datetime) -> str:
        if not dt:
            return "Unknown"
        delta = datetime.now() - dt
        if delta.days > 0:
            return f"{delta.days} day(s) ago"
        if delta.seconds >= 3600:
            return f"{delta.seconds//3600} hour(s) ago"
        if delta.seconds >= 60:
            return f"{delta.seconds//60} minute(s) ago"
        return "Just now"

    def get_stats(self) -> Dict:
        coins = [c for c in self.get_coins_data() if c['is_valid']]
        percentages = [c['total_percentage'] for c in coins]
        avg = sum(percentages) / len(percentages) if percentages else 0

        strong_buy = sum(1 for c in coins if c['signal_type'] == SignalType.STRONG_BUY.value)
        buy = sum(1 for c in coins if c['signal_type'] == SignalType.BUY.value)
        neutral = sum(1 for c in coins if c['signal_type'] == SignalType.NEUTRAL.value)
        sell = sum(1 for c in coins if c['signal_type'] == SignalType.SELL.value)
        strong_sell = sum(1 for c in coins if c['signal_type'] == SignalType.STRONG_SELL.value)

        total_coins = len(get_coins())
        return {
            'total_coins': total_coins,
            'updated_coins': len(coins),
            'avg_signal': avg,
            'strong_buy_signals': strong_buy,
            'buy_signals': buy,
            'neutral_signals': neutral,
            'sell_signals': sell,
            'strong_sell_signals': strong_sell,
            'last_update_str': self._format_time_delta(self.last_update) if self.last_update else 'Unknown',
            'total_notifications': len(self.notification_manager.history),
            'fear_greed_index': self.fear_greed_index,
            'btc_bullish': self.btc_bullish,
            'btc_trend': self.btc_trend_label,
            'system_status': 'healthy' if len(coins) >= total_coins * 0.7 else 'warning',
            'exchange_status': self.market.get_status() if self._market else {},
            'max_total_score': MAX_TOTAL_SCORE,
            'signal_accuracy': self.tracker.get_signal_stats(30),
            'indicator_accuracy': self.tracker.get_all_stats(),
            'config': {
                'timeframe': config.get('TIMEFRAME'),
                'htf_timeframe': config.get('HTF_TIMEFRAME'),
                'htf2_timeframe': _cfg('HTF2_TIMEFRAME', '1d'),
                'btc_filter_mode': config.get('USE_BTC_FILTER'),
                'htf_confirmation': config.get('USE_HTF_CONFIRMATION'),
                'mtf_confirmation': _cfg('USE_MTF_CONFIRMATION', False),
                'dynamic_weights': _cfg('USE_DYNAMIC_WEIGHTS', True),
                'update_interval': config.get('UPDATE_INTERVAL'),
                'exchange_priority': config.get('EXCHANGE_PRIORITY'),
            }
        }


# ======================================================================
# Update Scheduler (interval change-aware, no duplicate startup update)
# ======================================================================
class UpdateScheduler:
    """
    Runs update_all() immediately, then every UPDATE_INTERVAL seconds.
    Config changes wake it up early so a new interval takes effect at once.
    """

    def __init__(self, manager: SignalManager):
        self.manager = manager
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_run: Optional[datetime] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="UpdateScheduler")
        self._thread.start()
        logger.info("UpdateScheduler started")

    def _run_once(self):
        try:
            self.manager.update_all()
            self._last_run = datetime.now()
        except Exception as e:
            logger.error(f"Update error: {e}")

    def _run(self):
        # Initial update
        self._run_once()
        while not self._stop.is_set():
            interval = max(10, int(config.get('UPDATE_INTERVAL') or 60))
            # Wait for interval OR external wake-up (config change)
            triggered = self._wake.wait(timeout=interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            self._run_once()

    def wake(self):
        """Trigger an immediate update and reset the interval clock."""
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)


# ======================================================================
# Flask App
# ======================================================================
app = Flask(__name__)
app.secret_key = SECRET_KEY
signal_manager = SignalManager()
scheduler = UpdateScheduler(signal_manager)
start_time = time.time()

# Start scheduler once — no duplicate update_all() call
scheduler.start()


# ======================================================================
# Context Processor
# ======================================================================
@app.context_processor
def utility_processor():
    def signal_color_to_css(color_name):
        return {
            'success': '#3BB273',
            'primary': '#2E86AB',
            'secondary': '#8b98a5',
            'warning': '#e6a817',
            'danger': '#d64545',
        }.get(color_name, '#8b98a5')
    return dict(
        signal_color_to_css=signal_color_to_css,
        get_indicator_color=lambda k: INDICATOR_COLORS.get(k, '#2E86AB'),
        get_indicator_display_name=lambda k: INDICATOR_DISPLAY_NAMES.get(k, k),
    )


# ======================================================================
# Routes
# ======================================================================
@app.route('/')
def index():
    coins = signal_manager.get_coins_data()
    stats = signal_manager.get_stats()
    notifications = signal_manager.notification_manager.get_recent(10)
    return render_template('index.html', coins=coins, stats=stats,
                           notifications=notifications)


@app.route('/settings')
def settings_page():
    return render_template('settings.html')


@app.route('/api/signals')
def api_signals():
    return jsonify({
        'status': 'success',
        'data': signal_manager.get_coins_data(),
        'timestamp': datetime.now().isoformat(),
    })


@app.route('/api/update', methods=['POST'])
def manual_update():
    ok = signal_manager.update_all()
    return jsonify({
        'status': 'success' if ok else 'warning',
        'message': 'Update completed',
        'timestamp': datetime.now().isoformat(),
    })


@app.route('/api/health')
def health():
    last = signal_manager.last_update
    status = 'healthy'
    if last and (datetime.now() - last).total_seconds() > config.get('UPDATE_INTERVAL') * 3:
        status = 'warning'
    return jsonify({
        'status': status,
        'last_update': last.isoformat() if last else None,
        'coins': len(signal_manager.signals),
        'uptime': time.time() - start_time,
        'fear_greed': signal_manager.fear_greed_index,
        'btc_bullish': signal_manager.btc_bullish,
        'btc_trend': signal_manager.btc_trend_label,
        'notifications': len(signal_manager.notification_manager.history),
        'version': '8.0.0',
        'indicators_count': 10,
        'max_total_score': MAX_TOTAL_SCORE,
    })


@app.route('/api/notifications')
def get_notifications():
    limit = request.args.get('limit', 10, type=int)
    nots = signal_manager.notification_manager.get_recent(limit)
    return jsonify({
        'notifications': [asdict(n) for n in nots],
        'total': len(signal_manager.notification_manager.history),
    })


@app.route('/api/notifications/clear', methods=['POST'])
def clear_notifications():
    signal_manager.notification_manager.clear_history()
    return jsonify({'status': 'success', 'message': 'History cleared'})


@app.route('/api/exchange_status')
def exchange_status():
    status = signal_manager.market.get_status()
    keys = get_exchange_keys()
    for ex in status.get('exchanges', []):
        ex['has_keys'] = ex['name'] in keys
    return jsonify(status)


# ----------------------------------------------------------------------
# Signal Accuracy endpoints
# ----------------------------------------------------------------------
@app.route('/api/signal_stats')
def api_signal_stats():
    days = request.args.get('days', 30, type=int)
    return jsonify({
        'status': 'success',
        'signal_stats': signal_manager.tracker.get_signal_stats(days),
        'indicator_stats': signal_manager.tracker.get_all_stats(),
    })


# ----------------------------------------------------------------------
# Config endpoints
# ----------------------------------------------------------------------
@app.route('/api/config/full')
def api_config_full():
    return jsonify({
        'status': 'success',
        'values': config.get_all(),
        'metadata': SETTINGS_METADATA,
        'groups': SETTINGS_GROUPS,
        'config_file': os.path.exists('user_settings.json'),
    })


@app.route('/api/config/update', methods=['POST'])
def api_config_update():
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify({'status': 'error', 'message': 'Invalid payload'}), 400
        result = config.update(data)

        # Clear stale notifications when settings change
        signal_manager.notification_manager.clear_history()

        # Wake the scheduler to apply the new interval immediately
        scheduler.wake()
        return jsonify({
            'status': 'success' if not result['errors'] else 'warning',
            'values': result['values'],
            'errors': result['errors'],
        })
    except Exception as e:
        logger.error(f"Config update error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/config/reset', methods=['POST'])
def api_config_reset():
    values = config.reset_to_defaults()
    signal_manager.notification_manager.clear_history()
    scheduler.wake()
    return jsonify({'status': 'success', 'values': values})


@app.route('/api/test_ntfy')
def test_ntfy():
    msg = "Test notification - system is working properly"
    ok = signal_manager.notification_manager.send_ntfy(msg, "Signal Test", "3", "test_tube")
    return jsonify({'success': ok})


# ======================================================================
# Startup notification
# ======================================================================
def send_startup_notification():
    try:
        mode = config.get('USE_BTC_FILTER')
        htf = "ON" if config.get('USE_HTF_CONFIRMATION') else "OFF"
        mtf = "ON" if _cfg('USE_MTF_CONFIRMATION', False) else "OFF"
        dw = "ON" if _cfg('USE_DYNAMIC_WEIGHTS', True) else "OFF"
        coins = get_coins()
        keys = get_exchange_keys()
        msg = (
            f"Crypto Discriminating Analyzer Started (v8.0)\n"
            f"Tracking {len(coins)} coins\n"
            f"Indicators: 10 (4 base + 6 relative)\n"
            f"Max score: {MAX_TOTAL_SCORE}\n"
            f"Timeframe: {config.get('TIMEFRAME')} | HTF: {config.get('HTF_TIMEFRAME')} | HTF2: {_cfg('HTF2_TIMEFRAME', '1d')}\n"
            f"BTC filter mode: {mode}\n"
            f"HTF confirmation: {htf} | MTF: {mtf} | Dynamic weights: {dw}\n"
            f"Exchange priority: {config.get('EXCHANGE_PRIORITY')}\n"
            f"API keys: {list(keys.keys()) or 'none'}\n"
            f"Update interval: {config.get('UPDATE_INTERVAL')}s\n"
            f"Signal tracking: enabled (SQLite)"
        )
        signal_manager.notification_manager.send_ntfy(msg, "System Started", "3", "rocket")
    except Exception as e:
        logger.error(f"Startup notification error: {e}")


def delayed_startup():
    time.sleep(5)
    send_startup_notification()


threading.Thread(target=delayed_startup, daemon=True).start()


# ======================================================================
# Graceful shutdown
# ======================================================================
import signal as _signal_sys


def _shutdown_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down cleanly")
    scheduler.stop()
    raise SystemExit(0)


for _sig in (_signal_sys.SIGTERM, _signal_sys.SIGINT):
    try:
        _signal_sys.signal(_sig, _shutdown_handler)
    except Exception:
        pass


# ======================================================================
# Entry point
# ======================================================================
if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Crypto Discriminating Analyzer v8.0.0")
    logger.info(f"Coins: {[c.symbol for c in get_coins()]}")
    logger.info(f"Indicators: 10 (4 base + 6 relative)")
    logger.info(f"Max total score: {MAX_TOTAL_SCORE}")
    logger.info(f"Timeframe: {config.get('TIMEFRAME')} | HTF: {config.get('HTF_TIMEFRAME')} | HTF2: {_cfg('HTF2_TIMEFRAME', '1d')}")
    logger.info(f"BTC filter: {config.get('USE_BTC_FILTER')} | HTF: {config.get('USE_HTF_CONFIRMATION')} | MTF: {_cfg('USE_MTF_CONFIRMATION', False)}")
    logger.info(f"Dynamic weights: {_cfg('USE_DYNAMIC_WEIGHTS', True)}")
    logger.info(f"Exchange priority: {config.get('EXCHANGE_PRIORITY')}")
    logger.info(f"NTFY: {NTFY_URL}")
    logger.info(f"Port: {PORT}")
    logger.info("=" * 60)

    app.run(host='0.0.0.0', port=PORT, debug=FLASK_DEBUG)
