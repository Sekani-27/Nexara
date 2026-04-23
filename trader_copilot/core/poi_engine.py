"""
Trader Copilot — POI Engine
Detects Fair Value Gaps and Order Blocks.
FVG must be at the neckline/iCHoCH level to qualify as entry zone.
"""

from typing import List, Optional
from ..core.structures import Candle, FairValueGap, OrderBlock, Direction
from ..config.pairs import PairConfig


class POIEngine:

    def __init__(self, config: PairConfig):
        self.config = config

    # ─────────────────────────────────────────────
    # FAIR VALUE GAP DETECTION
    # ─────────────────────────────────────────────

    def detect_fvg(
        self,
        candles: List[Candle],
        direction: Direction,
        around_index: int,
        search_range: int = 5
    ) -> Optional[FairValueGap]:
        """
        3-candle imbalance rule:
        Bearish FVG: candle[i-1].low > candle[i+1].high  (gap between prev low and next high)
        Bullish FVG: candle[i-1].high < candle[i+1].low  (gap between prev high and next low)

        The displacement candle is candle[i] — the middle of the 3.
        FVG must be >= fvg_min_pips to be valid.

        Per Ntando's rule: FVG must sit AT the neckline/swing low area.
        We search within `search_range` candles of the BOS candle.
        """
        min_size = self.config.fvg_min_pips * self.config.pip_size
        start = max(1, around_index - search_range)
        end = min(len(candles) - 1, around_index + search_range)

        best_fvg: Optional[FairValueGap] = None
        best_size = 0.0

        for i in range(start, end):
            if i < 1 or i >= len(candles) - 1:
                continue

            prev = candles[i - 1]
            mid  = candles[i]
            nxt  = candles[i + 1]

            if direction == Direction.BEARISH:
                # Gap between bottom of prev candle and top of next candle
                gap_top    = prev.low
                gap_bottom = nxt.high
                gap_size   = gap_top - gap_bottom

                if gap_size >= min_size and mid.is_bearish:
                    if gap_size > best_size:
                        best_size = gap_size
                        best_fvg = FairValueGap(
                            top=gap_top,
                            bottom=gap_bottom,
                            direction=Direction.BEARISH,
                            candle_index=i,
                            timestamp=mid.timestamp,
                            size=gap_size
                        )

            elif direction == Direction.BULLISH:
                # Gap between top of prev candle and bottom of next candle
                gap_bottom = prev.high
                gap_top    = nxt.low
                gap_size   = gap_top - gap_bottom

                if gap_size >= min_size and mid.is_bullish:
                    if gap_size > best_size:
                        best_size = gap_size
                        best_fvg = FairValueGap(
                            top=gap_top,
                            bottom=gap_bottom,
                            direction=Direction.BULLISH,
                            candle_index=i,
                            timestamp=mid.timestamp,
                            size=gap_size
                        )

        return best_fvg

    def is_price_in_fvg(self, price: float, fvg: FairValueGap) -> bool:
        """Check if current price is inside the FVG zone."""
        return fvg.bottom <= price <= fvg.top

    def fvg_near_level(self, fvg: FairValueGap, level: float) -> bool:
        """
        Ntando's rule: FVG must be at the neckline.
        Check that the FVG zone overlaps with or is within
        fvg_min_pips distance of the neckline level.
        """
        tolerance = self.config.fvg_min_pips * self.config.pip_size * 3
        return (fvg.bottom - tolerance) <= level <= (fvg.top + tolerance)

    # ─────────────────────────────────────────────
    # ORDER BLOCK DETECTION
    # ─────────────────────────────────────────────

    def detect_order_block(
        self,
        candles: List[Candle],
        direction: Direction,
        sweep_index: int,
        lookback: int = 5
    ) -> Optional[OrderBlock]:
        """
        Bearish OB: the last BULLISH candle before the sweep rejection.
        Bullish OB: the last BEARISH candle before the sweep rejection.
        This is where institutions distributed / accumulated.
        """
        start = max(0, sweep_index - lookback)

        if direction == Direction.BEARISH:
            # Find last bullish candle before sweep
            for i in range(sweep_index - 1, start - 1, -1):
                c = candles[i]
                if c.is_bullish:
                    return OrderBlock(
                        top=c.high,
                        bottom=c.low,
                        direction=Direction.BEARISH,
                        candle=c,
                        timestamp=c.timestamp
                    )

        elif direction == Direction.BULLISH:
            # Find last bearish candle before sweep
            for i in range(sweep_index - 1, start - 1, -1):
                c = candles[i]
                if c.is_bearish:
                    return OrderBlock(
                        top=c.high,
                        bottom=c.low,
                        direction=Direction.BULLISH,
                        candle=c,
                        timestamp=c.timestamp
                    )

        return None

    def is_price_in_ob(self, price: float, ob: OrderBlock) -> bool:
        return ob.bottom <= price <= ob.top

    # ─────────────────────────────────────────────
    # RETEST DETECTION
    # ─────────────────────────────────────────────

    def detect_retest(
        self,
        candles: List[Candle],
        level: float,
        direction: Direction,
        bos_index: int,
        fvg: Optional[FairValueGap] = None
    ) -> Optional[int]:
        """
        After BOS, price retraces back to the iCHoCH / neckline level.
        If FVG is present at that level, the retest entering the FVG zone
        is the boosted entry confirmation.
        Returns the index of the retest candle if found.
        """
        tolerance = self.config.fvg_min_pips * self.config.pip_size * 2

        for i in range(bos_index + 1, len(candles)):
            c = candles[i]

            if direction == Direction.BEARISH:
                # Price retraces UP toward level after bearish BOS
                price_at_level = c.high >= (level - tolerance)
                if fvg:
                    in_fvg = self.is_price_in_fvg(c.high, fvg) or self.is_price_in_fvg(c.close, fvg)
                    if price_at_level and in_fvg:
                        return i
                else:
                    if price_at_level:
                        return i

            elif direction == Direction.BULLISH:
                # Price retraces DOWN toward level after bullish BOS
                price_at_level = c.low <= (level + tolerance)
                if fvg:
                    in_fvg = self.is_price_in_fvg(c.low, fvg) or self.is_price_in_fvg(c.close, fvg)
                    if price_at_level and in_fvg:
                        return i
                else:
                    if price_at_level:
                        return i

        return None
