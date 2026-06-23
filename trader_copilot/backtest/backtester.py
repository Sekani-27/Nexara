"""
Trader Copilot — Backtesting Harness
Replays the rules engine against historical OHLCV data.
Simulates bar-by-bar scanning to prevent lookahead bias.

Usage:
    from trader_copilot.backtest.backtester import Backtester
    bt = Backtester("XAUUSD")
    results = bt.run(htf_csv="xauusd_4h.csv", ltf_csv="xauusd_15m.csv")
    bt.print_report(results)
"""

from dataclasses import dataclass, field
from typing import List, Dict
import os
import sys

# Allow running standalone
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from trader_copilot.core.structures import TradeSignal, Direction
from trader_copilot.engine import TraderCopilot
from trader_copilot.journal.trade_journal import TradeJournal, Outcome
from trader_copilot.utils.mt5_connector import CSVConnector
from trader_copilot.config.pairs import PAIR_CONFIGS


@dataclass
class BacktestResult:
    symbol: str
    total_signals: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float
    avg_rr: float
    total_r: float
    max_drawdown_r: float
    best_trade_r: float
    worst_trade_r: float
    signals: List[dict] = field(default_factory=list)
    by_pattern: Dict[str, dict] = field(default_factory=dict)
    by_score: Dict[int, dict] = field(default_factory=dict)
    by_session: Dict[str, dict] = field(default_factory=dict)


