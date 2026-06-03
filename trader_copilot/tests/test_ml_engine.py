"""
Trader Copilot — MLEngine Test Suite
Tests ml_engine.py: analyse(), run(), and _signal_to_dict().

Approach: fully synthetic — no live MT5, no journal DB, no trained model file.
The ConfidenceModel falls back to rule-based scoring when untrained, so
MLEngine.analyse() produces real output without any pre-existing model.
"""

import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch

from trader_copilot.core.structures import (
    Candle, Direction, TradeSignal, BiasType
)
from trader_copilot.ml.ml_engine import MLEngine
from trader_copilot.config.pairs import PAIR_CONFIGS


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _make_signal(
    symbol="XAUUSDm",
    direction=Direction.BEARISH,
    entry=2350.0,
    sl=2365.0,
    tp=2320.0,
    score=4,
    fvg=True,
    ob=True,
    killzone=True,
    pattern="Double Top",
    ts=None,
) -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        direction=direction,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        timeframe="15M",
        timestamp=ts or datetime(2024, 6, 1, 13, 30),
        pattern=pattern,
        confluence_score=score,
        fvg_present=fvg,
        ob_present=ob,
        killzone_active=killzone,
        notes="Test signal",
    )


def _make_engine(symbol="XAUUSDm", min_prob=0.0) -> MLEngine:
    """
    Create an MLEngine with a throwaway model path (no file needed).
    min_probability=0.0 so no signal is filtered out by threshold.
    """
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
        model_path = f.name
    # Remove the empty file so ConfidenceModel.load() returns False cleanly
    os.unlink(model_path)
    return MLEngine(symbol=symbol, model_path=model_path, min_probability=min_prob)


# ─────────────────────────────────────────────────────────────────────────────
# _signal_to_dict
# ─────────────────────────────────────────────────────────────────────────────

def test_signal_to_dict_produces_valid_feature_dict():
    """
    _signal_to_dict() must produce a dict with all keys required by
    trade_to_features() — no live TradeJournal instance should be needed.
    """
    engine = _make_engine()
    signal = _make_signal()

    result = engine._signal_to_dict(signal)

    required_keys = [
        "symbol", "direction", "pattern", "confluence_score",
        "fvg_present", "ob_present", "killzone_active",
        "risk_reward", "signal_time", "session",
    ]
    for key in required_keys:
        assert key in result, f"Missing key in signal dict: {key}"

    assert result["symbol"]    == "XAUUSDm"
    assert result["direction"] == "bearish"
    assert result["pattern"]   == "Double Top"
    assert result["confluence_score"] == 4
    assert result["fvg_present"]      == 1
    assert result["ob_present"]       == 1
    assert result["killzone_active"]  == 1
    assert isinstance(result["risk_reward"], float)
    assert isinstance(result["signal_time"], str)
    assert result["session"] in (
        "london", "london_open", "new_york", "new_york_open",
        "london_ny_overlap_pre", "off_session"
    )
    print(f"PASS — _signal_to_dict: all keys present | session={result['session']}")


def test_signal_to_dict_session_new_york_open():
    """_signal_to_dict must return 'new_york_open' for a 13:30 UTC signal."""
    engine = _make_engine()
    signal = _make_signal(ts=datetime(2024, 6, 1, 13, 30))
    result = engine._signal_to_dict(signal)
    assert result["session"] == "new_york_open", \
        f"Expected 'new_york_open', got '{result['session']}'"
    print("PASS — _signal_to_dict session: new_york_open at 13:30 UTC")


def test_signal_to_dict_session_london():
    """_signal_to_dict must return 'london' for an 08:00 UTC signal."""
    engine = _make_engine()
    signal = _make_signal(ts=datetime(2024, 6, 1, 8, 0))
    result = engine._signal_to_dict(signal)
    assert result["session"] == "london", \
        f"Expected 'london', got '{result['session']}'"
    print("PASS — _signal_to_dict session: london at 08:00 UTC")


