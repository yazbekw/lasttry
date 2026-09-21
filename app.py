"""
Crypto Signal Analyzer Bot - Weighted Edition
Version 6.0.0 - Weighted Scoring + SELL Signals + BTC as Modifier + Web UI Settings
All notifications in English, no emojis.
"""

import os
import time
import logging
import threading
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum
from threading import Lock

from flask import Flask, render_template, jsonify, request
import ccxt

from config_manager import config
from settings_metadata import SETTINGS_METADATA, SETTINGS_GROUPS
from market_data import MarketDataClient

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
# Static application values (not changeable from UI)
# ======================================================================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '')
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', 'crypto_buy_alerts')
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
FGI_API_URL = "https://api.alternative.me/fng/"
SECRET_KEY = os.environ.get('SECRET_KEY', 'crypto-signal-secret-2026')
FLASK_DEBUG = os.environ.get('FLASK_DEBUG', 'false').lower() in ('true', '1', 'yes')
PORT = int(os.environ.get('PORT', 5000))

COIN_NAMES = {
    "BTC/USDT": "Bitcoin",
    "ETH/USDT": "Ethereum",
    "BNB/USDT": "Binance Coin",
    "SOL/USDT": "Solana",
    "XRP/USDT": "Ripple",
    "LTC/USDT": "Litecoin",
    "ADA/USDT": "Cardano",
    "DOGE/USDT": "Dogecoin",
    "AVAX/USDT": "Avalanche",
    "DOT/USDT": "Polkadot",
    "MATIC/USDT": "Polygon",
    "LINK/USDT": "Chainlink",
    "TRX/USDT": "Tron",
    "TON/USDT": "Toncoin",
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
}

INDICATOR_DISPLAY_NAMES = {
    IndicatorType.TREND.value: 'Trend (EMA 50/200)',
    IndicatorType.MOMENTUM.value: 'Momentum (RSI)',
    IndicatorType.VOLUME.value: 'Volume Confirmation',
    IndicatorType.STRUCTURE.value: 'Structure (Breakout/Breakdown)',
}

INDICATOR_DESCRIPTIONS = {
    IndicatorType.TREND.value: 'Price vs EMA50 and EMA200',
    IndicatorType.MOMENTUM.value: 'RSI oversold / overbought zones',
    IndicatorType.VOLUME.value: 'Volume spike with price direction',
    IndicatorType.STRUCTURE.value: 'Breakout above highs or breakdown below lows',
}


