"""
Signal Tracker - Dual backend (PostgreSQL / SQLite)
Automatically uses DATABASE_URL if set, otherwise falls back to SQLite.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from threading import Lock
from contextlib import contextmanager

logger = logging.getLogger(__name__)


# ======================================================================
# Detect backend
# ======================================================================
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_POSTGRES = DATABASE_URL.startswith(('postgres://', 'postgresql://'))

if USE_POSTGRES:
    try:
        import psycopg2
        import psycopg2.extras
        logger.info("SignalTracker: using PostgreSQL (Supabase)")
    except ImportError:
        logger.error(
            "psycopg2 not installed but DATABASE_URL is set. "
            "Falling back to SQLite. Add psycopg2-binary to requirements.txt."
        )
        USE_POSTGRES = False
else:
    logger.info("SignalTracker: using SQLite (no DATABASE_URL set)")


# ======================================================================
# Connection helpers
# ======================================================================
def _sqlite_path() -> str:
    """SQLite fallback path."""
    env = os.environ.get('SIGNAL_TRACKER_DB')
    if env:
        return env
    # If Render with persistent disk mounted
    if os.path.isdir('/data'):
        return '/data/signals.db'
    return 'signals.db'


class _PgConnection:
    """Thin wrapper to make psycopg2 behave like sqlite3 Connection
    for the patterns used in SignalTracker (execute + fetchone + fetchall)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: Tuple = ()):
        # Convert SQLite '?' placeholders to PostgreSQL '%s'
        sql = sql.replace('?', '%s')
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def executescript(self, script: str):
        """Split by ';' and execute each statement (SQLite compatibility)."""
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
        self._conn.commit()
        self._conn.close()


@contextmanager
def _get_conn():
    """Yields a connection wrapper (PostgreSQL or SQLite)."""
    if USE_POSTGRES:
        raw = psycopg2.connect(DATABASE_URL)
        yield _PgConnection(raw)
    else:
        conn = sqlite3.connect(_sqlite_path(), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# ======================================================================
# SQL dialects
# ======================================================================
def _ddl_signals() -> str:
    """Create signals table — works on both backends."""
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
    else:
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
    if USE_POSTGRES:
        return """
            CREATE TABLE IF NOT EXISTS indicator_accuracy (
                indicator TEXT PRIMARY KEY,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                updated_at TEXT
            )
        """
    else:
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


def _upsert_indicator() -> str:
    """Upsert clause differs between SQLite and PostgreSQL."""
    if USE_POSTGRES:
        return """
            INSERT INTO indicator_accuracy (indicator, wins, losses, total, updated_at)
            VALUES (%s, %s, %s, 1, %s)
            ON CONFLICT (indicator) DO UPDATE SET
                wins = indicator_accuracy.wins + EXCLUDED.wins,
                losses = indicator_accuracy.losses + EXCLUDED.losses,
                total = indicator_accuracy.total + 1,
                updated_at = EXCLUDED.updated_at
        """
    else:
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
# SignalTracker — dual backend
# ======================================================================
class SignalTracker:
    """
    Records every actionable signal and evaluates outcome at 1h/4h/24h.
    Tracks per-indicator accuracy for dynamic weights.
    Works with PostgreSQL (Supabase) or SQLite automatically.
    """

    HORIZONS = [('1h', 3600), ('4h', 4 * 3600), ('24h', 24 * 3600)]

    def __init__(self, db_path: Optional[str] = None):
        # db_path kept for backwards compatibility; ignored if PostgreSQL is active
        self.db_path = db_path or _sqlite_path()
        self._lock = Lock()
        self._init_db()

    # ------------------------------------------------------------------
    def _init_db(self):
        try:
            with _get_conn() as conn:
                conn.executescript(_ddl_signals())
                conn.executescript(_ddl_indicator_accuracy())
                for idx in _indexes():
                    try:
                        conn.execute(idx)
                    except Exception as e:
                        logger.debug(f"Index creation skipped: {e}")
            backend = "PostgreSQL" if USE_POSTGRES else f"SQLite ({self.db_path})"
            logger.info(f"SignalTracker ready on {backend}")
        except Exception as e:
            logger.error(f"SignalTracker init failed: {e}")
            raise

    # ------------------------------------------------------------------
    def record(self, signal) -> None:
        """Persist an actionable (non-neutral) signal."""
        try:
            # Local import to avoid circular dep
            from app1 import SignalType
            if signal.signal_type == SignalType.NEUTRAL:
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
                    signal.mtf_score,
                ))
        except Exception as e:
            logger.error(f"SignalTracker.record error: {e}")

    # ------------------------------------------------------------------
    def evaluate_pending(self, current_prices: Dict[str, float]) -> int:
        """Evaluate signals that reached their horizon. Returns count updated."""
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
                    # Support both sqlite3.Row and dict
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

                    # Mark as evaluated when 24h is filled
                    if updates.get('price_24h') is not None:
                        pct_24h = updates['pct_24h']
                        sig_type = r['signal_type']
                        if sig_type in ('BUY', 'STRONG_BUY'):
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

    # ------------------------------------------------------------------
    def _update_indicator_accuracy(self, conn, raw_scores_json: str, pct_24h: float):
        try:
            raw = json.loads(raw_scores_json)
        except Exception:
            return

        actual_dir = 1 if pct_24h > 0 else -1
        now_iso = datetime.now().isoformat()
        sql = _upsert_indicator()

        for ind, score in raw.items():
            if not score:
                continue
            ind_dir = 1 if score > 0 else -1
            correct = (ind_dir == actual_dir)
            params = (ind, 1 if correct else 0, 0 if correct else 1, now_iso)
            if USE_POSTGRES:
                # psycopg2 expects %s, handled by wrapper; but we pass a raw string here
                # because _upsert_indicator returns %s placeholders already
                pass
            conn.execute(sql, params)

    # ------------------------------------------------------------------
    def get_indicator_stats(self, indicator: str) -> Dict[str, int]:
        empty = {'wins': 0, 'losses': 0, 'total': 0}
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

    # ------------------------------------------------------------------
    def get_recent_signals(self, limit: int = 50) -> List[Dict]:
        """For a future /api/signals_history endpoint."""
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

    def health_check(self) -> Dict:
        """Check backend connectivity — useful for /api/health."""
        info = {
            'backend': 'postgresql' if USE_POSTGRES else 'sqlite',
            'connected': False,
        }
        try:
            with _get_conn() as conn:
                conn.execute("SELECT 1").fetchone()
                info['connected'] = True
        except Exception as e:
            info['error'] = str(e)
        return info


# ======================================================================
# Singleton
# ======================================================================
signal_tracker = SignalTracker()
