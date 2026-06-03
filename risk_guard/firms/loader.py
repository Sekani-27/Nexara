"""
Risk Guard — Firm Config Loader
Reads a YAML file from the firms/ directory and returns a validated FirmConfig.
"""

import os
from typing import List

from ..models import FirmConfig

_FIRMS_DIR = os.path.dirname(os.path.abspath(__file__))

_REQUIRED_FIELDS: List[str] = [
    "daily_dd_pct", "total_dd_pct", "target_pct", "dd_calc",
    "rollover_time", "rollover_tz",
    "min_hold_seconds", "min_valid_day_pct", "min_valid_days",
    "news", "weekend",
    "internal_soft_stop_pct", "internal_hard_stop_pct",
    "risk_per_trade_pct", "max_open_risk_pct",
    "max_trades_per_day", "revenge_lock_hours",
]


def load_firm(name: str) -> FirmConfig:
    """
    Load and validate a firm config by name (case-insensitive).
    Looks for  <name>.yaml  in the same directory as this file.
    """
    try:
        import yaml  # PyYAML
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required to load firm configs. "
            "Run: pip install pyyaml"
        ) from exc

    filename = f"{name.lower()}.yaml"
    path = os.path.join(_FIRMS_DIR, filename)
    if not os.path.exists(path):
        available = list_firms()
        raise FileNotFoundError(
            f"Firm config '{filename}' not found in {_FIRMS_DIR}. "
            f"Available: {available}"
        )

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"Firm config '{filename}' is not a valid YAML mapping.")

    missing = [f for f in _REQUIRED_FIELDS if f not in data]
    if missing:
        raise ValueError(
            f"Firm config '{name}' is missing required fields: {missing}"
        )

    return FirmConfig(
        name=name,
        daily_dd_pct=float(data["daily_dd_pct"]),
        total_dd_pct=float(data["total_dd_pct"]),
        target_pct=float(data["target_pct"]),
        dd_calc=str(data["dd_calc"]),
        rollover_time=str(data["rollover_time"]),
        rollover_tz=str(data["rollover_tz"]),
        min_hold_seconds=int(data["min_hold_seconds"]),
        min_valid_day_pct=float(data["min_valid_day_pct"]),
        min_valid_days=int(data["min_valid_days"]),
        news=bool(data["news"]),
        weekend=bool(data["weekend"]),
        internal_soft_stop_pct=float(data["internal_soft_stop_pct"]),
        internal_hard_stop_pct=float(data["internal_hard_stop_pct"]),
        risk_per_trade_pct=float(data["risk_per_trade_pct"]),
        max_open_risk_pct=float(data["max_open_risk_pct"]),
        max_trades_per_day=int(data["max_trades_per_day"]),
        revenge_lock_hours=int(data["revenge_lock_hours"]),
    )


def list_firms() -> List[str]:
    """Return names of all available firm configs (without .yaml extension)."""
    return sorted(
        f[:-5]
        for f in os.listdir(_FIRMS_DIR)
        if f.endswith(".yaml")
    )
