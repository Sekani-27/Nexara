"""
Risk Guard — Prop-Firm Compliance & Position-Sizing Module
==========================================================
Protects a funded account from breaching firm drawdown rules,
daily trade limits, hold-time minimums, and revenge-trading patterns.

Quick start — default .env
--------------------------
    from risk_guard import RiskGuard
    rg = RiskGuard(firm_name="goat", account_size=100_000)

Quick start — named env file (multi-trader)
-------------------------------------------
    rg = RiskGuard(env_file=".env.sbonelo")
    # firm, account_size, db_path, and Telegram credentials are all
    # read from .env.sbonelo; os.environ is never modified.
"""

import os
from typing import Optional

from .engine import RiskGuardEngine
from .models import (
    DecisionStatus, Decision, FirmConfig,
    TradeProposal, SessionState,
)
from .state import RiskGuardState
from .firms.loader import load_firm, list_firms


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_env_values(env_file: Optional[str]) -> dict:
    """
    Return a dict of key=value pairs from an env file WITHOUT modifying
    os.environ.  Uses dotenv_values() which is purely read-only.

    - If env_file is given, read that specific file.
    - If env_file is None, read the default .env in the cwd (if present).
    - Returns an empty dict if python-dotenv is not installed or file missing.
    """
    try:
        from dotenv import dotenv_values
        if env_file:
            return dotenv_values(env_file)
        return dotenv_values()   # reads .env in current working directory
    except ImportError:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# MAIN FAÇADE
# ─────────────────────────────────────────────────────────────────────────────

class RiskGuard:
    """
    One instance per funded account per process.
    Owns the FirmConfig, state DB connection, engine, and Telegram credentials.

    Parameters
    ----------
    firm_name       Prop-firm name (e.g. "goat").  Overrides env ACTIVE_FIRM.
    account_size    Notional account size in $.     Overrides env ACCOUNT_SIZE.
    db_path         SQLite file path.               Overrides env RISK_GUARD_DB_PATH.
    env_file        Path to a .env file to read config from.
                    When supplied, credentials are loaded from that file only —
                    os.environ is never touched, allowing multiple isolated
                    instances in the same process (one per trader).
    """

    def __init__(
        self,
        firm_name:    Optional[str]   = None,
        account_size: Optional[float] = None,
        db_path:      Optional[str]   = None,
        env_file:     Optional[str]   = None,
    ):
        # 1. Load env values without polluting os.environ
        env = _load_env_values(env_file)

        # 2. Resolve each config value: explicit arg > env file > hard default
        resolved_firm    = firm_name    or env.get("ACTIVE_FIRM", "goat")
        resolved_account = float(
            account_size if account_size is not None
            else env.get("ACCOUNT_SIZE") or os.getenv("ACCOUNT_SIZE", "10000")
        )
        resolved_db = (
            db_path
            or env.get("RISK_GUARD_DB_PATH", "risk_guard.db")
        )

        # 3. Telegram credentials — per-instance, isolated from os.environ
        #    Fall back to os.environ so existing callers (no env_file) still work.
        self.telegram_token   = (
            env.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
        )
        self.telegram_chat_id = (
            env.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
        )

        # 4. Ensure DB parent directory exists (e.g. ./data/)
        db_dir = os.path.dirname(os.path.abspath(resolved_db))
        os.makedirs(db_dir, exist_ok=True)

        # 5. Wire up core components
        self.config    = load_firm(resolved_firm)
        self.state_db  = RiskGuardState(db_path=resolved_db)
        self.engine    = RiskGuardEngine()
        self.state     = self.state_db.load_or_create(resolved_firm, resolved_account)

        # 6. Preserve for downstream use (monitor, runner scripts)
        self._env_file = env_file
        self._env      = env

    # ─────────────────────────────────────────────
    # PRIMARY API
    # ─────────────────────────────────────────────

    def check_trade(self, proposal: TradeProposal) -> Decision:
        """Run Fence 1 and return a Decision."""
        return self.engine.fence1_pre_trade(self.config, proposal, self.state)

    def record_trade(self):
        """Call immediately after a trade is placed to increment the counter."""
        self.state.trades_today += 1
        self.state_db.save(self.state)

    def update_pnl(self, pnl_delta: float, current_equity: float):
        """
        Update session P&L and rolling equity high.
        Call whenever a trade closes or an equity snapshot is received.
        """
        self.state.daily_pnl += pnl_delta
        if current_equity > self.state.equity_high:
            self.state.equity_high = current_equity
        self.state_db.save(self.state)

    def reload_state(self):
        """Re-read state from DB (useful after external modification)."""
        self.state = self.state_db.load_or_create(
            self.config.name, self.state.account_size
        )

    async def send_alert(self, text: str):
        """Send a Telegram alert using this instance's credentials."""
        from .alerts import send_rg_alert
        await send_rg_alert(
            text,
            token=self.telegram_token,
            chat_id=self.telegram_chat_id,
        )