# ======================================================================
# Helper to build coin list from config
# ======================================================================
def get_coins() -> List[CoinConfig]:
    """Build coin list from current config."""
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
# Fear & Greed Fetcher
# ======================================================================
class FearGreedFetcher:
    def __init__(self):
        self.last_value = 50
        self.last_update = None
        self.cache_ttl = 300

    def get(self) -> int:
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
# Indicator Calculators (weighted, signed)
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
    def trend_score(close_prices: List[float]) -> float:
        """Range: -1.5 .. +1.5"""
        if len(close_prices) < 200:
            return 0.0
        try:
            ema50 = IndicatorCalculator.ema(close_prices, 50)[-1]
            ema200 = IndicatorCalculator.ema(close_prices, 200)[-1]
            price = close_prices[-1]
            if price > ema50 > ema200:
                return 1.5
            if price > ema50 and ema50 <= ema200:
                return 0.75
            if price < ema50 < ema200:
                return -1.5
            if price < ema50 and ema50 >= ema200:
                return -0.75
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def momentum_score(close_prices: List[float]) -> float:
        """Range: -1.0 .. +1.0 based on RSI zones."""
        if len(close_prices) < 15:
            return 0.0
        try:
            rsi_vals = IndicatorCalculator.rsi(close_prices, 14)
            rsi = rsi_vals[-1] if rsi_vals and rsi_vals[-1] is not None else 50.0
            if rsi < config.get('RSI_OVERSOLD_STRONG'):
                return 1.0
            if rsi < config.get('RSI_OVERSOLD'):
                return 0.75
            if rsi < 50:
                return 0.25
            if rsi > config.get('RSI_OVERBOUGHT_STRONG'):
                return -1.0
            if rsi > config.get('RSI_OVERBOUGHT'):
                return -0.75
            if rsi > 50:
                return -0.25
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def volume_score(volumes: List[float], close_prices: List[float]) -> float:
        """Range: -1.0 .. +1.0"""
        if len(volumes) < 20 or len(close_prices) < 2:
            return 0.0
        try:
            avg_vol = sum(volumes[-20:]) / 20
            current_vol = volumes[-1]
            if avg_vol <= 0:
                return 0.0
            ratio = current_vol / avg_vol
            price_rising = close_prices[-1] > close_prices[-2]
            price_falling = close_prices[-1] < close_prices[-2]

            if ratio > 1.5 and price_rising:
                return 1.0
            if ratio > 1.0 and price_rising:
                return 0.75
            if ratio > 1.5 and price_falling:
                return -1.0
            if ratio > 1.0 and price_falling:
                return -0.75
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def structure_score(highs: List[float], lows: List[float],
                        close_prices: List[float], lookback: int = 20) -> float:
        """Range: -1.0 .. +1.0"""
        if len(highs) < lookback + 1 or len(lows) < lookback + 1:
            return 0.0
        try:
            recent_high = max(highs[-lookback-1:-1])
            recent_low = min(lows[-lookback-1:-1])
            price = close_prices[-1]

            if price > recent_high:
                return 1.0
            if price < recent_low:
                return -1.0
            if price > recent_high * 0.98:
                return 0.5
            if price < recent_low * 1.02:
                return -0.5
            return 0.0
        except Exception:
            return 0.0


