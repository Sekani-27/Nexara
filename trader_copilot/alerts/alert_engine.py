"""
Trader Copilot — Alert Engine
Formats and dispatches trade alerts.
Supports console output, Telegram (Bot API), webhook (Discord), and log file.

Telegram delivery
-----------------
Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as environment variables (or in
.env).  The engine reads them at module load time and logs clearly whether
they are present or missing — no silent failures.

Webhook (Discord / generic)
---------------------------
Pass webhook_urls=[...] to AlertEngine and the alert text is POSTed as
{"text": "<message>"} to each URL.  This is a secondary channel; Telegram
via the Bot API is the primary live-signal path.
"""

import json
import logging
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import List, Optional
from ..core.structures import TradeSignal, Direction

try:
    import risk_guard as _rg  # optional Risk Guard integration — see dispatch()
except ImportError:
    _rg = None

logger = logging.getLogger("trader_copilot.alerts")

# ── Telegram credentials — read once at import time ───────────────────────────
# Loaded from environment (Railway injects these as process env vars).
# python-dotenv may already have populated os.environ from a .env file; if not,
# we fall back to a secondary load attempt here so the engine works both locally
# (with .env) and on Railway (env vars only).
try:
    from dotenv import load_dotenv as _load_dotenv
    from pathlib import Path as _Path
    # Try the project root .env (two levels up from this file)
    _load_dotenv(_Path(__file__).parent.parent.parent / ".env", override=False)
except Exception:
    pass  # dotenv not installed or file missing — that's fine on Railway

_TG_TOKEN:   Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
_TG_CHAT_ID: Optional[str] = os.getenv("TELEGRAM_CHAT_ID")

# Log credential status at import time so it appears in every Railway deploy log.
if _TG_TOKEN and _TG_CHAT_ID:
    logger.info(
        "Telegram credentials loaded — token ...%s  chat_id %s",
        _TG_TOKEN[-6:],   # last 6 chars only — enough to verify without exposing
        _TG_CHAT_ID,
    )
else:
    logger.warning(
        "Telegram credentials MISSING — "
        "TELEGRAM_BOT_TOKEN=%s  TELEGRAM_CHAT_ID=%s  "
        "Alerts will be printed to stdout only.",
        "set" if _TG_TOKEN   else "NOT SET",
        "set" if _TG_CHAT_ID else "NOT SET",
    )


SCORE_LABELS = {
    1: "LOW",
    2: "LOW-MEDIUM",
    3: "MEDIUM",
    4: "HIGH",
    5: "PREMIUM",
}

DIRECTION_EMOJI = {
    Direction.BEARISH: "SELL",
    Direction.BULLISH: "BUY",
    Direction.NEUTRAL: "NEUTRAL",
}


