"""
Risk Guard — Live Equity Monitor
Background thread that polls equity every 30 s.
Detects rollover by comparing wall-clock time (in firm timezone) against
the firm's rollover_time setting.

Multi-trader usage
------------------
Use ``RiskGuardMonitor.from_env_file(".env.sbonelo", equity_fetcher)``
to create a fully self-contained monitor from a named env file.
Each instance carries its own Telegram credentials so multiple traders
can run in the same process without credential cross-contamination.
"""

import logging
import time
from datetime import datetime, date
from typing import Callable, Optional

from .engine import RiskGuardEngine
from .models import DecisionStatus, Decision, FirmConfig, SessionState
from .state import RiskGuardState

log = logging.getLogger(__name__)


class RiskGuardMonitor:
    """
    Instantiate once per session. Call run() in a background thread.

    Parameters
    ----------
    config          FirmConfig loaded from YAML.
    state_db        RiskGuardState instance (shared with main thread).
    equity_fetcher  Zero-arg callable that returns current equity as float.
    account_size    Notional account size (used for floor calculation).
    poll_interval   Seconds between equity polls (default 30).
    on_block        Optional callback(Decision) fired when BLOCK triggered.
    on_warn         Optional callback(Decision) fired when WARN triggered.
    on_rollover     Optional callback(SessionState) fired after rollover.
    telegram_token  Telegram bot token for this instance's alerts.
                    Falls back to os.environ TELEGRAM_BOT_TOKEN if omitted.
    telegram_chat_id Telegram chat ID for this instance's alerts.
                    Falls back to os.environ TELEGRAM_CHAT_ID if omitted.
    env_file        Path to a .env file.  When given, Telegram credentials
                    are read from that file (takes priority over os.environ).
                    Stored on the instance and passed through to any RiskGuard
                    objects this monitor creates.
    """

    def __init__(
        self,
        config:           FirmConfig,
        state_db:         RiskGuardState,
        equity_fetcher:   Callable[[], float],
        account_size:     float,
        poll_interval:    int = 30,
        on_block:         Optional[Callable[[Decision], None]] = None,
        on_warn:          Optional[Callable[[Decision], None]] = None,
        on_rollover:      Optional[Callable[[SessionState], None]] = None,
        telegram_token:   Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        env_file:         Optional[str] = None,
    ):
        self.config         = config
        self.state_db       = state_db
        self.equity_fetcher = equity_fetcher
        self.account_size   = account_size
        self.poll_interval  = poll_interval
        self.on_block       = on_block
        self.on_warn        = on_warn
        self.on_rollover    = on_rollover
        self._engine        = RiskGuardEngine()
        self._running       = False
        self._env_file      = env_file

        # Resolve Telegram credentials: explicit arg > env file > os.environ
        import os
        if env_file and telegram_token is None:
            try:
                from dotenv import dotenv_values
                _env = dotenv_values(env_file)
                telegram_token   = _env.get("TELEGRAM_BOT_TOKEN")
                telegram_chat_id = _env.get("TELEGRAM_CHAT_ID")
            except ImportError:
                pass
        self._tg_token   = telegram_token   or os.getenv("TELEGRAM_BOT_TOKEN")
        self._tg_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")

    # ─────────────────────────────────────────────
    # FACTORY — build from env file
    # ─────────────────────────────────────────────

    @classmethod
    def from_env_file(
        cls,
        env_file: str,
        equity_fetcher: Callable[[], float],
        poll_interval: int = 30,
        on_block:   Optional[Callable[[Decision], None]] = None,
        on_warn:    Optional[Callable[[Decision], None]] = None,
        on_rollover: Optional[Callable[[SessionState], None]] = None,
    ) -> "RiskGuardMonitor":
        """
        Create a fully self-contained monitor from a named env file.
        Reads ACTIVE_FIRM, ACCOUNT_SIZE, and RISK_GUARD_DB_PATH from the file,
        creates a RiskGuard instance, then wires it into the monitor.

        Typical usage in a per-trader runner script::

            monitor = RiskGuardMonitor.from_env_file(
                ".env.sbonelo", equity_fetcher=mt5.get_equity
            )
            monitor.run(monitor.state)
        """
        # Import here to avoid circular import (RiskGuard imports monitor)
        from . import RiskGuard

        rg = RiskGuard(env_file=env_file)

        instance = cls(
            config=rg.config,
            state_db=rg.state_db,
            equity_fetcher=equity_fetcher,
            account_size=rg.state.account_size,
            poll_interval=poll_interval,
            on_block=on_block,
            on_warn=on_warn,
            on_rollover=on_rollover,
            env_file=env_file,
        )
        # Expose the RiskGuard instance so callers can also use check_trade()
        instance.risk_guard = rg
        instance.state      = rg.state
        return instance

    # ─────────────────────────────────────────────
    # ROLLOVER DETECTION
    # ─────────────────────────────────────────────

    def _current_session_date(self) -> date:
        """
        Return the logical trading date given the firm's rollover time+tz.
        """
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo  # type: ignore

        tz = ZoneInfo(self.config.rollover_tz)
        return datetime.now(tz).date()

    def _check_and_apply_rollover(self, state: SessionState) -> SessionState:
        """If the session date has changed, archive and create a new session."""
        current_date = self._current_session_date()
        if state.session_date == current_date.isoformat():
            return state

        log.info(
            "[Monitor:%s] Rollover: session %s → %s",
            self.config.name, state.session_date, current_date.isoformat(),
        )
        new_state = self.state_db.rollover(state, current_date, self.config)
        if self.on_rollover:
            try:
                self.on_rollover(new_state)
            except Exception:
                log.exception("[Monitor] on_rollover callback raised")
        return new_state

    # ─────────────────────────────────────────────
    # TELEGRAM  (instance-scoped credentials)
    # ─────────────────────────────────────────────

    async def send_alert(self, text: str) -> None:
        """Send a Telegram alert using this monitor instance's credentials."""
        from .alerts import send_rg_alert
        await send_rg_alert(
            text,
            token=self._tg_token,
            chat_id=self._tg_chat_id,
        )

    # ─────────────────────────────────────────────
    # SINGLE POLL CYCLE
    # ─────────────────────────────────────────────

    def run_once(self, state: SessionState) -> SessionState:
        """
        Execute one poll cycle:
          1. Check for rollover.
          2. Fetch equity.
          3. Update equity_high if needed.
          4. Run Fence 2; fire callbacks on WARN/BLOCK.
        Returns the (possibly updated) SessionState.
        """
        state = self._check_and_apply_rollover(state)

        try:
            current_equity = float(self.equity_fetcher())
        except Exception as exc:
            log.warning("[Monitor:%s] Equity fetch failed: %s", self.config.name, exc)
            return state

        if current_equity > state.equity_high:
            state.equity_high = current_equity
            self.state_db.save(state)

        decision = self._engine.fence2_live_equity(
            config=self.config,
            account_size=self.account_size,
            current_equity=current_equity,
            equity_high=state.equity_high,
        )

        if decision.status == DecisionStatus.BLOCK:
            log.warning("[Monitor:%s] BLOCK — %s", self.config.name, decision.reason)
            if self.on_block:
                try:
                    self.on_block(decision)
                except Exception:
                    log.exception("[Monitor] on_block callback raised")

        elif decision.status == DecisionStatus.WARN:
            log.warning("[Monitor:%s] WARN — %s", self.config.name, decision.reason)
            if self.on_warn:
                try:
                    self.on_warn(decision)
                except Exception:
                    log.exception("[Monitor] on_warn callback raised")

        return state

    # ─────────────────────────────────────────────
    # BLOCKING LOOP
    # ─────────────────────────────────────────────

    def run(self, state: SessionState):
        """
        Blocking poll loop. Intended to be called in a daemon thread.
        Call stop() from another thread to exit gracefully.
        """
        self._running = True
        log.info(
            "[Monitor:%s] Started — polling every %ds | env_file=%s",
            self.config.name, self.poll_interval,
            self._env_file or "<default .env>",
        )
        while self._running:
            state = self.run_once(state)
            time.sleep(self.poll_interval)
        log.info("[Monitor:%s] Stopped.", self.config.name)

    def stop(self):
        """Signal the run() loop to exit after the current poll."""
        self._running = False