# ======================================================================
# Signal Processor
# ======================================================================
class SignalProcessor:
    MAX_TOTAL = 4.5

    @staticmethod
    def process(raw_scores: Dict[str, float],
                btc_bullish: bool,
                btc_label: str,
                htf_trend: str) -> Dict:
        total = sum(raw_scores.values())

        btc_mode = config.get('USE_BTC_FILTER')

        # ----- BTC filter -----
        if btc_mode == 'block':
            if not btc_bullish:
                return SignalProcessor._build_result(
                    0.0, raw_scores, forced_neutral=True
                )
        elif btc_mode == 'modify':
            if total > 0:
                if not btc_bullish:
                    if btc_label == "bearish":
                        total *= config.get('BTC_BEARISH_DISCOUNT')
                    else:
                        total *= config.get('BTC_NEUTRAL_DISCOUNT')
            elif total < 0 and not btc_bullish:
                total *= 1.15
        # 'off' → no change

        # ----- HTF confirmation -----
        if config.get('USE_HTF_CONFIRMATION') and total != 0:
            agrees = (
                (total > 0 and htf_trend == "bullish") or
                (total < 0 and htf_trend == "bearish")
            )
            disagrees = (
                (total > 0 and htf_trend == "bearish") or
                (total < 0 and htf_trend == "bullish")
            )
            if agrees:
                total *= config.get('HTF_BONUS')
            elif disagrees:
                total *= config.get('HTF_PENALTY')

        total = max(-SignalProcessor.MAX_TOTAL, min(SignalProcessor.MAX_TOTAL, total))
        return SignalProcessor._build_result(total, raw_scores, forced_neutral=False)

    @staticmethod
    def _build_result(total: float, raw_scores: Dict[str, float],
                      forced_neutral: bool = False) -> Dict:

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

        total_percentage = (total / SignalProcessor.MAX_TOTAL) * 100.0
        strength = SignalProcessor.get_signal_strength(signal_type)
        color = SignalProcessor.get_signal_color(signal_type)

        indicator_scores: Dict[str, IndicatorScore] = {}
        for name, raw in raw_scores.items():
            norm_pct = (raw / 1.5) * 100.0
            direction = "bullish" if raw > 0 else ("bearish" if raw < 0 else "neutral")
            indicator_scores[name] = IndicatorScore(
                name=name,
                raw_score=raw,
                weighted_score=raw,
                percentage=norm_pct,
                weight=1.0,
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
# Notification Manager
# ======================================================================
class NotificationManager:
    def __init__(self):
        self.history: List[Notification] = []
        self.max_history = 100
        self.last_notification_time: Dict[str, datetime] = {}

    def add(self, notification: Notification):
        self.history.append(notification)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

    def get_recent(self, limit: int = 10) -> List[Notification]:
        return self.history[-limit:] if self.history else []

    def should_send(self, coin_symbol: str, signal_type: SignalType) -> bool:
        if signal_type in (SignalType.BUY, SignalType.STRONG_BUY) and not config.get('NOTIFY_ON_BUY'):
            return False
        if signal_type in (SignalType.SELL, SignalType.STRONG_SELL) and not config.get('NOTIFY_ON_SELL'):
            return False
        if signal_type == SignalType.NEUTRAL:
            return False

        now = datetime.now()
        min_interval = config.get('MIN_NOTIFY_INTERVAL')
        if coin_symbol in self.last_notification_time:
            delta = now - self.last_notification_time[coin_symbol]
            if delta.total_seconds() < min_interval:
                return False
        return True

    def send_ntfy(self, message: str, title: str = "Crypto Signal",
                  priority: str = "3", tags: str = "chart") -> bool:
        try:
            headers = {
                "Title": title,
                "Priority": priority,
                "Tags": tags,
                "Content-Type": "text/plain; charset=utf-8"
            }
            safe = message.encode('ascii', errors='replace').decode('ascii')
            resp = requests.post(NTFY_URL, data=safe.encode('utf-8'),
                                 headers=headers, timeout=5)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"NTFY error: {e}")
            return False

    def create_notification(self, coin_signal: CoinSignal) -> Optional[Notification]:
        if not self.should_send(coin_signal.symbol, coin_signal.signal_type):
            return None

        st = coin_signal.signal_type
        title = f"{st.value}: {coin_signal.name}"
        message = (
            f"{title}\n"
            f"Weighted Score: {coin_signal.total_score:+.2f} / 4.5\n"
            f"Percentage: {coin_signal.total_percentage:+.1f}%\n"
            f"Price: ${coin_signal.current_price:,.4f}\n"
            f"24h Change: {coin_signal.price_change_24h:+.2f}%\n"
            f"HTF Trend: {coin_signal.htf_trend}\n"
            f"BTC Bullish: {coin_signal.btc_bullish}\n"
            f"Time: {coin_signal.last_updated.strftime('%Y-%m-%d %H:%M')}"
        )

        tags_map = {
            SignalType.STRONG_BUY: "heavy_plus_sign",
            SignalType.BUY: "chart_increasing",
            SignalType.SELL: "chart_decreasing",
            SignalType.STRONG_SELL: "warning",
        }
        tags = tags_map.get(st, "loudspeaker")

        priority_map = {
            SignalType.STRONG_BUY: "4",
            SignalType.BUY: "3",
            SignalType.SELL: "3",
            SignalType.STRONG_SELL: "4",
        }
        priority = priority_map.get(st, "3")

        if self.send_ntfy(message, title, priority, tags):
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
            self.add(notif)
            self.last_notification_time[coin_signal.symbol] = datetime.now()
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
        self.market = MarketDataClient()
        self.lock = Lock()
        self.notification_manager = NotificationManager()
        self.fgi_fetcher = FearGreedFetcher()
        self.fear_greed_index = 50
        self.btc_bullish = False
        self.btc_trend_label = "unknown"

    def _trend_label(self, closes: List[float]) -> str:
        if len(closes) < 200:
            return "unknown"
        try:
            ema50 = IndicatorCalculator.ema(closes, 50)[-1]
            ema200 = IndicatorCalculator.ema(closes, 200)[-1]
            price = closes[-1]
            if price > ema50 > ema200:
                return "bullish"
            if price < ema50 < ema200:
                return "bearish"
            return "neutral"
        except Exception:
            return "unknown"

    def check_btc_trend(self) -> Tuple[bool, str]:
        try:
            ohlcv = self.binance.fetch_ohlcv("BTC/USDT", config.get('TIMEFRAME'), 250)
            if not ohlcv or len(ohlcv) < 200:
                return False, "unknown"
            closes = [c[4] for c in ohlcv]
            label = self._trend_label(closes)
            return (label == "bullish"), label
        except Exception as e:
            logger.error(f"BTC trend error: {e}")
            return False, "unknown"

    def _htf_trend(self, symbol: str) -> str:
        if not config.get('USE_HTF_CONFIRMATION'):
            return "unknown"
        try:
            ohlcv = self.binance.fetch_ohlcv(symbol, config.get('HTF_TIMEFRAME'), 250)
            if not ohlcv or len(ohlcv) < 200:
                return "unknown"
            closes = [c[4] for c in ohlcv]
            return self._trend_label(closes)
        except Exception:
            return "unknown"

    def update_all(self) -> bool:
        with self.lock:
            coins = get_coins()
            logger.info(f"Updating {len(coins)} coins (weighted mode)...")

            self.btc_bullish, self.btc_trend_label = self.check_btc_trend()
            logger.info(f"BTC trend: {self.btc_trend_label} (bullish={self.btc_bullish})")

            self.fear_greed_index = self.fgi_fetcher.get()

            success = 0
            for coin in coins:
                if not coin.enabled:
                    continue
                try:
                    sig = self._process_coin(coin)
                    if sig and sig.is_valid:
                        self.signals[coin.symbol] = sig
                        success += 1
                        self.notification_manager.create_notification(sig)
                except Exception as e:
                    logger.error(f"Error on {coin.symbol}: {e}")

            self.last_update = datetime.now()
            self._save_history()
            logger.info(f"Updated {success}/{len(coins)} coins")
            return success > 0

    def _process_coin(self, coin: CoinConfig) -> Optional[CoinSignal]:
        ohlcv = self.binance.fetch_ohlcv(coin.symbol, config.get('TIMEFRAME'),
                                         config.get('MAX_CANDLES'))
        if not ohlcv or len(ohlcv) < 50:
            return None

        closes = [c[4] for c in ohlcv]
        highs = [c[2] for c in ohlcv]
        lows = [c[3] for c in ohlcv]
        volumes = [c[5] for c in ohlcv]

        ticker = self.binance.fetch_ticker(coin.symbol)
        if not ticker:
            return None

        raw_scores = {
            IndicatorType.TREND.value: IndicatorCalculator.trend_score(closes),
            IndicatorType.MOMENTUM.value: IndicatorCalculator.momentum_score(closes),
            IndicatorType.VOLUME.value: IndicatorCalculator.volume_score(volumes, closes),
            IndicatorType.STRUCTURE.value: IndicatorCalculator.structure_score(highs, lows, closes, 20),
        }

        htf = self._htf_trend(coin.symbol)
        result = SignalProcessor.process(raw_scores, self.btc_bullish,
                                         self.btc_trend_label, htf)

        return CoinSignal(
            symbol=coin.symbol,
            name=coin.name,
            current_price=ticker.get('last', 0.0) or 0.0,
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
        )

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
        data = []
        coins = get_coins()
        for coin in coins:
            sig = self.signals.get(coin.symbol)
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
                'percentage': v.percentage,
                'color': v.color,
                'direction': v.direction,
            })
        return {
            'symbol': s.symbol,
            'name': s.name,
            'current_price': s.current_price,
            'formatted_price': self._format_number(s.current_price),
            'price_change_24h': s.price_change_24h,
            'formatted_24h_change': self._format_percentage(s.price_change_24h),
            'volume_24h': s.volume_24h,
            'formatted_volume_24h': self._format_number(s.volume_24h),
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
            'is_valid': False,
        }

    @staticmethod
    def _format_number(v: float) -> str:
        try:
            if v >= 1_000_000:
                return f"{v/1_000_000:.2f}M"
            if v >= 1_000:
                return f"{v/1_000:.2f}K"
            if v >= 1:
                return f"{v:.2f}"
            return f"{v:.6f}"
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
            'exchange_status': signal_manager.market.get_status(),
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
            'config': {
                'timeframe': config.get('TIMEFRAME'),
                'htf_timeframe': config.get('HTF_TIMEFRAME'),
                'btc_filter_mode': config.get('USE_BTC_FILTER'),
                'htf_confirmation': config.get('USE_HTF_CONFIRMATION'),
                'update_interval': config.get('UPDATE_INTERVAL'),
            }
        }


