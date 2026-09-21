"""
Signal Tracker - Dual backend (PostgreSQL / SQLite)
- Lazy initialization: no DB connection until first real use
- Safe init: failures don't crash module import
- Works with Supabase Connection Pooler
"""

import os
import json
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from threading import Lock, RLock
from contextlib import contextmanager

logger = logging.getLogger(__name__)


# ======================================================================
# Detect backend
# ======================================================================
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_POSTGRES = DATABASE_URL.startswith(('postgres://', 'postgresql://'))
_psycopg2_available = False

if USE_POSTGRES:
    try:
        import psycopg2
        import psycopg2.extras
        _psycopg2_available = True
        logger.info("SignalTracker: PostgreSQL backend selected (Supabase)")
    except ImportError as e:
        logger.error(
            f"psycopg2 not installed but DATABASE_URL is set: {e}. "
            "Add psycopg2-binary to requirements.txt. Falling back to SQLite."
        )
        USE_POSTGRES = False
else:
    logger.info("SignalTracker: SQLite backend selected (no DATABASE_URL)")


# ======================================================================
# Connection helpers
# ======================================================================
def _sqlite_path() -> str:
    env = os.environ.get('SIGNAL_TRACKER_DB')
    if env:
        return env
    if os.path.isdir('/data'):
        return '/data/signals.db'
    return 'signals.db'


