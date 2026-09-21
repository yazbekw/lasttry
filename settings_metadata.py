"""
Metadata for the settings UI.
Each entry describes what the setting does and how to use it.
"""

SETTINGS_METADATA = {
    # ============ GENERAL ============
    'COINS_LIST': {
        'label': 'العملات المراقبة',
        'type': 'text',
        'group': 'general',
        'help': 'قائمة العملات مفصولة بفواصل، مثال: BTC/USDT,ETH/USDT,SOL/USDT',
        'default': 'BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,LTC/USDT',
    },
    'TIMEFRAME': {
        'label': 'الفريم الرئيسي',
        'type': 'select',
        'group': 'general',
        'options': ['1m', '5m', '15m', '30m', '1h', '4h', '1d'],
        'help': 'الفريم الزمني المستخدم لحساب المؤشرات. 15m = توازن جيد بين السرعة والدقة',
        'default': '15m',
    },
    'HTF_TIMEFRAME': {
        'label': 'الفريم الأعلى للتأكيد',
        'type': 'select',
        'group': 'general',
        'options': ['1h', '4h', '1d'],
        'help': 'فريم أعلى يُستخدم لتأكيد الاتجاه العام. يجب أن يكون أكبر من الفريم الرئيسي',
        'default': '4h',
    },
    'UPDATE_INTERVAL': {
        'label': 'فاصل التحديث (ثواني)',
        'type': 'number',
        'group': 'general',
        'min': 30,
        'max': 3600,
        'help': 'كم ثانية ينتظر البوت بين كل تحديث وآخر. 120 = دقيقتان (موصى به)',
        'default': 120,
    },
    'EXCHANGE_PRIORITY': {
        'label': 'ترتيب منصات البيانات',
        'type': 'text',
        'group': 'exchange',
        'help': (
            'ترتيب المنصات التي يجلب منها البوت البيانات، مفصولة بفواصل.\n'
            'okx,bybit,kraken,binance — إذا فشلت الأولى ينتقل للتي بعدها تلقائياً.\n'
            'OKX و Bybit أكثر تسامحاً مع IPs السحابية من Binance.'
        ),
        'default': 'okx,bybit,kraken,binance',
    },
    'MAX_CANDLES': {
        'label': 'عدد الشموع للتحليل',
        'type': 'number',
        'group': 'general',
        'min': 50,
        'max': 500,
        'help': 'عدد الشموع التي يجلبها البوت لكل عملة. 250 كافية لحساب EMA200',
        'default': 250,
    },

    # ============ BTC FILTER ============
    'USE_BTC_FILTER': {
        'label': 'فلتر البيتكوين',
        'type': 'select',
        'group': 'btc',
        'options': ['off', 'modify', 'block'],
        'option_labels': {
            'off': 'off — تجاهل BTC تماماً (إشارات كثيرة)',
            'modify': 'modify — تعديل النتيجة حسب BTC (موصى به)',
            'block': 'block — حجب كل الإشارات عند ضعف BTC (صارم)',
        },
        'help': (
            'off: يتجاهل حالة البيتكوين تماماً ويحسب الإشارة من العملة وحدها.\n'
            'modify: يخصم 30% عند BTC هابط و15% عند BTC عرضي، ويعزز إشارات البيع 15% عند ضعف BTC.\n'
            'block: يجبر كل الإشارات على NEUTRAL إذا لم يكن BTC صاعداً (الوضع القديم).'
        ),
        'default': 'modify',
    },
    'BTC_BEARISH_DISCOUNT': {
        'label': 'خصم BTC الهابط',
        'type': 'number',
        'group': 'btc',
        'min': 0.1,
        'max': 1.0,
        'step': 0.05,
        'help': 'معامل يُضرب في النتيجة عند BTC هابط (وضع modify). 0.70 = خصم 30%',
        'default': 0.70,
    },
    'BTC_NEUTRAL_DISCOUNT': {
        'label': 'خصم BTC العرضي',
        'type': 'number',
        'group': 'btc',
        'min': 0.1,
        'max': 1.0,
        'step': 0.05,
        'help': 'معامل عند BTC عرضي (لا صاعد ولا هابط). 0.85 = خصم 15%',
        'default': 0.85,
    },

    # ============ HTF CONFIRMATION ============
    'USE_HTF_CONFIRMATION': {
        'label': 'تأكيد الفريم الأعلى',
        'type': 'bool',
        'group': 'htf',
        'help': (
            'عند التفعيل: يقارن البوت اتجاه الفريم الرئيسي مع الفريم الأعلى.\n'
            'إذا توافقا: يرفع النتيجة 10%. إذا تعارضا: يخفضها 10%.'
        ),
        'default': True,
    },
    'HTF_BONUS': {
        'label': 'مكافأة توافق HTF',
        'type': 'number',
        'group': 'htf',
        'min': 1.0,
        'max': 1.5,
        'step': 0.01,
        'help': 'معامل رفع النتيجة عند توافق الفريمين. 1.10 = +10%',
        'default': 1.10,
    },
    'HTF_PENALTY': {
        'label': 'عقوبة تعارض HTF',
        'type': 'number',
        'group': 'htf',
        'min': 0.5,
        'max': 1.0,
        'step': 0.01,
        'help': 'معامل خفض النتيجة عند تعارض الفريمين. 0.90 = -10%',
        'default': 0.90,
    },

    # ============ THRESHOLDS ============
    'STRONG_BUY_THRESHOLD': {
        'label': 'عتبة الشراء القوي',
        'type': 'number',
        'group': 'thresholds',
        'min': 1.0,
        'max': 4.5,
        'step': 0.1,
        'help': 'الحد الأدنى للنتيجة المرجّحة لتصنيف الإشارة كـ STRONG BUY. نطاق النتيجة: -4.5 إلى +4.5',
        'default': 3.0,
    },
    'BUY_THRESHOLD': {
        'label': 'عتبة الشراء',
        'type': 'number',
        'group': 'thresholds',
        'min': 0.5,
        'max': 3.5,
        'step': 0.1,
        'help': 'الحد الأدنى لإشارة BUY عادية. يجب أن يكون أقل من عتبة الشراء القوي',
        'default': 1.5,
    },
    'SELL_THRESHOLD': {
        'label': 'عتبة البيع',
        'type': 'number',
        'group': 'thresholds',
        'min': -3.5,
        'max': -0.5,
        'step': 0.1,
        'help': 'الحد الأقصى (سلبي) لإشارة SELL. عند نزول النتيجة تحت هذه القيمة → بيع',
        'default': -1.5,
    },
    'STRONG_SELL_THRESHOLD': {
        'label': 'عتبة البيع القوي',
        'type': 'number',
        'group': 'thresholds',
        'min': -4.5,
        'max': -1.0,
        'step': 0.1,
        'help': 'الحد الأقصى لإشارة STRONG SELL. يجب أن يكون أقل من عتبة البيع',
        'default': -3.0,
    },

    # ============ RSI ============
    'RSI_OVERSOLD_STRONG': {
        'label': 'RSI تشبع بيعي قوي',
        'type': 'number',
        'group': 'rsi',
        'min': 10,
        'max': 45,
        'help': 'تحت هذه القيمة يعطي البوت أقصى نقاط للزخم (+1.0). عادة 30',
        'default': 30,
    },
    'RSI_OVERSOLD': {
        'label': 'RSI تشبع بيعي',
        'type': 'number',
        'group': 'rsi',
        'min': 20,
        'max': 55,
        'help': 'تحت هذه القيمة → +0.75 نقطة زخم. ارفعه إلى 45 لإشارات أكثر',
        'default': 40,
    },
    'RSI_OVERBOUGHT': {
        'label': 'RSI تشبع شرائي',
        'type': 'number',
        'group': 'rsi',
        'min': 45,
        'max': 80,
        'help': 'فوق هذه القيمة → -0.75 نقطة (بداية ضغط بيعي)',
        'default': 60,
    },
    'RSI_OVERBOUGHT_STRONG': {
        'label': 'RSI تشبع شرائي قوي',
        'type': 'number',
        'group': 'rsi',
        'min': 55,
        'max': 90,
        'help': 'فوق هذه القيمة → -1.0 نقطة (إشارة بيع قوية). عادة 70',
        'default': 70,
    },

    # ============ NOTIFICATIONS ============
    'NOTIFY_ON_BUY': {
        'label': 'إشعارات الشراء',
        'type': 'bool',
        'group': 'notify',
        'help': 'إرسال إشعار عبر ntfy عند ظهور إشارة BUY أو STRONG BUY',
        'default': True,
    },
    'NOTIFY_ON_SELL': {
        'label': 'إشعارات البيع',
        'type': 'bool',
        'group': 'notify',
        'help': 'إرسال إشعار عبر ntfy عند ظهور إشارة SELL أو STRONG SELL',
        'default': True,
    },
    'MIN_NOTIFY_INTERVAL': {
        'label': 'أدنى فاصل بين الإشعارات (ثواني)',
        'type': 'number',
        'group': 'notify',
        'min': 60,
        'max': 7200,
        'help': 'لمنع الإزعاج: لا يُرسل إشعار جديد لنفس العملة قبل مرور هذه المدة. 600 = 10 دقائق',
        'default': 600,
    },
}

SETTINGS_GROUPS = {
    'general':    {'label': 'الإعدادات العامة',       'icon': '⚙️', 'order': 1},
    'btc':        {'label': 'فلتر البيتكوين',          'icon': '₿',  'order': 2},
    'htf':        {'label': 'تأكيد الفريم الأعلى',     'icon': '📊', 'order': 3},
    'thresholds': {'label': 'عتبات الإشارات',          'icon': '🎯', 'order': 4},
    'rsi':        {'label': 'مؤشر RSI',                'icon': '📈', 'order': 5},
    'notify':     {'label': 'الإشعارات',               'icon': '🔔', 'order': 6},
}