# ======================================================================
# Background Updater
# ======================================================================
def background_updater():
    while True:
        try:
            signal_manager.update_all()
        except Exception as e:
            logger.error(f"Update error: {e}")
        time.sleep(config.get('UPDATE_INTERVAL'))


# ======================================================================
# Flask App
# ======================================================================
app = Flask(__name__)
app.secret_key = SECRET_KEY
signal_manager = SignalManager()
start_time = time.time()

updater_thread = threading.Thread(target=background_updater, daemon=True)
updater_thread.start()
signal_manager.update_all()


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

@app.route('/api/exchange_status')
def exchange_status():
    return jsonify(signal_manager.market.get_status())


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
        'version': '6.0.0-weighted',
    })


@app.route('/api/notifications')
def get_notifications():
    limit = request.args.get('limit', 10, type=int)
    nots = signal_manager.notification_manager.get_recent(limit)
    return jsonify({
        'notifications': [asdict(n) for n in nots],
        'total': len(signal_manager.notification_manager.history),
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
        threading.Thread(target=signal_manager.update_all, daemon=True).start()
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
    threading.Thread(target=signal_manager.update_all, daemon=True).start()
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
        coins = get_coins()
        msg = (
            f"Crypto Weighted Signal Analyzer Started (v6.0)\n"
            f"Tracking {len(coins)} coins\n"
            f"Timeframe: {config.get('TIMEFRAME')} | HTF: {config.get('HTF_TIMEFRAME')}\n"
            f"BTC filter mode: {mode}\n"
            f"HTF confirmation: {htf}\n"
            f"Update interval: {config.get('UPDATE_INTERVAL')}s\n"
            f"Signals: STRONG BUY / BUY / NEUTRAL / SELL / STRONG SELL"
        )
        signal_manager.notification_manager.send_ntfy(msg, "System Started", "3", "rocket")
    except Exception as e:
        logger.error(f"Startup notification error: {e}")


def delayed_startup():
    time.sleep(5)
    send_startup_notification()


threading.Thread(target=delayed_startup, daemon=True).start()


# ======================================================================
# Entry point
# ======================================================================
if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Crypto Weighted Signal Analyzer v6.0.0")
    logger.info(f"Coins: {[c.symbol for c in get_coins()]}")
    logger.info(f"Timeframe: {config.get('TIMEFRAME')} | HTF: {config.get('HTF_TIMEFRAME')}")
    logger.info(f"BTC filter: {config.get('USE_BTC_FILTER')} | HTF confirm: {config.get('USE_HTF_CONFIRMATION')}")
    logger.info(f"NTFY: {NTFY_URL}")
    logger.info(f"Port: {PORT}")
    logger.info("=" * 60)

    app.run(host='0.0.0.0', port=PORT, debug=FLASK_DEBUG)
