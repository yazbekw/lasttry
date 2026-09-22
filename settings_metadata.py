"""
Settings Metadata for Crypto Signal Analyzer
Version 8.0.0 - Supports MTF, Dynamic Weights, Risk Management, Signal Tracking
"""

from typing import Dict, List


# ======================================================================
# SETTINGS_METADATA
# Each entry: key -> {
#   'type': 'int'|'float'|'str'|'bool'|'select'|'multi_select',
#   'label': human readable,
#   'description': long help,
#   'min'/'max'/'step': for numeric,
#   'options': for select,
#   'default': default value
# }
# ======================================================================
SETTINGS_METADATA: Dict[str, Dict] = {

        # ------------------------------------------------------------------
    # Sell behavior
    # ------------------------------------------------------------------
    'ALLOW_SELLS_IN_BULL_MARKET': {
        'type': 'bool',
        'label': 'Allow Sell Signals in Bull Market',
        'description': 'Boost negative scores slightly when BTC is bullish (correction opportunities).',
        'default': True,
    },
    'SELL_BULL_BOOST': {
        'type': 'float',
        'label': 'Sell Boost in Bull Market',
        'description': 'Multiplier applied to negative scores when BTC is bullish (1.10 = +10%).',
        'min': 1.0, 'max': 1.5, 'step': 0.05,
        'default': 1.10,
    },
    'SELL_THRESHOLD_BULL': {
        'type': 'float',
        'label': 'Sell Threshold (Bull Market)',
        'description': 'Lower (easier) threshold for SELL when BTC is bullish.',
        'min': -7.0, 'max': 0.0, 'step': 0.1,
        'default': -1.8,
    },

    # ------------------------------------------------------------------
    # External bot webhook
    # ------------------------------------------------------------------
    'EXTERNAL_BOT_ENABLED': {
        'type': 'bool',
        'label': 'Enable External Bot Webhook',
        'description': 'Send entry signals to an external bot via HTTP POST.',
        'default': False,
    },
    'EXTERNAL_BOT_URL': {
        'type': 'str',
        'label': 'External Bot Webhook URL',
        'description': 'Full URL to POST entry signals to (e.g. https://other-bot.com/webhook).',
        'default': '',
    },
    'EXTERNAL_BOT_SECRET': {
        'type': 'str',
        'label': 'External Bot Shared Secret',
        'description': 'Sent as X-Signature header for authentication.',
        'default': '',
    },
    'EXTERNAL_BOT_NOTIFY_STATE_CHANGE': {
        'type': 'bool',
        'label': 'Notify External Bot on State Change',
        'description': 'Send a "close position" event when a signal no longer holds.',
        'default': True,
    },
    'STATE_CHANGE_THRESHOLD': {
        'type': 'float',
        'label': 'State Change Threshold (%)',
        'description': 'Signal percentage must drop by this much from entry to trigger close.',
        'min': 5.0, 'max': 90.0, 'step': 5.0,
        'default': 30.0,
    },

    # ------------------------------------------------------------------
    # Market & timeframes
    # ------------------------------------------------------------------
    'TIMEFRAME': {
        'type': 'select',
        'label': 'Primary Timeframe',
        'description': 'Candle interval used for the main 10 indicators.',
        'options': ['1m', '5m', '15m', '30m', '1h', '4h', '1d'],
        'default': '1h',
    },
    'HTF_TIMEFRAME': {
        'type': 'select',
        'label': 'Higher Timeframe (HTF)',
        'description': 'Secondary timeframe for trend confirmation.',
        'options': ['15m', '30m', '1h', '4h', '1d', '1w'],
        'default': '4h',
    },
    'HTF2_TIMEFRAME': {
        'type': 'select',
        'label': 'Second Higher Timeframe (HTF2)',
        'description': 'Third timeframe used when Multi-Timeframe confirmation is enabled.',
        'options': ['1h', '4h', '1d', '1w'],
        'default': '1d',
    },
    'MAX_CANDLES': {
        'type': 'int',
        'label': 'Max Candles',
        'description': 'Maximum number of candles fetched per request.',
        'min': 100, 'max': 1000, 'step': 50,
        'default': 250,
    },
    'UPDATE_INTERVAL': {
        'type': 'int',
        'label': 'Update Interval (seconds)',
        'description': 'How often the bot refreshes market data. Minimum 30s.',
        'min': 30, 'max': 3600, 'step': 10,
        'default': 60,
    },

    # ------------------------------------------------------------------
    # Coins & exchanges
    # ------------------------------------------------------------------
    'COINS_LIST': {
        'type': 'str',
        'label': 'Coins to Track',
        'description': 'Comma-separated symbols. Example: BTC/USDT,ETH/USDT,SOL/USDT',
        'default': 'BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT',
    },
    'EXCHANGE_PRIORITY': {
        'type': 'str',
        'label': 'Exchange Priority',
        'description': 'Comma-separated exchange names; first working one is used.',
        'default': 'binance,kucoin,coinbase,bybit,okx',
    },

    # ------------------------------------------------------------------
    # RSI thresholds
    # ------------------------------------------------------------------
    'RSI_OVERSOLD_STRONG': {
        'type': 'int',
        'label': 'RSI Oversold (Strong)',
        'description': 'RSI below this gives full bullish momentum score.',
        'min': 10, 'max': 40, 'step': 1,
        'default': 25,
    },
    'RSI_OVERSOLD': {
        'type': 'int',
        'label': 'RSI Oversold',
        'description': 'RSI below this gives partial bullish momentum score.',
        'min': 20, 'max': 45, 'step': 1,
        'default': 35,
    },
    'RSI_OVERBOUGHT': {
        'type': 'int',
        'label': 'RSI Overbought',
        'description': 'RSI above this gives partial bearish momentum score.',
        'min': 55, 'max': 80, 'step': 1,
        'default': 65,
    },
    'RSI_OVERBOUGHT_STRONG': {
        'type': 'int',
        'label': 'RSI Overbought (Strong)',
        'description': 'RSI above this gives full bearish momentum score.',
        'min': 60, 'max': 90, 'step': 1,
        'default': 75,
    },

    # ------------------------------------------------------------------
    # Signal thresholds
    # ------------------------------------------------------------------
    'STRONG_BUY_THRESHOLD': {
        'type': 'float',
        'label': 'Strong Buy Threshold',
        'description': 'Total weighted score at/above which the signal is STRONG BUY.',
        'min': 0.5, 'max': 7.8, 'step': 0.1,
        'default': 4.5,
    },
    'BUY_THRESHOLD': {
        'type': 'float',
        'label': 'Buy Threshold',
        'description': 'Total weighted score at/above which the signal is BUY.',
        'min': 0.1, 'max': 7.0, 'step': 0.1,
        'default': 2.5,
    },
    'SELL_THRESHOLD': {
        'type': 'float',
        'label': 'Sell Threshold',
        'description': 'Total weighted score at/below which the signal is SELL.',
        'min': -7.0, 'max': -0.1, 'step': 0.1,
        'default': -2.5,
    },
    'STRONG_SELL_THRESHOLD': {
        'type': 'float',
        'label': 'Strong Sell Threshold',
        'description': 'Total weighted score at/below which the signal is STRONG SELL.',
        'min': -7.8, 'max': -0.5, 'step': 0.1,
        'default': -4.5,
    },

    # ------------------------------------------------------------------
    # BTC filter & HTF confirmation
    # ------------------------------------------------------------------
    'USE_BTC_FILTER': {
        'type': 'select',
        'label': 'BTC Filter Mode',
        'description': 'off: no filter. modify: discount positive scores when BTC is bearish. block: force NEUTRAL when BTC is bearish.',
        'options': ['off', 'modify', 'block'],
        'default': 'modify',
    },
    'BTC_BEARISH_DISCOUNT': {
        'type': 'float',
        'label': 'BTC Bearish Discount',
        'description': 'Multiplier applied to positive scores when BTC is bearish.',
        'min': 0.1, 'max': 1.0, 'step': 0.05,
        'default': 0.6,
    },
    'BTC_NEUTRAL_DISCOUNT': {
        'type': 'float',
        'label': 'BTC Neutral Discount',
        'description': 'Multiplier applied to positive scores when BTC is neither bullish nor bearish.',
        'min': 0.1, 'max': 1.0, 'step': 0.05,
        'default': 0.8,
    },
    'USE_HTF_CONFIRMATION': {
        'type': 'bool',
        'label': 'Use HTF Confirmation',
        'description': 'Boost score when primary and higher timeframe trends agree; penalize disagreement.',
        'default': True,
    },
    'HTF_BONUS': {
        'type': 'float',
        'label': 'HTF Agreement Bonus',
        'description': 'Multiplier when HTF agrees with the signal direction.',
        'min': 1.0, 'max': 1.5, 'step': 0.05,
        'default': 1.15,
    },
    'HTF_PENALTY': {
        'type': 'float',
        'label': 'HTF Disagreement Penalty',
        'description': 'Multiplier when HTF disagrees with the signal direction.',
        'min': 0.5, 'max': 1.0, 'step': 0.05,
        'default': 0.85,
    },

    # ------------------------------------------------------------------
    # NEW: Multi-Timeframe confirmation
    # ------------------------------------------------------------------
    'USE_MTF_CONFIRMATION': {
        'type': 'bool',
        'label': 'Use Multi-Timeframe Confirmation',
        'description': 'Combine primary + HTF + HTF2 trends into a single MTF score that adjusts the final signal. When OFF, only HTF confirmation is used.',
        'default': False,
    },
    'MTF_INFLUENCE': {
        'type': 'float',
        'label': 'MTF Influence',
        'description': 'How strongly the MTF score affects the total. 0.3 means +/-30% adjustment max.',
        'min': 0.0, 'max': 1.0, 'step': 0.05,
        'default': 0.30,
    },

    # ------------------------------------------------------------------
    # NEW: Dynamic weights
    # ------------------------------------------------------------------
    'USE_DYNAMIC_WEIGHTS': {
        'type': 'bool',
        'label': 'Use Dynamic Indicator Weights',
        'description': 'Adjust each indicator weight based on its historical accuracy. Requires enough recorded signals. Does NOT modify your configured thresholds.',
        'default': True,
    },
    'DYNAMIC_WEIGHTS_MIN_SAMPLES': {
        'type': 'int',
        'label': 'Dynamic Weights - Minimum Samples',
        'description': 'Minimum number of evaluated signals per indicator before its weight is adjusted.',
        'min': 5, 'max': 200, 'step': 5,
        'default': 20,
    },

    # ------------------------------------------------------------------
    # NEW: Signal accuracy tracking
    # ------------------------------------------------------------------
    'SIGNAL_TRACKER_DB': {
        'type': 'str',
        'label': 'Signal Tracker DB Path',
        'description': 'SQLite file that stores every signal and its 1h/4h/24h outcome.',
        'default': 'signals.db',
    },

    # ------------------------------------------------------------------
    # NEW: Risk management
    # ------------------------------------------------------------------
    'ATR_STOP_MULT': {
        'type': 'float',
        'label': 'Stop Loss ATR Multiplier',
        'description': 'Stop Loss distance = ATR x this value.',
        'min': 0.5, 'max': 5.0, 'step': 0.1,
        'default': 1.5,
    },
    'ATR_TP_MULT': {
        'type': 'float',
        'label': 'Take Profit ATR Multiplier',
        'description': 'Take Profit distance = ATR x this value.',
        'min': 0.5, 'max': 10.0, 'step': 0.1,
        'default': 2.5,
    },
    'RISK_PER_TRADE_PCT': {
        'type': 'float',
        'label': 'Risk per Trade (%)',
        'description': 'Percentage of account to risk on each signal.',
        'min': 0.1, 'max': 10.0, 'step': 0.1,
        'default': 1.0,
    },
    'ACCOUNT_SIZE': {
        'type': 'float',
        'label': 'Account Size (USD)',
        'description': 'Used only to compute suggested position size in notifications.',
        'min': 10.0, 'max': 1_000_000.0, 'step': 100.0,
        'default': 1000.0,
    },

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    'NOTIFY_ON_BUY': {
        'type': 'bool',
        'label': 'Notify on Buy Signals',
        'description': 'Send NTFY notifications for BUY and STRONG BUY.',
        'default': True,
    },
    'NOTIFY_ON_SELL': {
        'type': 'bool',
        'label': 'Notify on Sell Signals',
        'description': 'Send NTFY notifications for SELL and STRONG SELL.',
        'default': True,
    },
    'MIN_NOTIFY_INTERVAL': {
        'type': 'int',
        'label': 'Minimum Notify Interval (seconds)',
        'description': 'Minimum time between two notifications for the same coin.',
        'min': 60, 'max': 86400, 'step': 60,
        'default': 900,
    },
}


