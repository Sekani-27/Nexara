"""
Trader Copilot — Stats Dashboard
Reads the live journal DB and prints a full performance breakdown.

Usage:
    python -m trader_copilot.journal.dashboard
    python -m trader_copilot.journal.dashboard --symbol XAUUSD
    python -m trader_copilot.journal.dashboard --db my_journal.db
"""

import argparse
import sys
import os

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from trader_copilot.journal.trade_journal import TradeJournal


def print_dashboard(journal: TradeJournal, symbol: str = None, pattern: str = None):
    stats = journal.get_stats(symbol=symbol, pattern=pattern)

    if "message" in stats:
        print(f"\n  {stats['message']}")
        return

    filter_label = f"{symbol or 'All pairs'}"
    if pattern:
        filter_label += f" | {pattern}"

    print(
        f"""
╔══════════════════════════════════════════════╗
  TRADER COPILOT — PERFORMANCE DASHBOARD
  {filter_label}
╚══════════════════════════════════════════════╝

  Total trades    : {stats['total_trades']}
  Wins            : {stats['wins']}
  Losses          : {stats['losses']}
  Breakeven       : {stats['breakeven']}
  Win rate        : {stats['win_rate_pct']}%
  Average R       : {stats['avg_rr']:+.2f}R per trade
  Total R banked  : {stats['total_r']:+.2f}R

── FVG Confirmation Edge ────────────────────────"""
    )

    fvg_wr = stats.get("fvg_present_wr")
    no_fvg_wr = stats.get("fvg_absent_wr")

    if fvg_wr is not None:
        edge = round(fvg_wr - (no_fvg_wr or 0), 1)
        print(f"  With FVG at neckline    : {fvg_wr}% WR")
        print(f"  Without FVG             : {no_fvg_wr}% WR")
        print(f"  FVG edge                : +{edge}%")

    print("\n── By Confluence Score ─────────────────────────")
    for score, breakdown in sorted(stats["score_breakdown"].items()):
        bar = "█" * breakdown["total"]
        print(
            f"  Score {score}/5  WR: {breakdown['win_rate']:>5}%  "
            f"Trades: {breakdown['total']:>3}  {bar}"
        )

    print("\n── By Pattern ──────────────────────────────────")
    for pat, breakdown in stats["pattern_breakdown"].items():
        print(
            f"  {pat:<32} WR: {breakdown['win_rate']:>5}%  "
            f"Trades: {breakdown['total']:>3}"
        )

    print(f"\n{'═' * 48}\n")


def list_pending(journal: TradeJournal):
    pending = journal.get_pending()
    if not pending:
        print("\n  No pending trades.\n")
        return
    print(f"\n── Pending Trades ({len(pending)}) ──────────────────────")
    for t in pending:
        print(
            f"  ID {t['id']:>4} | {t['symbol']} {t['direction']:<8} | "
            f"{t['pattern']:<28} | Entry: {t['entry_price']:.5f} | "
            f"{t['signal_time'][:16]}"
        )
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trader Copilot Dashboard")
    parser.add_argument("--db", default="trader_copilot_journal.db")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--pattern", default=None)
    parser.add_argument("--pending", action="store_true", help="Show pending trades")
    args = parser.parse_args()

    journal = TradeJournal(db_path=args.db)

    if args.pending:
        list_pending(journal)
    else:
        print_dashboard(journal, symbol=args.symbol, pattern=args.pattern)
