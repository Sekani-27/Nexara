"""
Risk Guard — Engine Test Suite
Covers all six Fence 1 checks.
No database or filesystem access needed — pure in-memory objects.

Account size : $10,000
Soft stop    : 2.5%  = $250
Hard stop    : 3.75% = $375
Max open     : 1.5%  = $150
Max trades   : 3
Min hold     : 120 s = 2 min
"""

from datetime import datetime, date

from risk_guard.engine import RiskGuardEngine
from risk_guard.models import (
    DecisionStatus, FirmConfig, TradeProposal, SessionState,
)

# ─────────────────────────────────────────────────────────────────────────────
# SHARED FIXTURES
# ─────────────────────────────────────────────────────────────────────────────

ACCOUNT = 10_000.0


def _config() -> FirmConfig:
    return FirmConfig(
        name="TEST",
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


def _state(**overrides) -> SessionState:
    base = SessionState(
        firm="TEST",
        session_date=date.today().isoformat(),
        account_size=ACCOUNT,
        starting_balance=ACCOUNT,
        equity_high=ACCOUNT,
        daily_pnl=0.0,
        trades_today=0,
        valid_trading_days=0,
        session_locked=False,
        revenge_locked_until=None,
        session_ended_via_hard_stop=False,
        cumulative_pnl=0.0,
        last_updated=datetime.utcnow().isoformat(),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _proposal(**overrides) -> TradeProposal:
    base = TradeProposal(
        firm="TEST",
        account_size=ACCOUNT,
        proposed_risk_dollars=50.0,     # 0.5% of 10k — safe
        current_daily_pnl=0.0,
        open_risk_dollars=0.0,
        trades_today=0,
        estimated_hold_minutes=60.0,    # 60 min — above 2-min minimum
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


ENGINE = RiskGuardEngine()


# ─────────────────────────────────────────────────────────────────────────────
# FENCE 1 TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_f1_session_locked_blocks():
    """Check 1: session_locked=True → BLOCK regardless of anything else."""
    state    = _state(session_locked=True)
    decision = ENGINE.fence1_pre_trade(_config(), _proposal(), state)

    assert decision.status == DecisionStatus.BLOCK
    assert "locked" in decision.reason.lower()
    print(f"PASS — session locked → BLOCK | reason: {decision.reason[:60]}")


def test_f1_hard_stop_blocks_and_locks_session():
    """
    Check 2: current_daily_pnl <= -hard_stop_dollars → BLOCK
    AND state.session_locked is set to True.
    """
    config   = _config()
    state    = _state()
    hard_dol = config.internal_hard_stop_pct / 100 * ACCOUNT   # 375.0
    proposal = _proposal(current_daily_pnl=-hard_dol)          # exactly at limit

    decision = ENGINE.fence1_pre_trade(config, proposal, state)

    assert decision.status == DecisionStatus.BLOCK
    assert state.session_locked is True,  "Hard stop must lock the session"
    assert state.session_ended_via_hard_stop is True
    assert "hard stop" in decision.reason.lower()
    print(
        f"PASS — hard stop hit (${hard_dol}) → BLOCK | session_locked=True"
    )


def test_f1_soft_stop_warns_and_halves_risk():
    """
    Check 3: current_daily_pnl <= -soft_stop_dollars → WARN
    AND adjusted_risk_dollars == proposed / 2.
    """
    config    = _config()
    soft_dol  = config.internal_soft_stop_pct / 100 * ACCOUNT  # 250.0
    proposed  = 100.0
    proposal  = _proposal(
        current_daily_pnl=-soft_dol,   # exactly at soft stop
        proposed_risk_dollars=proposed,
    )

    decision = ENGINE.fence1_pre_trade(config, _proposal(
        current_daily_pnl=-soft_dol,
        proposed_risk_dollars=proposed,
    ), _state())

    assert decision.status == DecisionStatus.WARN
    assert decision.adjusted_risk_dollars == proposed / 2
    assert "soft stop" in decision.reason.lower() or "halved" in decision.reason.lower()
    print(
        f"PASS — soft stop hit (${soft_dol}) → WARN | "
        f"risk halved to ${decision.adjusted_risk_dollars}"
    )


def test_f1_open_risk_ceiling_blocks():
    """
    Check 4: open_risk + adjusted_proposed > max_open_risk → BLOCK.
    With 0 daily pnl (no soft stop), adjusted == proposed.
    $100 open + $60 proposed = $160 > max $150.
    """
    config   = _config()
    proposal = _proposal(
        open_risk_dollars=100.0,
        proposed_risk_dollars=60.0,     # 100 + 60 = 160 > 150
    )
    decision = ENGINE.fence1_pre_trade(config, proposal, _state())

    assert decision.status == DecisionStatus.BLOCK
    assert "ceiling" in decision.reason.lower() or "open risk" in decision.reason.lower()
    print(
        f"PASS — open risk ceiling → BLOCK | reason: {decision.reason[:70]}"
    )


def test_f1_max_trades_blocks():
    """
    Check 5: trades_today >= max_trades_per_day → BLOCK.
    """
    config   = _config()
    state    = _state(trades_today=config.max_trades_per_day)  # 3/3
    decision = ENGINE.fence1_pre_trade(config, _proposal(), state)

    assert decision.status == DecisionStatus.BLOCK
    assert "cap" in decision.reason.lower() or "trade" in decision.reason.lower()
    print(
        f"PASS — max trades ({config.max_trades_per_day}) → BLOCK | "
        f"reason: {decision.reason[:60]}"
    )


def test_f1_short_hold_time_warns():
    """
    Check 6: estimated_hold_minutes < min_hold_seconds/60 → WARN.
    min_hold_seconds=120 → minimum 2 minutes; estimated=1 → WARN.
    """
    config   = _config()     # min_hold_seconds = 120 → 2 min
    proposal = _proposal(estimated_hold_minutes=1.0)   # 1 min < 2 min
    decision = ENGINE.fence1_pre_trade(config, proposal, _state())

    assert decision.status == DecisionStatus.WARN
    assert "hold" in decision.reason.lower()
    print(
        f"PASS — hold time 1m < 2m minimum → WARN | "
        f"reason: {decision.reason[:70]}"
    )


def test_f1_all_clear():
    """Nominal path: no limits hit → CLEAR."""
    config   = _config()
    decision = ENGINE.fence1_pre_trade(config, _proposal(), _state())

    assert decision.status == DecisionStatus.CLEAR
    assert decision.adjusted_risk_dollars == _proposal().proposed_risk_dollars
    print(f"PASS — nominal proposal → CLEAR | budget: ${decision.remaining_daily_risk}")


def test_f1_soft_stop_and_hold_time_combined():
    """
    If BOTH soft stop AND short hold time trigger, result is WARN with
    the combined reason message and halved risk.
    """
    config = _config()
    soft_dol = config.internal_soft_stop_pct / 100 * ACCOUNT
    proposal = _proposal(
        current_daily_pnl=-soft_dol,
        proposed_risk_dollars=100.0,
        estimated_hold_minutes=1.0,   # below 2-min min
    )
    decision = ENGINE.fence1_pre_trade(config, proposal, _state())

    assert decision.status == DecisionStatus.WARN
    assert decision.adjusted_risk_dollars == 50.0   # 100 / 2
    # Reason must mention both issues
    reason_lower = decision.reason.lower()
    assert "soft stop" in reason_lower or "halved" in reason_lower
    assert "hold" in reason_lower
    print(
        f"PASS — soft stop + hold warn combined → WARN | "
        f"adjusted risk ${decision.adjusted_risk_dollars}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n── Risk Guard — Engine Test Suite ──\n")
    tests = [
        test_f1_session_locked_blocks,
        test_f1_hard_stop_blocks_and_locks_session,
        test_f1_soft_stop_warns_and_halves_risk,
        test_f1_open_risk_ceiling_blocks,
        test_f1_max_trades_blocks,
        test_f1_short_hold_time_warns,
        test_f1_all_clear,
        test_f1_soft_stop_and_hold_time_combined,
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