# ======================================================================
# SETTINGS_GROUPS
# Used by the settings page to organize fields into sections
# ======================================================================
SETTINGS_GROUPS: List[Dict] = [

    {
        'name': 'Sell Behavior',
        'icon': 'arrow-down',
        'settings': [
            'ALLOW_SELLS_IN_BULL_MARKET', 'SELL_BULL_BOOST',
            'SELL_THRESHOLD_BULL',
        ],
    },
    {
        'name': 'External Bot',
        'icon': 'share',
        'settings': [
            'EXTERNAL_BOT_ENABLED', 'EXTERNAL_BOT_URL',
            'EXTERNAL_BOT_SECRET', 'EXTERNAL_BOT_NOTIFY_STATE_CHANGE',
            'STATE_CHANGE_THRESHOLD',
        ],
    },
    {
        'name': 'Market',
        'icon': 'chart-line',
        'settings': [
            'TIMEFRAME', 'HTF_TIMEFRAME', 'HTF2_TIMEFRAME',
            'MAX_CANDLES', 'UPDATE_INTERVAL',
        ],
    },
    {
        'name': 'Coins & Exchanges',
        'icon': 'coins',
        'settings': ['COINS_LIST', 'EXCHANGE_PRIORITY'],
    },
    {
        'name': 'RSI Thresholds',
        'icon': 'sliders',
        'settings': [
            'RSI_OVERSOLD_STRONG', 'RSI_OVERSOLD',
            'RSI_OVERBOUGHT', 'RSI_OVERBOUGHT_STRONG',
        ],
    },
    {
        'name': 'Signal Thresholds',
        'icon': 'signal',
        'settings': [
            'STRONG_BUY_THRESHOLD', 'BUY_THRESHOLD',
            'SELL_THRESHOLD', 'STRONG_SELL_THRESHOLD',
        ],
    },
    {
        'name': 'Confirmation Filters',
        'icon': 'filter',
        'settings': [
            'USE_BTC_FILTER', 'BTC_BEARISH_DISCOUNT', 'BTC_NEUTRAL_DISCOUNT',
            'USE_HTF_CONFIRMATION', 'HTF_BONUS', 'HTF_PENALTY',
            'USE_MTF_CONFIRMATION', 'MTF_INFLUENCE',
        ],
    },
    {
        'name': 'Dynamic Weights',
        'icon': 'balance-scale',
        'settings': [
            'USE_DYNAMIC_WEIGHTS', 'DYNAMIC_WEIGHTS_MIN_SAMPLES',
        ],
    },
    {
        'name': 'Signal Tracking',
        'icon': 'database',
        'settings': ['SIGNAL_TRACKER_DB'],
    },
    {
        'name': 'Risk Management',
        'icon': 'shield-alt',
        'settings': [
            'ATR_STOP_MULT', 'ATR_TP_MULT',
            'RISK_PER_TRADE_PCT', 'ACCOUNT_SIZE',
        ],
    },
    {
        'name': 'Notifications',
        'icon': 'bell',
        'settings': [
            'NOTIFY_ON_BUY', 'NOTIFY_ON_SELL', 'MIN_NOTIFY_INTERVAL',
        ],
    },
]


# ======================================================================
# Convenience helpers
# ======================================================================
def get_defaults() -> Dict[str, any]:
    """Return a dict of all default values."""
    return {k: v.get('default') for k, v in SETTINGS_METADATA.items()}


def get_metadata(key: str) -> Dict:
    """Return metadata for a single setting."""
    return SETTINGS_METADATA.get(key, {})


def group_of(key: str) -> str:
    """Return the group name a setting belongs to."""
    for g in SETTINGS_GROUPS:
        if key in g['settings']:
            return g['name']
    return 'Other'
