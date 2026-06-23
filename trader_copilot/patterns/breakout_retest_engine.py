"""
Trader Copilot — Breakout & Retest Pattern Engine (Currency Ruleset)
=====================================================================
Detects the Breakout & Retest entry model for currency pairs on the 30M timeframe.

Entry Model (per Ntando's chart analysis):
──────────────────────────────────────────
1. CHANNEL: Descending or ascending channel defines the dominant trend bias.
   The 90% rule level marks the channel origin (hard invalidation boundary).

2. CORRECTIVE WEDGE: Inside the channel, price forms a rising wedge (in downtrend)
   or falling wedge (in uptrend) — a corrective structure against the trend.

3. BREAKOUT: Price breaks out of the wedge in the direction of the channel trend.
   Breakout candle BODY must close beyond the wedge boundary (same rule as BOS).
   This displacement leaves an OB and optionally an FVG.

4. RETEST & ENTRY:
   - Price retraces back to the OB left by the breakout candle
   - Entry: LIMIT ORDER at the TOP of the OB (for shorts) / BOTTOM of OB (for longs)
   - Stop: beyond the OB (below OB low for shorts / above OB high for longs)
   - Hard invalidation: price closes beyond 90% rule level

Timeframe: 30M (currency pairs only)
Supported pairs: EURUSD, GBPUSD, EURAUD, EURCAD, AUDCAD, CADJPY, GBPCAD
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..core.structures import (
    Candle,
    Direction,
    BiasType,
    FairValueGap,
    OrderBlock,
)
from ..core.poi_engine import POIEngine
from ..config.pairs import PairConfig


# ─────────────────────────────────────────────────────────────────────────────
# RESULT DATACLASS
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BreakoutRetestResult:
    """Output of a confirmed Breakout & Retest setup."""

    pattern_name: str  # e.g. "Bearish Breakout & Retest"
    direction: Direction  # BEARISH or BULLISH
    channel_bias: BiasType  # Dominant channel direction

    # Channel levels
    channel_high: float  # Upper boundary of channel
    channel_low: float  # Lower boundary of channel
    ninety_pct_level: float  # 90% rule — channel origin / hard invalidation

    # Wedge levels
    wedge_high: float  # Top of corrective wedge
    wedge_low: float  # Bottom of corrective wedge
    wedge_breakout_index: int  # Candle index where body broke wedge

    # POI
    order_block: OrderBlock  # OB left by breakout candle
    fvg: Optional[FairValueGap]  # FVG from displacement (optional)

    # Entry levels
    entry_price: float  # TOP of OB (short) / BOTTOM of OB (long)
    stop_loss: float  # Beyond OB
    retest_index: Optional[int]  # Candle index where retest occurred

    valid: bool = True
    notes: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# CHANNEL DETECTOR
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Channel:
    high: float  # Upper trendline anchor
    low: float  # Lower trendline anchor
    bias: BiasType  # BEARISH = descending, BULLISH = ascending
    ninety_pct_level: float  # Origin of the channel (90% rule)
    start_index: int
    end_index: int


def detect_channel(candles: List[Candle], lookback: int = 60) -> Optional[Channel]:
    """
    Simplified channel detection:
    - Scans the last `lookback` candles
    - Identifies the swing high and swing low of that range
    - Determines bias from the slope: if recent highs and lows are declining → bearish
    - 90% rule level = the starting point (origin) of the channel range

    This is a structural approximation. For live use, this feeds from
    manually confirmed channels or from the structure engine's swing sequence.
    """
    if len(candles) < lookback:
        lookback = len(candles)

    window = candles[-lookback:]
    start_idx = len(candles) - lookback

    highs = [c.high for c in window]
    lows = [c.low for c in window]

    channel_high = max(highs)
    channel_low = min(lows)

    high_idx = highs.index(channel_high)
    low_idx = lows.index(channel_low)

    # Bias: if the high came before the low → bearish (price dropped)
    #       if the low came before the high → bullish (price rose)
    if high_idx < low_idx:
        bias = BiasType.BEARISH
        # 90% rule = the high that started the channel (origin)
        ninety_pct_level = channel_high
    elif low_idx < high_idx:
        bias = BiasType.BULLISH
        # 90% rule = the low that started the channel (origin)
        ninety_pct_level = channel_low
    else:
        return None  # Ambiguous

    return Channel(
        high=channel_high,
        low=channel_low,
        bias=bias,
        ninety_pct_level=ninety_pct_level,
        start_index=start_idx,
        end_index=len(candles) - 1,
    )


# ─────────────────────────────────────────────────────────────────────────────
# WEDGE DETECTOR
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Wedge:
    high: float  # Top of corrective wedge (resistance trendline)
    low: float  # Bottom of corrective wedge (support trendline)
    direction: (
        BiasType  # BULLISH = rising wedge (in downtrend), BEARISH = falling wedge
    )
    start_index: int
    end_index: int


def detect_corrective_wedge(
    candles: List[Candle], channel_bias: BiasType, lookback: int = 30
) -> Optional[Wedge]:
    """
    Detects the corrective wedge formed inside the channel.

    In a BEARISH channel → look for a rising wedge (corrective move up):
      - Recent swing highs are rising
      - Recent swing lows are rising
      - But the range is tightening (converging trendlines)

    In a BULLISH channel → look for a falling wedge (corrective move down):
      - Recent swing highs are falling
      - Recent swing lows are falling
      - Range is tightening

    Uses the most recent `lookback` candles as the wedge window.
    """
    if len(candles) < lookback:
        lookback = len(candles)

    window = candles[-lookback:]
    start_idx = len(candles) - lookback

    highs = [c.high for c in window]
    lows = [c.low for c in window]

    wedge_high = max(highs)
    wedge_low = min(lows)

    # Split window in half to check slope convergence
    mid = lookback // 2
    first_half_range = max(highs[:mid]) - min(lows[:mid])
    second_half_range = max(highs[mid:]) - min(lows[mid:])

    # Wedge must be tightening (corrective, losing momentum)
    if second_half_range >= first_half_range:
        return None  # Range is expanding, not a corrective wedge

    if channel_bias == BiasType.BEARISH:
        # Expect a rising wedge: recent highs and lows both drifting up
        first_avg_close = sum(c.close for c in window[:mid]) / mid
        second_avg_close = sum(c.close for c in window[mid:]) / (lookback - mid)
        if second_avg_close <= first_avg_close:
            return None  # Not rising — not a rising wedge in downtrend
        wedge_direction = BiasType.BULLISH  # Rising wedge = bullish correction

    elif channel_bias == BiasType.BULLISH:
        # Expect a falling wedge: recent price drifting down
        first_avg_close = sum(c.close for c in window[:mid]) / mid
        second_avg_close = sum(c.close for c in window[mid:]) / (lookback - mid)
        if second_avg_close >= first_avg_close:
            return None  # Not falling — not a falling wedge in uptrend
        wedge_direction = BiasType.BEARISH  # Falling wedge = bearish correction

    else:
        return None

    return Wedge(
        high=wedge_high,
        low=wedge_low,
        direction=wedge_direction,
        start_index=start_idx,
        end_index=len(candles) - 1,
    )


# ─────────────────────────────────────────────────────────────────────────────
# BREAKOUT DETECTOR
# ─────────────────────────────────────────────────────────────────────────────


def detect_wedge_breakout(
    candles: List[Candle], wedge: Wedge, channel_bias: BiasType, search_from: int
) -> Optional[Tuple[Candle, int]]:
    """
    Detects the breakout candle from the corrective wedge.

    Rule (matches Ntando's BOS rule): CANDLE BODY must close beyond the wedge boundary.
    Wick-only breaks do NOT count.

    Bearish channel → breakout is BEARISH (body closes below wedge low)
    Bullish channel → breakout is BULLISH (body closes above wedge high)
    """
    for i in range(search_from, len(candles)):
        c = candles[i]
        if channel_bias == BiasType.BEARISH:
            # Body closes below wedge low → bearish breakout confirmed
            if c.close < wedge.low and c.body_low < wedge.low:
                return (c, i)
        elif channel_bias == BiasType.BULLISH:
            # Body closes above wedge high → bullish breakout confirmed
            if c.close > wedge.high and c.body_high > wedge.high:
                return (c, i)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# RETEST DETECTOR
# ─────────────────────────────────────────────────────────────────────────────


def detect_ob_retest(
    candles: List[Candle],
    ob: OrderBlock,
    direction: Direction,
    breakout_index: int,
    ninety_pct_level: float,
    pip_size: float,
    tolerance_pips: int = 3,
) -> Optional[int]:
    """
    After the breakout, waits for price to retrace back to the OB.

    Entry rule: price touches the TOP of the OB (for shorts) or BOTTOM of OB (for longs).
    Hard invalidation: price closes BEYOND the 90% rule level.

    Returns the index of the retest candle if found, else None.
    """
    tolerance = tolerance_pips * pip_size

    for i in range(breakout_index + 1, len(candles)):
        c = candles[i]

        # Hard invalidation check — 90% rule breached
        if direction == Direction.BEARISH:
            if c.close > ninety_pct_level:
                return None  # Channel structure invalidated
            # Retest: price rises back to TOP of OB
            if c.high >= (ob.top - tolerance):
                return i

        elif direction == Direction.BULLISH:
            if c.close < ninety_pct_level:
                return None  # Channel structure invalidated
            # Retest: price drops back to BOTTOM of OB
            if c.low <= (ob.bottom + tolerance):
                return i

    return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────────────────────────────────────


class BreakoutRetestEngine:
    """
    Currency-specific Breakout & Retest detection engine.
    Operates exclusively on 30M candles.

    Integrates with the existing TraderCopilot architecture:
    - Uses POIEngine for OB and FVG detection
    - Outputs BreakoutRetestResult compatible with signal_generator
    """

    TIMEFRAME = "30M"

    def __init__(self, config: PairConfig):
        self.config = config
        self.poi_engine = POIEngine(config)

    def analyse(
        self,
        candles_30m: List[Candle],
        channel_lookback: int = 80,
        wedge_lookback: int = 30,
        breakout_scan: int = 10,
    ) -> Optional[BreakoutRetestResult]:
        """
        Full Breakout & Retest pipeline on 30M candles:

        1. Detect dominant channel (bias + 90% rule level)
        2. Detect corrective wedge inside channel
        3. Detect breakout candle (body close beyond wedge)
        4. Detect OB left by breakout candle
        5. Detect FVG from displacement (optional)
        6. Wait for retest of OB top/bottom
        7. Return confirmed setup with entry, stop, notes

        breakout_scan: number of recent candles to scan for the breakout.
                       The wedge is detected from BEFORE this window so the
                       breakout candle cannot expand the wedge detection range.
        """

        if len(candles_30m) < channel_lookback:
            return None

        # ── Step 1: Channel ──────────────────────────────────────────────────
        channel = detect_channel(candles_30m, lookback=channel_lookback)
        if not channel:
            return None

        direction = (
            Direction.BEARISH if channel.bias == BiasType.BEARISH else Direction.BULLISH
        )

        # ── Step 2: Corrective Wedge ─────────────────────────────────────────
        # KEY FIX: slice candles BEFORE the breakout scan window so the
        # breakout candle's extreme low/high cannot expand the tightening check
        # range and cause the wedge detector to reject a valid wedge.
        wedge_window_end = len(candles_30m) - breakout_scan
        if wedge_window_end < channel_lookback:
            return None

        wedge = detect_corrective_wedge(
            candles_30m[:wedge_window_end],
            channel_bias=channel.bias,
            lookback=wedge_lookback,
        )
        if not wedge:
            return None

        # ── Step 3: Breakout Candle ──────────────────────────────────────────
        # Search from the breakout scan window start through the full array
        search_from = wedge_window_end
        breakout_result = detect_wedge_breakout(
            candles_30m, wedge=wedge, channel_bias=channel.bias, search_from=search_from
        )
        if not breakout_result:
            return None

        breakout_candle, breakout_index = breakout_result

        # ── Step 4: Order Block ──────────────────────────────────────────────
        # OB = last candle of opposite colour before the breakout displacement
        ob = self.poi_engine.detect_order_block(
            candles=candles_30m,
            direction=direction,
            sweep_index=breakout_index,
            lookback=5,
        )
        if not ob:
            return None

        # ── Step 5: FVG from Displacement (optional) ─────────────────────────
        fvg = self.poi_engine.detect_fvg(
            candles=candles_30m,
            direction=direction,
            around_index=breakout_index,
            search_range=3,
        )

        # ── Step 6: Retest of OB ─────────────────────────────────────────────
        retest_index = detect_ob_retest(
            candles=candles_30m,
            ob=ob,
            direction=direction,
            breakout_index=breakout_index,
            ninety_pct_level=channel.ninety_pct_level,
            pip_size=self.config.pip_size,
            tolerance_pips=3,
        )

        # ── Step 7: Build Entry Levels ────────────────────────────────────────
        # SL must be at least as wide as the OB itself.
        # On indices (US500, US30, GER40) sweep_wick_pips * pip_size is a tiny
        # fraction of the actual OB range, producing absurdly tight stops and
        # inflated R:R. Using max(ob_height, pip_buffer) fixes this for all
        # instruments without changing forex behaviour (where ob_height ≈ pip_buffer).
        ob_height = ob.top - ob.bottom
        pip_buffer = self.config.sweep_wick_pips * self.config.pip_size
        min_sl_dist = max(ob_height, pip_buffer)

        if direction == Direction.BEARISH:
            entry_price = ob.top  # Limit short at TOP of OB
            stop_loss = ob.top + min_sl_dist
        else:
            entry_price = ob.bottom  # Limit long at BOTTOM of OB
            stop_loss = ob.bottom - min_sl_dist

        # Notes
        fvg_note = "FVG present at displacement" if fvg else "No FVG — OB only"
        retest_note = (
            f"Retest confirmed at candle {retest_index}"
            if retest_index is not None
            else "Retest pending — awaiting OB touch"
        )

        notes = (
            f"Channel: {channel.bias.value} | "
            f"90% rule: {channel.ninety_pct_level:.5f} | "
            f"Wedge: {wedge.low:.5f}–{wedge.high:.5f} | "
            f"OB: {ob.bottom:.5f}–{ob.top:.5f} | "
            f"{fvg_note} | {retest_note}"
        )

        return BreakoutRetestResult(
            pattern_name=f"{'Bearish' if direction == Direction.BEARISH else 'Bullish'} Breakout & Retest",
            direction=direction,
            channel_bias=channel.bias,
            channel_high=channel.high,
            channel_low=channel.low,
            ninety_pct_level=channel.ninety_pct_level,
            wedge_high=wedge.high,
            wedge_low=wedge.low,
            wedge_breakout_index=breakout_index,
            order_block=ob,
            fvg=fvg,
            entry_price=entry_price,
            stop_loss=stop_loss,
            retest_index=retest_index,
            valid=True,
            notes=notes,
        )

    def to_pattern_result(self, result: BreakoutRetestResult):
        """
        Converts BreakoutRetestResult to a PatternResult-compatible dict
        so it can feed into the existing signal_generator without changes.
        """
        from ..patterns.pattern_engine import PatternResult

        return PatternResult(
            pattern_name=result.pattern_name,
            direction=result.direction,
            neckline=(
                result.wedge_low
                if result.direction == Direction.BEARISH
                else result.wedge_high
            ),
            sweep_level=(
                result.channel_high
                if result.direction == Direction.BEARISH
                else result.channel_low
            ),
            sweep_candle_index=result.wedge_breakout_index,
            bos_candle_index=result.wedge_breakout_index,
            fvg=result.fvg,
            valid=result.valid,
            notes=result.notes,
        )
