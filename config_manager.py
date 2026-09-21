"""
Runtime Configuration Manager
Priority: JSON file > Environment variables > Defaults
Allows changing settings from web UI without redeploying.
"""
import os
import json
import logging
from typing import Any, Dict
from threading import Lock

logger = logging.getLogger(__name__)

CONFIG_FILE = os.environ.get('CONFIG_FILE', 'user_settings.json')


class ConfigManager:
    """Manages settings with runtime overrides saved to JSON file."""

    DEFAULTS: Dict[str, Any] = {
        # ============ General ============
        'COINS_LIST': "BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,LTC/USDT",
        'TIMEFRAME': '15m',
        'HTF_TIMEFRAME': '4h',
        'MAX_CANDLES': 250,
        'UPDATE_INTERVAL': 180,

        # ============ Exchange ============
        'EXCHANGE_PRIORITY': 'okx,bybit,kraken,binance',

        # ============ BTC filter ============
        'USE_BTC_FILTER': 'modify',           # off | modify | block
        'BTC_BEARISH_DISCOUNT': 0.70,
        'BTC_NEUTRAL_DISCOUNT': 0.85,

        # ============ HTF confirmation ============
        'USE_HTF_CONFIRMATION': True,
        'HTF_BONUS': 1.10,
        'HTF_PENALTY': 0.90,

        # ============ Signal thresholds ============
        # NOTE: v7.0 uses 10 indicators with max total score of 7.8
        'STRONG_BUY_THRESHOLD': 5.0,
        'BUY_THRESHOLD': 2.5,
        'SELL_THRESHOLD': -2.5,
        'STRONG_SELL_THRESHOLD': -5.0,

        # ============ RSI ============
        'RSI_OVERSOLD_STRONG': 30,
        'RSI_OVERSOLD': 40,
        'RSI_OVERBOUGHT': 60,
        'RSI_OVERBOUGHT_STRONG': 70,

        # ============ Notifications ============
        'NOTIFY_ON_BUY': True,
        'NOTIFY_ON_SELL': True,
        'MIN_NOTIFY_INTERVAL': 600,
    }

    TYPES: Dict[str, type] = {
        'MAX_CANDLES': int,
        'UPDATE_INTERVAL': int,
        'USE_HTF_CONFIRMATION': bool,
        'BTC_BEARISH_DISCOUNT': float,
        'BTC_NEUTRAL_DISCOUNT': float,
        'HTF_BONUS': float,
        'HTF_PENALTY': float,
        'STRONG_BUY_THRESHOLD': float,
        'BUY_THRESHOLD': float,
        'SELL_THRESHOLD': float,
        'STRONG_SELL_THRESHOLD': float,
        'RSI_OVERSOLD_STRONG': int,
        'RSI_OVERSOLD': int,
        'RSI_OVERBOUGHT': int,
        'RSI_OVERBOUGHT_STRONG': int,
        'NOTIFY_ON_BUY': bool,
        'NOTIFY_ON_SELL': bool,
        'MIN_NOTIFY_INTERVAL': int,
        'EXCHANGE_PRIORITY': str,
    }

    _instance = None
    _instance_lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self):
        self._lock = Lock()
        self._values: Dict[str, Any] = {}
        self._load()

    def _load(self):
        # 1. Start with defaults
        self._values = dict(self.DEFAULTS)

        # 2. Environment overrides
        for key in self.DEFAULTS:
            env_val = os.environ.get(key)
            if env_val is not None:
                self._values[key] = self._coerce(key, env_val)

        # 3. JSON file overrides (highest priority)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                for key, val in saved.items():
                    if key in self.DEFAULTS:
                        self._values[key] = self._coerce(key, val)
                logger.info(f"Loaded runtime settings from {CONFIG_FILE}")
            except Exception as e:
                logger.error(f"Failed to load {CONFIG_FILE}: {e}")

    def _coerce(self, key: str, value: Any) -> Any:
        target = self.TYPES.get(key, str)
        try:
            if target is bool:
                if isinstance(value, bool):
                    return value
                return str(value).strip().lower() in ('true', '1', 'yes', 'on')
            if target is int:
                return int(float(value))
            if target is float:
                return float(value)
            return str(value)
        except (ValueError, TypeError):
            return self.DEFAULTS[key]

    def get(self, key: str) -> Any:
        return self._values.get(key, self.DEFAULTS.get(key))

    def get_all(self) -> Dict[str, Any]:
        return dict(self._values)

    def update(self, new_values: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for key, val in new_values.items():
                if key in self.DEFAULTS:
                    self._values[key] = self._coerce(key, val)

            errors = self._validate()

            try:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(self._values, f, indent=2)
                logger.info(f"Settings saved to {CONFIG_FILE}")
            except Exception as e:
                logger.error(f"Failed to save settings: {e}")
                errors.append(f"Save failed: {e}")

            return {'values': dict(self._values), 'errors': errors}

    def _validate(self) -> list:
        errors = []
        v = self._values

        # BTC filter mode
        if v['USE_BTC_FILTER'] not in ('off', 'modify', 'block'):
            errors.append("USE_BTC_FILTER must be off / modify / block")
            v['USE_BTC_FILTER'] = 'modify'

        # Thresholds ordering
        if v['BUY_THRESHOLD'] >= v['STRONG_BUY_THRESHOLD']:
            errors.append("BUY_THRESHOLD must be < STRONG_BUY_THRESHOLD")

        if v['SELL_THRESHOLD'] <= v['STRONG_SELL_THRESHOLD']:
            errors.append("SELL_THRESHOLD must be > STRONG_SELL_THRESHOLD")

        # Discounts
        if not (0 < v['BTC_BEARISH_DISCOUNT'] <= 1.0):
            errors.append("BTC_BEARISH_DISCOUNT must be between 0 and 1")
            v['BTC_BEARISH_DISCOUNT'] = 0.70

        if not (0 < v['BTC_NEUTRAL_DISCOUNT'] <= 1.0):
            errors.append("BTC_NEUTRAL_DISCOUNT must be between 0 and 1")
            v['BTC_NEUTRAL_DISCOUNT'] = 0.85

        # HTF bonus/penalty
        if not (1.0 <= v['HTF_BONUS'] <= 2.0):
            errors.append("HTF_BONUS must be between 1.0 and 2.0")
            v['HTF_BONUS'] = 1.10

        if not (0.5 <= v['HTF_PENALTY'] <= 1.0):
            errors.append("HTF_PENALTY must be between 0.5 and 1.0")
            v['HTF_PENALTY'] = 0.90

        # RSI ordering
        if v['RSI_OVERSOLD_STRONG'] >= v['RSI_OVERSOLD']:
            errors.append("RSI_OVERSOLD_STRONG must be < RSI_OVERSOLD")

        if v['RSI_OVERSOLD'] >= v['RSI_OVERBOUGHT']:
            errors.append("RSI_OVERSOLD must be < RSI_OVERBOUGHT")

        if v['RSI_OVERBOUGHT'] >= v['RSI_OVERBOUGHT_STRONG']:
            errors.append("RSI_OVERBOUGHT must be < RSI_OVERBOUGHT_STRONG")

        # Update interval
        if v['UPDATE_INTERVAL'] < 30:
            errors.append("UPDATE_INTERVAL must be at least 30 seconds")
            v['UPDATE_INTERVAL'] = 30

        if v['UPDATE_INTERVAL'] > 3600:
            errors.append("UPDATE_INTERVAL must be at most 3600 seconds")
            v['UPDATE_INTERVAL'] = 3600

        # Candles
        if v['MAX_CANDLES'] < 100:
            errors.append("MAX_CANDLES must be at least 100")
            v['MAX_CANDLES'] = 100

        if v['MAX_CANDLES'] > 1000:
            errors.append("MAX_CANDLES must be at most 1000")
            v['MAX_CANDLES'] = 1000

        # Coins list
        if not v['COINS_LIST'] or ',' not in v['COINS_LIST']:
            errors.append("COINS_LIST must contain at least 2 symbols")
            v['COINS_LIST'] = self.DEFAULTS['COINS_LIST']

        # Exchange priority - validate each name
        valid_exchanges = {'okx', 'bybit', 'kraken', 'binance', 'coinbase',
                           'kucoin', 'gateio', 'bitfinex', 'huobi', 'mexc'}
        names = [n.strip() for n in v['EXCHANGE_PRIORITY'].split(',') if n.strip()]
        if not names:
            errors.append("EXCHANGE_PRIORITY cannot be empty")
            v['EXCHANGE_PRIORITY'] = self.DEFAULTS['EXCHANGE_PRIORITY']
        else:
            invalid = [n for n in names if n.lower() not in valid_exchanges]
            if invalid:
                errors.append(f"Unknown exchange(s): {', '.join(invalid)}")

        # Notification interval
        if v['MIN_NOTIFY_INTERVAL'] < 30:
            errors.append("MIN_NOTIFY_INTERVAL must be at least 30 seconds")
            v['MIN_NOTIFY_INTERVAL'] = 30

        return errors

    def reset_to_defaults(self) -> Dict[str, Any]:
        try:
            if os.path.exists(CONFIG_FILE):
                os.remove(CONFIG_FILE)
        except Exception as e:
            logger.error(f"Failed to remove {CONFIG_FILE}: {e}")
        self._load()
        return dict(self._values)


# Global singleton
config = ConfigManager()
