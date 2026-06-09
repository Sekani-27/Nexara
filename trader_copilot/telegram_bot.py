"""
telegram_bot.py
───────────────
Telegram alert layer + conversational intelligence for Trader Copilot.

Outbound (unchanged public API)
--------------------------------
  send_alert(alert_dict)          → fire a trade alert
  send_startup_message(symbols)   → announce the feed is live

Inbound — conversational handler
----------------------------------
  handle_message(text)            → route text → keyword reply OR Groq fallback
  start_polling_thread()          → launch Application polling in a daemon thread

Keyword routing
---------------
  briefing / morning  → morning briefing (pairs, session, RG snapshot)
  account / equity    → account state summary
  signals / setups    → pending signals from journal
  risk / guard        → Risk Guard status for both accounts
  performance / week  → journal performance summary
  <pair name>         → pair-specific status (EURUSD, GOLD, GBPJPY, etc.)
  <anything else>     → Groq llama-3.1-70b-versatile with trading context

Environment (.env)
------------------
  TELEGRAM_BOT_TOKEN=<your-bot-token>
  TELEGRAM_CHAT_ID=<your-chat-id>
  GROQ_API_KEY=<your-groq-api-key>         ← required for AI fallback

If either Telegram variable is missing the outbound functions are silent no-ops.
If GROQ_API_KEY is missing the fallback returns a clear error message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# ── env ──────────────────────────────────────────────────────────────────────
# Always resolve .env relative to this file so the feed can be launched
# from any working directory.
load_dotenv(Path(__file__).parent / ".env")

_TOKEN:        str | None = os.getenv("TELEGRAM_BOT_TOKEN")
_CHAT_ID:      str | None = os.getenv("TELEGRAM_CHAT_ID")
_GROQ_API_KEY: str | None = os.getenv("GROQ_API_KEY")

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# PAIR ALIAS MAP
# Maps every recognised token (upper-cased) → canonical symbol used in journal
# ─────────────────────────────────────────────────────────────────────────────

_PAIR_ALIASES: dict[str, str] = {
    # ── Majors ──
    "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY",
    "USDCHF": "USDCHF", "AUDUSD": "AUDUSD", "USDCAD": "USDCAD",
    "NZDUSD": "NZDUSD",
    # ── Euro crosses ──
    "EURGBP": "EURGBP", "EURJPY": "EURJPY", "EURCHF": "EURCHF",
    "EURAUD": "EURAUD", "EURCAD": "EURCAD", "EURNZD": "EURNZD",
    # ── Pound crosses ──
    "GBPJPY": "GBPJPY", "GBPCHF": "GBPCHF", "GBPAUD": "GBPAUD",
    "GBPCAD": "GBPCAD", "GBPNZD": "GBPNZD",
    # ── Yen crosses ──
    "AUDJPY": "AUDJPY", "CADJPY": "CADJPY", "CHFJPY": "CHFJPY",
    "NZDJPY": "NZDJPY",
    # ── Exotics ──
    "USDZAR": "USDZAR",
    # ── Commodities / indices ──
    "XAUUSD": "XAUUSD", "NAS100": "NAS100",
    # ── Common aliases ──
    "GOLD":   "XAUUSD", "XAU":    "XAUUSD",
    "NASDAQ": "NAS100", "US100":  "NAS100", "NAS": "NAS100",
    "US30":   "US30",   "DOW":    "US30",
    "US500":  "US500",  "SPX":    "US500",
}

# Active pairs the scanner currently watches
_ACTIVE_PAIRS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "CADJPY"]


# ─────────────────────────────────────────────────────────────────────────────
# ORIGINAL HELPERS  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

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
    div   = "━━━━━━━━━━━━━━━━━━━━"
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


# ─────────────────────────────────────────────────────────────────────────────
# SESSION / MARKET HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _current_session_label() -> str:
    """Return the active trading session name for the current UTC time."""
    hour = datetime.now(timezone.utc).hour
    if 7 <= hour < 10:
        return "London Open"
    if 10 <= hour < 12:
        return "London / NY Pre-market"
    if 12 <= hour < 16:
        return "New York"
    if 0 <= hour < 3:
        return "Tokyo"
    if 22 <= hour <= 23:
        return "Sydney"
    return "Off-session"


def _market_is_open() -> bool:
    """Mirrors is_forex_market_open() from run_multi.py."""
    now = datetime.now(timezone.utc)
    if now.weekday() == 5:
        return False
    if now.weekday() == 6 and now.hour < 21:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHERS  (all defensive — return empty/default on any error)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_rg_state(env_file: Optional[str] = None) -> Optional[dict]:
    """
    Load a RiskGuard session state snapshot as a plain dict.
    Returns None if the risk_guard package or DB is unavailable.
    """
    try:
        import sys, os as _os
        # Ensure project root is on the path so 'risk_guard' can be imported
        _root = str(Path(__file__).parent.parent)
        if _root not in sys.path:
            sys.path.insert(0, _root)

        from risk_guard import RiskGuard
        rg = RiskGuard(env_file=env_file)
        s  = rg.state
        c  = rg.config
        return {
            "firm":              c.name.upper(),
            "account_size":      s.account_size,
            "session_date":      s.session_date,
            "daily_pnl":         s.daily_pnl,
            "trades_today":      s.trades_today,
            "max_trades":        c.max_trades_per_day,
            "session_locked":    s.session_locked,
            "equity_high":       s.equity_high,
            "starting_balance":  s.starting_balance,
            "cumulative_pnl":    s.cumulative_pnl,
            "valid_days":        s.valid_trading_days,
            "soft_stop_pct":     c.internal_soft_stop_pct,
            "hard_stop_pct":     c.internal_hard_stop_pct,
            "daily_dd_pct":      c.daily_dd_pct,
            "revenge_locked_until": s.revenge_locked_until,
        }
    except Exception as exc:
        log.debug("_fetch_rg_state(%s) failed: %s", env_file, exc)
        return None


def _fetch_journal_stats(symbol: Optional[str] = None) -> dict:
    """Return journal stats dict. Empty dict on any error."""
    try:
        import sys
        _root = str(Path(__file__).parent.parent)
        if _root not in sys.path:
            sys.path.insert(0, _root)

        from trader_copilot.journal.trade_journal import TradeJournal
        db_path = os.getenv("TRADE_JOURNAL_DB_PATH", "trader_copilot_journal.db")
        j = TradeJournal(db_path=db_path)
        return j.get_stats(symbol=symbol)
    except Exception as exc:
        log.debug("_fetch_journal_stats failed: %s", exc)
        return {}


def _fetch_pending_signals() -> list[dict]:
    """Return list of pending (unresolved) trades from journal."""
    try:
        import sys
        _root = str(Path(__file__).parent.parent)
        if _root not in sys.path:
            sys.path.insert(0, _root)

        from trader_copilot.journal.trade_journal import TradeJournal
        db_path = os.getenv("TRADE_JOURNAL_DB_PATH", "trader_copilot_journal.db")
        j = TradeJournal(db_path=db_path)
        return j.get_pending()
    except Exception as exc:
        log.debug("_fetch_pending_signals failed: %s", exc)
        return []


def _fetch_recent_for_pair(symbol: str, limit: int = 5) -> list[dict]:
    """Return the most recent journal entries for a specific symbol."""
    try:
        import sys
        _root = str(Path(__file__).parent.parent)
        if _root not in sys.path:
            sys.path.insert(0, _root)

        from trader_copilot.journal.trade_journal import TradeJournal
        db_path = os.getenv("TRADE_JOURNAL_DB_PATH", "trader_copilot_journal.db")
        j  = TradeJournal(db_path=db_path)
        rows = j.get_by_symbol(symbol)
        return rows[:limit]
    except Exception as exc:
        log.debug("_fetch_recent_for_pair(%s) failed: %s", symbol, exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# RESPONSE BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

_DIV  = "━━━━━━━━━━━━━━━━━━━━"
_DIV2 = "────────────────────"


def _build_briefing() -> str:
    """Morning / session briefing."""
    now     = datetime.now(timezone.utc)
    session = _current_session_label()
    status  = "🟢 OPEN" if _market_is_open() else "🔴 CLOSED"
    rg      = _fetch_rg_state()          # default account
    rg_sb   = _fetch_rg_state(".env.sbonelo")   # Sbonelo account

    lines = [
        "🌅 TRADER COPILOT — MORNING BRIEFING",
        _DIV,
        f"Time       : {now.strftime('%Y-%m-%d %H:%M')} UTC",
        f"Session    : {session}",
        f"Market     : {status}",
        _DIV,
        f"Scanning   : {', '.join(_ACTIVE_PAIRS)}",
        _DIV,
    ]

    # Risk Guard snapshot — default account
    if rg:
        pnl_sign = "+" if rg["daily_pnl"] >= 0 else ""
        locked   = "🔒 LOCKED" if rg["session_locked"] else "✅ active"
        lines += [
            f"ACCOUNT ({rg['firm']})",
            f"  Session PnL  : {pnl_sign}{rg['daily_pnl']:.2f}",
            f"  Trades today : {rg['trades_today']}/{rg['max_trades']}",
            f"  Session      : {locked}",
        ]
    else:
        lines.append("ACCOUNT        : data unavailable")

    # Sbonelo account
    if rg_sb:
        pnl_sign = "+" if rg_sb["daily_pnl"] >= 0 else ""
        locked   = "🔒 LOCKED" if rg_sb["session_locked"] else "✅ active"
        lines += [
            _DIV2,
            f"SBONELO ({rg_sb['firm']})",
            f"  Session PnL  : {pnl_sign}{rg_sb['daily_pnl']:.2f}",
            f"  Trades today : {rg_sb['trades_today']}/{rg_sb['max_trades']}",
            f"  Session      : {locked}",
        ]

    # Pending signals
    pending = _fetch_pending_signals()
    lines.append(_DIV)
    if pending:
        lines.append(f"ACTIVE SETUPS  : {len(pending)} pending")
        for s in pending[:3]:   # show at most 3
            dir_emoji = "🟢" if s.get("direction") == "bullish" else "🔴"
            lines.append(
                f"  {dir_emoji} {s.get('symbol','?')} — {s.get('pattern','?')} "
                f"| entry {s.get('entry_price', 0):.5f}"
            )
        if len(pending) > 3:
            lines.append(f"  …and {len(pending) - 3} more. Type 'signals' for full list.")
    else:
        lines.append("ACTIVE SETUPS  : none pending")

    lines.append(_DIV)
    return "\n".join(lines)


def _build_account_summary() -> str:
    """Account / equity state for both instances."""
    rg    = _fetch_rg_state()
    rg_sb = _fetch_rg_state(".env.sbonelo")
    lines = ["💼 ACCOUNT STATE", _DIV]

    def _rg_block(label: str, r: dict) -> list[str]:
        pnl_sign    = "+" if r["daily_pnl"] >= 0 else ""
        cum_sign    = "+" if r["cumulative_pnl"] >= 0 else ""
        locked      = "🔒 SESSION LOCKED" if r["session_locked"] else "✅ Trading"
        soft_floor  = r["account_size"] * r["soft_stop_pct"] / 100
        hard_floor  = r["account_size"] * r["hard_stop_pct"] / 100
        dd_used_pct = abs(r["daily_pnl"]) / r["account_size"] * 100 if r["daily_pnl"] < 0 else 0
        return [
            f"{label}  ({r['firm']})",
            f"  Account      : ${r['account_size']:,.0f}",
            f"  Balance      : ${r['starting_balance']:,.2f}",
            f"  Equity high  : ${r['equity_high']:,.2f}",
            f"  Session PnL  : {pnl_sign}${r['daily_pnl']:,.2f}  ({pnl_sign}{r['daily_pnl']/r['account_size']*100:.2f}%)",
            f"  DD used      : {dd_used_pct:.2f}%  (soft@{r['soft_stop_pct']}% / hard@{r['hard_stop_pct']}%)",
            f"  Trades today : {r['trades_today']}/{r['max_trades']}",
            f"  Valid days   : {r['valid_days']}",
            f"  Cumulative   : {cum_sign}${r['cumulative_pnl']:,.2f}",
            f"  Status       : {locked}",
        ]

    if rg:
        lines += _rg_block("DEFAULT ACCOUNT", rg)
    else:
        lines.append("DEFAULT ACCOUNT : data unavailable")

    if rg_sb:
        lines.append(_DIV2)
        lines += _rg_block("SBONELO ACCOUNT", rg_sb)
    else:
        lines += [_DIV2, "SBONELO ACCOUNT : data unavailable"]

    lines.append(_DIV)
    return "\n".join(lines)


def _build_signals_summary() -> str:
    """Active / pending signals from journal."""
    pending = _fetch_pending_signals()
    lines   = ["📡 ACTIVE SETUPS", _DIV]

    if not pending:
        lines.append("No pending setups in journal.")
        lines.append("The scanner fires alerts as setups are confirmed.")
        lines.append(_DIV)
        return "\n".join(lines)

    lines.append(f"Found {len(pending)} pending setup(s):\n")
    for i, s in enumerate(pending, 1):
        dir_emoji = "🟢 BUY" if s.get("direction") == "bullish" else "🔴 SELL"
        lines += [
            f"{i}. {s.get('symbol','?')} — {dir_emoji}",
            f"   Pattern  : {s.get('pattern','?')}",
            f"   Entry    : {s.get('entry_price', 0):.5f}",
            f"   SL       : {s.get('stop_loss', 0):.5f}",
            f"   Score    : {s.get('confluence_score','?')}/5",
            f"   Logged   : {s.get('signal_time','?')[:16]}",
            "",
        ]

    lines.append(_DIV)
    return "\n".join(lines)


def _build_risk_guard_summary() -> str:
    """Risk Guard fence status for both accounts."""
    rg    = _fetch_rg_state()
    rg_sb = _fetch_rg_state(".env.sbonelo")
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["🛡 RISK GUARD STATUS", _DIV, f"As of {now}", _DIV]

    def _fence_block(label: str, r: dict) -> list[str]:
        daily_loss_pct = abs(r["daily_pnl"]) / r["account_size"] * 100 if r["daily_pnl"] < 0 else 0
        soft_hit = daily_loss_pct >= r["soft_stop_pct"]
        hard_hit = daily_loss_pct >= r["hard_stop_pct"]
        trades_ok = r["trades_today"] < r["max_trades"]

        if r["session_locked"]:
            status_icon = "🔴 BLOCKED"
        elif hard_hit:
            status_icon = "🔴 HARD STOP"
        elif soft_hit:
            status_icon = "🟡 SOFT STOP (size halved)"
        elif not trades_ok:
            status_icon = "🟡 TRADE CAP REACHED"
        else:
            status_icon = "🟢 CLEAR"

        revenge = f"  Revenge lock : until {r['revenge_locked_until'][:16]}" if r["revenge_locked_until"] else ""
        out = [
            f"{label}  ({r['firm']})",
            f"  Fence status : {status_icon}",
            f"  Daily DD     : {daily_loss_pct:.2f}% / limit {r['daily_dd_pct']}%",
            f"  Soft stop    : {r['soft_stop_pct']}%  {'⚡ TRIGGERED' if soft_hit else 'OK'}",
            f"  Hard stop    : {r['hard_stop_pct']}%  {'⚡ TRIGGERED' if hard_hit else 'OK'}",
            f"  Trades       : {r['trades_today']}/{r['max_trades']}  {'⚡ CAP HIT' if not trades_ok else 'OK'}",
        ]
        if revenge:
            out.append(revenge)
        return out

    if rg:
        lines += _fence_block("DEFAULT ACCOUNT", rg)
    else:
        lines.append("DEFAULT ACCOUNT : Risk Guard data unavailable")

    if rg_sb:
        lines.append(_DIV2)
        lines += _fence_block("SBONELO ACCOUNT", rg_sb)
    else:
        lines += [_DIV2, "SBONELO ACCOUNT : Risk Guard data unavailable"]

    lines.append(_DIV)
    return "\n".join(lines)


def _build_performance_summary() -> str:
    """Journal performance summary (all-time + this week)."""
    stats = _fetch_journal_stats()
    lines = ["📊 PERFORMANCE SUMMARY", _DIV]

    if not stats or "message" in stats:
        lines.append("No completed trades in journal yet.")
        lines.append(_DIV)
        return "\n".join(lines)

    total_r_sign = "+" if stats.get("total_r", 0) >= 0 else ""
    lines += [
        f"Total trades  : {stats.get('total_trades', 0)}",
        f"Win rate      : {stats.get('win_rate_pct', 0):.1f}%  "
        f"({stats.get('wins', 0)}W / {stats.get('losses', 0)}L / {stats.get('breakeven', 0)}BE)",
        f"Avg RR        : {stats.get('avg_rr', 0):+.2f}R",
        f"Total R       : {total_r_sign}{stats.get('total_r', 0):.2f}R",
        _DIV2,
    ]

    # Score breakdown
    sb = stats.get("score_breakdown", {})
    if sb:
        lines.append("Score breakdown:")
        for score, v in sorted(sb.items()):
            lines.append(f"  Score {score}/5  : {v['win_rate']:.0f}% WR  ({v['total']} trades)")

    # Pattern breakdown
    pb = stats.get("pattern_breakdown", {})
    if pb:
        lines.append(_DIV2)
        lines.append("Pattern breakdown:")
        for pat, v in pb.items():
            lines.append(f"  {pat[:28]:<28} : {v['win_rate']:.0f}% WR  ({v['total']})")

    # FVG edge
    fvg_wr    = stats.get("fvg_present_wr")
    no_fvg_wr = stats.get("fvg_absent_wr")
    if fvg_wr is not None and no_fvg_wr is not None:
        lines += [
            _DIV2,
            f"FVG confluence : {fvg_wr:.0f}% WR vs {no_fvg_wr:.0f}% without",
        ]

    lines.append(_DIV)
    return "\n".join(lines)


def _build_pair_summary(symbol: str) -> str:
    """Status and recent history for a single pair."""
    stats  = _fetch_journal_stats(symbol=symbol)
    recent = _fetch_recent_for_pair(symbol, limit=5)
    pending = [t for t in _fetch_pending_signals() if t.get("symbol") == symbol]

    lines = [f"🔍 {symbol} — PAIR STATUS", _DIV]

    # Active setup
    if pending:
        s = pending[0]
        dir_label = "BUY" if s.get("direction") == "bullish" else "SELL"
        dir_emoji = "🟢" if dir_label == "BUY" else "🔴"
        lines += [
            f"ACTIVE SETUP   : {dir_emoji} {dir_label}",
            f"  Pattern      : {s.get('pattern','?')}",
            f"  Entry        : {s.get('entry_price', 0):.5f}",
            f"  SL           : {s.get('stop_loss', 0):.5f}",
            f"  Score        : {s.get('confluence_score','?')}/5",
            _DIV2,
        ]
    else:
        lines += ["ACTIVE SETUP   : none pending", _DIV2]

    # Stats
    if stats and "message" not in stats:
        total_r_sign = "+" if stats.get("total_r", 0) >= 0 else ""
        lines += [
            f"JOURNAL STATS  ({stats.get('total_trades', 0)} trades)",
            f"  Win rate     : {stats.get('win_rate_pct', 0):.1f}%",
            f"  Avg RR       : {stats.get('avg_rr', 0):+.2f}R",
            f"  Total R      : {total_r_sign}{stats.get('total_r', 0):.2f}R",
            _DIV2,
        ]
    else:
        lines += ["JOURNAL STATS  : no completed trades yet", _DIV2]

    # Recent trades
    if recent:
        lines.append("RECENT TRADES:")
        for t in recent:
            outcome_map = {
                "tp_hit": "✅TP", "sl_hit": "❌SL",
                "manual_win": "✅WIN", "manual_loss": "❌LOSS",
                "breakeven": "⚖️BE", "pending": "⏳",
            }
            outcome = outcome_map.get(t.get("outcome", "pending"), "?")
            direction = "↑" if t.get("direction") == "bullish" else "↓"
            pnl = t.get("pnl_rr")
            pnl_str = f"  {pnl:+.2f}R" if pnl is not None else ""
            lines.append(
                f"  {direction} {t.get('pattern','?')[:22]:<22} {outcome}{pnl_str}"
                f"  {str(t.get('signal_time',''))[:10]}"
            )
    else:
        lines.append("RECENT TRADES  : none in journal")

    lines.append(_DIV)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# GROQ CONVERSATIONAL FALLBACK
# ─────────────────────────────────────────────────────────────────────────────

def _groq_system_prompt() -> str:
    """Build a context-rich system prompt injected into every Groq call."""
    now     = datetime.now(timezone.utc)
    session = _current_session_label()
    market  = "OPEN" if _market_is_open() else "CLOSED (weekend)"
    pairs   = ", ".join(_ACTIVE_PAIRS)

    # Pull a compact RG snapshot to give the AI real account context
    rg = _fetch_rg_state()
    rg_ctx = ""
    if rg:
        pnl_sign = "+" if rg["daily_pnl"] >= 0 else ""
        locked   = "LOCKED" if rg["session_locked"] else "active"
        rg_ctx = (
            f"\nRisk Guard ({rg['firm']}): session {locked}, "
            f"daily PnL {pnl_sign}${rg['daily_pnl']:.2f}, "
            f"{rg['trades_today']}/{rg['max_trades']} trades today."
        )

    return f"""You are Trader Copilot, an intelligent trading assistant for a professional SMC (Smart Money Concepts) forex and commodities trader.

