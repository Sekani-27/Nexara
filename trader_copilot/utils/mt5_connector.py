"""
Trader Copilot — MT5 Data Connector
Fetches OHLCV candles from MetaTrader 5 into Candle objects.

Requirements:
    pip install MetaTrader5

Usage:
    from trader_copilot.utils.mt5_connector import MT5Connector
    connector = MT5Connector()
    candles_htf = connector.get_candles("XAUUSD", "4H", count=200)
    candles_ltf = connector.get_candles("XAUUSD", "15M", count=200)
"""

import logging
from datetime import datetime
from typing import List, Optional
from ..core.structures import Candle

log = logging.getLogger(__name__)

# ── One-time availability check at import time ────────────────────────────────
# The MetaTrader5 package is Windows-only.  We try the import once here so
# that connect() / get_candles() can fail silently on Linux/Railway without
# printing the same "not installed" message hundreds of times per cycle.
try:
    import MetaTrader5 as _mt5_module

    _MT5_AVAILABLE = True
except ImportError:
    _mt5_module = None
    _MT5_AVAILABLE = False
    log.warning(
        "MetaTrader5 package not available — MT5Connector will return empty "
        "candles. Install MetaTrader5 (Windows only) or use TwelveData/Massive."
    )


TF_MAP = {
    "1M": 1,
    "5M": 5,
    "15M": 15,
    "30M": 30,
    "1H": 60,
    "4H": 240,
    "D": 1440,
}


class MT5Connector:
    """
    Wraps the MetaTrader5 terminal connection.

    The connection is opened once (lazily on the first fetch call) and held
    open for the lifetime of the instance.  Use disconnect() / a context
    manager to tear it down when the process exits.

    Opening and closing mt5.initialize()/mt5.shutdown() on every candle
    fetch is expensive and can hit terminal session limits when polling many
    pairs at high frequency.
    """

    def __init__(
        self,
        login: Optional[int] = None,
        password: Optional[str] = None,
        server: Optional[str] = None,
    ):
        self._connected = False
        self._mt5 = None  # module reference cached after first successful import
        self.login = login
        self.password = password
        self.server = server

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def connect(self) -> bool:
        """Open the MT5 terminal connection.  Safe to call multiple times — a
        no-op if already connected."""
        if self._connected:
            return True
        if not _MT5_AVAILABLE:
            return False  # already warned at import time — no repeat message
        try:
            self._mt5 = _mt5_module
            if self.login:
                ok = _mt5_module.initialize(
                    login=self.login, password=self.password, server=self.server
                )
            else:
                ok = _mt5_module.initialize()
            self._connected = ok
            if not ok:
                log.error("MT5 connection failed: %s", _mt5_module.last_error())
            return ok
        except Exception as exc:
            log.error("MT5 connect error: %s", exc)
            return False

    def disconnect(self):
        """Shut down the MT5 terminal connection and reset state."""
        if self._connected and self._mt5 is not None:
            try:
                self._mt5.shutdown()
            except Exception:
                pass
        self._connected = False
        self._mt5 = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    # ── Data fetching ─────────────────────────────────────────────────────────

    def _ensure_connected(self) -> bool:
        """Lazily open the connection on the first fetch call."""
        if not self._connected:
            return self.connect()
        return True

    def get_candles(
        self, symbol: str, timeframe: str, count: int = 300
    ) -> List[Candle]:
        """
        Fetch the last `count` candles for symbol on the given timeframe.
        Returns List[Candle] ready for the TraderCopilot engine.
        Reuses the open connection; does not call initialize/shutdown per call.
        """
        if not self._ensure_connected():
            return []

        try:
            mt5 = self._mt5

            tf_minutes = TF_MAP.get(timeframe, 15)
            mt5_tf_const = {
                1: mt5.TIMEFRAME_M1,
                5: mt5.TIMEFRAME_M5,
                15: mt5.TIMEFRAME_M15,
                30: mt5.TIMEFRAME_M30,
                60: mt5.TIMEFRAME_H1,
                240: mt5.TIMEFRAME_H4,
                1440: mt5.TIMEFRAME_D1,
            }.get(tf_minutes, mt5.TIMEFRAME_M15)

            rates = mt5.copy_rates_from_pos(symbol, mt5_tf_const, 0, count)
            if rates is None or len(rates) == 0:
                print(f"No data returned for {symbol} {timeframe}")
                return []

            candles = []
            for r in rates:
                candles.append(
                    Candle(
                        timestamp=datetime.utcfromtimestamp(r["time"]),
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r["tick_volume"]),
                        timeframe=timeframe,
                    )
                )
            return candles

        except Exception as e:
            print(f"Error fetching candles: {e}")
            self._connected = False  # treat as stale; next call will reconnect
            return []

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get latest bid price for a symbol."""
        if not _MT5_AVAILABLE or not self._connected:
            return None
        try:
            tick = _mt5_module.symbol_info_tick(symbol)
            return tick.bid if tick else None
        except Exception:
            return None


class CSVConnector:
    """
    Load candles from a CSV file (MT5 export format or generic OHLCV).
    Expected columns: time, open, high, low, close, volume (optional)
    """

    def load(self, filepath: str, timeframe: str = "15M") -> List[Candle]:
        import csv

        candles = []
        with open(filepath, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    # Handle both MT5 date format and ISO format
                    raw_time = (
                        row.get("time") or row.get("Date") or row.get("timestamp")
                    )
                    try:
                        ts = datetime.strptime(raw_time, "%Y.%m.%d %H:%M")
                    except ValueError:
                        ts = datetime.fromisoformat(raw_time)

                    candles.append(
                        Candle(
                            timestamp=ts,
                            open=float(row.get("open") or row.get("Open")),
                            high=float(row.get("high") or row.get("High")),
                            low=float(row.get("low") or row.get("Low")),
                            close=float(row.get("close") or row.get("Close")),
                            volume=float(row.get("volume") or row.get("Volume") or 0),
                            timeframe=timeframe,
                        )
                    )
                except Exception as e:
                    print(f"Skipping row: {e}")
        return candles
