"""
Trader Copilot — Core Data Structures
Candle, swing, and market structure primitives.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List


class Direction(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class StructureType(Enum):
    HH = "HH"   # Higher High
    HL = "HL"   # Higher Low
    LH = "LH"   # Lower High
    LL = "LL"   # Lower Low
    EQH = "EQH" # Equal High
    EQL = "EQL" # Equal Low


class BiasType(Enum):
    BULLISH = "bullish"   # HH / HL sequence → look for buys
    BEARISH = "bearish"   # LH / LL sequence → look for sells
    RANGING = "ranging"


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    timeframe: str = ""

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - self.body_high

    @property
    def lower_wick(self) -> float:
        return self.body_low - self.low

    @property
    def total_range(self) -> float:
        return self.high - self.low


@dataclass
class SwingPoint:
    candle: Candle
    swing_type: StructureType
    level: float          # The price of the swing high or low
    index: int            # Position in the candle array
    swept: bool = False   # Has this swing been swept


@dataclass
class MarketStructure:
    bias: BiasType
    swings: List[SwingPoint] = field(default_factory=list)
    last_bos_level: Optional[float] = None
    last_choch_level: Optional[float] = None

    @property
    def last_swing_high(self) -> Optional[SwingPoint]:
        highs = [s for s in self.swings if s.swing_type in (StructureType.HH, StructureType.LH, StructureType.EQH)]
        return highs[-1] if highs else None

    @property
    def last_swing_low(self) -> Optional[SwingPoint]:
        lows = [s for s in self.swings if s.swing_type in (StructureType.HL, StructureType.LL, StructureType.EQL)]
        return lows[-1] if lows else None


@dataclass
class FairValueGap:
    top: float
    bottom: float
    direction: Direction      # Bearish FVG (price fell through) or Bullish FVG
    candle_index: int         # Index of the displacement (middle) candle
    timestamp: datetime
    size: float               # Gap size in price
    mitigated: bool = False   # Has price returned to fill it

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class OrderBlock:
    top: float
    bottom: float
    direction: Direction      # Bearish OB or Bullish OB
    candle: Candle
    timestamp: datetime
    mitigated: bool = False


@dataclass
class LiquiditySweep:
    level: float
    sweep_candle: Candle
    direction: Direction      # Which side was swept (HIGH or LOW)
    wick_size: float
    body_rejected: bool       # Body closed back on the other side


@dataclass
class TradeSignal:
    symbol: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    timeframe: str
    timestamp: datetime
    pattern: str
    confluence_score: int     # 1-5 scale
    fvg_present: bool
    ob_present: bool
    killzone_active: bool
    notes: str = ""

    @property
    def risk_reward(self) -> float:
        if self.direction == Direction.BEARISH:
            risk = self.stop_loss - self.entry_price
            reward = self.entry_price - self.take_profit
        else:
            risk = self.entry_price - self.stop_loss
            reward = self.take_profit - self.entry_price
        return round(reward / risk, 2) if risk > 0 else 0.0
