"""
Trader Copilot — ML-Enhanced Engine (Phase 3)
Wraps the core engine with ML confidence scoring.

Every signal now comes with:
  - win_probability (0.0 – 1.0)
  - confidence_tier (PREMIUM / HIGH / MEDIUM / LOW)
  - action recommendation

Usage:
    from trader_copilot.ml.ml_engine import MLEngine

    engine = MLEngine("XAUUSD", model_path="trader_copilot_model.pkl")
    result = engine.analyse(candles_htf, candles_ltf)

    if result:
        signal, prediction = result
        print(f"{signal.pattern} | WinProb: {prediction['win_probability']:.0%}")
        print(f"Action: {prediction['action']}")
        engine.alert(signal, prediction)
"""

from typing import List, Optional, Tuple  # List used for webhook_urls

from ..engine import TraderCopilot
from ..core.structures import Candle, TradeSignal
from ..ml.confidence_model import ConfidenceModel
from ..alerts.alert_engine import AlertEngine


class MLEngine:

    def __init__(
        self,
        symbol: str,
        model_path: str = "trader_copilot_model.pkl",
        webhook_urls: Optional[List[str]] = None,
        log_path: Optional[str] = "trader_copilot_alerts.jsonl",
        backtest_mode: bool = False,
        min_probability: float = 0.45,  # Skip signals below this threshold
    ):
        self.symbol = symbol
        self.min_probability = min_probability
        self.backtest_mode = backtest_mode

        self.core_engine = TraderCopilot(
            symbol=symbol,
            log_path=log_path,
        )
        self.ml_model = ConfidenceModel(model_path=model_path)
        self.alert_engine = AlertEngine(webhook_urls=webhook_urls, log_path=log_path)

        # Try loading existing model
        loaded = self.ml_model.load()
        if not loaded:
            print(f"[MLEngine] No trained model found at {model_path}.")
            print(
                "[MLEngine] Rule-based scoring active. "
                "Train with: python -m trader_copilot.ml.trainer"
            )

    # ─────────────────────────────────────────────
    # ANALYSE
    # ─────────────────────────────────────────────

    def analyse(
        self,
        candles_htf: List[Candle],
        candles_ltf: List[Candle],
    ) -> Optional[Tuple[TradeSignal, dict]]:
        """
        Run core rules engine, then score with ML model.
        Returns (signal, prediction) or None.
        Filters out signals below min_probability threshold.
        """
        signal = self.core_engine.analyse(candles_htf, candles_ltf)

        if signal is None:
            return None

        # Build trade dict for ML scoring
        trade_dict = self._signal_to_dict(signal)
        prediction = self.ml_model.predict(trade_dict)

        # Filter by probability threshold
        if prediction["win_probability"] < self.min_probability:
            return None

        return signal, prediction

    def run(
        self,
        candles_htf: List[Candle],
        candles_ltf: List[Candle],
    ) -> Optional[Tuple[TradeSignal, dict]]:
        """Analyse and alert in one call."""
        result = self.analyse(candles_htf, candles_ltf)
        if result:
            signal, prediction = result
            self.alert(signal, prediction)
        return result

    # ─────────────────────────────────────────────
    # ALERT
    # ─────────────────────────────────────────────

    def alert(self, signal: TradeSignal, prediction: dict):
        """Dispatch ML-enhanced alert."""
        enhanced_alert = self._format_ml_alert(signal, prediction)
        print(enhanced_alert)

        if self.alert_engine.log_path:
            import json

            entry = self.alert_engine.format_json(signal)
            entry["win_probability"] = prediction["win_probability"]
            entry["confidence_tier"] = prediction["confidence_tier"]
            entry["action"] = prediction["action"]
            with open(self.alert_engine.log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

        if self.alert_engine.webhook_urls:
            self.alert_engine._send_webhook(enhanced_alert)

    def _format_ml_alert(self, signal: TradeSignal, prediction: dict) -> str:
        prob = prediction["win_probability"]
        tier = prediction["confidence_tier"]
        action = prediction["action"]
        model_b = prediction["model_based"]
        dir_lbl = "SELL" if signal.direction.value == "bearish" else "BUY"

        prob_bar_len = int(prob * 20)
        prob_bar = "█" * prob_bar_len + "░" * (20 - prob_bar_len)

        scoring_note = "ML model" if model_b else "Rule-based (no model yet)"

        return f"""
╔══════════════════════════════════════════════╗
  TRADER COPILOT — {dir_lbl} SIGNAL  [{tier}]
╚══════════════════════════════════════════════╝

  Pair        : {signal.symbol}
  Pattern     : {signal.pattern}
  Direction   : {dir_lbl}
  Time        : {signal.timestamp.strftime('%Y-%m-%d %H:%M UTC')}

  Entry       : {signal.entry_price:.5f}
  Stop Loss   : {signal.stop_loss:.5f}
  Take Profit : {signal.take_profit:.5f}
  R:R         : 1:{signal.risk_reward}

  Rules score : {signal.confluence_score}/5
  FVG present : {'Yes' if signal.fvg_present else 'No'}
  OB present  : {'Yes' if signal.ob_present else 'No'}
  Killzone    : {'Active' if signal.killzone_active else 'Inactive'}

  Win prob    : {prob:.0%}  [{prob_bar}]
  ML tier     : {tier}
  Action      : {action}
  Scoring     : {scoring_note}
{'═' * 48}""".strip()

    # ─────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────

    def _signal_to_dict(self, signal: TradeSignal) -> dict:
        from ..journal.trade_journal import TradeJournal

        journal_helper = TradeJournal.__new__(TradeJournal)
        session = journal_helper._infer_session(signal.timestamp, signal.symbol)

        return {
            "symbol": signal.symbol,
            "direction": signal.direction.value,
            "pattern": signal.pattern,
            "confluence_score": signal.confluence_score,
            "fvg_present": int(signal.fvg_present),
            "ob_present": int(signal.ob_present),
            "killzone_active": int(signal.killzone_active),
            "risk_reward": signal.risk_reward,
            "signal_time": signal.timestamp.isoformat(),
            "session": session,
        }

    def retrain(self, journal_path: str = "trader_copilot_journal.db"):
        """Retrain ML model from latest journal data."""
        from ..ml.trainer import train

        print(f"[MLEngine] Retraining from {journal_path}...")
        train(db_path=journal_path, model_path=self.ml_model.model_path)
        self.ml_model.load()
