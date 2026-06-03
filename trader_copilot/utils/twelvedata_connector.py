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
from itertools import cycle
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
    "USDZARm":     "USD/ZAR",
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

    Dual-key mode
    -------------
    When api_key_2 / TWELVEDATA_API_KEY_2 is set, requests alternate between
    key 1 and key 2 (even calls → key 1, odd calls → key 2).  Each key then
    sees half the request rate, allowing a 2s inter-call delay while staying
    under the 8 req/min free-tier cap per key.

    Parameters
    ----------
    api_key    Override TWELVEDATA_API_KEY env var for this instance.
    api_key_2  Override TWELVEDATA_API_KEY_2 env var (optional second key).
    """

    def __init__(
        self,
        api_key:   Optional[str] = None,
        api_key_2: Optional[str] = None,
    ):
        self.api_key   = api_key   or os.getenv("TWELVEDATA_API_KEY",   "")
        self.api_key_2 = api_key_2 or os.getenv("TWELVEDATA_API_KEY_2", "")

        # Build a round-robin cycle over available keys.
        # itertools.cycle advances unconditionally on every call — no integer
        # counter means no off-by-one, no frozen state on failed requests.
        _available = [k for k in [self.api_key, self.api_key_2] if k]
        self._key_cycle = cycle(_available) if _available else cycle([""])
        self._key_labels = {}
        if self.api_key:   self._key_labels[self.api_key]   = "key1"
        if self.api_key_2: self._key_labels[self.api_key_2] = "key2"

        if not self.api_key:
            log.warning(
                "[TwelveData] TWELVEDATA_API_KEY not set — "
                "set it in .env or as a Railway service variable."
            )
        if self.api_key_2:
            log.info(
                "[TwelveData] Dual-key mode — key1=...%s  key2=...%s",
                self.api_key[-6:], self.api_key_2[-6:],
            )
        else:
            log.info("[TwelveData] Single-key mode — key1=...%s", self.api_key[-6:] if self.api_key else "unset")

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

        # Advance the round-robin cycle and pick the next key.
        # next() is called unconditionally — before any early-return path —
        # so the cycle always rotates whether the request succeeds or fails.
        use_key   = next(self._key_cycle)
        key_label = self._key_labels.get(use_key, "key?")

        params = urllib.parse.urlencode({
            "symbol":     td_symbol,
            "interval":   interval,
            "outputsize": count,
            "apikey":     use_key,
            "order":      "ASC",          # oldest first — same as MT5
            "timezone":   "UTC",
            "format":     "JSON",
        })
        url = f"{_BASE}?{params}"

        log.debug("[TwelveData] GET %s [%s]", url.replace(use_key, "***"), key_label)

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
            "[TwelveData/%s] %-12s %-4s — %d candles (latest: %s)",
            key_label, symbol, timeframe, len(candles),
            candles[-1].timestamp.strftime("%Y-%m-%d %H:%M") if candles else "—",
        )
        time.sleep(2)     # 2s delay; each key sees every other call → 4s between same-key requests
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
