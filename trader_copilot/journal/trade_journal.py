"""
Trader Copilot — Trade Journal
Records every signal fired and tracks outcomes (TP hit / SL hit / manual close).
Persists to a SQLite database for querying and backtesting replay.

Schema:
    trades      — one row per signal fired
    outcomes    — one row per trade result (linked to trade)
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List
from enum import Enum

from ..core.structures import TradeSignal, Direction


class Outcome(Enum):
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    MANUAL_WIN = "manual_win"
    MANUAL_LOSS = "manual_loss"
    BREAKEVEN = "breakeven"
    PENDING = "pending"


@dataclass
class TradeRecord:
    id: Optional[int]
    symbol: str
    direction: str
    pattern: str
    timeframe: str
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    confluence_score: int
    fvg_present: bool
    ob_present: bool
    killzone_active: bool
    signal_time: str  # ISO timestamp of signal
    notes: str
    outcome: str = Outcome.PENDING.value
    close_price: Optional[float] = None
    close_time: Optional[str] = None
    pnl_rr: Optional[float] = None  # Result in R multiples (e.g. +2.0, -1.0)
    session: Optional[str] = None  # london / new_york etc
    taken: bool = False  # True if the trader actually entered this trade


class TradeJournal:

    # Each entry is (version: int, description: str, sql: str).
    # Append new migrations here — never edit or remove existing ones.
    _MIGRATIONS: List[tuple] = [
        (
            1,
            "initial schema — trades table",
            """
            CREATE TABLE IF NOT EXISTS trades (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT NOT NULL,
                direction        TEXT NOT NULL,
                pattern          TEXT NOT NULL,
                timeframe        TEXT,
                entry_price      REAL NOT NULL,
                stop_loss        REAL NOT NULL,
                take_profit      REAL NOT NULL,
                risk_reward      REAL,
                confluence_score INTEGER,
                fvg_present      INTEGER,
                ob_present       INTEGER,
                killzone_active  INTEGER,
                signal_time      TEXT NOT NULL,
                notes            TEXT,
                outcome          TEXT DEFAULT 'pending',
                close_price      REAL,
                close_time       TEXT,
                pnl_rr           REAL,
                session          TEXT,
                created_at       TEXT DEFAULT (datetime('now'))
            )
            """,
        ),
        (
            2,
            "add taken column — marks whether the trader actually entered the trade",
            "ALTER TABLE trades ADD COLUMN taken INTEGER NOT NULL DEFAULT 0",
        ),
    ]

    def __init__(self, db_path: str = "trader_copilot_journal.db"):
        self.db_path = db_path
        self._init_db()

    def close(self):
        """
        Release any lingering SQLite connection handles so the DB file can be
        safely deleted on Windows. Every public method opens and exits its own
        ``with sqlite3.connect(...)`` block, so there is no persistent
        connection to close — but on CPython+Windows the underlying file lock
        can outlive the ``with`` exit until the connection object is garbage
        collected. Forcing a GC pass here makes tests (and consumers that
        ``os.unlink`` the DB) deterministic.
        """
        import gc

        gc.collect()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            # Migration-tracking table — created unconditionally on every startup.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     INTEGER PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """
            )
            conn.commit()

            applied = {
                row[0]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }

            for version, description, sql in self._MIGRATIONS:
                if version in applied:
                    continue
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                    (version, description),
                )
                conn.commit()

    # ─────────────────────────────────────────────
    # WRITE
    # ─────────────────────────────────────────────

    def log_signal(self, signal: TradeSignal) -> int:
        """Log a new signal. Returns the trade ID."""
        session = self._infer_session(signal.timestamp, signal.symbol)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    symbol, direction, pattern, timeframe,
                    entry_price, stop_loss, take_profit, risk_reward,
                    confluence_score, fvg_present, ob_present, killzone_active,
                    signal_time, notes, session, taken
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    signal.symbol,
                    signal.direction.value,
                    signal.pattern,
                    signal.timeframe,
                    signal.entry_price,
                    signal.stop_loss,
                    signal.take_profit,
                    signal.risk_reward,
                    signal.confluence_score,
                    int(signal.fvg_present),
                    int(signal.ob_present),
                    int(signal.killzone_active),
                    signal.timestamp.isoformat(),
                    signal.notes,
                    session,
                    0,  # taken defaults to False; set via record_outcome() or a separate call
                ),
            )
            trade_id = cur.lastrowid
            conn.commit()
        print(
            f"[Journal] Signal logged — ID: {trade_id} | {signal.symbol} {signal.direction.value} | {signal.pattern}"
        )
        return trade_id

    def record_outcome(
        self,
        trade_id: int,
        outcome: Outcome,
        close_price: float,
        close_time: Optional[datetime] = None,
        taken: bool = True,
    ):
        """Record the result of a trade after it closes."""
        if close_time is None:
            close_time = datetime.utcnow()

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT entry_price, stop_loss, direction FROM trades WHERE id=?",
                (trade_id,),
            ).fetchone()
            if not row:
                print(f"[Journal] Trade ID {trade_id} not found.")
                return

            entry, sl, direction = row
            risk = abs(entry - sl)
            if direction == Direction.BEARISH.value:
                pnl_rr = round((entry - close_price) / risk, 2) if risk > 0 else 0.0
            else:
                pnl_rr = round((close_price - entry) / risk, 2) if risk > 0 else 0.0

            conn.execute(
                """
                UPDATE trades SET
                    outcome = ?,
                    close_price = ?,
                    close_time = ?,
                    pnl_rr = ?,
                    taken = ?
                WHERE id = ?
            """,
                (
                    outcome.value,
                    close_price,
                    close_time.isoformat(),
                    pnl_rr,
                    int(taken),
                    trade_id,
                ),
            )
            conn.commit()

        print(
            f"[Journal] Outcome recorded — ID: {trade_id} | {outcome.value} | P&L: {pnl_rr:+.2f}R"
        )

    # ─────────────────────────────────────────────
    # READ
    # ─────────────────────────────────────────────

    def get_all_trades(self) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY signal_time DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_pending(self) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades WHERE outcome='pending' ORDER BY signal_time DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_by_symbol(self, symbol: str) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades WHERE symbol=? ORDER BY signal_time DESC",
                (symbol,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_by_pattern(self, pattern: str) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades WHERE pattern LIKE ? ORDER BY signal_time DESC",
                (f"%{pattern}%",),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─────────────────────────────────────────────
    # STATS (feeds Phase 3 ML layer)
    # ─────────────────────────────────────────────

    def get_stats(self, symbol: str = None, pattern: str = None) -> dict:
        """
        Compute win rate, average RR, and breakdown stats.
        Filters by symbol and/or pattern if provided.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            where = ["outcome != 'pending'"]
            params = []
            if symbol:
                where.append("symbol = ?")
                params.append(symbol)
            if pattern:
                where.append("pattern LIKE ?")
                params.append(f"%{pattern}%")

            clause = " AND ".join(where)
            rows = conn.execute(
                f"SELECT * FROM trades WHERE {clause}", params
            ).fetchall()

        if not rows:
            return {"message": "No completed trades found."}

        total = len(rows)
        wins = [r for r in rows if r["outcome"] in ("tp_hit", "manual_win")]
        losses = [r for r in rows if r["outcome"] in ("sl_hit", "manual_loss")]
        be = [r for r in rows if r["outcome"] == "breakeven"]

        win_rate = round(len(wins) / total * 100, 1)
        avg_rr = round(sum(r["pnl_rr"] or 0 for r in rows) / total, 2)
        total_r = round(sum(r["pnl_rr"] or 0 for r in rows), 2)

        # Breakdown by score
        score_wins = {}
        for r in rows:
            sc = r["confluence_score"]
            if sc not in score_wins:
                score_wins[sc] = {"total": 0, "wins": 0}
            score_wins[sc]["total"] += 1
            if r["outcome"] in ("tp_hit", "manual_win"):
                score_wins[sc]["wins"] += 1

        score_breakdown = {
            sc: {
                "total": v["total"],
                "win_rate": round(v["wins"] / v["total"] * 100, 1),
            }
            for sc, v in sorted(score_wins.items())
        }

        # Pattern breakdown
        pattern_wins = {}
        for r in rows:
            p = r["pattern"]
            if p not in pattern_wins:
                pattern_wins[p] = {"total": 0, "wins": 0}
            pattern_wins[p]["total"] += 1
            if r["outcome"] in ("tp_hit", "manual_win"):
                pattern_wins[p]["wins"] += 1

        pattern_breakdown = {
            p: {"total": v["total"], "win_rate": round(v["wins"] / v["total"] * 100, 1)}
            for p, v in pattern_wins.items()
        }

        # FVG boost validation
        fvg_trades = [r for r in rows if r["fvg_present"]]
        no_fvg_trades = [r for r in rows if not r["fvg_present"]]
        fvg_wr = (
            round(
                len([r for r in fvg_trades if r["outcome"] in ("tp_hit", "manual_win")])
                / len(fvg_trades)
                * 100,
                1,
            )
            if fvg_trades
            else None
        )
        no_fvg_wr = (
            round(
                len(
                    [
                        r
                        for r in no_fvg_trades
                        if r["outcome"] in ("tp_hit", "manual_win")
                    ]
                )
                / len(no_fvg_trades)
                * 100,
                1,
            )
            if no_fvg_trades
            else None
        )

        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(be),
            "win_rate_pct": win_rate,
            "avg_rr": avg_rr,
            "total_r": total_r,
            "score_breakdown": score_breakdown,
            "pattern_breakdown": pattern_breakdown,
            "fvg_present_wr": fvg_wr,
            "fvg_absent_wr": no_fvg_wr,
        }

    def _infer_session(self, timestamp: datetime, symbol: str) -> str:
        hour = timestamp.hour
        if 7 <= hour < 10:
            return "london"
        elif 10 <= hour < 12:
            return "london_ny_overlap_pre"
        elif 13 <= hour < 14:
            return "new_york_open"
        elif 12 <= hour < 16:
            return "new_york"
        return "off_session"

    def export_csv(self, filepath: str):
        """Export full journal to CSV for external analysis."""
        import csv

        trades = self.get_all_trades()
        if not trades:
            print("No trades to export.")
            return
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=trades[0].keys())
            writer.writeheader()
            writer.writerows(trades)
        print(f"[Journal] Exported {len(trades)} trades to {filepath}")