def test_signal_to_dict_does_not_require_live_db():
    """
    _signal_to_dict() uses TradeJournal.__new__() to call _infer_session()
    without opening any database. The call must succeed without DB path or
    any database file being present.
    """
    engine = _make_engine()
    signal = _make_signal()

    # Should not raise even though no DB file exists anywhere
    try:
        result = engine._signal_to_dict(signal)
        assert "session" in result
        print("PASS — _signal_to_dict works without live DB instance")
    except Exception as e:
        raise AssertionError(f"_signal_to_dict raised unexpectedly: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MLEngine.analyse()
# ─────────────────────────────────────────────────────────────────────────────

def test_analyse_returns_correct_structure_when_signal_produced():
    """
    When the core engine produces a signal, MLEngine.analyse() must return
    a (TradeSignal, prediction_dict) tuple with all required prediction keys.
    """
    engine = _make_engine(min_prob=0.0)

    # Patch the core engine's analyse() to return a known signal
    fake_signal = _make_signal()
    engine.core_engine.analyse = MagicMock(return_value=fake_signal)

    result = engine.analyse(candles_htf=[], candles_ltf=[])

    assert result is not None, "Should return a result when signal is produced"
    signal, prediction = result
    assert isinstance(signal, TradeSignal), "First element must be a TradeSignal"
    assert "win_probability"  in prediction
    assert "confidence_tier"  in prediction
    assert "action"           in prediction
    assert "model_based"      in prediction
    assert 0.0 <= prediction["win_probability"] <= 1.0
    assert prediction["confidence_tier"] in ("PREMIUM", "HIGH", "MEDIUM", "LOW")
    assert prediction["model_based"] == False, "No model file — should be rule-based"
    print(f"PASS — analyse() structure | prob={prediction['win_probability']:.2f} | "
          f"tier={prediction['confidence_tier']}")


def test_analyse_returns_none_when_no_signal():
    """
    When the core engine returns None, MLEngine.analyse() must return None.
    """
    engine = _make_engine()
    engine.core_engine.analyse = MagicMock(return_value=None)

    result = engine.analyse(candles_htf=[], candles_ltf=[])

    assert result is None, "analyse() must return None when core engine returns None"
    print("PASS — analyse() returns None when no signal")


def test_analyse_filters_by_min_probability():
    """
    Signals with win_probability below min_probability must be filtered out.
    """
    engine = _make_engine(min_prob=0.99)  # Effectively impossible threshold

    fake_signal = _make_signal(score=1, fvg=False, ob=False, killzone=False)
    engine.core_engine.analyse = MagicMock(return_value=fake_signal)

    result = engine.analyse(candles_htf=[], candles_ltf=[])

    # Rule-based: score=1 → prob ≈ 0.40+0.09 = 0.49 — well below 0.99
    assert result is None, "Low-probability signal should be filtered by min_probability"
    print("PASS — analyse() filters signal below min_probability threshold")


# ─────────────────────────────────────────────────────────────────────────────
# MLEngine.run()
# ─────────────────────────────────────────────────────────────────────────────

def test_run_returns_none_and_does_not_alert_when_no_signal():
    """
    run() wraps analyse(). When no signal is found, it must return None
    and not call alert().
    """
    engine = _make_engine()
    engine.core_engine.analyse = MagicMock(return_value=None)
    engine.alert = MagicMock()

    result = engine.run(candles_htf=[], candles_ltf=[])

    assert result is None
    engine.alert.assert_not_called()
    print("PASS — run() returns None and skips alert when no signal")


def test_run_calls_alert_when_signal_above_threshold():
    """
    run() must call alert() exactly once when a signal passes the probability
    threshold.
    """
    engine = _make_engine(min_prob=0.0)

    fake_signal = _make_signal()
    engine.core_engine.analyse = MagicMock(return_value=fake_signal)
    engine.alert = MagicMock()

    result = engine.run(candles_htf=[], candles_ltf=[])

    assert result is not None, "run() should return (signal, prediction)"
    engine.alert.assert_called_once()

    call_args = engine.alert.call_args[0]
    assert call_args[0] is fake_signal, "alert() should receive the TradeSignal"
    assert "win_probability" in call_args[1], "alert() should receive the prediction dict"
    print("PASS — run() calls alert() exactly once with signal + prediction")


def test_run_returns_tuple_structure():
    """run() must return (TradeSignal, dict) — not just the signal alone."""
    engine = _make_engine(min_prob=0.0)

    fake_signal = _make_signal()
    engine.core_engine.analyse = MagicMock(return_value=fake_signal)
    engine.alert = MagicMock()

    result = engine.run(candles_htf=[], candles_ltf=[])

    assert isinstance(result, tuple), "run() must return a tuple"
    assert len(result) == 2, "Tuple must have exactly 2 elements"
    signal, prediction = result
    assert isinstance(signal, TradeSignal)
    assert isinstance(prediction, dict)
    print("PASS — run() returns (TradeSignal, dict) tuple")


# ─────────────────────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n── Trader Copilot — MLEngine Test Suite ──\n")

    tests = [
        test_signal_to_dict_produces_valid_feature_dict,
        test_signal_to_dict_session_new_york_open,
        test_signal_to_dict_session_london,
        test_signal_to_dict_does_not_require_live_db,
        test_analyse_returns_correct_structure_when_signal_produced,
        test_analyse_returns_none_when_no_signal,
        test_analyse_filters_by_min_probability,
        test_run_returns_none_and_does_not_alert_when_no_signal,
        test_run_calls_alert_when_signal_above_threshold,
        test_run_returns_tuple_structure,
    ]

    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback
            print(f"FAIL — {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n── Results: {passed} passed, {failed} failed ──\n")
