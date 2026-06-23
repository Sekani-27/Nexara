"""
Risk Guard — Alert Formatting & Dispatch
Three message templates: CLEAR footer, WARN (reduced size), BLOCK.
Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from environment.
"""

import logging
import os
from typing import Optional

from .models import Decision

log = logging.getLogger(__name__)

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


# ─────────────────────────────────────────────────────────────────────────────
# FORMATTERS
# ─────────────────────────────────────────────────────────────────────────────


def format_clear(signal_text: str, decision: Decision) -> str:
    """Append account-state footer to a confirmed signal alert."""
    return (
        f"{signal_text}\n\n"
        f"{_DIV}\n"
        f"📊 Risk Guard  [✅ CLEAR]\n"
        f"  Equity High  : ${decision.session_equity_high:>10,.2f}\n"
        f"  Daily Floor  : ${decision.dynamic_daily_floor:>10,.2f}\n"
        f"  Risk Budget  : ${decision.remaining_daily_risk:>10,.2f}  remaining\n"
        f"  Trades Left  : {decision.trades_remaining}\n"
        f"  Valid Days   : {decision.valid_trading_days}\n"
        f"  Target Prog  : {decision.progress_to_target_pct:.1f}%\n"
        f"{_DIV}"
    )


def format_warn(signal_text: str, decision: Decision) -> str:
    """Prepend warning + adjusted size to a signal that passed with caveats."""
    adjusted_line = ""
    if decision.adjusted_risk_dollars is not None:
        adjusted_line = f"  Adjusted Risk: ${decision.adjusted_risk_dollars:>10,.2f}\n"

    return (
        f"⚠️  Risk Guard  [WARN]\n"
        f"{_DIV}\n"
        f"{decision.reason}\n"
        f"{adjusted_line}"
        f"{_DIV}\n\n"
        f"{signal_text}\n\n"
        f"{_DIV}\n"
        f"📊 Account State\n"
        f"  Equity High  : ${decision.session_equity_high:>10,.2f}\n"
        f"  Daily Floor  : ${decision.dynamic_daily_floor:>10,.2f}\n"
        f"  Risk Budget  : ${decision.remaining_daily_risk:>10,.2f}  remaining\n"
        f"  Trades Left  : {decision.trades_remaining}\n"
        f"  Target Prog  : {decision.progress_to_target_pct:.1f}%\n"
        f"{_DIV}"
    )


def format_block(decision: Decision) -> str:
    """Full BLOCK message — no signal text included."""
    return (
        f"🔴  Risk Guard  [BLOCKED]\n"
        f"{_DIV}\n"
        f"{decision.reason}\n"
        f"{_DIV}\n"
        f"📊 Account State\n"
        f"  Daily Floor  : ${decision.dynamic_daily_floor:>10,.2f}\n"
        f"  Risk Budget  : ${decision.remaining_daily_risk:>10,.2f}\n"
        f"  Trades Left  : {decision.trades_remaining}\n"
        f"  Valid Days   : {decision.valid_trading_days}\n"
        f"  Target Prog  : {decision.progress_to_target_pct:.1f}%\n"
        f"{_DIV}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCH
# ─────────────────────────────────────────────────────────────────────────────


async def send_rg_alert(
    text: str,
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> None:
    """
    Send a Risk Guard alert via Telegram.

    Credentials resolution order:
      1. Explicit ``token`` / ``chat_id`` arguments (per-instance override).
      2. Module-level ``_TOKEN`` / ``_CHAT_ID`` (read from os.environ at import).

    Silent no-op if neither source supplies both credentials.
    """
    _tok = token or _TOKEN
    _cid = chat_id or _CHAT_ID
    if not (_tok and _cid):
        log.debug("Telegram not configured — RG alert skipped.")
        return
    try:
        from telegram import Bot

        async with Bot(token=_tok) as bot:
            await bot.send_message(chat_id=_cid, text=text)
        log.debug("RG alert sent (%d chars)", len(text))
    except Exception as exc:
        log.warning("RG Telegram send failed: %s", exc)
