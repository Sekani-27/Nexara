"""
Trader Copilot — Massive (Polygon.io) Data Connector
=====================================================
Alternative to MT5 for fetching OHLCV candles via the Polygon.io REST API.
Used as the primary data source on Railway / Linux where MetaTrader5 is
unavailable.

Usage
-----
    from trader_copilot.utils.massive_connector import MassiveConnector
    connector = MassiveConnector()
    candles = connector.get_candles("EURUSDm", "30M", count=300)

Environment
-----------
    MASSIVE_API_KEY   — Polygon.io API key (required)
"""

import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..core.structures import Candle

log = logging.getLogger(__name__)

_BASE = "https://api.polygon.io"

# ── Internal symbol → Polygon.io ticker ──────────────────────────────────────
# Forex pairs use the "C:" (currency) prefix.
# Indices use the "I:" prefix.
# Gold spot (XAUUSD) is treated as a forex pair on Polygon.
SYMBOL_MAP: dict = {
    # ── Active runner pairs ───────────────────────────────────────────────────
    "EURUSDm":     "C:EURUSD",
    "GBPUSDm":     "C:GBPUSD",
    "EURAUDm":     "C:EURAUD",
    "EURCADm":     "C:EURCAD",
    "CADJPYm":     "C:CADJPY",
    "GBPCADm":     "C:GBPCAD",
    "XAUUSDm":     "C:XAUUSD",     # Gold spot — commodity, price-unit pip precision
    "USTEC_x100m": "I:NDX",        # Nasdaq-100 index — point-unit precision
    # ── Additional PAIR_CONFIGS entries (not in default ALL_PAIRS) ────────────
    "USDJPYm":     "C:USDJPY",
    "USDCHFm":     "C:USDCHF",
    "AUDUSDm":     "C:AUDUSD",
    "USDCADm":     "C:USDCAD",
    "NZDUSDm":     "C:NZDUSD",
    "EURGBPm":     "C:EURGBP",
    "EURJPYm":     "C:EURJPY",
    "EURCHFm":     "C:EURCHF",
    "EURNZDm":     "C:EURNZD",
    "GBPJPYm":     "C:GBPJPY",
    "GBPCHFm":     "C:GBPCHF",
    "GBPAUDm":     "C:GBPAUD",
    "GBPNZDm":     "C:GBPNZD",
    "AUDJPYm":     "C:AUDJPY",
    "CHFJPYm":     "C:CHFJPY",
    "NZDJPYm":     "C:NZDJPY",
    "AUDCADm":     "C:AUDCAD",
}

# ── Internal timeframe → (multiplier, timespan) ───────────────────────────────
TF_MAP: dict = {
    "1M":  (1,   "minute"),
    "5M":  (5,   "minute"),
    "15M": (15,  "minute"),
    "30M": (30,  "minute"),
    "1H":  (1,   "hour"),
    "4H":  (4,   "hour"),
    "D":   (1,   "day"),
}

# Minutes per bar — used to calculate the lookback date window
_TF_MINUTES: dict = {
    "1M": 1, "5M": 5, "15M": 15, "30M": 30,
    "1H": 60, "4H": 240, "D": 1440,
}


class MassiveConnector:
    """
    Fetches OHLCV candles from Polygon.io.
    Returns List[Candle] in the same format as MT5Connector so the two
    connectors are interchangeable everywhere in the engine pipeline.

    Parameters
    ----------
    api_key   Override MASSIVE_API_KEY env var for this instance.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("MASSIVE_API_KEY", "")
        if not self.api_key:
            log.warning(
                "[Massive] MASSIVE_API_KEY not set — "
                "set it in .env or as a Railway service variable."
            )

    # ─────────────────────────────────────────────
    # PRIMARY: get_candles
    # ─────────────────────────────────────────────

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int = 300,
    ) -> List[Candle]:
        """
        Fetch the last `count` completed bars for `symbol` on `timeframe`.

        Calls:
            GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}

        symbol    — internal name e.g. "EURUSDm", "XAUUSDm", "USTEC_x100m"
        timeframe — e.g. "30M", "4H", "15M"
        count     — number of bars to return (trimmed from the API response tail)
        """
        if not self.api_key:
            log.warning("[Massive] No API key — skipping %s %s", symbol, timeframe)
            return []

        ticker = SYMBOL_MAP.get(symbol)
        if not ticker:
            log.warning("[Massive] No ticker mapping for symbol: %s", symbol)
            return []

        tf_entry = TF_MAP.get(timeframe)
        if not tf_entry:
            log.warning("[Massive] Unknown timeframe: %s", timeframe)
            return []

        multiplier, timespan = tf_entry
        minutes_per_bar = _TF_MINUTES.get(timeframe, 30)

        # Build date window — extra 60% buffer for weekends + holidays
        now     = datetime.now(timezone.utc)
        lookback = timedelta(minutes=count * minutes_per_bar * 1.6)
        from_dt  = now - lookback

        from_str = from_dt.strftime("%Y-%m-%d")
        to_str   = now.strftime("%Y-%m-%d")

        url = (
            f"{_BASE}/v2/aggs/ticker/{ticker}/range"
            f"/{multiplier}/{timespan}/{from_str}/{to_str}"
            f"?adjusted=true&sort=asc&limit={count}&apiKey={self.api_key}"
        )

        log.debug("[Massive] GET %s", url.replace(self.api_key, "***"))

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "TraderCopilot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        except Exception as exc:
            log.error("[Massive] HTTP error for %s %s: %s", symbol, timeframe, exc)
            return []

        status = data.get("status", "")
        if status not in ("OK", "DELAYED"):
            log.warning(
                "[Massive] API status=%s for %s %s — %s",
                status, symbol, timeframe,
                data.get("error") or data.get("message", ""),
            )
            return []

        results = data.get("results") or []
        if not results:
            log.warning(
                "[Massive] 0 bars returned for %s %s (%s → %s)",
                symbol, timeframe, from_str, to_str,
            )
            return []

        # Trim to exactly `count` most-recent bars
        bars = results[-count:]

        candles: List[Candle] = []
        for bar in bars:
            ts = datetime.fromtimestamp(
                bar["t"] / 1000.0, tz=timezone.utc
            ).replace(tzinfo=None)
            candles.append(Candle(
                timestamp=ts,
                open=float(bar["o"]),
                high=float(bar["h"]),
                low=float(bar["l"]),
                close=float(bar["c"]),
                volume=float(bar.get("v", 0.0)),
                timeframe=timeframe,
            ))

        log.info(
            "[Massive] %-12s %-4s — %d candles (latest: %s)",
            symbol, timeframe, len(candles),
            candles[-1].timestamp.strftime("%Y-%m-%d %H:%M") if candles else "—",
        )
        return candles

    # ─────────────────────────────────────────────
    # CONVENIENCE
    # ─────────────────────────────────────────────

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Return the latest close price for a symbol (uses 1-min bar)."""
        bars = self.get_candles(symbol, "1M", count=1)
        return bars[-1].close if bars else None

    def is_available(self) -> bool:
        """Quick health check — returns True if API key is configured."""
        return bool(self.api_key)
