"""
Trader Copilot — Market Structure Engine
Detects swing points, market bias, BOS, and internal CHoCH.
All rules derived from Ntando's SMC framework.
"""

from typing import List, Optional, Tuple
from ..core.structures import (
    Candle, SwingPoint, MarketStructure, StructureType,
    BiasType, Direction, LiquiditySweep
)
from ..config.pairs import PairConfig


class StructureEngine:

    def __init__(self, config: PairConfig):
        self.config = config

    # ─────────────────────────────────────────────
    # SWING DETECTION
    # ─────────────────────────────────────────────

    def detect_swings(self, candles: List[Candle], left: int = 3, right: int = 3) -> List[SwingPoint]:
        """
        Detect swing highs and lows using a left/right lookback pivot method.
        A swing high: highest high with `left` lower highs before and `right` lower highs after.
        A swing low: lowest low with `left` higher lows before and `right` higher lows after.
        """
        swings: List[SwingPoint] = []

        for i in range(left, len(candles) - right):
            c = candles[i]

            # Swing high check
            is_swing_high = all(candles[i].high >= candles[i - j].high for j in range(1, left + 1)) and \
                            all(candles[i].high >= candles[i + j].high for j in range(1, right + 1))

            # Swing low check
            is_swing_low = all(candles[i].low <= candles[i - j].low for j in range(1, left + 1)) and \
                           all(candles[i].low <= candles[i + j].low for j in range(1, right + 1))

            if is_swing_high:
                swing_type = self._classify_high(c.high, swings)
                swings.append(SwingPoint(candle=c, swing_type=swing_type, level=c.high, index=i))

            elif is_swing_low:
                swing_type = self._classify_low(c.low, swings)
                swings.append(SwingPoint(candle=c, swing_type=swing_type, level=c.low, index=i))

        return swings

    def _classify_high(self, level: float, existing: List[SwingPoint]) -> StructureType:
        prev_highs = [s for s in existing if s.swing_type in (StructureType.HH, StructureType.LH, StructureType.EQH)]
        if not prev_highs:
            return StructureType.HH
        last = prev_highs[-1].level
        tolerance = self.config.peak_equality_pips * self.config.pip_size
        if abs(level - last) <= tolerance:
            return StructureType.EQH
        return StructureType.HH if level > last else StructureType.LH

    def _classify_low(self, level: float, existing: List[SwingPoint]) -> StructureType:
        prev_lows = [s for s in existing if s.swing_type in (StructureType.HL, StructureType.LL, StructureType.EQL)]
        if not prev_lows:
            return StructureType.HL
        last = prev_lows[-1].level
        tolerance = self.config.peak_equality_pips * self.config.pip_size
        if abs(level - last) <= tolerance:
            return StructureType.EQL
        return StructureType.HL if level > last else StructureType.LL

    # ─────────────────────────────────────────────
    # BIAS DETERMINATION
    # ─────────────────────────────────────────────

    def determine_bias(self, swings: List[SwingPoint]) -> BiasType:
        """
        Bullish bias: sequence of HH and HL.
        Bearish bias: sequence of LH and LL.
        Uses the last 4 swing points for recency weighting.
        """
        if len(swings) < 4:
            return BiasType.RANGING

        recent = swings[-4:]
        highs = [s for s in recent if s.swing_type in (StructureType.HH, StructureType.LH, StructureType.EQH)]
        lows  = [s for s in recent if s.swing_type in (StructureType.HL, StructureType.LL, StructureType.EQL)]

        bullish_count = sum(1 for s in highs if s.swing_type == StructureType.HH) + \
                        sum(1 for s in lows  if s.swing_type == StructureType.HL)

        bearish_count = sum(1 for s in highs if s.swing_type == StructureType.LH) + \
                        sum(1 for s in lows  if s.swing_type == StructureType.LL)

        if bullish_count > bearish_count:
            return BiasType.BULLISH
        elif bearish_count > bullish_count:
            return BiasType.BEARISH
        return BiasType.RANGING

    # ─────────────────────────────────────────────
    # LIQUIDITY SWEEP DETECTION
    # ─────────────────────────────────────────────

    def detect_sweep(
        self,
        candles: List[Candle],
        level: float,
        direction: Direction,
        index: int
    ) -> Optional[LiquiditySweep]:
        """
        A valid sweep:
        - Wick pierces the level by at least sweep_wick_pips
        - Candle BODY closes back on the opposite side of the level
        This is the stop hunt confirmation rule.
        """
        min_wick = self.config.sweep_wick_pips * self.config.pip_size
        c = candles[index]

        if direction == Direction.BEARISH:
            # Sweeping a high — wick above, body closes below
            wick_through = c.high - level
            body_rejected = c.body_high < level
            if wick_through >= min_wick and body_rejected:
                return LiquiditySweep(
                    level=level,
                    sweep_candle=c,
                    direction=direction,
                    wick_size=wick_through,
                    body_rejected=body_rejected
                )

        elif direction == Direction.BULLISH:
            # Sweeping a low — wick below, body closes above
            wick_through = level - c.low
            body_rejected = c.body_low > level
            if wick_through >= min_wick and body_rejected:
                return LiquiditySweep(
                    level=level,
                    sweep_candle=c,
                    direction=direction,
                    wick_size=wick_through,
                    body_rejected=body_rejected
                )

        return None

    # ─────────────────────────────────────────────
    # BREAK OF STRUCTURE (BOS)
    # ─────────────────────────────────────────────

    def detect_bos(
        self,
        candles: List[Candle],
        neckline: float,
        direction: Direction,
        start_index: int
    ) -> Optional[Tuple[Candle, int]]:
        """
        BOS rule — Ntando's exact rule:
        The CANDLE BODY must close above/below the neckline.
        A wick through the level does NOT count.
        Returns the BOS candle and its index.
        """
        for i in range(start_index, len(candles)):
            c = candles[i]
            if direction == Direction.BEARISH:
                # Body must close BELOW neckline
                if c.body_low < neckline and c.close < neckline:
                    return (c, i)
            elif direction == Direction.BULLISH:
                # Body must close ABOVE neckline
                if c.body_high > neckline and c.close > neckline:
                    return (c, i)
        return None

    # ─────────────────────────────────────────────
    # INTERNAL CHANGE OF CHARACTER (iCHoCH)
    # ─────────────────────────────────────────────

    def detect_ichoch(
        self,
        candles: List[Candle],
        bias: BiasType,
        swing_level: float,
        start_index: int
    ) -> Optional[Tuple[Candle, int]]:
        """
        iCHoCH — internal character change within the swing.
        For sells (bearish bias): an internal low is broken by body close below it.
        For buys (bullish bias): an internal high is broken by body close above it.
        This is the entry trigger — more precise than the major BOS.
        """
        return self.detect_bos(
            candles=candles,
            neckline=swing_level,
            direction=Direction.BEARISH if bias == BiasType.BEARISH else Direction.BULLISH,
            start_index=start_index
        )

    # ─────────────────────────────────────────────
    # EQUAL HIGHS / LOWS (NAS100 edge + double top/bottom)
    # ─────────────────────────────────────────────

    def find_equal_levels(
        self,
        swings: List[SwingPoint],
        level_type: str = "high"
    ) -> List[Tuple[SwingPoint, SwingPoint]]:
        """
        Find pairs of swing points that form equal highs or equal lows.
        These are the liquidity pools for double top / double bottom patterns.
        NAS100 specific edge: equal highs form frequently and get raided.
        """
        results = []
        relevant = [
            s for s in swings
            if (level_type == "high" and s.swing_type in (StructureType.HH, StructureType.LH, StructureType.EQH)) or
               (level_type == "low"  and s.swing_type in (StructureType.HL, StructureType.LL, StructureType.EQL))
        ]

        tolerance = self.config.peak_equality_pips * self.config.pip_size

        for i in range(len(relevant)):
            for j in range(i + 1, len(relevant)):
                if abs(relevant[i].level - relevant[j].level) <= tolerance:
                    results.append((relevant[i], relevant[j]))

        return results