class _PgConnection:
    """Wrap psycopg2 to behave like sqlite3.Connection for our usage."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: Tuple = ()):
        sql = sql.replace('?', '%s')
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def executescript(self, script: str):
        cur = self._conn.cursor()
        for stmt in script.split(';'):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self._conn.commit()
        finally:
            self._conn.close()


@contextmanager
def _get_conn():
    if USE_POSTGRES:
        # Short connect timeout so worker boot doesn't hang
        raw = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        try:
            yield _PgConnection(raw)
        finally:
            try:
                raw.close()
            except Exception:
                pass
    else:
        conn = sqlite3.connect(_sqlite_path(), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# ======================================================================
# DDL
# ======================================================================
def _ddl_signals() -> str:
    if USE_POSTGRES:
        return """
            CREATE TABLE IF NOT EXISTS signals (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                score DOUBLE PRECISION NOT NULL,
                percentage DOUBLE PRECISION NOT NULL,
                price DOUBLE PRECISION NOT NULL,
                created_at TEXT NOT NULL,
                raw_scores TEXT,
                mtf_score DOUBLE PRECISION DEFAULT 0,
                price_1h DOUBLE PRECISION,
                price_4h DOUBLE PRECISION,
                price_24h DOUBLE PRECISION,
                pct_1h DOUBLE PRECISION,
                pct_4h DOUBLE PRECISION,
                pct_24h DOUBLE PRECISION,
                outcome TEXT,
                evaluated_at TEXT
            )
        """
    return """
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
        )
    """


def _ddl_indicator_accuracy() -> str:
    return """
        CREATE TABLE IF NOT EXISTS indicator_accuracy (
            indicator TEXT PRIMARY KEY,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """


def _indexes() -> List[str]:
    return [
        "CREATE INDEX IF NOT EXISTS idx_signals_sym_created ON signals(symbol, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_signals_evaluated ON signals(evaluated_at)",
    ]


def _upsert_indicator_sql() -> str:
    if USE_POSTGRES:
        return """
            INSERT INTO indicator_accuracy (indicator, wins, losses, total, updated_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT (indicator) DO UPDATE SET
                wins = indicator_accuracy.wins + EXCLUDED.wins,
                losses = indicator_accuracy.losses + EXCLUDED.losses,
                total = indicator_accuracy.total + 1,
                updated_at = EXCLUDED.updated_at
        """
    return """
        INSERT INTO indicator_accuracy (indicator, wins, losses, total, updated_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(indicator) DO UPDATE SET
            wins = wins + excluded.wins,
            losses = losses + excluded.losses,
            total = total + 1,
            updated_at = excluded.updated_at
    """


# ======================================================================
# SignalTracker — Lazy + Safe
# ======================================================================
class SignalTracker:
    """Records signals and tracks outcome at 1h/4h/24h."""

    HORIZONS = [('1h', 3600), ('4h', 4 * 3600), ('24h', 24 * 3600)]

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _sqlite_path()
        self._lock = RLock()
        self._initialized = False
        self._init_failed = False
        # NOTE: intentionally NOT calling _init_db() here.
        # gunicorn worker boot must stay fast.

    # ------------------------------------------------------------------
    def _ensure_init(self) -> bool:
        """Initialize DB schema once, lazily. Returns True if usable."""
        if self._initialized:
            return True
        if self._init_failed:
            return False
        with self._lock:
            if self._initialized:
                return True
            if self._init_failed:
                return False
            try:
                self._init_db()
                self._initialized = True
                backend = "PostgreSQL" if USE_POSTGRES else f"SQLite ({self.db_path})"
                logger.info(f"SignalTracker ready on {backend}")
                return True
            except Exception as e:
                logger.error(f"SignalTracker init failed: {e}")
                self._init_failed = True
                return False

    def _init_db(self):
        with _get_conn() as conn:
            conn.executescript(_ddl_signals())
            conn.executescript(_ddl_indicator_accuracy())
            for idx in _indexes():
                try:
                    conn.execute(idx)
                except Exception as e:
                    logger.debug(f"Index creation skipped: {e}")

    # ------------------------------------------------------------------
    def record(self, signal) -> None:
        if not self._ensure_init():
            return
        try:
            # Avoid circular import for SignalType
            try:
                from app2 import SignalType  # noqa
            except Exception:
                try:
                    from app1 import SignalType  # noqa
                except Exception:
                    SignalType = None

            if SignalType is not None and signal.signal_type == SignalType.NEUTRAL:
                return
        except Exception:
            pass

        try:
            raw_scores = {k: v.raw_score for k, v in signal.indicator_scores.items()}
            with self._lock, _get_conn() as conn:
                conn.execute("""
                    INSERT INTO signals
                        (symbol, signal_type, score, percentage, price, created_at,
                         raw_scores, mtf_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    signal.symbol,
                    signal.signal_type.name,
                    signal.total_score,
                    signal.total_percentage,
                    signal.current_price,
                    signal.last_updated.isoformat(),
                    json.dumps(raw_scores),
                    getattr(signal, 'mtf_score', 0.0),
                ))
        except Exception as e:
            logger.error(f"SignalTracker.record error: {e}")

    # ------------------------------------------------------------------
    def evaluate_pending(self, current_prices: Dict[str, float]) -> int:
        if not self._ensure_init():
            return 0
        now = datetime.now()
        updated_count = 0
        try:
            with self._lock, _get_conn() as conn:
                rows = conn.execute("""
                    SELECT id, symbol, price, created_at, raw_scores,
                           price_1h, price_4h, price_24h,
                           signal_type, pct_1h, pct_4h, pct_24h
                    FROM signals
                    WHERE evaluated_at IS NULL OR price_24h IS NULL
                """).fetchall()

                for row in rows:
                    r = dict(row) if not isinstance(row, dict) else row
                    created = datetime.fromisoformat(r['created_at'])
                    age = (now - created).total_seconds()
                    price_now = current_prices.get(r['symbol'])
                    if not price_now or not r['price']:
                        continue

                    updates: Dict[str, Any] = {}
                    for label, secs in self.HORIZONS:
                        col_price = f'price_{label}'
                        col_pct = f'pct_{label}'
                        if r.get(col_price) is None and age >= secs:
                            updates[col_price] = price_now
                            updates[col_pct] = (price_now - r['price']) / r['price'] * 100.0

                    if updates:
                        set_clause = ", ".join(f"{k}=?" for k in updates)
                        conn.execute(
                            f"UPDATE signals SET {set_clause} WHERE id=?",
                            list(updates.values()) + [r['id']]
                        )
                        updated_count += 1

                    if updates.get('price_24h') is not None:
                        pct_24h = updates['pct_24h']
                        st = r['signal_type']
                        if st in ('BUY', 'STRONG_BUY'):
                            sig_correct = pct_24h > 0.5
                        else:
                            sig_correct = pct_24h < -0.5

                        conn.execute(
                            "UPDATE signals SET outcome=?, evaluated_at=? WHERE id=?",
                            ('win' if sig_correct else 'loss',
                             now.isoformat(), r['id'])
                        )

                        if r.get('raw_scores'):
                            self._update_indicator_accuracy(conn, r['raw_scores'], pct_24h)

        except Exception as e:
            logger.error(f"SignalTracker.evaluate_pending error: {e}")
        return updated_count

    def _update_indicator_accuracy(self, conn, raw_scores_json: str, pct_24h: float):
        try:
            raw = json.loads(raw_scores_json)
        except Exception:
            return
        actual_dir = 1 if pct_24h > 0 else -1
        now_iso = datetime.now().isoformat()
        sql = _upsert_indicator_sql()
        for ind, score in raw.items():
            if not score:
                continue
            ind_dir = 1 if score > 0 else -1
            correct = (ind_dir == actual_dir)
            try:
                conn.execute(sql, (ind, 1 if correct else 0, 0 if correct else 1, now_iso))
            except Exception as e:
                logger.debug(f"Indicator upsert failed for {ind}: {e}")

    # ------------------------------------------------------------------
    def get_indicator_stats(self, indicator: str) -> Dict[str, int]:
        empty = {'wins': 0, 'losses': 0, 'total': 0}
        if not self._ensure_init():
            return empty
        try:
            with _get_conn() as conn:
                row = conn.execute(
                    "SELECT wins, losses, total FROM indicator_accuracy WHERE indicator=?",
                    (indicator,)
                ).fetchone()
                if row:
                    r = dict(row) if not isinstance(row, dict) else row
                    return {
                        'wins': int(r['wins'] or 0),
                        'losses': int(r['losses'] or 0),
                        'total': int(r['total'] or 0),
                    }
        except Exception as e:
            logger.error(f"get_indicator_stats error: {e}")
        return empty

    def get_all_stats(self) -> List[Dict]:
        if not self._ensure_init():
            return []
        try:
            with _get_conn() as conn:
                rows = conn.execute("""
                    SELECT indicator, wins, losses, total FROM indicator_accuracy
                """).fetchall()
                out = []
                for row in rows:
                    r = dict(row) if not isinstance(row, dict) else row
                    total = int(r['total'] or 0)
                    wins = int(r['wins'] or 0)
                    out.append({
                        'indicator': r['indicator'],
                        'wins': wins,
                        'losses': int(r['losses'] or 0),
                        'total': total,
                        'win_rate': (wins / total * 100.0) if total else 0.0,
                    })
                return out
        except Exception as e:
            logger.error(f"get_all_stats error: {e}")
            return []

    def get_signal_stats(self, days: int = 30) -> Dict:
        if not self._ensure_init():
            return {}
        since = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            with _get_conn() as conn:
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

                r = dict(row) if not isinstance(row, dict) else row
                total = int(r['total'] or 0)
                wins = int(r['wins'] or 0)
                losses = int(r['losses'] or 0)
                decided = wins + losses

                return {
                    'total_signals': total,
                    'wins': wins,
                    'losses': losses,
                    'win_rate': (wins / decided * 100.0) if decided else 0.0,
                    'avg_pct_24h': float(r['avg_pct'] or 0.0),
                    'avg_outcome_pct': float(r['avg_outcome'] or 0.0),
                    'period_days': days,
                }
        except Exception as e:
            logger.error(f"get_signal_stats error: {e}")
            return {}

    def get_recent_signals(self, limit: int = 50) -> List[Dict]:
        if not self._ensure_init():
            return []
        try:
            with _get_conn() as conn:
                rows = conn.execute("""
                    SELECT symbol, signal_type, score, percentage, price,
                           created_at, pct_1h, pct_4h, pct_24h, outcome
                    FROM signals
                    ORDER BY id DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                return [dict(r) if not isinstance(r, dict) else r for r in rows]
        except Exception as e:
            logger.error(f"get_recent_signals error: {e}")
            return []

    # ------------------------------------------------------------------
    def health_check(self) -> Dict:
        info = {
            'backend': 'postgresql' if USE_POSTGRES else 'sqlite',
            'connected': False,
            'initialized': self._initialized,
            'init_failed': self._init_failed,
        }
        # Do NOT force init here — just report
        if not self._initialized and not self._init_failed:
            info['note'] = 'lazy — not yet initialized'
            return info
        if self._init_failed:
            return info
        try:
            with _get_conn() as conn:
                conn.execute("SELECT 1").fetchone()
                info['connected'] = True
        except Exception as e:
            info['error'] = str(e)
        return info


# ======================================================================
# Singleton — lazy
# ======================================================================
signal_tracker = SignalTracker()
