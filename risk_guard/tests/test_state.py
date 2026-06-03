"""
Risk Guard — State Persistence Test Suite

Tests:
  1. State persists across RiskGuardState instantiation (crash-safe)
  2. New day creates a fresh session with zero daily_pnl and trades
  3. Rollover resets daily fields and carries forward cumulative totals
  4. Revenge lock is set when rollover follows a hard-stop session
"""

import gc
import os
import tempfile
from datetime import date, timedelta

from risk_guard.state import RiskGuardState
from risk_guard.models import FirmConfig

# ─────────────────────────────────────────────────────────────────────────────
# SHARED FIXTURES
# ─────────────────────────────────────────────────────────────────────────────

ACCOUNT = 10_000.0
FIRM    = "TESTFIRM"


def _config() -> FirmConfig:
    return FirmConfig(
        name=FIRM,
        daily_dd_pct=5.0,
        total_dd_pct=10.0,
        target_pct=10.0,
        dd_calc="rolling_equity_high",
        rollover_time="17:00",
        rollover_tz="US/Eastern",
        min_hold_seconds=120,
        min_valid_day_pct=0.5,
        min_valid_days=3,
        news=True,
        weekend=True,
        internal_soft_stop_pct=2.5,
        internal_hard_stop_pct=3.75,
        risk_per_trade_pct=0.5,
        max_open_risk_pct=1.5,
        max_trades_per_day=3,
        revenge_lock_hours=24,
    )