class Backtester:

    def __init__(self, symbol: str, journal_db: str = None):
        self.symbol = symbol
        self.config = PAIR_CONFIGS[symbol]
        self.engine = TraderCopilot(symbol=symbol, log_path=None, backtest_mode=True)
        self.journal = TradeJournal(journal_db or f"{symbol}_backtest.db")
        self.csv = CSVConnector()

    # ─────────────────────────────────────────────
    # MAIN RUN
    # ─────────────────────────────────────────────

    def run(
        self,
        htf_csv: str,
        ltf_csv: str,
        htf_window: int = 200,  # How many HTF candles to feed per bar
        ltf_window: int = 200,  # How many LTF candles to feed per bar
        min_score: int = 3,
    ) -> BacktestResult:
        """
        Bar-by-bar backtest. Advances one LTF candle at a time.
        For each bar: feeds trailing window of HTF + LTF candles to engine.
        When a signal fires: simulates outcome against subsequent price action.
        """
        print(f"\n── Backtesting {self.symbol} ──")
        print(f"HTF: {self.config.structure_tf} | LTF: {self.config.entry_tf}")
        print("Loading data...")

        candles_htf = self.csv.load(htf_csv, timeframe=self.config.structure_tf)
        candles_ltf = self.csv.load(ltf_csv, timeframe=self.config.entry_tf)

        if not candles_htf or not candles_ltf:
            print("ERROR: Could not load candle data. Check CSV paths.")
            return None

        print(f"HTF candles: {len(candles_htf)} | LTF candles: {len(candles_ltf)}")

        signals_fired = []
        seen_signals = set()  # Deduplicate signals at the same level

        # Walk forward bar by bar on LTF
        for i in range(ltf_window, len(candles_ltf)):
            ltf_slice = candles_ltf[max(0, i - ltf_window) : i]

            # Align HTF candles to current LTF timestamp
            current_time = candles_ltf[i - 1].timestamp
            htf_slice = [c for c in candles_htf if c.timestamp <= current_time]
            htf_slice = htf_slice[-htf_window:]

            if len(htf_slice) < 50:
                continue

            signal = self.engine.analyse(htf_slice, ltf_slice)

            if signal is None:
                continue
            if signal.confluence_score < min_score:
                continue

            # Deduplicate: same pattern + entry level within 5 bars
            sig_key = f"{signal.pattern}_{signal.entry_price:.4f}"
            if sig_key in seen_signals:
                continue
            seen_signals.add(sig_key)

            # Log to journal
            trade_id = self.journal.log_signal(signal)

            # Simulate outcome on future LTF candles
            outcome, close_price = self._simulate_outcome(
                signal=signal, future_candles=candles_ltf[i:]
            )

            self.journal.record_outcome(
                trade_id=trade_id,
                outcome=outcome,
                close_price=close_price,
                close_time=current_time,
            )

            # Calculate P&L in R
            risk = abs(signal.entry_price - signal.stop_loss)
            if signal.direction == Direction.BEARISH:
                pnl_r = (
                    round((signal.entry_price - close_price) / risk, 2)
                    if risk > 0
                    else 0
                )
            else:
                pnl_r = (
                    round((close_price - signal.entry_price) / risk, 2)
                    if risk > 0
                    else 0
                )

            signals_fired.append(
                {
                    "id": trade_id,
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
                    "time": signal.timestamp.isoformat(),
                    "outcome": outcome.value,
                    "close": close_price,
                    "pnl_r": pnl_r,
                }
            )

            print(
                f"  Signal {len(signals_fired):>3} | {signal.pattern:<28} | "
                f"Score: {signal.confluence_score}/5 | "
                f"{outcome.value:<12} | {pnl_r:+.2f}R"
            )

        return self._compile_results(signals_fired)

    # ─────────────────────────────────────────────
    # OUTCOME SIMULATION
    # ─────────────────────────────────────────────

    def _simulate_outcome(
        self,
        signal: TradeSignal,
        future_candles: list,
        max_bars: int = 100,
    ):
        """
        Walk forward through future candles.
        First level hit (TP or SL) determines outcome.
        If neither hit within max_bars, record as manual close at last price.
        """
        for c in future_candles[:max_bars]:
            if signal.direction == Direction.BEARISH:
                if c.low <= signal.take_profit:
                    return Outcome.TP_HIT, signal.take_profit
                if c.high >= signal.stop_loss:
                    return Outcome.SL_HIT, signal.stop_loss
            else:
                if c.high >= signal.take_profit:
                    return Outcome.TP_HIT, signal.take_profit
                if c.low <= signal.stop_loss:
                    return Outcome.SL_HIT, signal.stop_loss

        # Neither hit — close at last available price
        last_price = future_candles[min(max_bars - 1, len(future_candles) - 1)].close
        _risk = abs(signal.entry_price - signal.stop_loss)
        if signal.direction == Direction.BEARISH:
            pnl = signal.entry_price - last_price
        else:
            pnl = last_price - signal.entry_price

        if pnl > 0:
            return Outcome.MANUAL_WIN, last_price
        elif pnl < 0:
            return Outcome.MANUAL_LOSS, last_price
        return Outcome.BREAKEVEN, last_price

    # ─────────────────────────────────────────────
    # COMPILE RESULTS
    # ─────────────────────────────────────────────

    def _compile_results(self, signals: list) -> BacktestResult:
        if not signals:
            print("\nNo signals fired during backtest period.")
            return BacktestResult(
                symbol=self.symbol,
                total_signals=0,
                wins=0,
                losses=0,
                breakeven=0,
                win_rate=0,
                avg_rr=0,
                total_r=0,
                max_drawdown_r=0,
                best_trade_r=0,
                worst_trade_r=0,
            )

        wins = [s for s in signals if s["outcome"] in ("tp_hit", "manual_win")]
        losses = [s for s in signals if s["outcome"] in ("sl_hit", "manual_loss")]
        breakeven = [s for s in signals if s["outcome"] == "breakeven"]

        total = len(signals)
        win_rate = round(len(wins) / total * 100, 1)
        pnl_list = [s["pnl_r"] for s in signals]
        avg_rr = round(sum(pnl_list) / total, 2)
        total_r = round(sum(pnl_list), 2)

        # Max drawdown
        equity = 0
        peak = 0
        max_dd = 0
        for p in pnl_list:
            equity += p
            peak = max(peak, equity)
            dd = peak - equity
            max_dd = max(max_dd, dd)

        # Breakdowns
        by_pattern = {}
        by_score = {}
        _by_session = {}

        for s in signals:
            # Pattern
            p = s["pattern"]
            by_pattern.setdefault(p, {"total": 0, "wins": 0, "total_r": 0.0})
            by_pattern[p]["total"] += 1
            by_pattern[p]["total_r"] += s["pnl_r"]
            if s["outcome"] in ("tp_hit", "manual_win"):
                by_pattern[p]["wins"] += 1

            # Score
            sc = s["score"]
            by_score.setdefault(sc, {"total": 0, "wins": 0, "total_r": 0.0})
            by_score[sc]["total"] += 1
            by_score[sc]["total_r"] += s["pnl_r"]
            if s["outcome"] in ("tp_hit", "manual_win"):
                by_score[sc]["wins"] += 1

        # Win rates
        for p in by_pattern:
            by_pattern[p]["win_rate"] = round(
                by_pattern[p]["wins"] / by_pattern[p]["total"] * 100, 1
            )
            by_pattern[p]["avg_r"] = round(
                by_pattern[p]["total_r"] / by_pattern[p]["total"], 2
            )
        for sc in by_score:
            by_score[sc]["win_rate"] = round(
                by_score[sc]["wins"] / by_score[sc]["total"] * 100, 1
            )
            by_score[sc]["avg_r"] = round(
                by_score[sc]["total_r"] / by_score[sc]["total"], 2
            )

        return BacktestResult(
            symbol=self.symbol,
            total_signals=total,
            wins=len(wins),
            losses=len(losses),
            breakeven=len(breakeven),
            win_rate=win_rate,
            avg_rr=avg_rr,
            total_r=total_r,
            max_drawdown_r=round(max_dd, 2),
            best_trade_r=round(max(pnl_list), 2),
            worst_trade_r=round(min(pnl_list), 2),
            signals=signals,
            by_pattern=by_pattern,
            by_score=by_score,
        )

    # ─────────────────────────────────────────────
    # REPORT
    # ─────────────────────────────────────────────

    def print_report(self, result: BacktestResult):
        if result is None or result.total_signals == 0:
            print("No results to report.")
            return

        print(
            f"""
╔══════════════════════════════════════════════╗
  TRADER COPILOT — BACKTEST REPORT
  {result.symbol}
╚══════════════════════════════════════════════╝

  Total signals   : {result.total_signals}
  Wins            : {result.wins}
  Losses          : {result.losses}
  Breakeven       : {result.breakeven}
  Win rate        : {result.win_rate}%
  Average R       : {result.avg_rr:+.2f}R
  Total R         : {result.total_r:+.2f}R
  Max drawdown    : -{result.max_drawdown_r:.2f}R
  Best trade      : +{result.best_trade_r:.2f}R
  Worst trade     : {result.worst_trade_r:.2f}R

── By Pattern ──────────────────────────────────"""
        )

        for pattern, stats in result.by_pattern.items():
            print(
                f"  {pattern:<30} WR: {stats['win_rate']:>5}%  "
                f"Trades: {stats['total']:>3}  Avg: {stats['avg_r']:+.2f}R"
            )

        print("\n── By Confluence Score ─────────────────────────")
        for score, stats in sorted(result.by_score.items()):
            bar = "█" * stats["total"]
            print(
                f"  Score {score}/5  WR: {stats['win_rate']:>5}%  "
                f"Trades: {stats['total']:>3}  Avg: {stats['avg_r']:+.2f}R  {bar}"
            )

        print(f"\n{'═' * 48}")

    def export_report_csv(self, result: BacktestResult, filepath: str):
        """Export signal-level detail to CSV."""
        import csv

        if not result or not result.signals:
            return
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=result.signals[0].keys())
            writer.writeheader()
            writer.writerows(result.signals)
        print(f"[Backtest] Exported {len(result.signals)} signals to {filepath}")
