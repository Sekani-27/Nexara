"""
Trader Copilot — ML Trainer
Reads journal, trains confidence model, prints evaluation.

Usage:
    python -m trader_copilot.ml.trainer
    python -m trader_copilot.ml.trainer --db my_journal.db --model my_model.pkl
    python -m trader_copilot.ml.trainer --symbol XAUUSD
"""

import argparse
import sys
import os

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from trader_copilot.journal.trade_journal import TradeJournal
from trader_copilot.ml.confidence_model import ConfidenceModel


def train(
    db_path: str = "trader_copilot_journal.db",
    model_path: str = "trader_copilot_model.pkl",
    symbol: str = None,
):
    journal = TradeJournal(db_path=db_path)
    model = ConfidenceModel(model_path=model_path)

    # Pull completed trades
    if symbol:
        trades = journal.get_by_symbol(symbol)
    else:
        trades = journal.get_all_trades()

    completed = [t for t in trades if t.get("outcome") not in ("pending", None)]
    print(f"\n[Trainer] Loaded {len(completed)} completed trades from {db_path}")

    if symbol:
        print(f"[Trainer] Filtered to {symbol}")

    # Train
    stats = model.train(completed)

    if "error" in stats:
        print(f"\n[Trainer] Cannot train: {stats['error']}")
        print("[Trainer] Rule-based scoring will be used until more data is available.")
        return None

    model.print_report()

    # Sample predictions on recent trades
    recent = completed[-10:]
    if recent:
        print("── Sample predictions on recent trades ──────────────")
        for t in recent:
            pred = model.predict(t)
            actual = "WIN" if t["outcome"] in ("tp_hit", "manual_win") else "LOSS"
            print(
                f"  {t['symbol']:<7} {t['pattern']:<28} "
                f"Prob:{pred['win_probability']:.2f}  "
                f"Tier:{pred['confidence_tier']:<8}  "
                f"Actual:{actual}"
            )
        print()

    return model


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trader Copilot ML Trainer")
    parser.add_argument("--db", default="trader_copilot_journal.db")
    parser.add_argument("--model", default="trader_copilot_model.pkl")
    parser.add_argument("--symbol", default=None)
    args = parser.parse_args()

    train(db_path=args.db, model_path=args.model, symbol=args.symbol)