def _tmpdb() -> str:
    """Return a path to a fresh temporary SQLite file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)   # delete so RiskGuardState creates it clean
    return path


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — PERSISTENCE ACROSS INSTANTIATION
# ─────────────────────────────────────────────────────────────────────────────

def test_state_persists_across_restart():
    """
    Modifying state and calling save() must be visible to a brand-new
    RiskGuardState instance pointing at the same DB file.
    """
    path = _tmpdb()
    today = date.today()

    try:
        # First instance — create and modify state
        db1   = RiskGuardState(db_path=path)
        state = db1.load_or_create(FIRM, ACCOUNT, today=today)
        assert state.trades_today == 0
        assert state.daily_pnl   == 0.0

        state.trades_today = 2
        state.daily_pnl    = 150.0
        state.equity_high  = ACCOUNT + 150.0
        db1.save(state)

        # Second instance — completely new Python object, same file
        db2       = RiskGuardState(db_path=path)
        reloaded  = db2.load_or_create(FIRM, ACCOUNT, today=today)

        assert reloaded.trades_today == 2,    f"Expected 2, got {reloaded.trades_today}"
        assert reloaded.daily_pnl    == 150.0, f"Expected 150.0, got {reloaded.daily_pnl}"
        assert reloaded.equity_high  == ACCOUNT + 150.0

        print("PASS — state persists: trades_today=2, daily_pnl=150 survived restart")
    finally:
        gc.collect()   # release SQLite file handles before deletion (Windows)
        if os.path.exists(path):
            os.unlink(path)


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — NEW DAY CREATES FRESH STATE
# ─────────────────────────────────────────────────────────────────────────────

def test_new_day_creates_fresh_state():
    """
    load_or_create() for a date that has no existing record must return a
    fresh session: daily_pnl=0, trades_today=0, session_locked=False.
    """
    path  = _tmpdb()
    today = date.today()

    try:
        db    = RiskGuardState(db_path=path)
        state = db.load_or_create(FIRM, ACCOUNT, today=today)

        assert state.daily_pnl            == 0.0,  "Fresh state: daily_pnl must be 0"
        assert state.trades_today         == 0,    "Fresh state: trades_today must be 0"
        assert state.session_locked       is False, "Fresh state: must not be locked"
        assert state.revenge_locked_until is None,  "Fresh state: no revenge lock"
        assert state.session_date         == today.isoformat()

        print(f"PASS — new day creates fresh state for {today}")
    finally:
        gc.collect()   # release SQLite file handles before deletion (Windows)
        if os.path.exists(path):
            os.unlink(path)


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — ROLLOVER RESETS DAILY FIELDS
# ─────────────────────────────────────────────────────────────────────────────

def test_rollover_resets_daily_fields():
    """
    After rollover(), the new session must have:
      - daily_pnl  = 0
      - trades_today = 0
      - starting_balance = prior session's ending balance
      - cumulative_pnl carries forward
    """
    path  = _tmpdb()
    today = date.today()
    next_ = today + timedelta(days=1)
    config = _config()

    try:
        db    = RiskGuardState(db_path=path)
        state = db.load_or_create(FIRM, ACCOUNT, today=today)

        # Simulate a profitable session
        state.daily_pnl    = 300.0
        state.trades_today = 2
        state.equity_high  = ACCOUNT + 300.0
        db.save(state)

        # Roll over to next day
        new_state = db.rollover(state, next_, config)

        assert new_state.daily_pnl    == 0.0,            "Rollover: daily_pnl must reset to 0"
        assert new_state.trades_today == 0,              "Rollover: trades_today must reset to 0"
        assert new_state.session_date == next_.isoformat()
        assert new_state.starting_balance == ACCOUNT + 300.0, \
            "Rollover: starting_balance must carry prior ending balance"
        assert new_state.cumulative_pnl == 300.0, \
            "Rollover: cumulative_pnl must accumulate prior session P&L"

        print(
            f"PASS — rollover resets daily fields | "
            f"new balance=${new_state.starting_balance}, "
            f"cumulative=${new_state.cumulative_pnl}"
        )
    finally:
        gc.collect()   # release SQLite file handles before deletion (Windows)
        if os.path.exists(path):
            os.unlink(path)


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — REVENGE LOCK SET AFTER HARD-STOP SESSION
# ─────────────────────────────────────────────────────────────────────────────

def test_revenge_lock_set_after_hard_stop_session():
    """
    If session_ended_via_hard_stop=True on rollover, the new session must
    have revenge_locked_until set to (now + revenge_lock_hours).
    """
    path   = _tmpdb()
    today  = date.today()
    next_  = today + timedelta(days=1)
    config = _config()   # revenge_lock_hours = 24

    try:
        db    = RiskGuardState(db_path=path)
        state = db.load_or_create(FIRM, ACCOUNT, today=today)

        # Simulate hard stop being hit during the session
        state.session_ended_via_hard_stop = True
        state.session_locked              = True
        state.daily_pnl                   = -375.0
        db.save(state)

        new_state = db.rollover(state, next_, config)

        assert new_state.revenge_locked_until is not None, \
            "Revenge lock must be set after hard-stop rollover"
        assert new_state.session_locked is False, \
            "New session must start unlocked"
        assert new_state.session_ended_via_hard_stop is False, \
            "New session must reset hard-stop flag"

        # Verify the unlock time is ~24 h in the future
        from datetime import datetime
        unlock_dt = datetime.fromisoformat(new_state.revenge_locked_until)
        hours_ahead = (unlock_dt - datetime.utcnow()).total_seconds() / 3600
        assert 23.0 < hours_ahead < 25.0, \
            f"Revenge lock should be ~24h ahead, got {hours_ahead:.1f}h"

        print(
            f"PASS — revenge lock set after hard stop | "
            f"unlocks at {new_state.revenge_locked_until[:16]} UTC"
        )
    finally:
        gc.collect()   # release SQLite file handles before deletion (Windows)
        if os.path.exists(path):
            os.unlink(path)


def test_no_revenge_lock_after_normal_session():
    """Normal session rollover must NOT set revenge_locked_until."""
    path   = _tmpdb()
    today  = date.today()
    next_  = today + timedelta(days=1)
    config = _config()

    try:
        db    = RiskGuardState(db_path=path)
        state = db.load_or_create(FIRM, ACCOUNT, today=today)
        state.daily_pnl = 100.0   # normal positive session
        db.save(state)

        new_state = db.rollover(state, next_, config)

        assert new_state.revenge_locked_until is None, \
            "Normal session must NOT set a revenge lock"
        print("PASS — normal session rollover has no revenge lock")
    finally:
        gc.collect()   # release SQLite file handles before deletion (Windows)
        if os.path.exists(path):
            os.unlink(path)


# ─────────────────────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n── Risk Guard — State Test Suite ──\n")
    tests = [
        test_state_persists_across_restart,
        test_new_day_creates_fresh_state,
        test_rollover_resets_daily_fields,
        test_revenge_lock_set_after_hard_stop_session,
        test_no_revenge_lock_after_normal_session,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:
            import traceback
            print(f"FAIL — {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n── {passed} passed, {failed} failed ──\n")
