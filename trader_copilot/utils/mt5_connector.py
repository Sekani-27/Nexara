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

from datetime import datetime
from typing import List, Optional
from ..core.structures import Candle


TF_MAP = {
    "1M":  1,
    "5M":  5,
    "15M": 15,
    "30M": 30,
    "1H":  60,
    "4H":  240,
    "D":   1440,
}


class MT5Connector:

    def __init__(self, login: Optional[int] = None, password: Optional[str] = None,
                 server: Optional[str] = None):
        self._connected = False
        self.login    = login
        self.password = password
        self.server   = server

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
            if self.login:
                ok = mt5.initialize(login=self.login, password=self.password, server=self.server)
            else:
                ok = mt5.initialize()
            self._connected = ok
            if not ok:
                print(f"MT5 connection failed: {mt5.last_error()}")
            return ok
        except ImportError:
            print("MetaTrader5 package not installed. Run: pip install MetaTrader5")
            return False

    def disconnect(self):
        try:
            import MetaTrader5 as mt5
            mt5.shutdown()
        except ImportError:
            pass

    def get_candles(self, symbol: str, timeframe: str, count: int = 300) -> List[Candle]:
        """
        Fetch the last `count` candles for symbol on the given timeframe.
        Returns List[Candle] ready for the TraderCopilot engine.
        """
        if not self._connected:
            if not self.connect():
                return []

        try:
            import MetaTrader5 as mt5
            import MetaTrader5 as mt5_tf

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
                candles.append(Candle(
                    timestamp=datetime.utcfromtimestamp(r["time"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["tick_volume"]),
                    timeframe=timeframe,
                ))
            return candles

        except Exception as e:
            print(f"Error fetching candles: {e}")
            return []

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get latest bid price for a symbol."""
        try:
            import MetaTrader5 as mt5
            tick = mt5.symbol_info_tick(symbol)
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
                    raw_time = row.get("time") or row.get("Date") or row.get("timestamp")
                    try:
                        ts = datetime.strptime(raw_time, "%Y.%m.%d %H:%M")
                    except ValueError:
                        ts = datetime.fromisoformat(raw_time)

                    candles.append(Candle(
                        timestamp=ts,
                        open=float(row.get("open") or row.get("Open")),
                        high=float(row.get("high") or row.get("High")),
                        low=float(row.get("low")  or row.get("Low")),
                        close=float(row.get("close") or row.get("Close")),
                        volume=float(row.get("volume") or row.get("Volume") or 0),
                        timeframe=timeframe,
                    ))
                except Exception as e:
                    print(f"Skipping row: {e}")
        return candles
