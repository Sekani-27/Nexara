"""
Trader Copilot — Phase 2 Test Suite
Tests journal logging, outcome recording, stats, and backtest result compilation.
Run: PYTHONPATH=/path/to/project python trader_copilot/tests/test_phase2.py
"""

import os
import tempfile

from datetime import datetime
from trader_copilot.core.structures import TradeSignal, Direction
from trader_copilot.journal.trade_journal import TradeJournal, Outcome
from trader_copilot.backtest.backtester import Backtester, BacktestResult


def make_signal(direction=Direction.BEARISH, score=4, fvg=True, ob=True) -> TradeSignal:
    return TradeSignal(
        symbol="XAUUSD",
        direction=direction,
        entry_price=2350.00,
        stop_loss=2365.00,
        take_profit=2320.00,
        timeframe="15M",
        timestamp=datetime(2024, 6, 1, 13, 30),
        pattern="Double Top",
        confluence_score=score,
        fvg_present=fvg,
        ob_present=ob,
        killzone_active=True,
        notes="Test signal",
    )


def test_journal_log_and_retrieve():
    """Signal logs correctly and is retrievable."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    journal = TradeJournal(db_path=db_path)
    signal  = make_signal()
    trade_id = journal.log_signal(signal)

    assert trade_id > 0, "Trade ID should be positive"

    trades = journal.get_all_trades()
    assert len(trades) == 1
    assert trades[0]["symbol"]    == "XAUUSD"
    assert trades[0]["pattern"]   == "Double Top"
    assert trades[0]["outcome"]   == "pending"
    assert trades[0]["fvg_present"] == 1

    print("PASS — Journal log and retrieve")
    journal.close()
    os.unlink(db_path)


def test_outcome_recording():
    """Outcome updates correctly and P&L in R is calculated."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    journal  = TradeJournal(db_path=db_path)
    signal   = make_signal()  # entry=2350, sl=2365, tp=2320 (bearish)
    trade_id = journal.log_signal(signal)

    # TP hit at 2320 → risk=15, reward=30 → +2.0R
    journal.record_outcome(trade_id, Outcome.TP_HIT, close_price=2320.00)

    trades = journal.get_all_trades()
    assert trades[0]["outcome"]  == "tp_hit"
    assert trades[0]["pnl_rr"]   == 2.0, f"Expected 2.0R, got {trades[0]['pnl_rr']}"
    assert trades[0]["close_price"] == 2320.00

    print(f"PASS — Outcome recording | P&L: +{trades[0]['pnl_rr']}R")
    journal.close()
    os.unlink(db_path)


def test_sl_outcome_recording():
    """SL hit records negative R correctly."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    journal  = TradeJournal(db_path=db_path)
    signal   = make_signal()  # entry=2350, sl=2365 → risk=15
    trade_id = journal.log_signal(signal)

    # SL hit at 2365 → -1.0R
    journal.record_outcome(trade_id, Outcome.SL_HIT, close_price=2365.00)

    trades = journal.get_all_trades()
    assert trades[0]["outcome"] == "sl_hit"
    assert trades[0]["pnl_rr"]  == -1.0, f"Expected -1.0R, got {trades[0]['pnl_rr']}"

    print(f"PASS — SL outcome | P&L: {trades[0]['pnl_rr']}R")
    journal.close()
    os.unlink(db_path)


def test_stats_computation():
    """Stats correctly compute win rate, avg R, and FVG edge."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    journal = TradeJournal(db_path=db_path)

    # Log 4 trades: 3 wins, 1 loss
    outcomes = [
        (Outcome.TP_HIT,  2320.00, True),   # +2R, FVG
        (Outcome.TP_HIT,  2320.00, True),   # +2R, FVG
        (Outcome.SL_HIT,  2365.00, False),  # -1R, no FVG
        (Outcome.TP_HIT,  2320.00, True),   # +2R, FVG
    ]

    for outcome, price, fvg in outcomes:
        sig = make_signal(fvg=fvg)
        tid = journal.log_signal(sig)
        journal.record_outcome(tid, outcome, close_price=price)

    stats = journal.get_stats()
    assert stats["total_trades"]  == 4
    assert stats["wins"]          == 3
    assert stats["losses"]        == 1
    assert stats["win_rate_pct"]  == 75.0
    assert stats["total_r"]       == 5.0   # 2+2-1+2
    assert stats["fvg_present_wr"] == 100.0  # 3/3 FVG trades won
    assert stats["fvg_absent_wr"]  == 0.0    # 0/1 no-FVG trades won

    print(f"PASS — Stats: WR={stats['win_rate_pct']}% | "
          f"Total R={stats['total_r']}R | "
          f"FVG WR={stats['fvg_present_wr']}% vs No-FVG WR={stats['fvg_absent_wr']}%")
    journal.close()
    os.unlink(db_path)


