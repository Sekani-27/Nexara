"""
Trader Copilot — Alert Engine
Formats and dispatches trade alerts.
Supports console output, webhook (Telegram/Discord), and log file.
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional
from ..core.structures import TradeSignal, Direction

try:
    import risk_guard as _rg  # optional Risk Guard integration — see dispatch()
except ImportError:
    _rg = None

logger = logging.getLogger("trader_copilot.alerts")


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
        Print alert and optionally write to log / send webhook.
        If a RiskGuard instance is attached, gates the alert through Fence 1:
          BLOCK → send block message only, return early.
          WARN  → prepend warning + adjusted size, then send.
          CLEAR → send normally with account-state footer appended.
        """
        # ── Risk Guard gate (optional) ────────────────────────────────────────
        if self.risk_guard is not None and _rg is not None:
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

            if decision.status == _rg.DecisionStatus.BLOCK:
                block_text = _rg.alerts.format_block(decision)
                print(block_text)
                if self.log_path:
                    with open(self.log_path, "a") as f:
                        f.write(json.dumps({
                            "type": "rg_block", "reason": decision.reason,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }) + "\n")
                if self.webhook_urls:
                    self._send_webhook(block_text)
                return  # do not dispatch the trade alert

            alert_text = self.format_alert(signal)

            if decision.status == _rg.DecisionStatus.WARN:
                alert_text = _rg.alerts.format_warn(alert_text, decision)
            else:
                alert_text = _rg.alerts.format_clear(alert_text, decision)
        else:
            alert_text = self.format_alert(signal)

        # ── Normal dispatch ───────────────────────────────────────────────────
        print(alert_text)

        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(self.format_json(signal)) + "\n")
            logger.info(f"Alert logged: {signal.symbol} {signal.pattern} {signal.direction.value}")

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
            with open(pending_log_path, "a") as f:
                f.write(json.dumps(self.format_pending_json(
                    symbol=symbol,
                    pattern=pattern,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    notes=notes,
                    direction=direction,
                )) + "\n")
            logger.info(f"Pending setup logged: {symbol} {pattern} — watch {entry_price:.5f}")

        if self.webhook_urls:
            self._send_webhook(alert_text)

    # ─────────────────────────────────────────────
    # SHARED: WEBHOOK
    # ─────────────────────────────────────────────

    def _send_webhook(self, message: str):
        """Send to every configured Telegram or Discord webhook."""
        import urllib.request
        payload = json.dumps({"text": message}).encode()
        for url in self.webhook_urls:
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                logger.error(f"Webhook dispatch failed for {url}: {e}")
