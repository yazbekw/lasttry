"""
Crypto Signal Analyzer Bot - Discriminating Edition
Version 8.4.0
- Fixed: sell signals now appear in bull markets
- Added: external bot webhook (entry + state change)
- Added: ML-ready feature storage
- Optimized: batch queries + pooling + async /api/update
"""

import os
import json
import time
import logging
import threading
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict, field
from enum import Enum
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template, jsonify, request

from config_manager import config
from settings_metadata import SETTINGS_METADATA, SETTINGS_GROUPS
from market_data import MarketDataClient, get_exchange_keys
from signal_tracker import SignalTracker, signal_tracker, USE_POSTGRES
from external_bot import external_bot


# ======================================================================
# Flask app
# ======================================================================
app = Flask(__name__)


# ======================================================================
# Logging
# ======================================================================
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
LOG_TO_FILE = os.environ.get('LOG_TO_FILE', 'false').lower() in ('true', '1', 'yes')

_handlers = [logging.StreamHandler()]
if LOG_TO_FILE:
    try:
        _handlers.append(logging.FileHandler('crypto_signal.log', encoding='utf-8'))
    except Exception as e:
        print(f"File logging disabled: {e}", flush=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=_handlers,
)
logger = logging.getLogger(__name__)


# ======================================================================
# Safe config getter
# ======================================================================
def _cfg(key: str, default=None):
    try:
        v = config.get(key)
        return v if v is not None else default
    except Exception:
        return default


# ======================================================================
# Static values
# ======================================================================
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', 'crypto_buy_alerts')
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
FGI_API_URL = "https://api.alternative.me/fng/"
SECRET_KEY = os.environ.get('SECRET_KEY', 'crypto-signal-secret-2026')
FLASK_DEBUG = os.environ.get('FLASK_DEBUG', 'false').lower() in ('true', '1', 'yes')
PORT = int(os.environ.get('PORT', 5000))

INITIAL_UPDATE_DELAY = int(os.environ.get('INITIAL_UPDATE_DELAY', '20'))
STARTUP_NOTIFY_DELAY = int(os.environ.get('STARTUP_NOTIFY_DELAY', '30'))
FETCH_MAX_WORKERS = int(os.environ.get('FETCH_MAX_WORKERS', '3'))