def test_backtest_result_compilation():
    """Backtester compiles results correctly from signal list."""
    bt = Backtester.__new__(Backtester)
    bt.symbol = "XAUUSD"

    signals = [
        {"id": 1, "symbol": "XAUUSD", "direction": "bearish", "pattern": "Double Top",
         "entry": 2350, "sl": 2365, "tp": 2320, "rr": 2.0, "score": 4,
         "fvg": True, "ob": True, "killzone": True,
         "time": "2024-01-01T13:30:00", "outcome": "tp_hit",
         "close": 2320, "pnl_r": 2.0},
        {"id": 2, "symbol": "XAUUSD", "direction": "bearish", "pattern": "Double Top",
         "entry": 2350, "sl": 2365, "tp": 2320, "rr": 2.0, "score": 3,
         "fvg": False, "ob": False, "killzone": True,
         "time": "2024-01-02T13:30:00", "outcome": "sl_hit",
         "close": 2365, "pnl_r": -1.0},
        {"id": 3, "symbol": "XAUUSD", "direction": "bearish", "pattern": "Head and Shoulders",
         "entry": 2380, "sl": 2400, "tp": 2340, "rr": 2.0, "score": 5,
         "fvg": True, "ob": True, "killzone": True,
         "time": "2024-01-03T13:30:00", "outcome": "tp_hit",
         "close": 2340, "pnl_r": 2.0},
    ]

    result = bt._compile_results(signals)

    assert result.total_signals == 3
    assert result.wins          == 2
    assert result.losses        == 1
    assert result.win_rate      == round(2/3 * 100, 1)
    assert result.total_r       == 3.0  # 2 - 1 + 2
    assert "Double Top" in result.by_pattern
    assert "Head and Shoulders" in result.by_pattern
    assert result.by_pattern["Double Top"]["win_rate"] == 50.0
    assert result.by_pattern["Head and Shoulders"]["win_rate"] == 100.0

    print(f"PASS — Backtest compilation: WR={result.win_rate}% | "
          f"Total R={result.total_r}R | Patterns: {list(result.by_pattern.keys())}")


def test_export_csv():
    """Journal exports to CSV correctly."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
        csv_path = f.name

    journal  = TradeJournal(db_path=db_path)
    signal   = make_signal()
    trade_id = journal.log_signal(signal)
    journal.record_outcome(trade_id, Outcome.TP_HIT, close_price=2320.00)
    journal.export_csv(csv_path)

    import csv
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "XAUUSD"
    assert rows[0]["outcome"] == "tp_hit"

    print(f"PASS — CSV export: {len(rows)} row(s) written")
    journal.close()
    os.unlink(db_path)
    os.unlink(csv_path)


if __name__ == "__main__":
    print("\n── Trader Copilot Phase 2 — Test Suite ──\n")
    test_journal_log_and_retrieve()
    test_outcome_recording()
    test_sl_outcome_recording()
    test_stats_computation()
    test_backtest_result_compilation()
    test_export_csv()
    print("\n── All Phase 2 tests passed ──\n")