class AlertEngine:

    def __init__(
        self,
        webhook_urls: Optional[List[str]] = None,
        log_path: Optional[str] = None,
        risk_guard=None,   # optional RiskGuard instance for pre-dispatch gating
    ):
        self.webhook_urls = webhook_urls or []
        self.log_path     = log_path
        self.risk_guard   = risk_guard   # set to a RiskGuard instance to enable

        # Surface Risk Guard status in every startup log so Railway makes it obvious.
        if self.risk_guard is not None and _rg is not None:
            logger.info("Risk Guard: ACTIVE (firm=%s)", self.risk_guard.config.name)
        elif self.risk_guard is not None and _rg is None:
            logger.warning(
                "Risk Guard: instance supplied but risk_guard package failed to "
                "import — gate is DISABLED.  Check requirements.txt."
            )
        else:
            logger.info("Risk Guard: not attached to this AlertEngine — gate bypassed.")

    # ─────────────────────────────────────────────
    # CONFIRMED SIGNAL — FORMAT + DISPATCH
    # ─────────────────────────────────────────────

    def format_alert(self, signal: TradeSignal) -> str:
        direction_label = DIRECTION_EMOJI.get(signal.direction, "—")
        score_label     = SCORE_LABELS.get(signal.confluence_score, "UNKNOWN")
        confluences     = []

        if signal.fvg_present:
            confluences.append("FVG at neckline")
        if signal.ob_present:
            confluences.append("Order Block")
        if signal.fvg_present and signal.ob_present:
            confluences.append("Unicorn confluence")
        if signal.killzone_active:
            confluences.append("Killzone active")

        confluence_str = " + ".join(confluences) if confluences else "Base retest only"

        alert = f"""
╔══════════════════════════════════════╗
  TRADER COPILOT — {direction_label} ALERT
╚══════════════════════════════════════╝

  Pair        : {signal.symbol}
  Pattern     : {signal.pattern}
  Direction   : {direction_label}
  Timeframe   : {signal.timeframe}
  Time        : {signal.timestamp.strftime('%Y-%m-%d %H:%M UTC')}

  Entry       : {signal.entry_price:.5f}
  Stop Loss   : {signal.stop_loss:.5f}
  Take Profit : {signal.take_profit:.5f}
  R:R         : 1:{signal.risk_reward}

  Confluence  : {score_label} ({signal.confluence_score}/5)
  Factors     : {confluence_str}

  Notes       : {signal.notes}
{"═" * 42}"""
        return alert.strip()

    def format_json(self, signal: TradeSignal) -> dict:
        return {
            "symbol": signal.symbol,
            "direction": signal.direction.value,
            "pattern": signal.pattern,
            "entry": signal.entry_price,
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
            "rr": signal.risk_reward,
            "score": signal.confluence_score,
            "fvg": signal.fvg_present,
            "ob": signal.ob_present,
            "killzone": signal.killzone_active,
            "timeframe": signal.timeframe,
            "timestamp": signal.timestamp.isoformat(),
            "notes": signal.notes,
        }

    def dispatch(self, signal: TradeSignal):
        """
        Print alert and optionally write to log / send Telegram / send webhook.

        Dispatch order
        --------------
        1. Risk Guard gate (if an RG instance is attached).
           BLOCK  → log + send block message, return early (trade not sent).
           WARN   → prepend warning + adjusted size, continue.
           CLEAR  → append account-state footer, continue.
        2. Print alert to stdout (always — visible in Railway logs).
        3. Append to .jsonl log file (if log_path set).
        4. Send via Telegram Bot API (if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID set).
        5. Send via generic webhook URLs (if webhook_urls set — Discord / other).

        Every step is explicitly logged so Railway logs show the full trace.
        """
        logger.info(
            "dispatch() called — symbol=%s  direction=%s  entry=%.5f",
            signal.symbol,
            signal.direction.value,
            signal.entry_price,
        )

        # ── 1. Risk Guard gate (optional) ─────────────────────────────────────
        if self.risk_guard is not None and _rg is not None:
            logger.info("Risk Guard gate: checking proposal…")
            try:
                proposal = _rg.TradeProposal(
                    firm=self.risk_guard.config.name,
                    account_size=self.risk_guard.state.account_size,
                    proposed_risk_dollars=0.0,   # dollar risk not carried on signal
                    current_daily_pnl=self.risk_guard.state.daily_pnl,
                    open_risk_dollars=0.0,
                    trades_today=self.risk_guard.state.trades_today,
                    estimated_hold_minutes=60.0, # conservative default
                )
                decision = self.risk_guard.check_trade(proposal)
                logger.info(
                    "Risk Guard decision: %s — %s",
                    decision.status.value,
                    decision.reason,
                )

                if decision.status == _rg.DecisionStatus.BLOCK:
                    block_text = _rg.alerts.format_block(decision)
                    logger.warning(
                        "Risk Guard BLOCKED signal for %s: %s",
                        signal.symbol, decision.reason,
                    )
                    print(block_text)
                    if self.log_path:
                        with open(self.log_path, "a") as f:
                            f.write(json.dumps({
                                "type": "rg_block", "reason": decision.reason,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }) + "\n")
                    if self.webhook_urls:
                        self._send_webhook(block_text)
                    self._send_telegram(block_text)
                    return  # do not dispatch the trade alert

                alert_text = self.format_alert(signal)

                if decision.status == _rg.DecisionStatus.WARN:
                    alert_text = _rg.alerts.format_warn(alert_text, decision)
                else:
                    alert_text = _rg.alerts.format_clear(alert_text, decision)

            except Exception as exc:
                logger.error(
                    "Risk Guard check raised an exception — bypassing gate: %s",
                    exc, exc_info=True,
                )
                alert_text = self.format_alert(signal)
        else:
            logger.info("Risk Guard gate: not attached — proceeding directly.")
            alert_text = self.format_alert(signal)

        # ── 2. Console (always visible in Railway logs) ───────────────────────
        print(alert_text)

        # ── 3. Append to .jsonl log ───────────────────────────────────────────
        if self.log_path:
            try:
                with open(self.log_path, "a") as f:
                    f.write(json.dumps(self.format_json(signal)) + "\n")
                logger.info("Alert logged to %s", self.log_path)
            except Exception as exc:
                logger.error("Failed to write alert log: %s", exc, exc_info=True)

        # ── 4. Telegram Bot API ───────────────────────────────────────────────
        self._send_telegram(alert_text)

        # ── 5. Generic webhook URLs (Discord / other) ─────────────────────────
        if self.webhook_urls:
            self._send_webhook(alert_text)

    # ─────────────────────────────────────────────
    # PENDING ALERT — RETEST NOT YET CONFIRMED
    # ─────────────────────────────────────────────

    def format_pending_alert(
        self,
        symbol: str,
        pattern: str,
        entry_price: float,
        stop_loss: float,
        notes: str,
        direction: Optional[Direction] = None,
    ) -> str:
        direction_label = DIRECTION_EMOJI.get(direction, "—") if direction else "—"

        alert = f"""
╔══════════════════════════════════════╗
  TRADER COPILOT — SETUP PENDING ⏳
╚══════════════════════════════════════╝

  Pair        : {symbol}
  Pattern     : {pattern}
  Direction   : {direction_label}
  Timeframe   : 30M
  Time        : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

  Watch Level : {entry_price:.5f}  ← Limit entry if retested
  Stop Loss   : {stop_loss:.5f}
  Status      : Awaiting OB retest confirmation

  Notes       : {notes}
{"═" * 42}"""
        return alert.strip()

    def format_pending_json(
        self,
        symbol: str,
        pattern: str,
        entry_price: float,
        stop_loss: float,
        notes: str,
        direction: Optional[Direction] = None,
    ) -> dict:
        return {
            "type": "pending",
            "symbol": symbol,
            "pattern": pattern,
            "direction": direction.value if direction else "unknown",
            "watch_level": entry_price,
            "sl": stop_loss,
            "timeframe": "30M",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "notes": notes,
        }

    def log_pending(
        self,
        symbol: str,
        pattern: str,
        entry_price: float,
        stop_loss: float,
        notes: str,
        direction: Optional[Direction] = None,
    ):
        """
        Called when a Breakout & Retest setup is confirmed but the retest
        has not yet occurred. Prints a watch-level alert and logs to file.

        This tells the trader: setup is valid, limit order level is ready,
        waiting for price to return to the OB.
        """
        alert_text = self.format_pending_alert(
            symbol=symbol,
            pattern=pattern,
            entry_price=entry_price,
            stop_loss=stop_loss,
            notes=notes,
            direction=direction,
        )
        print(alert_text)

        if self.log_path:
            pending_log_path = self.log_path.replace(".jsonl", "_pending.jsonl")
            try:
                with open(pending_log_path, "a") as f:
                    f.write(json.dumps(self.format_pending_json(
                        symbol=symbol,
                        pattern=pattern,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        notes=notes,
                        direction=direction,
                    )) + "\n")
                logger.info("Pending setup logged: %s %s — watch %.5f", symbol, pattern, entry_price)
            except Exception as exc:
                logger.error("Failed to write pending log: %s", exc, exc_info=True)

        if self.webhook_urls:
            self._send_webhook(alert_text)

    # ─────────────────────────────────────────────
    # TELEGRAM — BOT API (primary delivery path)
    # ─────────────────────────────────────────────

    def _send_telegram(self, message: str):
        """
        Send *message* via the Telegram Bot API.

        Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the module-level
        constants that were resolved at import time.  Both must be present;
        if either is missing this is a no-op with a warning already emitted
        at startup — no repeated spam per signal.

        Uses stdlib urllib so there is no extra dependency and no async
        complexity.  Timeout is 10 s — generous for a Telegram API call.
        """
        if not _TG_TOKEN or not _TG_CHAT_ID:
            # Warning already logged once at module load — don't repeat it.
            logger.debug("_send_telegram: skipped (credentials not set)")
            return

        url     = f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": _TG_CHAT_ID,
            "text":    message,
        }).encode("utf-8")

        logger.info(
            "Telegram: sending to chat_id=%s  (%d chars)…",
            _TG_CHAT_ID, len(message),
        )

        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                body   = resp.read().decode("utf-8", errors="replace")

            if status == 200:
                logger.info("Telegram: message delivered OK (HTTP 200)")
            else:
                logger.error(
                    "Telegram: unexpected HTTP %d — response: %s",
                    status, body[:300],
                )

        except urllib.error.HTTPError as exc:
            # Read the body so we can log Telegram's error description.
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = "<unreadable>"
            logger.error(
                "Telegram: HTTP %d error — %s — body: %s",
                exc.code, exc.reason, err_body[:300],
            )

        except urllib.error.URLError as exc:
            logger.error(
                "Telegram: network error (URLError) — %s  "
                "(check outbound internet access from the container)",
                exc.reason,
            )

        except Exception as exc:
            logger.error(
                "Telegram: unexpected exception — %s",
                exc, exc_info=True,
            )

    # ─────────────────────────────────────────────
    # GENERIC WEBHOOK (Discord / other)
    # ─────────────────────────────────────────────

    def _send_webhook(self, message: str):
        """
        POST *message* as {"text": <message>} to every URL in self.webhook_urls.
        This is the Discord-compatible generic path; Telegram uses _send_telegram().
        """
        payload = json.dumps({"text": message}).encode("utf-8")
        for url in self.webhook_urls:
            logger.info("Webhook: sending to %s…", url[:60])
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
                logger.info("Webhook: delivered OK to %s", url[:60])
            except Exception as exc:
                logger.error(
                    "Webhook: dispatch failed for %s — %s",
                    url[:60], exc, exc_info=True,
                )
