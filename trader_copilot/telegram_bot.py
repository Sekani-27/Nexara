"""
telegram_bot.py
───────────────
Telegram alert layer for Trader Copilot.

Public API
----------
  send_alert(alert_dict)          → fire a trade alert
  send_startup_message(symbols)   → announce the feed is live

Wire-in points (mt5_feed.py)
-----------------------------
  On feed start:
      from telegram_bot import send_startup_message
      asyncio.run(send_startup_message(symbols))   # or await inside async ctx

  On confirmed alert:
      from telegram_bot import send_alert
      asyncio.run(send_alert(alert))               # or await inside async ctx

Environment (.env)
------------------
  TELEGRAM_BOT_TOKEN=<your-bot-token>
  TELEGRAM_CHAT_ID=<your-chat-id>

If either variable is missing the module is a silent no-op.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode

# ── env ──────────────────────────────────────────────────────────────────────
# Always resolve .env relative to this file, so the feed can be launched
# from any working directory (e.g. the parent folder) and still find creds.

load_dotenv(Path(__file__).parent / ".env")

_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
_CHAT_ID: str | None = os.getenv("TELEGRAM_CHAT_ID")

log = logging.getLogger(__name__)

# ── helpers ───────────────────────────────────────────────────────────────────

def _bot_ready() -> bool:
    """Return True only when both credentials are present."""
    return bool(_TOKEN and _CHAT_ID)


def _grade_label(confidence: int) -> str:
    """Map a 0-100 confidence score to a letter grade."""
    if confidence >= 90:
        return "S-grade"
    if confidence >= 80:
        return "A-grade"
    if confidence >= 70:
        return "B-grade"
    if confidence >= 60:
        return "C-grade"
    return "D-grade"


def _build_alert_text(alert: dict[str, Any]) -> str:
    """
    Format an alert dict into the standard Trader Copilot message.

    Expected keys (all optional — missing values render as '—'):
        symbol        str     e.g. "GBPUSD"
        direction     str     "BUY" | "SELL"
        pattern       str     e.g. "Breakout & Retest"
        session       str     e.g. "London"
        confidence    int     0-100
        entry         float
        stop_loss     float
        take_profit   float
        rr            str     e.g. "2.4"  (ratio after the colon)
        why           str     narrative reasoning
        memory        str     e.g. "9 similar — 8W 1L. ..."
        news_warning  str     e.g. "CPI in 47min — reduce size"
    """
    symbol     = alert.get("symbol", "—")
    direction  = str(alert.get("direction", "—")).upper()
    pattern    = alert.get("pattern", "—")
    session    = alert.get("session", "—")
    confidence = int(alert.get("confidence", 0))
    entry      = alert.get("entry")
    sl         = alert.get("stop_loss")
    tp         = alert.get("take_profit")
    rr_val     = alert.get("rr", "—")
    why        = alert.get("why", "—")
    memory     = alert.get("memory", "—")
    news_warn  = alert.get("news_warning", "")

    emoji  = "🟢" if direction == "BUY" else "🔴"
    grade  = _grade_label(confidence)
    div    = "━━━━━━━━━━━━━━━━━━━━"

    entry_str = f"{entry:.4f}" if isinstance(entry, (int, float)) else str(entry or "—")
    sl_str    = f"{sl:.4f}"    if isinstance(sl,    (int, float)) else str(sl    or "—")
    tp_str    = f"{tp:.4f}"    if isinstance(tp,    (int, float)) else str(tp    or "—")
    rr_str    = f"1 : {rr_val}" if rr_val != "—" else "—"

    lines = [
        f"{emoji} {symbol} — {direction}",
        div,
        f"Pattern:     {pattern}",
        f"Session:     {session}",
        f"Confidence:  {confidence}/100  ({grade})",
        div,
        f"Entry:       {entry_str}",
        f"Stop Loss:   {sl_str}",
        f"Take Profit: {tp_str}",
        f"RR:          {rr_str}",
        div,
        f"WHY: {why}",
        f"MEMORY: {memory}",
        div,
    ]

    if news_warn:
        lines.append(f"⚠️ {news_warn}")

    return "\n".join(lines)


def _build_startup_text(symbols: list[str]) -> str:
    div = "━━━━━━━━━━━━━━━━━━━━"
    count = len(symbols)
    return "\n".join([
        "🤖 Trader Copilot is live",
        div,
        f"Watching: {count} symbol{'s' if count != 1 else ''}",
        "Memory:   Qdrant active",
        "Mode:     Alert only",
        div,
        "Waiting for setups...",
    ])


# ── core send ─────────────────────────────────────────────────────────────────

async def _send(text: str) -> None:
    """Internal coroutine — sends a plain-text message to _CHAT_ID."""
    if not _bot_ready():
        return
    try:
        async with Bot(token=_TOKEN) as bot:  # type: ignore[arg-type]
            await bot.send_message(
                chat_id=_CHAT_ID,
                text=text,
                # Use Markdown for monospace feel but keep the body plain
                parse_mode=None,
            )
        log.debug("Telegram message sent OK (%d chars)", len(text))
    except Exception:  # noqa: BLE001
        log.exception("Telegram send failed — full traceback:")


# ── public API ────────────────────────────────────────────────────────────────

async def send_alert(alert_dict: dict[str, Any]) -> None:
    """
    Fire a formatted trade alert to the configured Telegram chat.

    Usage in an async context:
        await send_alert(alert)

    Usage in a sync context (e.g. from mt5_feed.py callback):
        asyncio.run(send_alert(alert))

    Silently no-ops if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.
    """
    try:
        text = _build_alert_text(alert_dict)
        await _send(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("send_alert error: %s", exc)


async def send_startup_message(symbols: list[str]) -> None:
    """
    Announce that the feed is live.

    Usage in an async context:
        await send_startup_message(symbols)

    Usage in a sync context:
        asyncio.run(send_startup_message(symbols))

    Silently no-ops if credentials are missing.
    """
    try:
        text = _build_startup_text(symbols)
        await _send(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("send_startup_message error: %s", exc)


# ── standalone smoke-test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Quick smoke-test — run directly to verify credentials & formatting:
        python telegram_bot.py
    """
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not _bot_ready():
        print("⚠️  TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env — skipping send.")
        print("\n── Startup message preview ──")
        print(_build_startup_text(["GBPUSD", "EURUSD", "XAUUSD"]))
        print("\n── Alert preview ──")
        sample_alert = {
            "symbol": "GBPUSD",
            "direction": "SELL",
            "pattern": "Breakout & Retest",
            "session": "London",
            "confidence": 82,
            "entry": 1.2681,
            "stop_loss": 1.2714,
            "take_profit": 1.2615,
            "rr": "2.4",
            "why": "BOS confirmed 1H. Sweep at Asian low. FVG at retest. 4H OB confluence.",
            "memory": "9 similar — 8W 1L. Failure: weak displacement. Now: strong.",
            "news_warning": "CPI in 47min — reduce size",
        }
        print(_build_alert_text(sample_alert))
        sys.exit(0)

    async def _smoke() -> None:
        await send_startup_message(["GBPUSD", "EURUSD", "XAUUSD"])
        await send_alert({
            "symbol": "GBPUSD",
            "direction": "SELL",
            "pattern": "Breakout & Retest",
            "session": "London",
            "confidence": 82,
            "entry": 1.2681,
            "stop_loss": 1.2714,
            "take_profit": 1.2615,
            "rr": "2.4",
            "why": "BOS confirmed 1H. Sweep at Asian low. FVG at retest. 4H OB confluence.",
            "memory": "9 similar — 8W 1L. Failure: weak displacement. Now: strong.",
            "news_warning": "CPI in 47min — reduce size",
        })
        print("Done — check your Telegram chat.")

    asyncio.run(_smoke())