app.secret_key = SECRET_KEY

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
# Enums + Dataclasses
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
    mtf_score: float = 0.0
    mtf_details: Dict[str, str] = field(default_factory=dict)
    atr_value: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    risk_reward_ratio: float = 0.0
    risk_amount_usd: float = 0.0
    suggested_position_usd: float = 0.0
    # For ML / state tracking
    btc_trend_label: str = "unknown"
    market_regime: str = "unknown"


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
# Helpers
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
# Fear & Greed
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
# Indicator Calculators
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
    def atr(highs, lows, closes, period: int = 14) -> List[float]:
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
    def macd(prices, fast=12, slow=26, signal=9):
        if len(prices) < slow + signal:
            return [], [], []
        ema_fast = IndicatorCalculator.ema(prices, fast)
        ema_slow = IndicatorCalculator.ema(prices, slow)
        macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
        signal_line = IndicatorCalculator.ema(macd_line, signal)
        histogram = [m - s for m, s in zip(macd_line, signal_line)]
        return macd_line, signal_line, histogram

    @staticmethod
    def trend_score(close_prices):
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
    def momentum_score(close_prices):
        """
        Symmetric RSI scoring.
        Buy zones:  <25 → +1.0, <35 → +0.75, <45 → +0.35
        Neutral:    45-55 → 0.0
        Sell zones: >55 → -0.35, >65 → -0.75, >75 → -1.0
        """
        if len(close_prices) < 15:
            return 0.0
        try:
            rsi_vals = IndicatorCalculator.rsi(close_prices, 14)
            rsi = rsi_vals[-1] if rsi_vals and rsi_vals[-1] is not None else 50.0

            # Buy side
            if rsi < 25: return 1.0
            if rsi < 35: return 0.75
            if rsi < 45: return 0.35

            # Neutral
            if rsi <= 55: return 0.0

            # Sell side (now symmetric)
            if rsi <= 65: return -0.35
            if rsi <= 75: return -0.75
            return -1.0
        except Exception:
            return 0.0

    @staticmethod
    def volume_score(volumes, close_prices):
        if len(volumes) < 20 or len(close_prices) < 2:
            return 0.0
        try:
            avg_vol = sum(volumes[-20:]) / 20
            current_vol = volumes[-1]
            if avg_vol <= 0:
                return 0.0
            ratio = current_vol / avg_vol
            pr = close_prices[-1] > close_prices[-2]
            pf = close_prices[-1] < close_prices[-2]
            if ratio > 1.5 and pr: return 1.0
            if ratio > 1.0 and pr: return 0.75
            if ratio > 1.5 and pf: return -1.0
            if ratio > 1.0 and pf: return -0.75
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def structure_score(highs, lows, close_prices, lookback: int = 20):
        """
        Breakout/breakdown detection. Also uses a wider lookback
        to detect support breakdown in bull markets.
        """
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

    @staticmethod
    def relative_rsi_score(coin_rsi, market_avg_rsi):
        if coin_rsi is None or market_avg_rsi <= 0:
            return 0.0
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
    def relative_strength_score(coin_change_24h, btc_change_24h):
        if not coin_change_24h and not btc_change_24h:
            return 0.0
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
    def volume_momentum_score(volumes):
        if len(volumes) < 6:
            return 0.0
        try:
            current = volumes[-1]
            past = volumes[-6]
            if past <= 0:
                return 0.0
            ratio = current / past
            if ratio > 2.0: return 0.5
            if ratio > 1.5: return 0.25
            if ratio < 0.5: return -0.5
            if ratio < 0.7: return -0.25
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def atr_volatility_score(highs, lows, closes):
        try:
            atr_series = IndicatorCalculator.atr(highs, lows, closes, 14)
            if len(atr_series) < 25:
                return 0.0
            current_atr = atr_series[-1]
            past_atr = atr_series[-21] if len(atr_series) >= 21 else atr_series[0]
            if past_atr <= 0 or current_atr <= 0:
                return 0.0
            ratio = current_atr / past_atr
            if ratio > 1.5: return 0.3
            if ratio > 1.2: return 0.15
            if ratio < 0.7: return -0.3
            if ratio < 0.85: return -0.15
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def bollinger_position_score(close_prices, period=20, num_std=2.0):
        if len(close_prices) < period:
            return 0.0
        try:
            middle = IndicatorCalculator.sma(close_prices, period)
            std = IndicatorCalculator.std_dev(close_prices, period)
            if std == 0:
                return 0.0
            upper = middle + num_std * std
            lower = middle - num_std * std
            band_range = upper - lower
            if band_range <= 0:
                return 0.0
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
    def macd_histogram_score(close_prices):
        try:
            _, _, hist = IndicatorCalculator.macd(close_prices)
            if not hist or len(hist) < 3:
                return 0.0
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
# Signal Processor
# ======================================================================
class SignalProcessor:

    @staticmethod
    def _get_weights(tracker: Optional[SignalTracker]) -> Dict[str, float]:
        base = {k: 1.0 for k in BASE_MAX_PER_INDICATOR}
        if not _cfg('USE_DYNAMIC_WEIGHTS', True) or tracker is None:
            return base
        try:
            all_stats = tracker.get_all_indicator_stats_batch()
        except Exception:
            return base
        if not all_stats:
            return base

        min_samples = _cfg('DYNAMIC_WEIGHTS_MIN_SAMPLES', 20)
        adjusted: Dict[str, float] = {}
        for name in base:
            stats = all_stats.get(name, {'wins': 0, 'losses': 0, 'total': 0})
            if stats['total'] >= min_samples and stats['total'] > 0:
                wr = stats['wins'] / stats['total']
                adjusted[name] = 0.5 + wr
            else:
                adjusted[name] = 1.0

        max_weighted = sum(BASE_MAX_PER_INDICATOR[k] * adjusted[k]
                           for k in BASE_MAX_PER_INDICATOR)
        if max_weighted <= 0:
            return base
        scale = MAX_TOTAL_SCORE / max_weighted
        return {k: v * scale for k, v in adjusted.items()}

    @staticmethod
    def process(raw_scores, btc_bullish, btc_label, htf_trend,
                mtf_score: float = 0.0, tracker=None, weights=None) -> Dict:
        if weights is None:
            weights = SignalProcessor._get_weights(tracker)

        weighted = {k: raw_scores[k] * weights.get(k, 1.0) for k in raw_scores}
        total = sum(weighted.values())

        btc_mode = config.get('USE_BTC_FILTER')

        if btc_mode == 'block':
            if not btc_bullish:
                return SignalProcessor._build_result(
                    0.0, weighted, weights, raw_scores,
                    forced_neutral=True, mtf_score=mtf_score,
                    btc_bullish=btc_bullish,
                )

        elif btc_mode == 'modify':
            if total > 0 and not btc_bullish:
                total *= (_cfg('BTC_BEARISH_DISCOUNT', 0.5)
                          if btc_label == "bearish"
                          else _cfg('BTC_NEUTRAL_DISCOUNT', 0.75))
            elif total < 0:
                if not btc_bullish:
                    total *= 1.15
                elif _cfg('ALLOW_SELLS_IN_BULL_MARKET', True):
                    total *= _cfg('SELL_BULL_BOOST', 1.10)

        # HTF confirmation
        if _cfg('USE_HTF_CONFIRMATION', True) and total != 0 and not _cfg('USE_MTF_CONFIRMATION', False):
            agrees = ((total > 0 and htf_trend == "bullish") or
                      (total < 0 and htf_trend == "bearish"))
            disagrees = ((total > 0 and htf_trend == "bearish") or
                         (total < 0 and htf_trend == "bullish"))
            if agrees:
                total *= _cfg('HTF_BONUS', 1.15)
            elif disagrees:
                total *= _cfg('HTF_PENALTY', 0.85)

        # MTF confirmation
        if _cfg('USE_MTF_CONFIRMATION', False) and total != 0:
            influence = _cfg('MTF_INFLUENCE', 0.3)
            total *= (1.0 + mtf_score * influence)

        total = max(-MAX_TOTAL_SCORE, min(MAX_TOTAL_SCORE, total))
        return SignalProcessor._build_result(
            total, weighted, weights, raw_scores,
            forced_neutral=False, mtf_score=mtf_score,
            btc_bullish=btc_bullish,
        )

    @staticmethod
    def _build_result(total, weighted, weights, raw_scores,
                      forced_neutral=False, mtf_score=0.0,
                      btc_bullish=True) -> Dict:
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
            # Bull-market-specific sell threshold (easier)
            if (btc_bullish and _cfg('ALLOW_SELLS_IN_BULL_MARKET', True)
                    and total <= _cfg('SELL_THRESHOLD_BULL', -1.8)):
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
                name=name, raw_score=raw, weighted_score=w_score,
                percentage=norm_pct, weight=w,
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
    def get_signal_strength(signal_type):
        return {
            SignalType.STRONG_BUY: "Strong Bullish",
            SignalType.BUY: "Moderate Bullish",
            SignalType.STRONG_SELL: "Strong Bearish",
            SignalType.SELL: "Moderate Bearish",
        }.get(signal_type, "Neutral")

    @staticmethod
    def get_signal_color(signal_type):
        return {
            SignalType.STRONG_BUY: "success",
            SignalType.BUY: "primary",
            SignalType.NEUTRAL: "secondary",
            SignalType.SELL: "warning",
            SignalType.STRONG_SELL: "danger",
        }.get(signal_type, "secondary")