Current context:
- UTC time: {now.strftime('%Y-%m-%d %H:%M')} ({now.strftime('%A')})
- Active session: {session}
- Market status: {market}
- Scanning pairs: {pairs}
- Strategy: Smart Money Concepts — Breakout & Retest on 30M, BOS/CHoCH, FVG, Order Blocks, sweep detection
- Risk framework: Prop-firm compliance — internal soft stop (halve size), hard stop (lock session), max daily DD, daily trade cap, revenge-trade lock{rg_ctx}

Your role:
- Answer concisely and precisely, like a professional SMC trader
- Reference actual strategy concepts (BOS, FVG, OB, sweep, killzone, confluence score)
- For trade ideas, always mention entry, SL placement, and RR expectation
- Do not give generic financial advice — be specific to the SMC context
- Keep responses under 350 words unless the user asks for detail
- Use plain text suitable for Telegram (no markdown ##-headers, minimal symbols)
- If asked about a specific pair, state whether it is actively scanned or not
- Acknowledge the current session and whether the market is open

Respond directly without preamble."""


def _groq_chat(user_message: str) -> str:
    """
    Send user_message to Groq (llama-3.1-70b-versatile) with trading context.
    Returns the reply text, or a clear error string on failure.
    Uses stdlib urllib — no extra dependency.
    """
    api_key = _GROQ_API_KEY or os.getenv("GROQ_API_KEY")
    if not api_key:
        return (
            "⚠️ GROQ_API_KEY is not configured.\n"
            "Add it to Railway env vars or your .env file to enable AI responses."
        )

    url     = "https://api.groq.com/openai/v1/chat/completions"
    payload = json.dumps({
        "model":       "llama-3.1-70b-versatile",
        "messages": [
            {"role": "system",  "content": _groq_system_prompt()},
            {"role": "user",    "content": user_message},
        ],
        "temperature": 0.65,
        "max_tokens":  500,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"].strip()

    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = "<unreadable>"
        log.error("Groq API HTTP %d: %s", exc.code, err_body[:300])
        if exc.code == 401:
            return "⚠️ Groq API key rejected (HTTP 401). Check GROQ_API_KEY."
        if exc.code == 429:
            return "⚠️ Groq rate limit hit. Try again in a moment."
        return f"⚠️ Groq API error (HTTP {exc.code}) — check logs."

    except urllib.error.URLError as exc:
        log.error("Groq network error: %s", exc.reason)
        return "⚠️ Could not reach Groq API — check network connectivity."

    except (KeyError, json.JSONDecodeError) as exc:
        log.error("Groq response parse error: %s", exc)
        return "⚠️ Unexpected response from Groq — check logs."

    except Exception as exc:
        log.error("Groq unexpected error: %s", exc, exc_info=True)
        return "⚠️ AI response unavailable — check logs."


# ─────────────────────────────────────────────────────────────────────────────
# KEYWORD ROUTER
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pair(tokens: list[str]) -> Optional[str]:
    """
    Return the first recognised pair symbol found in the token list.
    Tokens should already be upper-cased.
    """
    for tok in tokens:
        if tok in _PAIR_ALIASES:
            return _PAIR_ALIASES[tok]
    return None


async def handle_message(text: str) -> str:
    """
    Main conversational dispatcher.

    Routing priority (first match wins):
      1. Recognised pair token anywhere in the message → pair summary
      2. Keyword: briefing / morning                   → morning briefing
      3. Keyword: account / equity / balance           → account summary
      4. Keyword: signal / setup / alert               → active signals
      5. Keyword: risk / guard / fence                 → Risk Guard status
      6. Keyword: performance / stats / week / journal → performance summary
      7. Keyword: help                                 → command list
      8. Fallback: Groq llama-3.1-70b-versatile        → AI response
    """
    normalised = text.strip().lower()
    tokens     = [t.upper() for t in text.strip().split()]

    # ── 1. Pair name anywhere in message ─────────────────────────────────────
    pair = _extract_pair(tokens)
    if pair:
        return _build_pair_summary(pair)

    # ── 2-7. Keyword checks ───────────────────────────────────────────────────
    if any(kw in normalised for kw in ("briefing", "morning", "good morning", "gm")):
        return _build_briefing()

    if any(kw in normalised for kw in ("account", "equity", "balance", "pnl")):
        return _build_account_summary()

    if any(kw in normalised for kw in ("signal", "setup", "alert", "setups", "alerts")):
        return _build_signals_summary()

    if any(kw in normalised for kw in ("risk", "guard", "fence", "drawdown", "dd")):
        return _build_risk_guard_summary()

    if any(kw in normalised for kw in ("performance", "stats", "week", "journal", "history")):
        return _build_performance_summary()

    if any(kw in normalised for kw in ("help", "commands", "what can you")):
        return _build_help()

    # ── 8. Groq fallback ─────────────────────────────────────────────────────
    log.info("handle_message: no keyword match — routing to Groq for: %r", text[:80])
    return _groq_chat(text)


def _build_help() -> str:
    div = "━━━━━━━━━━━━━━━━━━━━"
    return "\n".join([
        "🤖 TRADER COPILOT — COMMANDS",
        div,
        "briefing / morning  → session briefing",
        "account / equity    → account & PnL state",
        "signals / setups    → active pending setups",
        "risk / guard        → Risk Guard fence status",
        "performance / week  → journal stats",
        "<pair>              → pair deep-dive",
        "                      e.g. EURUSD, GOLD, GBPJPY",
        div,
        "Anything else is sent to the AI assistant (Groq).",
        "Example: 'explain BOS vs iCHoCH'",
        div,
    ])


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM APPLICATION — incoming message handler + polling setup
# ─────────────────────────────────────────────────────────────────────────────

async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Telegram Application callback for all incoming text messages.
    Passes the text through handle_message() and sends the reply back.
    """
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    if not user_text:
        return

    user_name = (
        update.message.from_user.first_name
        if update.message.from_user
        else "User"
    )
    log.info("Incoming message from %s: %r", user_name, user_text[:80])

    try:
        reply = await handle_message(user_text)
    except Exception as exc:
        log.error("handle_message raised: %s", exc, exc_info=True)
        reply = "⚠️ An internal error occurred — check the scanner logs."

    await update.message.reply_text(reply)


def start_polling_thread() -> threading.Thread:
    """
    Launch the Telegram Application in polling mode inside a daemon thread.

    This lets run_multi.py start the bot alongside the scan loop without
    blocking the main thread:

        from trader_copilot.telegram_bot import start_polling_thread
        start_polling_thread()          # non-blocking
        run(pairs=ALL_PAIRS, ...)       # your existing scan loop

    Returns the Thread so the caller can join() it if needed.
    The thread is a daemon — it exits automatically when the main process ends.
    """
    if os.getenv("ENABLE_TELEGRAM_POLLING", "").lower() != "true":
        log.info("Telegram polling disabled — set ENABLE_TELEGRAM_POLLING=true to enable.")
        return None

    if not _TOKEN:
        log.warning("start_polling_thread: TELEGRAM_BOT_TOKEN not set — polling disabled.")
        return threading.Thread(target=lambda: None, daemon=True)  # no-op thread

    def _run():
        # app.run_polling() registers OS signal handlers which only work on the
        # main thread — calling it from a daemon thread raises:
        #   RuntimeError: set_wakeup_fd only works in main thread of the main interpreter
        # Instead we drive the Application lifecycle manually so no signal
        # handlers are ever registered.
        app = Application.builder().token(_TOKEN).build()   # type: ignore[arg-type]
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message)
        )

        async def _poll():
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            log.info("Telegram polling started — listening for inbound messages.")
            # Sleep indefinitely; the daemon thread is killed when the process exits.
            while True:
                await asyncio.sleep(3600)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_poll())

    t = threading.Thread(target=_run, name="telegram-polling", daemon=True)
    t.start()
    return t


