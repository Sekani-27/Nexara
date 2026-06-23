"""
Risk Guard — Session State Persistence
SQLite-backed store for session state and session history.

Two tables:
  session_state  — one row per (firm, session_date); UPSERT on every save
  session_log    — append-only archive of completed sessions

State survives process crashes: daily PnL and trade count are written to DB
on every update, so a restart picks up exactly where it left off.
"""

import sqlite3
from datetime import datetime, date, timedelta
from typing import List, Optional

from .models import FirmConfig, SessionState


class RiskGuardState:

    def __init__(self, db_path: str = "risk_guard.db"):
        self.db_path = db_path
        self._init_db()

    # ─────────────────────────────────────────────
    # SCHEMA
    # ─────────────────────────────────────────────

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_state (
                    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                    firm                        TEXT    NOT NULL,
                    session_date                TEXT    NOT NULL,
                    account_size                REAL    NOT NULL,
                    starting_balance            REAL    NOT NULL,
                    equity_high                 REAL    NOT NULL,
                    daily_pnl                   REAL    NOT NULL DEFAULT 0,
                    trades_today                INTEGER NOT NULL DEFAULT 0,
                    valid_trading_days          INTEGER NOT NULL DEFAULT 0,
                    session_locked              INTEGER NOT NULL DEFAULT 0,
                    revenge_locked_until        TEXT,
                    session_ended_via_hard_stop INTEGER NOT NULL DEFAULT 0,
                    cumulative_pnl              REAL    NOT NULL DEFAULT 0,
                    last_updated                TEXT    NOT NULL,
                    UNIQUE(firm, session_date)
                )
            """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_log (
                    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                    firm                   TEXT    NOT NULL,
                    session_date           TEXT    NOT NULL,
                    session_pnl            REAL    NOT NULL,
                    trades_count           INTEGER NOT NULL,
                    was_valid_day          INTEGER NOT NULL DEFAULT 0,
                    ended_via_hard_stop    INTEGER NOT NULL DEFAULT 0,
                    equity_high            REAL    NOT NULL,
                    logged_at              TEXT    NOT NULL
                )
            """
            )
            conn.commit()

    # ─────────────────────────────────────────────
    # LOAD / CREATE
    # ─────────────────────────────────────────────

    def load_or_create(
        self,
        firm: str,
        account_size: float,
        today: Optional[date] = None,
    ) -> SessionState:
        """
        Load today's session state from DB, or create a fresh one.
        Cumulative P&L and valid-day count are carried forward from
        session_log so a fresh record starts with correct running totals.
        """
        if today is None:
            today = date.today()
        today_str = today.isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            row = conn.execute(
                "SELECT * FROM session_state WHERE firm=? AND session_date=?",
                (firm, today_str),
            ).fetchone()

            if row:
                return self._row_to_state(row)

            # Sum prior sessions
            prior = conn.execute(
                "SELECT COALESCE(SUM(session_pnl), 0) AS total "
                "FROM session_log WHERE firm=? AND session_date < ?",
                (firm, today_str),
            ).fetchone()["total"]

            vtd = conn.execute(
                "SELECT COUNT(*) AS cnt FROM session_log "
                "WHERE firm=? AND was_valid_day=1",
                (firm,),
            ).fetchone()["cnt"]

            balance = account_size + prior
            now_str = datetime.utcnow().isoformat()

            state = SessionState(
                firm=firm,
                session_date=today_str,
                account_size=account_size,
                starting_balance=balance,
                equity_high=balance,
                daily_pnl=0.0,
                trades_today=0,
                valid_trading_days=int(vtd),
                session_locked=False,
                revenge_locked_until=None,
                session_ended_via_hard_stop=False,
                cumulative_pnl=float(prior),
                last_updated=now_str,
            )
            self._upsert(conn, state)
            return state

    # ─────────────────────────────────────────────
    # SAVE
    # ─────────────────────────────────────────────

    def save(self, state: SessionState):
        """Persist current state. Stamps last_updated to now."""
        state.last_updated = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            self._upsert(conn, state)

    def _upsert(self, conn: sqlite3.Connection, state: SessionState):
        conn.execute(
            """
            INSERT INTO session_state (
                firm, session_date, account_size, starting_balance,
                equity_high, daily_pnl, trades_today, valid_trading_days,
                session_locked, revenge_locked_until,
                session_ended_via_hard_stop, cumulative_pnl, last_updated
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(firm, session_date) DO UPDATE SET
                equity_high                 = excluded.equity_high,
                daily_pnl                   = excluded.daily_pnl,
                trades_today                = excluded.trades_today,
                valid_trading_days          = excluded.valid_trading_days,
                session_locked              = excluded.session_locked,
                revenge_locked_until        = excluded.revenge_locked_until,
                session_ended_via_hard_stop = excluded.session_ended_via_hard_stop,
                cumulative_pnl              = excluded.cumulative_pnl,
                last_updated                = excluded.last_updated
            """,
            (
                state.firm,
                state.session_date,
                state.account_size,
                state.starting_balance,
                state.equity_high,
                state.daily_pnl,
                state.trades_today,
                state.valid_trading_days,
                int(state.session_locked),
                state.revenge_locked_until,
                int(state.session_ended_via_hard_stop),
                state.cumulative_pnl,
                state.last_updated,
            ),
        )
        conn.commit()

    # ─────────────────────────────────────────────
    # SESSION LOG
    # ─────────────────────────────────────────────

    def log_session(self, state: SessionState, was_valid_day: bool):
        """Archive a completed session to session_log."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO session_log (
                    firm, session_date, session_pnl, trades_count,
                    was_valid_day, ended_via_hard_stop, equity_high, logged_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    state.firm,
                    state.session_date,
                    state.daily_pnl,
                    state.trades_today,
                    int(was_valid_day),
                    int(state.session_ended_via_hard_stop),
                    state.equity_high,
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()

    def get_recent_sessions(self, firm: str, count: int = 3) -> List[dict]:
        """Return most recent completed sessions (newest first)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM session_log WHERE firm=? "
                "ORDER BY session_date DESC LIMIT ?",
                (firm, count),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─────────────────────────────────────────────
    # ROLLOVER
    # ─────────────────────────────────────────────

    def rollover(
        self,
        state: SessionState,
        new_date: date,
        config: FirmConfig,
    ) -> SessionState:
        """
        Archive the current session and create a fresh one for new_date.
        Carries forward: cumulative_pnl, valid_trading_days.
        Sets revenge_locked_until if session ended via hard stop.
        """
        # Determine valid-day status for the session being closed
        was_valid = config.min_valid_day_pct == 0.0 or state.daily_pnl >= (
            config.min_valid_day_pct / 100.0 * state.account_size
        )

        self.save(state)
        self.log_session(state, was_valid)

        revenge_locked_until: Optional[str] = None
        if state.session_ended_via_hard_stop and config.revenge_lock_hours > 0:
            unlock_at = datetime.utcnow() + timedelta(hours=config.revenge_lock_hours)
            revenge_locked_until = unlock_at.isoformat()

        new_balance = state.starting_balance + state.daily_pnl

        new_state = SessionState(
            firm=state.firm,
            session_date=new_date.isoformat(),
            account_size=state.account_size,
            starting_balance=new_balance,
            equity_high=new_balance,
            daily_pnl=0.0,
            trades_today=0,
            valid_trading_days=state.valid_trading_days + (1 if was_valid else 0),
            session_locked=False,
            revenge_locked_until=revenge_locked_until,
            session_ended_via_hard_stop=False,
            cumulative_pnl=state.cumulative_pnl + state.daily_pnl,
            last_updated=datetime.utcnow().isoformat(),
        )
        with sqlite3.connect(self.db_path) as conn:
            self._upsert(conn, new_state)
        return new_state

    # ─────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────

    @staticmethod
    def _row_to_state(row: sqlite3.Row) -> SessionState:
        return SessionState(
            firm=row["firm"],
            session_date=row["session_date"],
            account_size=row["account_size"],
            starting_balance=row["starting_balance"],
            equity_high=row["equity_high"],
            daily_pnl=row["daily_pnl"],
            trades_today=row["trades_today"],
            valid_trading_days=row["valid_trading_days"],
            session_locked=bool(row["session_locked"]),
            revenge_locked_until=row["revenge_locked_until"],
            session_ended_via_hard_stop=bool(row["session_ended_via_hard_stop"]),
            cumulative_pnl=row["cumulative_pnl"],
            last_updated=row["last_updated"],
        )
