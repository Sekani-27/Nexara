"""
Trader Copilot — Phase 3 Test Suite
Tests feature engineering, model training, prediction, and ML engine.
Run: PYTHONPATH=/path/to/project python trader_copilot/tests/test_phase3.py
"""

import os, tempfile

import numpy as np
from datetime import datetime, timedelta
from trader_copilot.ml.features import (
    trade_to_features, trades_to_dataset, feature_names, FEATURE_DIM
)
from trader_copilot.ml.confidence_model import ConfidenceModel
from trader_copilot.core.structures import TradeSignal, Direction


def make_trade(outcome="tp_hit", score=4, fvg=True, ob=True,
               pattern="Double Top", symbol="XAUUSD",
               direction="bearish", killzone=True, rr=2.0,
               hour=13) -> dict:
    ts = datetime(2024, 6, 3, hour, 30)
    return {
        "symbol":           symbol,
        "direction":        direction,
        "pattern":          pattern,
        "confluence_score": score,
        "fvg_present":      int(fvg),
        "ob_present":       int(ob),
        "killzone_active":  int(killzone),
        "risk_reward":      rr,
        "signal_time":      ts.isoformat(),
        "session":          "new_york",
        "outcome":          outcome,
        "pnl_rr":           2.0 if outcome in ("tp_hit","manual_win") else -1.0,
    }


def test_feature_vector_shape():
    """Feature vector has correct dimension and all values are finite."""
    trade    = make_trade()
    features = trade_to_features(trade)
    assert features is not None, "Should produce features"
    assert features.shape == (FEATURE_DIM,), f"Expected ({FEATURE_DIM},), got {features.shape}"
    assert np.all(np.isfinite(features)), "All features should be finite"
    print(f"PASS — Feature vector shape: {features.shape} | All finite")


def test_dataset_construction():
    """Dataset skips pending trades and labels correctly."""
    trades = [
        make_trade("tp_hit"),
        make_trade("sl_hit"),
        make_trade("tp_hit"),
        make_trade("pending"),      # should be skipped
        make_trade("manual_win"),
        make_trade("manual_loss"),
    ]
    X, y = trades_to_dataset(trades)
    assert len(X) == 5, f"Expected 5 rows (1 pending skipped), got {len(X)}"
    assert y.sum() == 3, f"Expected 3 wins, got {y.sum()}"
    assert (y == 0).sum() == 2, f"Expected 2 losses, got {(y==0).sum()}"
    print(f"PASS — Dataset: {len(X)} samples | {y.sum()} wins | {(y==0).sum()} losses")


def test_rule_based_fallback():
    """Rule-based fallback scales correctly before model is trained."""
    model = ConfidenceModel.__new__(ConfidenceModel)
    model.is_trained = False

    high_q = make_trade(score=5, fvg=True, ob=True, killzone=True)
    low_q  = make_trade(score=3, fvg=False, ob=False, killzone=False)

    pred_high = model._rule_based_fallback(high_q)
    pred_low  = model._rule_based_fallback(low_q)

    assert pred_high["win_probability"] > pred_low["win_probability"], \
        "High-quality setup should have higher probability"
    assert pred_high["win_probability"] >= 0.70, \
        f"Score-5 setup should be >= 0.70, got {pred_high['win_probability']}"
    assert not pred_high["model_based"], "Should flag as rule-based"

    print(f"PASS — Rule-based: score-5={pred_high['win_probability']:.2f}  "
          f"score-3={pred_low['win_probability']:.2f}")


def test_model_training():
    """Model trains successfully with sufficient data and produces valid predictions."""
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
        model_path = f.name

    # Generate 60 synthetic trades — 40 wins, 20 losses
    trades = []
    for i in range(40):
        trades.append(make_trade(
            outcome="tp_hit", score=4 + (i % 2),
            fvg=(i % 3 != 0), ob=(i % 2 == 0),
            killzone=True, hour=13 + (i % 3),
            pattern=["Double Top","Head and Shoulders","Double Bottom"][i % 3]
        ))
    for i in range(20):
        trades.append(make_trade(
            outcome="sl_hit", score=3,
            fvg=False, ob=False,
            killzone=False, hour=5 + i % 4,
            pattern=["Double Top","Double Bottom"][i % 2],
            direction=["bearish","bullish"][i % 2]
        ))

    model  = ConfidenceModel(model_path=model_path)
    stats  = model.train(trades)

    assert "error" not in stats, f"Training failed: {stats.get('error')}"
    assert model.is_trained, "Model should be trained"
    assert stats["roc_auc"] >= 0.5, f"AUC should be >= 0.5, got {stats['roc_auc']}"

    # Prediction on a premium setup
    premium = make_trade(score=5, fvg=True, ob=True, killzone=True)
    pred    = model.predict(premium)

    assert pred["model_based"], "Should use ML model"
    assert 0.0 <= pred["win_probability"] <= 1.0, "Probability must be in [0,1]"
    assert pred["confidence_tier"] in ("PREMIUM","HIGH","MEDIUM","LOW")

    # Premium setup should outscore a weak setup
    weak     = make_trade(score=3, fvg=False, ob=False, killzone=False)
    pred_weak = model.predict(weak)
    assert pred["win_probability"] >= pred_weak["win_probability"], \
        "Premium setup should have higher probability than weak setup"

    print(f"PASS — Model trained | AUC={stats['roc_auc']} | "
          f"Premium prob={pred['win_probability']:.2f} > "
          f"Weak prob={pred_weak['win_probability']:.2f}")

    os.unlink(model_path)


def test_model_save_load():
    """Model saves and loads correctly, predictions are stable."""
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
        model_path = f.name

    trades = []
    for i in range(40):
        trades.append(make_trade("tp_hit", score=4, fvg=True, killzone=True))
    for i in range(20):
        trades.append(make_trade("sl_hit", score=3, fvg=False, killzone=False))

    model1 = ConfidenceModel(model_path=model_path)
    model1.train(trades)

    trade = make_trade(score=4, fvg=True)
    pred1 = model1.predict(trade)

    # Load into fresh model
    model2 = ConfidenceModel(model_path=model_path)
    model2.load()
    pred2  = model2.predict(trade)

    assert abs(pred1["win_probability"] - pred2["win_probability"]) < 0.001, \
        "Predictions should be identical after save/load"

    print(f"PASS — Save/load stable | prob={pred1['win_probability']:.3f}")
    os.unlink(model_path)


def test_feature_names_match_dim():
    """Feature names list matches FEATURE_DIM exactly."""
    names = feature_names()
    assert len(names) == FEATURE_DIM, \
        f"feature_names() returns {len(names)} but FEATURE_DIM={FEATURE_DIM}"
    assert len(set(names)) == len(names), "Feature names must be unique"
    print(f"PASS — Feature names: {len(names)} unique features")


def test_tier_thresholds():
    """Tier labels are assigned at correct probability cutoffs."""
    model = ConfidenceModel.__new__(ConfidenceModel)
    assert model._tier(0.80)[0] == "PREMIUM"
    assert model._tier(0.65)[0] == "HIGH"
    assert model._tier(0.50)[0] == "MEDIUM"
    assert model._tier(0.35)[0] == "LOW"
    print("PASS — Tier thresholds correct")


if __name__ == "__main__":
    print("\n── Trader Copilot Phase 3 — Test Suite ──\n")
    test_feature_vector_shape()
    test_dataset_construction()
    test_rule_based_fallback()
    test_model_training()
    test_model_save_load()
    test_feature_names_match_dim()
    test_tier_thresholds()
    print("\n── All Phase 3 tests passed ──\n")