# ─────────────────────────────────────────────────────────────────────────────
# CORE SEND  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

async def _send(text: str) -> None:
    """Internal coroutine — sends a plain-text message to _CHAT_ID."""
    if not _bot_ready():
        return
    try:
        async with Bot(token=_TOKEN) as bot:    # type: ignore[arg-type]
            await bot.send_message(
                chat_id=_CHAT_ID,
                text=text,
                parse_mode=None,
            )
        log.debug("Telegram message sent OK (%d chars)", len(text))
    except Exception:   # noqa: BLE001
        log.exception("Telegram send failed — full traceback:")


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

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
    except Exception as exc:    # noqa: BLE001
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
    except Exception as exc:    # noqa: BLE001
        log.warning("send_startup_message error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE SMOKE-TEST
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Quick smoke-test — run directly to verify credentials, formatting, and the
    conversational handler:
        python telegram_bot.py
        python telegram_bot.py --poll       # also start polling loop
    """
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    async def _smoke() -> None:
        print("── Keyword handler previews ──\n")

        test_messages = [
            "morning briefing",
            "account",
            "signals",
            "risk guard",
            "performance",
            "EURUSD",
            "GOLD",
            "help",
        ]

        for msg in test_messages:
            print(f">>> {msg}")
            reply = await handle_message(msg)
            print(reply)
            print()

        # Groq fallback (only if key is set)
        if _GROQ_API_KEY:
            print(">>> explain what an FVG is")
            reply = await handle_message("explain what an FVG is")
            print(reply)
            print()
        else:
            print("GROQ_API_KEY not set — skipping AI fallback test.\n")

        # Outbound alert test
        if not _bot_ready():
            print("⚠️  Telegram credentials not set — skipping live send.")
            print("\n── Alert format preview ──")
            print(_build_startup_text(["GBPUSD", "EURUSD", "XAUUSD"]))
        else:
            await send_startup_message(["GBPUSD", "EURUSD", "XAUUSD"])
            await send_alert({
                "symbol": "GBPUSD", "direction": "SELL",
                "pattern": "Breakout & Retest", "session": "London",
                "confidence": 82, "entry": 1.2681, "stop_loss": 1.2714,
                "take_profit": 1.2615, "rr": "2.4",
                "why": "BOS confirmed 1H. Sweep at Asian low. FVG at retest.",
                "memory": "9 similar — 8W 1L.",
                "news_warning": "CPI in 47min — reduce size",
            })
            print("Outbound messages sent — check your Telegram chat.")

    asyncio.run(_smoke())

    if "--poll" in sys.argv:
        print("\nStarting polling loop (Ctrl-C to stop)…")
        t = start_polling_thread()
        t.join()
