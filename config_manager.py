"""
Runtime Configuration Manager
Priority: JSON file > Environment variables > Defaults
Allows changing settings from web UI without redeploying.

v2 — Auto-syncs DEFAULTS from settings_metadata.SETTINGS_METADATA
"""
import os
import json
import logging
from typing import Any, Dict
from threading import Lock

logger = logging.getLogger(__name__)

CONFIG_FILE = os.environ.get('CONFIG_FILE', 'user_settings.json')


try:
    from settings_metadata import SETTINGS_METADATA
except Exception as e:
    logger.warning(f"settings_metadata not available: {e}")
    SETTINGS_METADATA = {}


class ConfigManager:
    """Manages settings with runtime overrides saved to JSON file."""

    # Fallback defaults — used ONLY if settings_metadata is missing
    _FALLBACK_DEFAULTS: Dict[str, Any] = {
        'COINS_LIST': "BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,LTC/USDT",
        'TIMEFRAME': '15m',
        'HTF_TIMEFRAME': '4h',
        'MAX_CANDLES': 250,
        'UPDATE_INTERVAL': 180,
        'EXCHANGE_PRIORITY': 'okx,bybit,kraken,binance',
        'USE_BTC_FILTER': 'modify',
        'BTC_BEARISH_DISCOUNT': 0.70,
        'BTC_NEUTRAL_DISCOUNT': 0.85,
        'USE_HTF_CONFIRMATION': True,
        'HTF_BONUS': 1.10,
        'HTF_PENALTY': 0.90,
        'STRONG_BUY_THRESHOLD': 5.0,
        'BUY_THRESHOLD': 2.5,
        'SELL_THRESHOLD': -2.5,
        'STRONG_SELL_THRESHOLD': -5.0,
        'RSI_OVERSOLD_STRONG': 30,
        'RSI_OVERSOLD': 40,
        'RSI_OVERBOUGHT': 60,
        'RSI_OVERBOUGHT_STRONG': 70,
        'NOTIFY_ON_BUY': True,
        'NOTIFY_ON_SELL': True,
        'MIN_NOTIFY_INTERVAL': 600,
        # NEW — must exist here so they load from env / JSON
        'ALLOW_SELLS_IN_BULL_MARKET': True,
        'SELL_BULL_BOOST': 1.10,
        'SELL_THRESHOLD_BULL': -1.8,
        'EXTERNAL_BOT_ENABLED': False,
        'EXTERNAL_BOT_URL': '',
        'EXTERNAL_BOT_SECRET': '',
        'EXTERNAL_BOT_NOTIFY_STATE_CHANGE': True,
        'STATE_CHANGE_THRESHOLD': 30.0,
        'USE_MTF_CONFIRMATION': False,
        'MTF_INFLUENCE': 0.30,
        'HTF2_TIMEFRAME': '1d',
        'USE_DYNAMIC_WEIGHTS': True,
        'DYNAMIC_WEIGHTS_MIN_SAMPLES': 20,
        'SIGNAL_TRACKER_DB': 'signals.db',
        'ATR_STOP_MULT': 1.5,
        'ATR_TP_MULT': 2.5,
        'RISK_PER_TRADE_PCT': 1.0,
        'ACCOUNT_SIZE': 1000.0,
    }

    # Build DEFAULTS from settings_metadata (preferred), fallback otherwise
    @classmethod
    def _build_defaults(cls) -> Dict[str, Any]:
        if SETTINGS_METADATA:
            return {k: v.get('default') for k, v in SETTINGS_METADATA.items()}
        return dict(cls._FALLBACK_DEFAULTS)

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
        self.DEFAULTS = self._build_defaults()
        self.TYPES = self._build_types()
        self._values: Dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------
    def _build_types(self) -> Dict[str, type]:
        """Derive types from settings_metadata."""
        out: Dict[str, type] = {}
        if not SETTINGS_METADATA:
            # Minimal fallback types
            for k, v in self._FALLBACK_DEFAULTS.items():
                if isinstance(v, bool): out[k] = bool
                elif isinstance(v, int): out[k] = int
                elif isinstance(v, float): out[k] = float
                else: out[k] = str
            return out

        for k, meta in SETTINGS_METADATA.items():
            t = meta.get('type', 'str')
            out[k] = {
                'bool': bool,
                'int': int,
                'float': float,
                'str': str,
                'select': str,
                'multi_select': str,
            }.get(t, str)
        return out

    # ------------------------------------------------------------------
    def _load(self):
        # 1. Start with defaults
        self._values = dict(self.DEFAULTS)

        # 2. Environment overrides
        env_count = 0
        for key in self.DEFAULTS:
            env_val = os.environ.get(key)
            if env_val is not None:
                self._values[key] = self._coerce(key, env_val)
                env_count += 1
        if env_count:
            logger.info(f"Loaded {env_count} settings from environment")

        # 3. JSON file overrides (highest priority)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                json_count = 0
                for key, val in saved.items():
                    if key in self.DEFAULTS:
                        self._values[key] = self._coerce(key, val)
                        json_count += 1
                logger.info(f"Loaded {json_count} runtime settings from {CONFIG_FILE}")
            except Exception as e:
                logger.error(f"Failed to load {CONFIG_FILE}: {e}")

        # 4. Verify critical external bot keys are present
        for k in ('EXTERNAL_BOT_ENABLED', 'EXTERNAL_BOT_URL',
                  'EXTERNAL_BOT_SECRET'):
            if k not in self._values:
                logger.warning(f"Missing key in config: {k}")

    # ------------------------------------------------------------------
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
            default = self.DEFAULTS.get(key)
            logger.warning(f"Invalid value for {key}: {value!r}, using default {default!r}")
            return default

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
                    json.dump(self._values, f, indent=2, default=str)
                logger.info(f"Settings saved to {CONFIG_FILE}")
            except Exception as e:
                logger.error(f"Failed to save settings: {e}")
                errors.append(f"Save failed: {e}")

            return {'values': dict(self._values), 'errors': errors}

    # ------------------------------------------------------------------
    def _validate(self) -> list:
        errors = []
        v = self._values

        # BTC filter
        if v.get('USE_BTC_FILTER') not in ('off', 'modify', 'block'):
            errors.append("USE_BTC_FILTER must be off / modify / block")
            v['USE_BTC_FILTER'] = 'modify'

        # Threshold ordering (guard with .get for safety)
        if v.get('BUY_THRESHOLD', 0) >= v.get('STRONG_BUY_THRESHOLD', 0):
            errors.append("BUY_THRESHOLD must be < STRONG_BUY_THRESHOLD")

        if v.get('SELL_THRESHOLD', 0) <= v.get('STRONG_SELL_THRESHOLD', 0):
            errors.append("SELL_THRESHOLD must be > STRONG_SELL_THRESHOLD")

        # Discounts
        if not (0 < v.get('BTC_BEARISH_DISCOUNT', 0) <= 1.0):
            errors.append("BTC_BEARISH_DISCOUNT must be between 0 and 1")
            v['BTC_BEARISH_DISCOUNT'] = 0.70

        if not (0 < v.get('BTC_NEUTRAL_DISCOUNT', 0) <= 1.0):
            errors.append("BTC_NEUTRAL_DISCOUNT must be between 0 and 1")
            v['BTC_NEUTRAL_DISCOUNT'] = 0.85

        # HTF
        if not (1.0 <= v.get('HTF_BONUS', 0) <= 2.0):
            errors.append("HTF_BONUS must be between 1.0 and 2.0")
            v['HTF_BONUS'] = 1.10

        if not (0.5 <= v.get('HTF_PENALTY', 0) <= 1.0):
            errors.append("HTF_PENALTY must be between 0.5 and 1.0")
            v['HTF_PENALTY'] = 0.90

        # RSI ordering
        if v.get('RSI_OVERSOLD_STRONG', 0) >= v.get('RSI_OVERSOLD', 0):
            errors.append("RSI_OVERSOLD_STRONG must be < RSI_OVERSOLD")

        if v.get('RSI_OVERSOLD', 0) >= v.get('RSI_OVERBOUGHT', 0):
            errors.append("RSI_OVERSOLD must be < RSI_OVERBOUGHT")

        if v.get('RSI_OVERBOUGHT', 0) >= v.get('RSI_OVERBOUGHT_STRONG', 0):
            errors.append("RSI_OVERBOUGHT must be < RSI_OVERBOUGHT_STRONG")

        # Update interval
        ui = v.get('UPDATE_INTERVAL', 0)
        if ui < 30:
            errors.append("UPDATE_INTERVAL must be at least 30 seconds")
            v['UPDATE_INTERVAL'] = 30
        elif ui > 3600:
            errors.append("UPDATE_INTERVAL must be at most 3600 seconds")
            v['UPDATE_INTERVAL'] = 3600

        # Candles
        mc = v.get('MAX_CANDLES', 0)
        if mc < 100:
            errors.append("MAX_CANDLES must be at least 100")
            v['MAX_CANDLES'] = 100
        elif mc > 1000:
            errors.append("MAX_CANDLES must be at most 1000")
            v['MAX_CANDLES'] = 1000

        # Coins list
        if not v.get('COINS_LIST') or ',' not in v.get('COINS_LIST', ''):
            errors.append("COINS_LIST must contain at least 2 symbols")
            v['COINS_LIST'] = self.DEFAULTS.get('COINS_LIST', 'BTC/USDT,ETH/USDT')

        # Exchange priority
        valid_exchanges = {'okx', 'bybit', 'kraken', 'binance', 'coinbase',
                           'kucoin', 'gateio', 'bitfinex', 'huobi', 'mexc'}
        names = [n.strip() for n in v.get('EXCHANGE_PRIORITY', '').split(',') if n.strip()]
        if not names:
            errors.append("EXCHANGE_PRIORITY cannot be empty")
            v['EXCHANGE_PRIORITY'] = self.DEFAULTS.get('EXCHANGE_PRIORITY', 'binance')
        else:
            invalid = [n for n in names if n.lower() not in valid_exchanges]
            if invalid:
                errors.append(f"Unknown exchange(s): {', '.join(invalid)}")

        # Notification interval
        if v.get('MIN_NOTIFY_INTERVAL', 0) < 30:
            errors.append("MIN_NOTIFY_INTERVAL must be at least 30 seconds")
            v['MIN_NOTIFY_INTERVAL'] = 30

        # External bot — if enabled, URL must be non-empty
        if v.get('EXTERNAL_BOT_ENABLED') and not v.get('EXTERNAL_BOT_URL', '').strip():
            errors.append("EXTERNAL_BOT_URL is required when EXTERNAL_BOT_ENABLED is true")

        return errors

    # ------------------------------------------------------------------
    def reset_to_defaults(self) -> Dict[str, Any]:
        try:
            if os.path.exists(CONFIG_FILE):
                os.remove(CONFIG_FILE)
        except Exception as e:
            logger.error(f"Failed to remove {CONFIG_FILE}: {e}")
        self._load()
        return dict(self._values)

    # ------------------------------------------------------------------
    def describe_env_sources(self) -> Dict[str, str]:
        """
        Debug helper — shows where each key's value came from.
        Useful for /api/config/sources endpoint.
        """
        sources: Dict[str, str] = {}
        # Reload defaults fresh (don't mutate)
        defaults = self._build_defaults()

        json_vals = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    json_vals = json.load(f)
            except Exception:
                pass

        for key in defaults:
            if key in json_vals:
                sources[key] = 'json'
            elif os.environ.get(key) is not None:
                sources[key] = 'env'
            else:
                sources[key] = 'default'
        return sources


# Global singleton
config = ConfigManager()
