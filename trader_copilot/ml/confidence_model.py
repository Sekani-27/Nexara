"""
Trader Copilot — ML Confidence Model
Trains a gradient-boosted classifier on journal outcomes.
Outputs a win probability (0.0 – 1.0) for each new signal.

Model: GradientBoostingClassifier
  - Handles small datasets well (50+ trades)
  - Interpretable via feature importances
  - No GPU required
  - Fast inference (<1ms per prediction)

Fallback: if < MIN_SAMPLES completed trades exist,
  the model returns the rule-based confluence score
  scaled to a probability (score/5 * 0.85).
"""

import os
import json
import pickle
import warnings
import numpy as np
from typing import Optional, Tuple, List, Dict
from datetime import datetime

from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.preprocessing import StandardScaler
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import (
    classification_report, roc_auc_score,
    confusion_matrix, brier_score_loss
)
import warnings
warnings.filterwarnings("ignore")

from .features import trades_to_dataset, trade_to_features, feature_names, FEATURE_DIM

MIN_SAMPLES   = 30    # Minimum completed trades before ML kicks in
MIN_PER_CLASS = 5     # Need at least 5 wins and 5 losses to train


class ConfidenceModel:

    def __init__(self, model_path: str = "trader_copilot_model.pkl"):
        self.model_path  = model_path
        self.model       = None
        self.scaler      = StandardScaler()
        self.is_trained  = False
        self.train_stats = {}
        self.feature_importances: Dict[str, float] = {}

    # ─────────────────────────────────────────────
    # TRAIN
    # ─────────────────────────────────────────────

    def train(self, trades: List[dict]) -> dict:
        """
        Train on completed journal trades.
        Returns evaluation metrics dict.
        """
        X, y = trades_to_dataset(trades)

        if len(X) == 0:
            return {"error": "No completed trades found in journal."}

        n_wins   = int(y.sum())
        n_losses = len(y) - n_wins

        if len(X) < MIN_SAMPLES:
            return {
                "error": f"Need {MIN_SAMPLES} completed trades to train. "
                         f"Currently have {len(X)}. Using rule-based scoring."
            }

        if n_wins < MIN_PER_CLASS or n_losses < MIN_PER_CLASS:
            return {
                "error": f"Need at least {MIN_PER_CLASS} wins and {MIN_PER_CLASS} losses. "
                         f"Currently: {n_wins} wins, {n_losses} losses."
            }

        print(f"[ML] Training on {len(X)} trades | {n_wins} wins | {n_losses} losses")

        # Scale features
        X_scaled = self.scaler.fit_transform(X)

        # Primary model: Gradient Boosting (handles small datasets well)
        base_model = GradientBoostingClassifier(
            n_estimators=150,
            max_depth=3,
            learning_rate=0.08,
            subsample=0.85,
            min_samples_leaf=3,
            random_state=42,
        )

        # Calibrate probabilities (Platt scaling)
        self.model = CalibratedClassifierCV(base_model, cv=3, method="sigmoid")
        self.model.fit(X_scaled, y)
        self.is_trained = True

        # ── Cross-validation evaluation
        cv = StratifiedKFold(n_splits=min(5, n_wins), shuffle=True, random_state=42)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cv_scores = cross_val_score(self.model, X_scaled, y, cv=cv, scoring="roc_auc")

        # ── In-sample metrics
        y_pred  = self.model.predict(X_scaled)
        y_proba = self.model.predict_proba(X_scaled)[:, 1]

        auc     = round(roc_auc_score(y, y_proba), 4)
        brier   = round(brier_score_loss(y, y_proba), 4)
        cv_mean = round(cv_scores.mean(), 4)
        cv_std  = round(cv_scores.std(), 4)

        # ── Feature importances (from base estimators)
        try:
            raw_model = self.model.calibrated_classifiers_[0].estimator
            importances = raw_model.feature_importances_
            names = feature_names()
            self.feature_importances = {
                names[i]: round(float(importances[i]), 4)
                for i in range(len(names))
            }
        except Exception:
            self.feature_importances = {}

        self.train_stats = {
            "trained_at":   datetime.utcnow().isoformat(),
            "n_samples":    len(X),
            "n_wins":       n_wins,
            "n_losses":     n_losses,
            "win_rate":     round(n_wins / len(X) * 100, 1),
            "roc_auc":      auc,
            "brier_score":  brier,
            "cv_auc_mean":  cv_mean,
            "cv_auc_std":   cv_std,
            "top_features": dict(sorted(
                self.feature_importances.items(),
                key=lambda x: -x[1]
            )[:8]),
        }

        self.save()

        print(f"[ML] Training complete")
        print(f"     ROC-AUC    : {auc} (1.0 = perfect)")
        print(f"     CV AUC     : {cv_mean} ± {cv_std}")
        print(f"     Brier score: {brier} (0.0 = perfect)")

        return self.train_stats

    # ─────────────────────────────────────────────
    # PREDICT
    # ─────────────────────────────────────────────

    def predict(self, trade: dict) -> dict:
        """
        Predict win probability for a new signal.
        Returns a dict with probability, confidence tier, and recommendation.
        Falls back to rule-based scoring if model not trained.
        """
        if not self.is_trained:
            return self._rule_based_fallback(trade)

        features = trade_to_features(trade)
        if features is None:
            return self._rule_based_fallback(trade)

        try:
            X_scaled = self.scaler.transform(features.reshape(1, -1))
            prob     = float(self.model.predict_proba(X_scaled)[0][1])
        except Exception:
            return self._rule_based_fallback(trade)

        tier, action = self._tier(prob)

        return {
            "win_probability":  round(prob, 3),
            "confidence_tier":  tier,
            "action":           action,
            "model_based":      True,
            "confluence_score": trade.get("confluence_score", 0),
        }

    def predict_batch(self, trades: List[dict]) -> List[dict]:
        return [self.predict(t) for t in trades]

    def _rule_based_fallback(self, trade: dict) -> dict:
        """
        Before enough data exists, scale confluence score to probability.
        Score 3 → ~0.55, Score 4 → ~0.70, Score 5 → ~0.85
        FVG and killzone boost it slightly.
        """
        score = trade.get("confluence_score", 3)
        base  = 0.40 + (score / 5.0) * 0.45
        if trade.get("fvg_present"):    base += 0.05
        if trade.get("ob_present"):     base += 0.03
        if trade.get("killzone_active"): base += 0.04
        prob = min(base, 0.92)

        tier, action = self._tier(prob)
        return {
            "win_probability":  round(prob, 3),
            "confidence_tier":  tier,
            "action":           action,
            "model_based":      False,
            "confluence_score": score,
        }

    def _tier(self, prob: float) -> Tuple[str, str]:
        if prob >= 0.75:
            return "PREMIUM",  "Take trade — size normally or larger"
        elif prob >= 0.60:
            return "HIGH",     "Take trade — standard size"
        elif prob >= 0.45:
            return "MEDIUM",   "Take trade — reduce size or skip"
        else:
            return "LOW",      "Skip — below confidence threshold"

    # ─────────────────────────────────────────────
    # SAVE / LOAD
    # ─────────────────────────────────────────────

    def save(self):
        payload = {
            "model":      self.model,
            "scaler":     self.scaler,
            "stats":      self.train_stats,
            "importances": self.feature_importances,
        }
        with open(self.model_path, "wb") as f:
            pickle.dump(payload, f)
        print(f"[ML] Model saved → {self.model_path}")

    def load(self) -> bool:
        if not os.path.exists(self.model_path):
            return False
        try:
            with open(self.model_path, "rb") as f:
                payload = pickle.load(f)
            self.model               = payload["model"]
            self.scaler              = payload["scaler"]
            self.train_stats         = payload.get("stats", {})
            self.feature_importances = payload.get("importances", {})
            self.is_trained          = True
            print(f"[ML] Model loaded from {self.model_path}")
            return True
        except Exception as e:
            print(f"[ML] Load failed: {e}")
            return False

    # ─────────────────────────────────────────────
    # REPORT
    # ─────────────────────────────────────────────

    def print_report(self):
        if not self.train_stats:
            print("[ML] No training stats available.")
            return

        s = self.train_stats
        print(f"""
╔══════════════════════════════════════════════╗
  TRADER COPILOT — ML CONFIDENCE MODEL REPORT
╚══════════════════════════════════════════════╝

  Trained at    : {s.get('trained_at','—')[:19]}
  Samples       : {s.get('n_samples')}  ({s.get('n_wins')} wins / {s.get('n_losses')} losses)
  Base win rate : {s.get('win_rate')}%

  ROC-AUC       : {s.get('roc_auc')}   (>0.70 = useful, >0.80 = strong)
  CV AUC        : {s.get('cv_auc_mean')} ± {s.get('cv_auc_std')}
  Brier score   : {s.get('brier_score')}  (<0.20 = good calibration)

── Top predictive features ──────────────────────""")

        for feat, imp in list(s.get("top_features", {}).items())[:8]:
            bar = "█" * int(imp * 100)
            print(f"  {feat:<35} {imp:.4f}  {bar}")

        print(f"\n{'═' * 48}\n")
