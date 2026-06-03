"""
Risk Guard — Data Models
All dataclasses used across the Risk Guard module.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────────────────────

class DecisionStatus(Enum):
    CLEAR = "CLEAR"
    WARN  = "WARN"
    BLOCK = "BLOCK"


# ─────────────────────────────────────────────────────────────────────────────
# FIRM CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FirmConfig:
    """All parameters for a single prop-firm account."""
    name: str

    # Draw-down limits
    daily_dd_pct:         float   # Firm's daily draw-down % (e.g. 5.0)
    total_dd_pct:         float   # Firm's total draw-down % (e.g. 10.0)
    target_pct:           float   # Profit target % (e.g. 10.0)
    dd_calc:              str     # "rolling_equity_high" | "static_balance"

    # Rollover
    rollover_time:        str     # "HH:MM" wall-clock in rollover_tz
    rollover_tz:          str     # IANA timezone string, e.g. "US/Eastern"

    # Trade rules
    min_hold_seconds:     int     # Minimum hold duration per trade
    min_valid_day_pct:    float   # Min daily PnL% to count as a valid day
    min_valid_days:       int     # Min valid days required before evaluation

    # News / weekend
    news:                 bool    # True = news-filter rule applies
    weekend:              bool    # True = weekend-holding rule applies

    # Internal thresholds (tighter than firm limits)
    internal_soft_stop_pct:  float  # Warn + halve risk at this daily loss %
    internal_hard_stop_pct:  float  # Block + lock session at this daily loss %

    # Position sizing
    risk_per_trade_pct:   float   # Max risk per trade as % of account
    max_open_risk_pct:    float   # Max total open risk as % of account
    max_trades_per_day:   int     # Hard cap on daily trade count
    revenge_lock_hours:   int     # Hours to lock after hard-stop session


# ─────────────────────────────────────────────────────────────────────────────
# TRADE PROPOSAL  (input to Fence 1)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeProposal:
    """Snapshot of the proposed trade and current account state."""
    firm:                   str
    account_size:           float   # Notional account size in $
    proposed_risk_dollars:  float   # $ at risk on this trade
    current_daily_pnl:      float   # Realised daily P&L so far (negative = loss)
    open_risk_dollars:      float   # $ currently at risk in open positions
    trades_today:           int     # Number of trades already taken today
    estimated_hold_minutes: float   # Expected trade duration in minutes


# ─────────────────────────────────────────────────────────────────────────────
# DECISION  (output of all fences)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Decision:
    """Risk Guard verdict for a trade proposal or equity event."""
    status:                 DecisionStatus
    reason:                 str
    remaining_daily_risk:   float   # $ budget before hitting dynamic floor
    trades_remaining:       int     # Trades left before daily cap
    session_equity_high:    float   # Equity high-water mark this session
    dynamic_daily_floor:    float   # Current floor = equity_high - daily_dd
    valid_trading_days:     int     # Accumulated valid trading days
    progress_to_target_pct: float   # Cumulative PnL / account_size * 100
    adjusted_risk_dollars:  Optional[float] = None  # Set on WARN (soft stop)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE  (persisted to SQLite)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    """All mutable state for one firm's current trading session."""
    firm:                       str
    session_date:               str     # ISO date "YYYY-MM-DD"
    account_size:               float   # Notional account size
    starting_balance:           float   # Balance at session open
    equity_high:                float   # Rolling equity high-water mark
    daily_pnl:                  float   # Realised P&L this session
    trades_today:               int     # Trades taken this session
    valid_trading_days:         int     # Cumulative valid days (all sessions)
    session_locked:             bool    # True if hard stop hit this session
    revenge_locked_until:       Optional[str]   # ISO datetime or None
    session_ended_via_hard_stop: bool   # True if hard stop ended last session
    cumulative_pnl:             float   # Total P&L across all sessions
    last_updated:               str     # ISO datetime of last write
