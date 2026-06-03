"""
Trader Copilot — Pair Configuration
Pair-specific timeframes, sessions, and tolerances.
Exness demo account symbol names (m suffix).
"""
from dataclasses import dataclass
from datetime import time
from typing import List, Tuple
@dataclass
class PairConfig:
    symbol: str
    structure_tf: str
    entry_tf: str
    killzones: List[str]
    sweep_wick_pips: float
    fvg_min_pips: float
    peak_equality_pips: float
    pip_size: float
    @property
    def killzone_windows(self) -> List[Tuple[time, time]]:
        windows = []
        for name in self.killzones:
            if name in KILLZONE_WINDOWS:
                h_start, m_start, h_end, m_end = KILLZONE_WINDOWS[name]
                windows.append((time(h_start, m_start), time(h_end, m_end)))
        return windows
KILLZONE_WINDOWS = {
    "london":        (7,  0,  10, 0),
    "london_open":   (7,  0,   8, 30),
    "new_york":      (12, 0,  16, 0),
    "new_york_open": (13, 0,  14, 30),
    "london_close":  (15, 0,  16, 0),
    "tokyo":         (0,  0,   3, 0),
    "sydney":        (22, 0,  24, 0),
}
PAIR_CONFIGS = {
    # ── Majors ────────────────────────────────────────────────────────────────
    "EURUSDm": PairConfig("EURUSDm", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    "GBPUSDm": PairConfig("GBPUSDm", "30M", "5M", ["london","new_york"],         4.0, 2.5, 6.0, 0.0001),
    "USDJPYm": PairConfig("USDJPYm", "30M", "5M", ["london","new_york","tokyo"], 4.0, 2.5, 6.0, 0.01),
    "USDCHFm": PairConfig("USDCHFm", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    "AUDUSDm": PairConfig("AUDUSDm", "30M", "5M", ["london","sydney"],           3.0, 2.0, 5.0, 0.0001),
    "USDCADm": PairConfig("USDCADm", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    "NZDUSDm": PairConfig("NZDUSDm", "30M", "5M", ["london","new_york"],          3.0, 2.0, 5.0, 0.0001),
    # ── Euro crosses ──────────────────────────────────────────────────────────
    "EURGBPm": PairConfig("EURGBPm", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    "EURJPYm": PairConfig("EURJPYm", "30M", "5M", ["london","new_york","tokyo"], 4.0, 2.5, 6.0, 0.01),
    "EURCHFm": PairConfig("EURCHFm", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    "EURAUDm": PairConfig("EURAUDm", "30M", "5M", ["london","sydney"],           4.0, 2.5, 6.0, 0.0001),
    "EURCADm": PairConfig("EURCADm", "30M", "5M", ["london","new_york"],         4.0, 2.5, 6.0, 0.0001),
    "EURNZDm": PairConfig("EURNZDm", "30M", "5M", ["london","sydney"],           4.0, 2.5, 6.0, 0.0001),
    # ── Pound crosses ─────────────────────────────────────────────────────────
    "GBPJPYm": PairConfig("GBPJPYm", "30M", "5M", ["london","new_york","tokyo"], 5.0, 3.0, 8.0, 0.01),
    "GBPCHFm": PairConfig("GBPCHFm", "30M", "5M", ["london","new_york"],         4.0, 2.5, 6.0, 0.0001),
    "GBPAUDm": PairConfig("GBPAUDm", "30M", "5M", ["london","sydney"],           5.0, 3.0, 7.0, 0.0001),
    "GBPCADm": PairConfig("GBPCADm", "30M", "5M", ["london","new_york"],         5.0, 3.0, 7.0, 0.0001),
    "GBPNZDm": PairConfig("GBPNZDm", "30M", "5M", ["london","sydney"],           5.0, 3.0, 7.0, 0.0001),
    # ── Yen crosses ───────────────────────────────────────────────────────────
    "AUDJPYm": PairConfig("AUDJPYm", "30M", "5M", ["london","sydney","tokyo"],   4.0, 2.5, 6.0, 0.01),
    "CADJPYm": PairConfig("CADJPYm", "30M", "5M", ["london","new_york","tokyo"], 4.0, 2.5, 6.0, 0.01),
    "CHFJPYm": PairConfig("CHFJPYm", "30M", "5M", ["london","new_york","tokyo"], 4.0, 2.5, 6.0, 0.01),
    "NZDJPYm": PairConfig("NZDJPYm", "30M", "5M", ["london","sydney","tokyo"],   4.0, 2.5, 6.0, 0.01),
    # ── Exotics ───────────────────────────────────────────────────────────────
    "USDZARm": PairConfig("USDZARm", "30M", "5M", ["london_open","new_york_open"], 5.0, 3.0, 8.0, 0.0001),
    # ── Commodities / Indices ─────────────────────────────────────────────────
    "XAUUSDm":     PairConfig("XAUUSDm",     "4H", "15M", ["london_open","new_york_open"], 0.50, 0.30, 0.80, 1.0),
    "USTEC_x100m": PairConfig("USTEC_x100m", "4H", "15M", ["new_york_open"],               10.0, 5.0, 20.0, 1.0),
}
