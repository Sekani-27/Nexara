"""
Trader Copilot — Pair Configuration
Pair-specific timeframes, sessions, and tolerances.
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
    "EURUSD": PairConfig("EURUSD", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    "GBPUSD": PairConfig("GBPUSD", "30M", "5M", ["london","new_york"],         4.0, 2.5, 6.0, 0.0001),
    "USDJPY": PairConfig("USDJPY", "30M", "5M", ["london","new_york","tokyo"], 4.0, 2.5, 6.0, 0.01),
    "USDCHF": PairConfig("USDCHF", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    "AUDUSD": PairConfig("AUDUSD", "30M", "5M", ["london","sydney"],           3.0, 2.0, 5.0, 0.0001),
    "USDCAD": PairConfig("USDCAD", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    "NZDUSD": PairConfig("NZDUSD", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    # ── Euro crosses ──────────────────────────────────────────────────────────
    "EURGBP": PairConfig("EURGBP", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    "EURJPY": PairConfig("EURJPY", "30M", "5M", ["london","new_york","tokyo"], 4.0, 2.5, 6.0, 0.01),
    "EURCHF": PairConfig("EURCHF", "30M", "5M", ["london","new_york"],         3.0, 2.0, 5.0, 0.0001),
    "EURAUD": PairConfig("EURAUD", "30M", "5M", ["london","sydney"],           4.0, 2.5, 6.0, 0.0001),
    "EURCAD": PairConfig("EURCAD", "30M", "5M", ["london","new_york"],         4.0, 2.5, 6.0, 0.0001),
    "EURNZD": PairConfig("EURNZD", "30M", "5M", ["london","sydney"],           4.0, 2.5, 6.0, 0.0001),
    # ── Pound crosses ─────────────────────────────────────────────────────────
    "GBPJPY": PairConfig("GBPJPY", "30M", "5M", ["london","new_york","tokyo"], 5.0, 3.0, 8.0, 0.01),
    "GBPCHF": PairConfig("GBPCHF", "30M", "5M", ["london","new_york"],         4.0, 2.5, 6.0, 0.0001),
    "GBPAUD": PairConfig("GBPAUD", "30M", "5M", ["london","sydney"],           5.0, 3.0, 7.0, 0.0001),
    "GBPCAD": PairConfig("GBPCAD", "30M", "5M", ["london","new_york"],         5.0, 3.0, 7.0, 0.0001),
    "GBPNZD": PairConfig("GBPNZD", "30M", "5M", ["london","sydney"],           5.0, 3.0, 7.0, 0.0001),
    # ── Yen crosses ───────────────────────────────────────────────────────────
    "AUDJPY": PairConfig("AUDJPY", "30M", "5M", ["london","sydney","tokyo"],   4.0, 2.5, 6.0, 0.01),
    "CADJPY": PairConfig("CADJPY", "30M", "5M", ["london","new_york","tokyo"], 4.0, 2.5, 6.0, 0.01),
    "CHFJPY": PairConfig("CHFJPY", "30M", "5M", ["london","new_york","tokyo"], 4.0, 2.5, 6.0, 0.01),
    "NZDJPY": PairConfig("NZDJPY", "30M", "5M", ["london","sydney","tokyo"],   4.0, 2.5, 6.0, 0.01),
    # ── Exotics ───────────────────────────────────────────────────────────────
    "USDZAR": PairConfig("USDZAR", "30M", "5M", ["london_open","new_york_open"], 5.0, 3.0, 8.0, 0.0001),
    # ── Commodities / Indices ─────────────────────────────────────────────────
    "XAUUSD": PairConfig("XAUUSD", "4H", "15M", ["london_open","new_york_open"], 0.50, 0.30, 0.80, 1.0),
    "NAS100": PairConfig("NAS100", "4H", "15M", ["new_york_open"],               10.0, 5.0, 20.0, 1.0),
}
