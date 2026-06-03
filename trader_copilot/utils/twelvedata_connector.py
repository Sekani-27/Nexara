"""
Trader Copilot — TwelveData Connector
======================================
Third data source in the chain: MT5 → Massive (Polygon) → TwelveData.
Used when both MT5 and Massive return empty candles (e.g. Railway deployment
without an MT5 terminal and on a Polygon free-tier key).

API endpoint
------------
    GET https://api.twelvedata.com/time_series
        ?symbol={symbol}
        &interval={interval}
        &outputsize={count}
        &apikey={key}

TwelveData interval strings: 1min, 5min, 15min, 30min, 1h, 4h, 1day
Response (JSON):
    {
      "meta":   { "symbol": "EUR/USD", ... },
      "values": [
          { "datetime": "2024-06-03 14:30:00",
            "open": "1.08530", "high": "1.08560",
            "low": "1.08490", "close": "1.08510",
            "volume": "4389" },
          ...
      ],
      "status": "ok"
    }
Values are returned newest-first; we reverse to give the engine
chronological (oldest-first) order matching MT5Connector output.

Environment
-----------
    TWELVEDATA_API_KEY   — TwelveData API key (required)
"""

import json
import logging
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime
from typing import List, Optional

from ..core.structures import Candle

log = logging.getLogger(__name__)

_BASE = "https://api.twelvedata.com/time_series"

# ── Internal symbol → TwelveData symbol ──────────────────────────────────────
SYMBOL_MAP: dict = {
    # ── Active runner pairs ───────────────────────────────────────────────────
    "EURUSDm":     "EUR/USD",
    "GBPUSDm":     "GBP/USD",
    "EURAUDm":     "EUR/AUD",
    "EURCADm":     "EUR/CAD",
    "CADJPYm":     "CAD/JPY",
    "GBPCADm":     "GBP/CAD",
    "XAUUSDm":     "XAU/USD",   # Gold spot
    "USTEC_x100m": "QQQ",       # Nasdaq-100 (QQQ ETF — NDX requires paid tier)
    # ── Additional PAIR_CONFIGS entries ──────────────────────────────────────
    "USDJPYm":     "USD/JPY",
    "USDCHFm":     "USD/CHF",
    "AUDUSDm":     "AUD/USD",
    "USDCADm":     "USD/CAD",
    "NZDUSDm":     "NZD/USD",
    "EURGBPm":     "EUR/GBP",
    "EURJPYm":     "EUR/JPY",
    "EURCHFm":     "EUR/CHF",
    "EURNZDm":     "EUR/NZD",
    "GBPJPYm":     "GBP/JPY",
    "GBPCHFm":     "GBP/CHF",
    "GBPAUDm":     "GBP/AUD",
    "GBPNZDm":     "GBP/NZD",
    "AUDJPYm":     "AUD/JPY",
    "CHFJPYm":     "CHF/JPY",
    "NZDJPYm":     "NZD/JPY",
    "AUDCADm":     "AUD/CAD",
}

# ── Internal timeframe → TwelveData interval string ──────────────────────────
TF_MAP: dict = {
    "1M":  "1min",
    "5M":  "5min",
    "15M": "15min",
    "30M": "30min",
    "1H":  "1h",
    "4H":  "4h",
    "D":   "1day",
}


class TwelveDataConnector:
    """
    Fetches OHLCV candles from TwelveData.
    Returns List[Candle] in the same format as MT5Connector and
    MassiveConnector — fully interchangeable in the engine pipeline.

    Parameters
    ----------
    api_key   Override TWELVEDATA_API_KEY env var for this instance.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("TWELVEDATA_API_KEY", "")
        if not self.api_key:
            log.warning(
                "[TwelveData] TWELVEDATA_API_KEY not set — "
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

        symbol    — internal name e.g. "EURUSDm", "XAUUSDm", "USTEC_x100m"
        timeframe — e.g. "30M", "4H", "15M"
        count     — number of bars to return (TwelveData max outputsize=5000)
        """
        if not self.api_key:
            log.warning("[TwelveData] No API key — skipping %s %s", symbol, timeframe)
            return []

        td_symbol = SYMBOL_MAP.get(symbol)
        if not td_symbol:
            log.warning("[TwelveData] No symbol mapping for: %s", symbol)
            return []

        interval = TF_MAP.get(timeframe)
        if not interval:
            log.warning("[TwelveData] Unknown timeframe: %s", timeframe)
            return []

        params = urllib.parse.urlencode({
            "symbol":     td_symbol,
            "interval":   interval,
            "outputsize": count,
            "apikey":     self.api_key,
            "order":      "ASC",          # oldest first — same as MT5
            "timezone":   "UTC",
            "format":     "JSON",
        })
        url = f"{_BASE}?{params}"

        log.debug("[TwelveData] GET %s", url.replace(self.api_key, "***"))

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "TraderCopilot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.error("[TwelveData] HTTP error for %s %s: %s", symbol, timeframe, exc)
            return []

        # TwelveData returns {"code": 400, "message": "..."} on errors
        if data.get("status") == "error" or "code" in data:
            log.warning(
                "[TwelveData] API error for %s %s — %s",
                symbol, timeframe,
                data.get("message", data),
            )
            return []

        values = data.get("values")
        if not values:
            log.warning(
                "[TwelveData] 0 bars returned for %s %s", symbol, timeframe
            )
            return []

        candles: List[Candle] = []
        for bar in values:
            try:
                # TwelveData datetime format: "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"
                raw_dt = bar["datetime"]
                try:
                    ts = datetime.strptime(raw_dt, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    ts = datetime.strptime(raw_dt, "%Y-%m-%d")

                candles.append(Candle(
                    timestamp=ts,
                    open=float(bar["open"]),
                    high=float(bar["high"]),
                    low=float(bar["low"]),
                    close=float(bar["close"]),
                    volume=float(bar.get("volume") or 0.0),
                    timeframe=timeframe,
                ))
            except (KeyError, ValueError) as exc:
                log.debug("[TwelveData] Skipping malformed bar: %s — %s", bar, exc)

        log.info(
            "[TwelveData] %-12s %-4s — %d candles (latest: %s)",
            symbol, timeframe, len(candles),
            candles[-1].timestamp.strftime("%Y-%m-%d %H:%M") if candles else "—",
        )
        time.sleep(0.5)   # respect TwelveData free-tier limit (8 req/min)
        return candles

    # ─────────────────────────────────────────────
    # CONVENIENCE
    # ─────────────────────────────────────────────

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Return the latest close price for a symbol."""
        bars = self.get_candles(symbol, "1M", count=1)
        return bars[-1].close if bars else None

    def is_available(self) -> bool:
        """True if API key is configured."""
        return bool(self.api_key)
