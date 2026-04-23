"""
Trader Copilot — Feature Engineering
Converts raw journal trade records into ML-ready feature vectors.

Features extracted per trade:
  — Pattern type (one-hot)
  — Direction (binary)
  — Confluence score (ordinal)
  — FVG present (binary)
  — OB present (binary)
  — Unicorn confluence — FVG + OB together (binary)
  — Killzone active (binary)
  — Session (one-hot)
  — Symbol (one-hot)
  — Hour of day (cyclical sin/cos encoding)
  — Day of week (cyclical sin/cos encoding)
  — Risk:Reward ratio
"""

import math
import numpy as np
from typing import List, Dict, Tuple, Optional
from datetime import datetime


# All known pattern names
PATTERN_TYPES = [
    "Double Top",
    "Double Bottom",
    "Head and Shoulders",
    "Inverse Head and Shoulders",
    "Ascending Flag",
    "Descending Flag",
]

SYMBOLS = ["XAUUSDm", "USTEC_x100m", "EURUSDm", "GBPUSDm"]

SESSIONS = ["london", "london_open", "new_york", "new_york_open",
            "london_ny_overlap_pre", "off_session"]


def trade_to_features(trade: dict) -> Optional[np.ndarray]:
    """
    Convert a single journal trade record to a feature vector.
    Returns None if trade is missing required fields.
    """
    try:
        # ── Pattern one-hot (6 features)
        pattern_vec = [0.0] * len(PATTERN_TYPES)
        pattern_name = trade.get("pattern", "")
        if pattern_name in PATTERN_TYPES:
            pattern_vec[PATTERN_TYPES.index(pattern_name)] = 1.0

        # ── Symbol one-hot (4 features)
        symbol_vec = [0.0] * len(SYMBOLS)
        symbol = trade.get("symbol", "")
        if symbol in SYMBOLS:
            symbol_vec[SYMBOLS.index(symbol)] = 1.0

        # ── Session one-hot (6 features)
        session_vec = [0.0] * len(SESSIONS)
        session = trade.get("session", "off_session")
        if session in SESSIONS:
            session_vec[SESSIONS.index(session)] = 1.0

        # ── Direction (1 feature): bearish=0, bullish=1
        direction = 1.0 if trade.get("direction") == "bullish" else 0.0

        # ── Confluence factors (5 features)
        score   = float(trade.get("confluence_score", 3)) / 5.0   # normalise 0-1
        fvg     = float(bool(trade.get("fvg_present", 0)))
        ob      = float(bool(trade.get("ob_present", 0)))
        unicorn = fvg * ob                                          # both present
        kz      = float(bool(trade.get("killzone_active", 0)))

        # ── Risk:Reward (1 feature, clipped 0-5)
        rr = float(trade.get("risk_reward", 2.0))
        rr_norm = min(rr, 5.0) / 5.0

        # ── Temporal: hour of day cyclical (2 features)
        signal_time = trade.get("signal_time", "")
        try:
            ts   = datetime.fromisoformat(signal_time)
            hour = ts.hour + ts.minute / 60.0
            dow  = ts.weekday()
        except Exception:
            hour = 12.0
            dow  = 1

        hour_sin = math.sin(2 * math.pi * hour / 24.0)
        hour_cos = math.cos(2 * math.pi * hour / 24.0)
        dow_sin  = math.sin(2 * math.pi * dow / 5.0)
        dow_cos  = math.cos(2 * math.pi * dow / 5.0)

        # ── Assemble feature vector
        features = (
            pattern_vec      +   # 6
            symbol_vec       +   # 4
            session_vec      +   # 6
            [direction]      +   # 1
            [score, fvg, ob, unicorn, kz]  +  # 5
            [rr_norm]        +   # 1
            [hour_sin, hour_cos, dow_sin, dow_cos]  # 4
        )

        return np.array(features, dtype=np.float32)

    except Exception as e:
        return None


def trades_to_dataset(
    trades: List[dict],
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Convert a list of completed journal trades into (X, y) arrays.
    y=1 for wins (tp_hit, manual_win), y=0 for losses/breakeven.
    Skips pending trades.
    """
    X_rows = []
    y_rows = []

    for trade in trades:
        if trade.get("outcome") in ("pending", None):
            continue

        features = trade_to_features(trade)
        if features is None:
            continue

        label = 1 if trade["outcome"] in ("tp_hit", "manual_win") else 0
        X_rows.append(features)
        y_rows.append(label)

    if not X_rows:
        return np.array([]), np.array([])

    return np.array(X_rows, dtype=np.float32), np.array(y_rows, dtype=np.int32)


def feature_names() -> List[str]:
    """Return ordered list of feature names matching the vector layout."""
    names = []
    for p in PATTERN_TYPES:
        names.append(f"pattern_{p.lower().replace(' ', '_')}")
    for s in SYMBOLS:
        names.append(f"symbol_{s.lower()}")
    for s in SESSIONS:
        names.append(f"session_{s}")
    names += [
        "direction_bullish",
        "confluence_score_norm",
        "fvg_present",
        "ob_present",
        "unicorn_confluence",
        "killzone_active",
        "rr_norm",
        "hour_sin", "hour_cos",
        "dow_sin",  "dow_cos",
    ]
    return names


FEATURE_DIM = len(feature_names())
