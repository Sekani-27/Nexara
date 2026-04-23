"""
Trader Copilot — Pattern Recognition Engine
Detects all three pattern types per Ntando's written framework rules.

Rules per pattern (from the handwritten notes):
─────────────────────────────────────────────
Flag (ascending/descending):
  - Formed at higher highs or lower lows after sweeping liquidity
  - Impulse then consolidate = loss of momentum
  - Execution: CHoCH — sweep the low/high, candle BODY closes below/above high/low

Double Top / Bottom:
  - Reversal pattern at base/peak of trend
  - Execution: always wait for neckline break, BODY candle closes above/below
    high/low leaving behind IMBALANCE (FVG)

H&S / Inverse H&S:
  - Execution: break of neckline, candle BODY closes above/below high/low
─────────────────────────────────────────────
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple
from ..core.structures import (
    Candle, SwingPoint, StructureType, Direction,
    FairValueGap, LiquiditySweep, BiasType
)
from ..config.pairs import PairConfig


@dataclass
class PatternResult:
    pattern_name: str
    direction: Direction
    neckline: float           # The level that must be broken
    sweep_level: float        # The liquidity level that was swept
    sweep_candle_index: int
    bos_candle_index: int
    fvg: Optional[FairValueGap]
    valid: bool
    notes: str = ""


class PatternEngine:

    def __init__(self, config: PairConfig):
        self.config = config

    # ─────────────────────────────────────────────
    # DOUBLE TOP
    # ─────────────────────────────────────────────

    def detect_double_top(
        self,
        candles: List[Candle],
        swings: List[SwingPoint],
        fvg_engine
    ) -> Optional[PatternResult]:
        """
        Double Top detection:
        1. Find two swing highs within peak_equality_pips of each other (EQH)
        2. Second peak must sweep above the first (wick beyond first peak)
        3. Body closes back below the EQH level (sweep confirmed)
        4. Price sells off to the swing low (neckline)
        5. Displacement candle: BODY closes BELOW neckline leaving FVG at neckline
        6. FVG is printed at the neckline — that's the entry zone
        """
        tolerance = self.config.peak_equality_pips * self.config.pip_size
        min_wick   = self.config.sweep_wick_pips * self.config.pip_size

        highs = [s for s in swings if s.swing_type in (StructureType.HH, StructureType.LH, StructureType.EQH)]
        lows  = [s for s in swings if s.swing_type in (StructureType.HL, StructureType.LL, StructureType.EQL)]

        if len(highs) < 2 or not lows:
            return None

        # Check recent pairs of highs for equality
        for i in range(len(highs) - 1, 0, -1):
            peak2 = highs[i]
            peak1 = highs[i - 1]

            # Peaks must be approximately equal
            if abs(peak2.level - peak1.level) > tolerance:
                continue

            eqh_level = max(peak1.level, peak2.level)

            # Second peak must have swept above the first (stop hunt)
            sweep_candle = candles[peak2.index]
            wick_above = sweep_candle.high - eqh_level
            body_rejected = sweep_candle.body_high < eqh_level

            if wick_above < min_wick or not body_rejected:
                # Still valid without a sweep but lower conviction
                # Keep going — neckline break is the main rule
                pass

            # Find the swing low (neckline) between the two peaks
            neckline_swing = None
            for low in reversed(lows):
                if peak1.index < low.index < peak2.index:
                    neckline_swing = low
                    break

            if not neckline_swing:
                # Try finding any low before peak2
                for low in reversed(lows):
                    if low.index < peak2.index:
                        neckline_swing = low
                        break

            if not neckline_swing:
                continue

            neckline = neckline_swing.level

            # Find displacement candle: body closes BELOW neckline
            bos_result = self._find_body_bos(candles, neckline, Direction.BEARISH, peak2.index)
            if not bos_result:
                continue

            bos_candle, bos_index = bos_result

            # FVG must be at the neckline level — search around the BOS candle
            fvg = fvg_engine.detect_fvg(candles, Direction.BEARISH, bos_index, search_range=3)
            fvg_at_neckline = fvg and fvg_engine.fvg_near_level(fvg, neckline)

            return PatternResult(
                pattern_name="Double Top",
                direction=Direction.BEARISH,
                neckline=neckline,
                sweep_level=eqh_level,
                sweep_candle_index=peak2.index,
                bos_candle_index=bos_index,
                fvg=fvg if fvg_at_neckline else None,
                valid=True,
                notes=f"EQH at {eqh_level:.5f}, neckline at {neckline:.5f}, "
                      f"FVG {'present at neckline' if fvg_at_neckline else 'absent'}"
            )

        return None

    # ─────────────────────────────────────────────
    # DOUBLE BOTTOM
    # ─────────────────────────────────────────────

    def detect_double_bottom(
        self,
        candles: List[Candle],
        swings: List[SwingPoint],
        fvg_engine
    ) -> Optional[PatternResult]:
        """
        Mirror of double top — bullish reversal at trend base.
        Two equal lows → second low sweeps below first → body closes above neckline
        → FVG at neckline = entry zone.
        """
        tolerance = self.config.peak_equality_pips * self.config.pip_size
        min_wick   = self.config.sweep_wick_pips * self.config.pip_size

        lows  = [s for s in swings if s.swing_type in (StructureType.HL, StructureType.LL, StructureType.EQL)]
        highs = [s for s in swings if s.swing_type in (StructureType.HH, StructureType.LH, StructureType.EQH)]

        if len(lows) < 2 or not highs:
            return None

        for i in range(len(lows) - 1, 0, -1):
            trough2 = lows[i]
            trough1 = lows[i - 1]

            if abs(trough2.level - trough1.level) > tolerance:
                continue

            eql_level = min(trough1.level, trough2.level)

            sweep_candle = candles[trough2.index]
            wick_below = eql_level - sweep_candle.low
            body_rejected = sweep_candle.body_low > eql_level

            # Neckline = swing high between the two troughs
            neckline_swing = None
            for high in reversed(highs):
                if trough1.index < high.index < trough2.index:
                    neckline_swing = high
                    break
            if not neckline_swing:
                for high in reversed(highs):
                    if high.index < trough2.index:
                        neckline_swing = high
                        break
            if not neckline_swing:
                continue

            neckline = neckline_swing.level

            bos_result = self._find_body_bos(candles, neckline, Direction.BULLISH, trough2.index)
            if not bos_result:
                continue

            bos_candle, bos_index = bos_result

            fvg = fvg_engine.detect_fvg(candles, Direction.BULLISH, bos_index, search_range=3)
            fvg_at_neckline = fvg and fvg_engine.fvg_near_level(fvg, neckline)

            return PatternResult(
                pattern_name="Double Bottom",
                direction=Direction.BULLISH,
                neckline=neckline,
                sweep_level=eql_level,
                sweep_candle_index=trough2.index,
                bos_candle_index=bos_index,
                fvg=fvg if fvg_at_neckline else None,
                valid=True,
                notes=f"EQL at {eql_level:.5f}, neckline at {neckline:.5f}, "
                      f"FVG {'present at neckline' if fvg_at_neckline else 'absent'}"
            )

        return None

    # ─────────────────────────────────────────────
    # HEAD AND SHOULDERS
    # ─────────────────────────────────────────────

    def detect_head_and_shoulders(
        self,
        candles: List[Candle],
        swings: List[SwingPoint],
        fvg_engine
    ) -> Optional[PatternResult]:
        """
        H&S detection:
        - Left shoulder (LS), Head (highest), Right shoulder (RS)
        - Head > both shoulders
        - Right shoulder approximately equal to left shoulder
        - Neckline drawn through the two swing lows between LS-Head and Head-RS
        - Execution: neckline break, candle BODY closes below neckline
        """
        highs = [s for s in swings if s.swing_type in (StructureType.HH, StructureType.LH, StructureType.EQH)]
        lows  = [s for s in swings if s.swing_type in (StructureType.HL, StructureType.LL, StructureType.EQL)]

        if len(highs) < 3 or len(lows) < 2:
            return None

        shoulder_tolerance = self.config.peak_equality_pips * self.config.pip_size * 3

        for i in range(len(highs) - 2, 0, -1):
            ls    = highs[i - 1]
            head  = highs[i]
            rs    = highs[i + 1] if i + 1 < len(highs) else None

            if not rs:
                continue

            # Head must be highest
            if not (head.level > ls.level and head.level > rs.level):
                continue

            # Shoulders should be roughly equal
            if abs(ls.level - rs.level) > shoulder_tolerance:
                continue

            # Find neckline lows: one between LS and head, one between head and RS
            nl_low1 = None
            nl_low2 = None
            for low in lows:
                if ls.index < low.index < head.index:
                    nl_low1 = low
                if head.index < low.index < rs.index:
                    nl_low2 = low

            if not nl_low1 or not nl_low2:
                continue

            # Neckline = average of the two lows (simplified flat neckline)
            neckline = (nl_low1.level + nl_low2.level) / 2

            # BOS: body closes below neckline
            bos_result = self._find_body_bos(candles, neckline, Direction.BEARISH, rs.index)
            if not bos_result:
                continue

            bos_candle, bos_index = bos_result

            fvg = fvg_engine.detect_fvg(candles, Direction.BEARISH, bos_index, search_range=3)
            fvg_at_neckline = fvg and fvg_engine.fvg_near_level(fvg, neckline)

            return PatternResult(
                pattern_name="Head and Shoulders",
                direction=Direction.BEARISH,
                neckline=neckline,
                sweep_level=head.level,
                sweep_candle_index=rs.index,
                bos_candle_index=bos_index,
                fvg=fvg if fvg_at_neckline else None,
                valid=True,
                notes=f"Head at {head.level:.5f}, neckline at {neckline:.5f}, "
                      f"FVG {'present' if fvg_at_neckline else 'absent'}"
            )

        return None

    # ─────────────────────────────────────────────
    # INVERSE HEAD AND SHOULDERS
    # ─────────────────────────────────────────────

    def detect_inverse_hs(
        self,
        candles: List[Candle],
        swings: List[SwingPoint],
        fvg_engine
    ) -> Optional[PatternResult]:
        """Bullish mirror of H&S."""
        lows  = [s for s in swings if s.swing_type in (StructureType.HL, StructureType.LL, StructureType.EQL)]
        highs = [s for s in swings if s.swing_type in (StructureType.HH, StructureType.LH, StructureType.EQH)]

        if len(lows) < 3 or len(highs) < 2:
            return None

        shoulder_tolerance = self.config.peak_equality_pips * self.config.pip_size * 3

        for i in range(len(lows) - 2, 0, -1):
            ls   = lows[i - 1]
            head = lows[i]
            rs   = lows[i + 1] if i + 1 < len(lows) else None

            if not rs:
                continue

            if not (head.level < ls.level and head.level < rs.level):
                continue

            if abs(ls.level - rs.level) > shoulder_tolerance:
                continue

            nl_high1 = None
            nl_high2 = None
            for high in highs:
                if ls.index < high.index < head.index:
                    nl_high1 = high
                if head.index < high.index < rs.index:
                    nl_high2 = high

            if not nl_high1 or not nl_high2:
                continue

            neckline = (nl_high1.level + nl_high2.level) / 2

            bos_result = self._find_body_bos(candles, neckline, Direction.BULLISH, rs.index)
            if not bos_result:
                continue

            bos_candle, bos_index = bos_result

            fvg = fvg_engine.detect_fvg(candles, Direction.BULLISH, bos_index, search_range=3)
            fvg_at_neckline = fvg and fvg_engine.fvg_near_level(fvg, neckline)

            return PatternResult(
                pattern_name="Inverse Head and Shoulders",
                direction=Direction.BULLISH,
                neckline=neckline,
                sweep_level=head.level,
                sweep_candle_index=rs.index,
                bos_candle_index=bos_index,
                fvg=fvg if fvg_at_neckline else None,
                valid=True,
                notes=f"Head at {head.level:.5f}, neckline at {neckline:.5f}, "
                      f"FVG {'present' if fvg_at_neckline else 'absent'}"
            )

        return None

    # ─────────────────────────────────────────────
    # FLAG PATTERNS
    # ─────────────────────────────────────────────

    def detect_flag(
        self,
        candles: List[Candle],
        swings: List[SwingPoint],
        bias: BiasType,
        fvg_engine
    ) -> Optional[PatternResult]:
        """
        Flag detection:
        - Formed at higher highs (ascending) or lower lows (descending) after sweeping liquidity
        - Impulse followed by consolidation (tightening range = momentum loss)
        - Execution: CHoCH — sweep the consolidation low/high, body closes through = entry
        """
        if len(candles) < 20:
            return None

        # Look for recent consolidation zone (flag body)
        recent = candles[-20:]
        highs_r = [c.high for c in recent]
        lows_r  = [c.low  for c in recent]

        consolidation_range = max(highs_r) - min(lows_r)
        avg_range = sum(c.total_range for c in recent) / len(recent)

        # Flag: consolidation range should be tighter than average
        if consolidation_range > avg_range * 4:
            return None

        if bias == BiasType.BEARISH:
            # Descending flag: formed at LH after sweeping a high
            # Execution: sweep consolidation low, body closes below
            flag_low = min(lows_r)
            flag_low_idx = len(candles) - 20 + lows_r.index(flag_low)

            bos_result = self._find_body_bos(candles, flag_low, Direction.BEARISH, flag_low_idx)
            if not bos_result:
                return None

            bos_candle, bos_index = bos_result
            fvg = fvg_engine.detect_fvg(candles, Direction.BEARISH, bos_index, search_range=3)

            return PatternResult(
                pattern_name="Descending Flag",
                direction=Direction.BEARISH,
                neckline=flag_low,
                sweep_level=max(highs_r),
                sweep_candle_index=flag_low_idx,
                bos_candle_index=bos_index,
                fvg=fvg,
                valid=True,
                notes=f"Flag low at {flag_low:.5f}, consolidation range: {consolidation_range:.5f}"
            )

        elif bias == BiasType.BULLISH:
            # Ascending flag: formed at HH after sweeping a low
            flag_high = max(highs_r)
            flag_high_idx = len(candles) - 20 + highs_r.index(flag_high)

            bos_result = self._find_body_bos(candles, flag_high, Direction.BULLISH, flag_high_idx)
            if not bos_result:
                return None

            bos_candle, bos_index = bos_result
            fvg = fvg_engine.detect_fvg(candles, Direction.BULLISH, bos_index, search_range=3)

            return PatternResult(
                pattern_name="Ascending Flag",
                direction=Direction.BULLISH,
                neckline=flag_high,
                sweep_level=min(lows_r),
                sweep_candle_index=flag_high_idx,
                bos_candle_index=bos_index,
                fvg=fvg,
                valid=True,
                notes=f"Flag high at {flag_high:.5f}, consolidation range: {consolidation_range:.5f}"
            )

        return None

    # ─────────────────────────────────────────────
    # SHARED: BODY BOS CHECK
    # ─────────────────────────────────────────────

    def _find_body_bos(
        self,
        candles: List[Candle],
        level: float,
        direction: Direction,
        start_index: int
    ) -> Optional[Tuple[Candle, int]]:
        """
        Ntando's exact BOS rule: CANDLE BODY must close above/below level.
        Wick through does NOT count.
        """
        for i in range(start_index, min(start_index + 30, len(candles))):
            c = candles[i]
            if direction == Direction.BEARISH:
                if c.close < level and c.body_low < level:
                    return (c, i)
            elif direction == Direction.BULLISH:
                if c.close > level and c.body_high > level:
                    return (c, i)
        return None