# ======================================================================
# Risk Management
# ======================================================================
class RiskManager:
    @staticmethod
    def compute(signal_type, entry_price, atr_value) -> Dict[str, float]:
        empty = {'stop_loss': 0.0, 'take_profit': 0.0, 'risk_reward_ratio': 0.0,
                 'risk_amount_usd': 0.0, 'suggested_position_usd': 0.0}
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

    def should_send(self, coin_symbol, signal_type):
        if signal_type in (SignalType.BUY, SignalType.STRONG_BUY) and not config.get('NOTIFY_ON_BUY'):
            return False
        if signal_type in (SignalType.SELL, SignalType.STRONG_SELL) and not config.get('NOTIFY_ON_SELL'):
            return False
        if signal_type == SignalType.NEUTRAL:
            return False
        if self.last_signal_type.get(coin_symbol) == signal_type:
            return False
        now = datetime.now()
        min_interval = config.get('MIN_NOTIFY_INTERVAL')
        if coin_symbol in self.last_notification_time:
            delta = now - self.last_notification_time[coin_symbol]
            if delta.total_seconds() < min_interval:
                return False
        return True

    def send_ntfy(self, message, title="Crypto Signal", priority="3",
                  tags="chart", retries=3) -> bool:
        headers = {
            "Title": title, "Priority": priority, "Tags": tags,
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

    def create_notification(self, coin_signal):
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
        if coin_signal.mtf_details:
            mtf_str = " | ".join(f"{tf}:{v}" for tf, v in coin_signal.mtf_details.items())
            lines.append(f"MTF Trend: {mtf_str}")
        lines.append(f"HTF Trend: {coin_signal.htf_trend}")
        lines.append(f"BTC Bullish: {coin_signal.btc_bullish}")

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
# Signal Manager
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
                f"MarketDataClient initialized: {names} | Authenticated: {authed or 'none'}"
            )
        return self._market

    @staticmethod
    def _timeframe_seconds(tf: str) -> int:
        unit = tf[-1].lower()
        try:
            n = int(tf[:-1])
        except Exception:
            return 3600
        return n * {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}.get(unit, 3600)

    def _trend_label(self, closes) -> str:
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
        key = (symbol, timeframe)
        now = datetime.now()
        with self._trend_cache_lock:
            cached = self._trend_cache.get(key)
            if cached:
                value, expires_at = cached
                if expires_at > now:
                    return value
        try:
            ohlcv = self.market.fetch_ohlcv(symbol, timeframe, 250)
            trend = "unknown"
            if ohlcv and len(ohlcv) >= 200:
                closes = [c[4] for c in ohlcv]
                trend = self._trend_label(closes)
        except Exception as e:
            logger.debug(f"HTF trend fetch failed {symbol}/{timeframe}: {e}")
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

    def _multi_timeframe_score(self, symbol):
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
    def _compute_market_regime(self, btc_change_24h: float) -> str:
        """
        Classify current market regime:
        - 'strong_bull': BTC +3% or more in 24h
        - 'bull': BTC +1% to +3%
        - 'ranging': BTC -1% to +1%
        - 'bear': BTC -1% to -3%
        - 'strong_bear': BTC -3% or less
        """
        try:
            if btc_change_24h >= 3.0:
                return 'strong_bull'
            if btc_change_24h >= 1.0:
                return 'bull'
            if btc_change_24h <= -3.0:
                return 'strong_bear'
            if btc_change_24h <= -1.0:
                return 'bear'
            return 'ranging'
        except Exception:
            return 'ranging'

    # ------------------------------------------------------------------
    def _fetch_base(self, coin: CoinConfig) -> Optional[Dict]:
        try:
            ohlcv = self.market.fetch_ohlcv(
                coin.symbol, config.get('TIMEFRAME'), config.get('MAX_CANDLES')
            )
        except Exception as e:
            logger.warning(f"ohlcv fetch failed {coin.symbol}: {e}")
            return None

        if not ohlcv or len(ohlcv) < 50:
            return None

        closes = [c[4] for c in ohlcv]
        highs = [c[2] for c in ohlcv]
        lows = [c[3] for c in ohlcv]
        volumes = [c[5] for c in ohlcv]

        try:
            ticker = self.market.fetch_ticker(coin.symbol)
        except Exception as e:
            logger.warning(f"ticker fetch failed {coin.symbol}: {e}")
            return None
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
            'coin': coin, 'closes': closes, 'highs': highs, 'lows': lows,
            'volumes': volumes, 'ticker': ticker, 'rsi_value': coin_rsi,
            'atr_value': current_atr,
            'price_change_24h': ticker.get('percentage', 0.0) or 0.0,
            'base_scores': base_scores,
        }

    def _fetch_all_parallel(self, coins: List[CoinConfig]) -> List[Dict]:
        results: List[Dict] = []
        enabled = [c for c in coins if c.enabled]
        if not enabled:
            return results

        if len(enabled) <= 2:
            for coin in enabled:
                try:
                    data = self._fetch_base(coin)
                    if data:
                        results.append(data)
                except Exception as e:
                    logger.error(f"Fetch error on {coin.symbol}: {e}")
            return results

        workers = min(FETCH_MAX_WORKERS, len(enabled))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch") as ex:
            futures = {ex.submit(self._fetch_base, c): c for c in enabled}
            for f in as_completed(futures):
                coin = futures[f]
                try:
                    data = f.result()
                    if data:
                        results.append(data)
                except Exception as e:
                    logger.error(f"Fetch error on {coin.symbol}: {e}")
        return results

    def _finalize_signal(self, data, market_avg_rsi, btc_change_24h,
                         weights=None, market_regime: str = "ranging"):
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
            htf, mtf_score=mtf_score, tracker=self.tracker,
            weights=weights,
        )

        entry_price = ticker.get('last', 0.0) or 0.0
        risk = RiskManager.compute(result['signal_type'], entry_price, data['atr_value'])

        return CoinSignal(
            symbol=coin.symbol, name=coin.name,
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
            btc_bullish=self.btc_bullish, htf_trend=htf, is_valid=True,
            mtf_score=mtf_score, mtf_details=mtf_details,
            atr_value=data['atr_value'],
            stop_loss=risk['stop_loss'], take_profit=risk['take_profit'],
            risk_reward_ratio=risk['risk_reward_ratio'],
            risk_amount_usd=risk['risk_amount_usd'],
            suggested_position_usd=risk['suggested_position_usd'],
            btc_trend_label=self.btc_trend_label,
            market_regime=market_regime,
        )

    # ------------------------------------------------------------------
    def _detect_state_changes(self, new_signals: Dict[str, CoinSignal],
                              current_prices: Dict[str, float]):
        """
        Detect when a previously actionable signal weakens or flips.
        Notifies external bot to close the position.
        """
        if not _cfg('EXTERNAL_BOT_NOTIFY_STATE_CHANGE', True):
            return

        try:
            states = self.tracker.get_all_states()
        except Exception as e:
            logger.debug(f"get_all_states failed: {e}")
            return

        for symbol, new_sig in new_signals.items():
            prev_state = states.get(symbol)
            if not prev_state:
                continue

            prev_type = prev_state.get('last_signal_type') or 'NEUTRAL'
            prev_pct = float(prev_state.get('confidence') or 0.0)
            curr_type = new_sig.signal_type.name
            curr_pct = float(new_sig.total_percentage)

            # Detect actionable change
            was_actionable = prev_type in ('BUY', 'STRONG_BUY', 'SELL', 'STRONG_SELL')
            is_actionable = curr_type in ('BUY', 'STRONG_BUY', 'SELL', 'STRONG_SELL')

            if not was_actionable:
                continue

            # Case 1: signal flipped to NEUTRAL
            # Case 2: signal flipped direction
            # Case 3: signal weakened beyond threshold
            reason = None
            if curr_type == 'NEUTRAL':
                reason = "signal_now_neutral"
            elif prev_type in ('BUY', 'STRONG_BUY') and curr_type in ('SELL', 'STRONG_SELL'):
                reason = "signal_flipped_to_sell"
            elif prev_type in ('SELL', 'STRONG_SELL') and curr_type in ('BUY', 'STRONG_BUY'):
                reason = "signal_flipped_to_buy"
            else:
                # Check magnitude weakening
                if abs(prev_pct) > 0 and abs(curr_pct) < abs(prev_pct):
                    drop = abs(prev_pct) - abs(curr_pct)
                    threshold = _cfg('STATE_CHANGE_THRESHOLD', 30.0)
                    if drop >= threshold:
                        reason = f"signal_weakened_by_{drop:.1f}%"

            if reason:
                logger.info(
                    f"State change detected: {symbol} "
                    f"{prev_type} -> {curr_type} ({reason})"
                )
                ok = external_bot.send_state_change(
                    symbol=symbol,
                    name=new_sig.name,
                    previous_type=prev_type,
                    current_type=curr_type,
                    previous_percentage=prev_pct,
                    current_percentage=curr_pct,
                    current_price=current_prices.get(symbol, new_sig.current_price),
                    reason=reason,
                )
                if ok:
                    logger.info(f"External bot notified to close {symbol}")

    # ------------------------------------------------------------------
    def update_all(self) -> bool:
        with self.lock:
            coins = get_coins()
            logger.info(f"Updating {len(coins)} coins...")

            self.btc_bullish, self.btc_trend_label = self.check_btc_trend()
            logger.info(f"BTC trend: {self.btc_trend_label} (bullish={self.btc_bullish})")

            self.fear_greed_index = self.fgi_fetcher.get()

            # Compute weights once per cycle
            try:
                weights = SignalProcessor._get_weights(self.tracker)
                logger.info(f"Weights computed for {len(weights)} indicators")
            except Exception as e:
                logger.warning(f"Weights computation failed: {e}")
                weights = {k: 1.0 for k in BASE_MAX_PER_INDICATOR}

            intermediate = self._fetch_all_parallel(coins)

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

            market_regime = self._compute_market_regime(btc_change_24h)

            logger.info(
                f"Market context: avg_rsi={market_avg_rsi:.1f}, "
                f"btc_change_24h={btc_change_24h:+.2f}%, regime={market_regime}"
            )

            success = 0
            current_prices: Dict[str, float] = {}
            new_signals: Dict[str, CoinSignal] = {}

            for data in intermediate:
                try:
                    sig = self._finalize_signal(
                        data, market_avg_rsi, btc_change_24h,
                        weights=weights, market_regime=market_regime,
                    )
                    if sig and sig.is_valid:
                        self.signals[data['coin'].symbol] = sig
                        new_signals[data['coin'].symbol] = sig
                        current_prices[data['coin'].symbol] = sig.current_price
                        success += 1

                        # Record signal & get its DB id
                        signal_id = self.tracker.record(sig)

                        # Notify NTFY
                        self.notification_manager.create_notification(sig)

                        # Send to external bot if actionable
                        if sig.signal_type.name in (
                            'BUY', 'STRONG_BUY', 'SELL', 'STRONG_SELL'
                        ):
                            external_bot.send_entry(
                                sig,
                                confidence=abs(sig.total_percentage),
                                signal_id=signal_id,
                            )

                        # Update state
                        self.tracker.update_state(
                            symbol=sig.symbol,
                            signal_type=sig.signal_type.name,
                            signal_id=signal_id,
                            price=sig.current_price,
                            confidence=sig.total_percentage,
                        )
                except Exception as e:
                    logger.error(f"Pass 2 error on {data['coin'].symbol}: {e}")

            # Detect state changes for external bot (close positions)
            try:
                self._detect_state_changes(new_signals, current_prices)
            except Exception as e:
                logger.error(f"State change detection error: {e}")

            # Evaluate pending signals
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
            'symbol': s.symbol, 'name': s.name,
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
            'mtf_score': s.mtf_score, 'mtf_details': s.mtf_details,
            'atr_value': s.atr_value, 'stop_loss': s.stop_loss,
            'take_profit': s.take_profit, 'risk_reward_ratio': s.risk_reward_ratio,
            'risk_amount_usd': s.risk_amount_usd,
            'suggested_position_usd': s.suggested_position_usd,
            'market_regime': s.market_regime,
            'is_valid': True,
        }

    def _default_coin(self, coin: CoinConfig) -> Dict:
        return {
            'symbol': coin.symbol, 'name': coin.name,
            'current_price': 0, 'formatted_price': '0',
            'price_change_24h': 0, 'formatted_24h_change': '0.00%',
            'volume_24h': 0, 'formatted_volume_24h': '0',
            'total_percentage': 0, 'total_score': 0,
            'signal_type': SignalType.NEUTRAL.value,
            'signal_strength': 'Unavailable', 'signal_color': 'secondary',
            'indicators': [], 'last_updated_str': 'Unknown',
            'fear_greed_value': self.fear_greed_index,
            'btc_bullish': self.btc_bullish, 'htf_trend': 'unknown',
            'mtf_score': 0.0, 'mtf_details': {}, 'atr_value': 0.0,
            'stop_loss': 0.0, 'take_profit': 0.0, 'risk_reward_ratio': 0.0,
            'risk_amount_usd': 0.0, 'suggested_position_usd': 0.0,
            'market_regime': 'unknown',
            'is_valid': False,
        }

    @staticmethod
    def _format_price(v: float) -> str:
        try:
            if v >= 1000: return f"{v:,.2f}"
            if v >= 1: return f"{v:.4f}"
            if v >= 0.01: return f"{v:.5f}"
            return f"{v:.8f}"
        except Exception:
            return "0"

    @staticmethod    
    def _format_volume(v: float) -> str:
        try:
            if v >= 1_000_000_000: return f"{v/1_000_000_000:.2f}B"
            if v >= 1_000_000: return f"{v/1_000_000:.2f}M"
            if v >= 1_000: return f"{v/1_000:.2f}K"
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
        if delta.days > 0: return f"{delta.days} day(s) ago"
        if delta.seconds >= 3600: return f"{delta.seconds//3600} hour(s) ago"
        if delta.seconds >= 60: return f"{delta.seconds//60} minute(s) ago"
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
            'total_coins': total_coins, 'updated_coins': len(coins),
            'avg_signal': avg,
            'strong_buy_signals': strong_buy, 'buy_signals': buy,
            'neutral_signals': neutral, 'sell_signals': sell,
            'strong_sell_signals': strong_sell,
            'last_update_str': self._format_time_delta(self.last_update) if self.last_update else 'Unknown',
            'total_notifications': len(self.notification_manager.history),
            'fear_greed_index': self.fear_greed_index,
            'btc_bullish': self.btc_bullish, 'btc_trend': self.btc_trend_label,
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
                'allow_sells_in_bull': _cfg('ALLOW_SELLS_IN_BULL_MARKET', True),
                'external_bot_enabled': _cfg('EXTERNAL_BOT_ENABLED', False),
            }
        }


