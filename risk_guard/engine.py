"""
Risk Guard — Decision Engine
Three fences that gate every trade through the session.

Fence 1  pre_trade()       Called before placing any trade.
Fence 2  live_equity()     Called by monitor on every equity poll.
Fence 3  session_close()   Called at session rollover.
"""

from datetime import datetime
from typing import List, Optional

from .models import DecisionStatus, Decision, FirmConfig, TradeProposal, SessionState


class RiskGuardEngine:

    # ─────────────────────────────────────────────
    # FENCE 1 — PRE-TRADE
    # ─────────────────────────────────────────────

    def fence1_pre_trade(
        self,
        config: FirmConfig,
        proposal: TradeProposal,
        state: SessionState,
    ) -> Decision:
        """
        Six sequential checks before a trade is placed.
        Returns the first failing Decision (BLOCK or WARN).
        BLOCK always wins over WARN — soft-stop halves risk but we still
        continue checking whether the adjusted proposal is safe.
        """
        account       = proposal.account_size
        soft_dollars  = config.internal_soft_stop_pct  / 100.0 * account
        hard_dollars  = config.internal_hard_stop_pct  / 100.0 * account
        max_open_dols = config.max_open_risk_pct        / 100.0 * account

        # Dynamic floor and budget helpers
        dynamic_floor     = state.equity_high - (config.daily_dd_pct / 100.0 * account)
        current_equity    = state.starting_balance + state.daily_pnl
        remaining_risk    = round(current_equity - dynamic_floor, 2)
        trades_remaining  = max(0, config.max_trades_per_day - state.trades_today)
        progress_pct      = round(state.cumulative_pnl / account * 100.0, 2)

        def _decision(status: DecisionStatus, reason: str,
                      adjusted: Optional[float] = None) -> Decision:
            return Decision(
                status=status,
                reason=reason,
                remaining_daily_risk=remaining_risk,
                trades_remaining=trades_remaining,
                session_equity_high=state.equity_high,
                dynamic_daily_floor=round(dynamic_floor, 2),
                valid_trading_days=state.valid_trading_days,
                progress_to_target_pct=progress_pct,
                adjusted_risk_dollars=adjusted,
            )

        # ── Check 1: Session locked from a prior hard-stop day ────────────────
        if state.session_locked:
            return _decision(
                DecisionStatus.BLOCK,
                "Session locked — hard stop was triggered this session. "
                "No new trades until rollover.",
            )

        # ── Revenge lock (set on rollover after hard-stop session) ────────────
        if state.revenge_locked_until:
            try:
                unlock_dt = datetime.fromisoformat(state.revenge_locked_until)
                if datetime.utcnow() < unlock_dt:
                    return _decision(
                        DecisionStatus.BLOCK,
                        f"Revenge lock active — trading resumes after "
                        f"{unlock_dt.strftime('%Y-%m-%d %H:%M UTC')}.",
                    )
            except (ValueError, TypeError):
                pass  # malformed datetime → treat as expired

        # ── Check 2: Daily PnL <= internal hard stop → BLOCK + lock ──────────
        if proposal.current_daily_pnl <= -hard_dollars:
            state.session_locked = True
            state.session_ended_via_hard_stop = True
            return _decision(
                DecisionStatus.BLOCK,
                f"Hard stop hit: daily loss ${-proposal.current_daily_pnl:,.2f} "
                f">= ${hard_dollars:,.2f} ({config.internal_hard_stop_pct}%). "
                f"Session locked until rollover.",
            )

        # ── Check 3: Daily PnL <= internal soft stop → WARN, halve risk ──────
        adjusted_risk  = proposal.proposed_risk_dollars
        soft_stop_hit  = False
        if proposal.current_daily_pnl <= -soft_dollars:
            adjusted_risk = proposal.proposed_risk_dollars / 2.0
            soft_stop_hit = True

        # ── Check 4: Open risk + adjusted proposed > max open risk ────────────
        total_open = proposal.open_risk_dollars + adjusted_risk
        if total_open > max_open_dols:
            return _decision(
                DecisionStatus.BLOCK,
                f"Open risk ceiling: ${proposal.open_risk_dollars:,.2f} open "
                f"+ ${adjusted_risk:,.2f} proposed = ${total_open:,.2f} "
                f"> max ${max_open_dols:,.2f} ({config.max_open_risk_pct}%).",
            )

        # ── Check 5: Max trades per day ────────────────────────────────────────
        if state.trades_today >= config.max_trades_per_day:
            return _decision(
                DecisionStatus.BLOCK,
                f"Daily trade cap reached: "
                f"{state.trades_today}/{config.max_trades_per_day} trades taken.",
            )

        # ── Check 6: Estimated hold time < minimum hold ───────────────────────
        min_hold_minutes = config.min_hold_seconds / 60.0
        hold_warn_triggered = (
            config.min_hold_seconds > 0
            and proposal.estimated_hold_minutes < min_hold_minutes
        )
        if hold_warn_triggered:
            parts = []
            if soft_stop_hit:
                parts.append(
                    f"Soft stop: risk halved to ${adjusted_risk:,.2f}."
                )
            parts.append(
                f"Hold time warning: estimated {proposal.estimated_hold_minutes:.0f}m "
                f"< minimum {min_hold_minutes:.0f}m ({config.min_hold_seconds}s). "
                f"Verify trade duration rules."
            )
            return _decision(DecisionStatus.WARN, " ".join(parts), adjusted_risk)

        # ── Soft stop only ─────────────────────────────────────────────────────
        if soft_stop_hit:
            return _decision(
                DecisionStatus.WARN,
                f"Soft stop: daily loss ${-proposal.current_daily_pnl:,.2f} "
                f">= ${soft_dollars:,.2f} ({config.internal_soft_stop_pct}%). "
                f"Risk halved to ${adjusted_risk:,.2f}.",
                adjusted_risk,
            )

        # ── All checks passed ──────────────────────────────────────────────────
        return _decision(
            DecisionStatus.CLEAR,
            "All pre-trade checks passed.",
            adjusted_risk,
        )

    # ─────────────────────────────────────────────
    # FENCE 2 — LIVE EQUITY MONITOR
    # ─────────────────────────────────────────────

    def fence2_live_equity(
        self,
        config: FirmConfig,
        account_size: float,
        current_equity: float,
        equity_high: float,
    ) -> Decision:
        """
        Called on every equity poll (every 30 s).
        Returns BLOCK if floor breached, WARN if within 1%, CLEAR otherwise.
        """
        dynamic_floor     = equity_high - (config.daily_dd_pct / 100.0 * account_size)
        proximity_buffer  = 0.01 * account_size
        remaining         = round(current_equity - dynamic_floor, 2)

        base = dict(
            remaining_daily_risk=remaining,
            trades_remaining=0,
            session_equity_high=equity_high,
            dynamic_daily_floor=round(dynamic_floor, 2),
            valid_trading_days=0,
            progress_to_target_pct=0.0,
        )

        if current_equity < dynamic_floor:
            return Decision(
                status=DecisionStatus.BLOCK,
                reason=(
                    f"Equity ${current_equity:,.2f} breached dynamic floor "
                    f"${dynamic_floor:,.2f}. No new trades permitted."
                ),
                **base,
            )

        if current_equity <= dynamic_floor + proximity_buffer:
            return Decision(
                status=DecisionStatus.WARN,
                reason=(
                    f"Equity ${current_equity:,.2f} is within 1% of dynamic "
                    f"floor ${dynamic_floor:,.2f}. Caution: reduce exposure."
                ),
                **base,
            )

        return Decision(
            status=DecisionStatus.CLEAR,
            reason="Equity within safe range.",
            **base,
        )

    # ─────────────────────────────────────────────
    # FENCE 3 — SESSION CLOSE
    # ─────────────────────────────────────────────

    def fence3_session_close(
        self,
        config: FirmConfig,
        state: SessionState,
        recent_sessions: List[dict],
    ) -> dict:
        """
        Called at session rollover. Returns a summary dict.
        Does NOT mutate state — the caller (monitor/rollover) applies results.
        """
        account = state.account_size

        # 1. Valid trading day
        is_valid_day = (
            config.min_valid_day_pct == 0.0
            or state.daily_pnl >= (config.min_valid_day_pct / 100.0 * account)
        )

        # 2. Progress to target
        progress_pct = round(state.cumulative_pnl / account * 100.0, 2)

        # 3. Losing streak — last 3 sessions all negative
        losing_streak = (
            len(recent_sessions) >= 3
            and all(s["session_pnl"] < 0.0 for s in recent_sessions[:3])
        )

        # 4. Revenge lock — only if session ended via hard stop
        revenge_locked      = (
            state.session_ended_via_hard_stop
            and config.revenge_lock_hours > 0
        )

        return {
            "is_valid_day":          is_valid_day,
            "valid_trading_days":    state.valid_trading_days + (1 if is_valid_day else 0),
            "progress_to_target_pct": progress_pct,
            "losing_streak":         losing_streak,
            "revenge_locked":        revenge_locked,
            "revenge_lock_hours":    config.revenge_lock_hours if revenge_locked else 0,
        }