# ======================================================================
# Update Scheduler
# ======================================================================
class UpdateScheduler:
    def __init__(self, manager: SignalManager):
        self.manager = manager
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_run: Optional[datetime] = None
        self._initial_delay = max(0, INITIAL_UPDATE_DELAY)
        self._run_lock = Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="UpdateScheduler")
        self._thread.start()
        logger.info(
            f"UpdateScheduler started (initial delay: {self._initial_delay}s, "
            f"interval: {config.get('UPDATE_INTERVAL')}s)"
        )

    def _run_once(self):
        if not self._run_lock.acquire(blocking=False):
            logger.debug("update_all already running — skipping tick")
            return
        try:
            self.manager.update_all()
            self._last_run = datetime.now()
        except Exception as e:
            logger.error(f"Update error: {e}")
        finally:
            self._run_lock.release()

    def _run(self):
        if self._stop.wait(timeout=self._initial_delay):
            return
        self._run_once()

        while not self._stop.is_set():
            interval = max(10, int(config.get('UPDATE_INTERVAL') or 60))
            self._wake.wait(timeout=interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            self._run_once()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_in_background(self):
        threading.Thread(
            target=self._run_once,
            daemon=True, name="ManualUpdate"
        ).start()


# ======================================================================
# Initialize
# ======================================================================
try:
    signal_manager = SignalManager()
    scheduler = UpdateScheduler(signal_manager)
    start_time = time.time()
    scheduler.start()
    logger.info("SignalManager and scheduler initialized")
except Exception as e:
    import traceback
    logger.error(f"FATAL: SignalManager init failed: {e}")
    traceback.print_exc()
    signal_manager = None
    scheduler = None
    start_time = time.time()


# ======================================================================
# Context Processor
# ======================================================================
@app.context_processor
def utility_processor():
    def signal_color_to_css(color_name):
        return {
            'success': '#3BB273', 'primary': '#2E86AB',
            'secondary': '#8b98a5', 'warning': '#e6a817', 'danger': '#d64545',
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
    if signal_manager is None:
        return "Initialization failed — check logs.", 503
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
    if signal_manager is None:
        return jsonify({'status': 'error', 'message': 'not initialized'}), 503
    return jsonify({
        'status': 'success',
        'data': signal_manager.get_coins_data(),
        'timestamp': datetime.now().isoformat(),
    })


@app.route('/api/update', methods=['POST'])
def manual_update():
    if signal_manager is None or scheduler is None:
        return jsonify({'status': 'error'}), 503
    scheduler.run_in_background()
    return jsonify({
        'status': 'accepted',
        'message': 'Update started in background.',
        'timestamp': datetime.now().isoformat(),
    }), 202


@app.route('/api/health')
def health():
    if signal_manager is None:
        return jsonify({
            'status': 'error',
            'message': 'SignalManager failed to initialize',
            'uptime': time.time() - start_time,
            'version': '8.4.0',
        }), 503

    last = signal_manager.last_update
    status = 'healthy'
    if last and (datetime.now() - last).total_seconds() > config.get('UPDATE_INTERVAL') * 3:
        status = 'warning'
    elif not last:
        status = 'starting'

    return jsonify({
        'status': status,
        'last_update': last.isoformat() if last else None,
        'coins': len(signal_manager.signals),
        'uptime': time.time() - start_time,
        'fear_greed': signal_manager.fear_greed_index,
        'btc_bullish': signal_manager.btc_bullish,
        'btc_trend': signal_manager.btc_trend_label,
        'notifications': len(signal_manager.notification_manager.history),
        'version': '8.4.0',
        'tracker': signal_manager.tracker.health_check(),
        'external_bot_enabled': _cfg('EXTERNAL_BOT_ENABLED', False),
        'max_total_score': MAX_TOTAL_SCORE,
    })


@app.route('/api/notifications')
def get_notifications():
    if signal_manager is None:
        return jsonify({'notifications': [], 'total': 0}), 503
    limit = request.args.get('limit', 10, type=int)
    nots = signal_manager.notification_manager.get_recent(limit)
    return jsonify({
        'notifications': [asdict(n) for n in nots],
        'total': len(signal_manager.notification_manager.history),
    })


@app.route('/api/notifications/clear', methods=['POST'])
def clear_notifications():
    if signal_manager is None:
        return jsonify({'status': 'error'}), 503
    signal_manager.notification_manager.clear_history()
    return jsonify({'status': 'success'})


@app.route('/api/exchange_status')
def exchange_status():
    if signal_manager is None:
        return jsonify({'exchanges': []}), 503
    status = signal_manager.market.get_status()
    keys = get_exchange_keys()
    for ex in status.get('exchanges', []):
        ex['has_keys'] = ex['name'] in keys
    return jsonify(status)


@app.route('/api/signal_stats')
def api_signal_stats():
    if signal_manager is None:
        return jsonify({'status': 'error'}), 503
    days = request.args.get('days', 30, type=int)
    return jsonify({
        'status': 'success',
        'signal_stats': signal_manager.tracker.get_signal_stats(days),
        'indicator_stats': signal_manager.tracker.get_all_stats(),
        'recent_signals': signal_manager.tracker.get_recent_signals(20),
    })


@app.route('/api/signal_states')
def api_signal_states():
    if signal_manager is None:
        return jsonify({'status': 'error'}), 503
    return jsonify({
        'status': 'success',
        'states': signal_manager.tracker.get_all_states(),
    })


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
        if signal_manager is not None:
            signal_manager.notification_manager.clear_history()
        if scheduler is not None:
            scheduler.wake()
        # Reload external bot config
        try:
            external_bot.reload_config()
        except Exception:
            pass
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
    if signal_manager is not None:
        signal_manager.notification_manager.clear_history()
    if scheduler is not None:
        scheduler.wake()
    try:
        external_bot.reload_config()
    except Exception:
        pass
    return jsonify({'status': 'success', 'values': values})


@app.route('/api/test_ntfy')
def test_ntfy():
    if signal_manager is None:
        return jsonify({'success': False}), 503
    msg = "Test notification - system is working properly"
    ok = signal_manager.notification_manager.send_ntfy(msg, "Signal Test", "3", "test_tube")
    return jsonify({'success': ok})


@app.route('/api/test_external_bot')
def test_external_bot():
    """Send a test event to the external bot."""
    if not _cfg('EXTERNAL_BOT_ENABLED', False):
        return jsonify({'status': 'error', 'message': 'External bot disabled'}), 400
    if signal_manager is None:
        return jsonify({'status': 'error'}), 503

    test_signal = type('S', (), {})()  # dummy object
    payload = {
        'symbol': 'TEST/USDT',
        'name': 'Test',
        'signal_type': 'BUY',
        'direction': 'long',
        'entry_price': 100.0,
        'score': 3.5,
        'percentage': 44.8,
        'confidence': 44.8,
        'signal_id': None,
        'stop_loss': 98.0,
        'take_profit': 104.0,
        'risk_reward_ratio': 2.0,
        'suggested_position_usd': 50.0,
        'risk_amount_usd': 10.0,
        'btc_bullish': True,
        'htf_trend': 'bullish',
        'fear_greed': 50,
        'atr_value': 1.5,
        'mtf_details': {'15m': 'bullish'},
        'price_change_24h': 1.2,
    }
    ok = external_bot._post('entry', payload)
    return jsonify({'success': ok})


# ======================================================================
# Startup notification
# ======================================================================
def send_startup_notification():
    try:
        if signal_manager is None:
            return
        mode = config.get('USE_BTC_FILTER')
        htf = "ON" if config.get('USE_HTF_CONFIRMATION') else "OFF"
        mtf = "ON" if _cfg('USE_MTF_CONFIRMATION', False) else "OFF"
        dw = "ON" if _cfg('USE_DYNAMIC_WEIGHTS', True) else "OFF"
        ext_bot = "ON" if _cfg('EXTERNAL_BOT_ENABLED', False) else "OFF"
        backend_name = "PostgreSQL (Supabase)" if USE_POSTGRES else "SQLite (local)"
        sell_bull = "ON" if _cfg('ALLOW_SELLS_IN_BULL_MARKET', True) else "OFF"

        coins = get_coins()
        keys = get_exchange_keys()
        msg = (
            f"Crypto Discriminating Analyzer Started (v8.4)\n"
            f"Tracking {len(coins)} coins\n"
            f"Timeframe: {config.get('TIMEFRAME')} | HTF: {config.get('HTF_TIMEFRAME')}\n"
            f"BTC filter: {mode}\n"
            f"HTF: {htf} | MTF: {mtf} | Dynamic weights: {dw}\n"
            f"Sells in bull market: {sell_bull}\n"
            f"External bot: {ext_bot}\n"
            f"Update interval: {config.get('UPDATE_INTERVAL')}s\n"
            f"Signal tracking: {backend_name}"
        )
        signal_manager.notification_manager.send_ntfy(msg, "System Started", "3", "rocket")
    except Exception as e:
        logger.error(f"Startup notification error: {e}")


def delayed_startup():
    time.sleep(STARTUP_NOTIFY_DELAY)
    send_startup_notification()


threading.Thread(target=delayed_startup, daemon=True, name="StartupNotify").start()


# ======================================================================
# Graceful shutdown
# ======================================================================
import signal as _signal_sys


def _shutdown_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down cleanly")
    if scheduler is not None:
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
    logger.info("Crypto Discriminating Analyzer v8.4.0 (local run)")
    logger.info(f"Backend: {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")
    logger.info(f"Coins: {[c.symbol for c in get_coins()]}")
    logger.info(f"Max total score: {MAX_TOTAL_SCORE}")
    logger.info(f"External bot: {'ON' if _cfg('EXTERNAL_BOT_ENABLED', False) else 'OFF'}")
    logger.info(f"NTFY: {NTFY_URL}")
    logger.info(f"Port: {PORT}")
    logger.info("=" * 60)

    app.run(host='0.0.0.0', port=PORT, debug=FLASK_DEBUG)
