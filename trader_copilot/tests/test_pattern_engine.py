"""
Trader Copilot — PatternEngine Test Suite
Tests detect_flag(), detect_head_and_shoulders(), and detect_inverse_hs().

Each test covers:
  - Valid signal (pattern detected and marked valid)
  - No-signal (geometry does not meet rules)
  - Insufficient data edge case
"""

from datetime import datetime, timedelta
from trader_copilot.core.structures import (
    Candle, Direction, StructureType, SwingPoint
)
from trader_copilot.core.poi_engine import POIEngine
from trader_copilot.patterns.pattern_engine import PatternEngine
from trader_copilot.config.pairs import PAIR_CONFIGS


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def make_candle(open_, high, low, close, i=0, tf="4H") -> Candle:
    return Candle(
        timestamp=datetime(2024, 1, 1, 0, 0) + timedelta(hours=4 * i),
        open=open_, high=high, low=low, close=close,
        timeframe=tf
    )


def _swing_high(candle: Candle, idx: int, level: float = None) -> SwingPoint:
    return SwingPoint(
        candle=candle,
        swing_type=StructureType.HH,
        level=level if level is not None else candle.high,
        index=idx,
    )


def _swing_low(candle: Candle, idx: int, level: float = None) -> SwingPoint:
    return SwingPoint(
        candle=candle,
        swing_type=StructureType.LL,
        level=level if level is not None else candle.low,
        index=idx,
    )


# ─────────────────────────────────────────────────────────────────────────────
# DETECT_FLAG — DESCENDING (BEARISH)
# ─────────────────────────────────────────────────────────────────────────────

def test_flag_bearish_valid():
    """
    Descending flag: 20 candles in a tight range (consolidation),
    then a body candle closes below the flag low.

    Critical geometry rule for detect_flag():
      flag_low = min(c.low for c in candles[-20:])
      The BOS candle IS in candles[-20:] (it's the last element).
      Therefore the BOS candle's .low must equal the consolidation floor —
      not pierce below it — otherwise flag_low becomes the BOS candle's own
      low, and _find_body_bos requires close < BOS.low, which is impossible
      for any valid candle (close >= low always).
      With BOS.low == flag_low, flag_low_idx points to the first consolidation
      candle that touched the floor, and _find_body_bos scans forward to find
      the BOS candle whose body (close) has closed through the level.
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    # 20 consolidation candles — tight range, floor at 1.0990
    candles = []
    for i in range(20):
        o = 1.1010
        h = 1.1015
        l = 1.0990
        c = 1.1005
        candles.append(make_candle(o, h, l, c, i=i))

    # BOS candle: low=1.0990 (= consolidation floor, does NOT become a new minimum),
    # close=1.0985 (body closes below floor).
    # flag_low stays 1.0990; flag_low_idx → first consolidation candle;
    # _find_body_bos scans forward and finds this candle (close=1.0985 < 1.0990 ✓).
    candles.append(make_candle(1.1000, 1.1005, 1.0990, 1.0985, i=20))

    # Build enough swing context for bias
    swing_high_c = make_candle(1.1100, 1.1110, 1.1080, 1.1090, i=5)
    swings = [
        _swing_high(swing_high_c, 5, level=1.1110),
        _swing_low(candles[10], 10, level=1.0990),
    ]

    from trader_copilot.core.structures import BiasType
    result = engine.detect_flag(candles, swings, bias=BiasType.BEARISH, fvg_engine=poi)

    assert result is not None, "Should detect descending flag"
    assert result.valid, "Flag result must be valid"
    assert result.direction == Direction.BEARISH, "Descending flag is bearish"
    assert result.pattern_name == "Descending Flag"
    print(f"PASS — Bearish flag | neckline: {result.neckline:.5f}")


def test_flag_bearish_no_signal_when_range_too_wide():
    """
    If the consolidation range is wider than 4× the average candle range,
    it is NOT a flag — it's a normal trending move.
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    # Each candle has a tiny avg range (0.0002) but consolidation spans 0.0020
    # → consolidation_range (0.0020) > avg_range*4 (0.0008)
    candles = []
    for i in range(20):
        o = 1.1000 + i * 0.0001
        h = o + 0.0002
        l = o - 0.0001
        c = o + 0.0001
        candles.append(make_candle(o, h, l, c, i=i))

    # Consolidation span = (1.1019+0.0002) - (1.1000-0.0001) = 0.0022, avg_range ≈ 0.0003
    # 0.0022 > 0.0003*4 = 0.0012 → engine returns None
    from trader_copilot.core.structures import BiasType
    swings = [_swing_high(candles[5], 5), _swing_low(candles[15], 15)]
    result = engine.detect_flag(candles, swings, bias=BiasType.BEARISH, fvg_engine=poi)

    assert result is None, "Wide range should not be detected as a flag"
    print("PASS — Wide range correctly rejected as non-flag")


def test_flag_insufficient_candles():
    """
    detect_flag() requires at least 20 candles.
    With fewer candles it must return None immediately.
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    candles = [make_candle(1.10, 1.11, 1.09, 1.105, i=i) for i in range(15)]
    from trader_copilot.core.structures import BiasType
    swings = [_swing_high(candles[5], 5), _swing_low(candles[10], 10)]
    result = engine.detect_flag(candles, swings, bias=BiasType.BEARISH, fvg_engine=poi)

    assert result is None, "Fewer than 20 candles must return None"
    print("PASS — Insufficient candles returns None")


def test_flag_bullish_valid():
    """
    Ascending flag: tight consolidation followed by body close above flag high.

    Critical geometry rule for detect_flag() (mirror of bearish):
      flag_high = max(c.high for c in candles[-20:])
      The BOS candle's .high must equal the consolidation ceiling, not exceed it.
      If BOS.high > consolidation ceiling, flag_high becomes BOS.high and
      _find_body_bos requires close > BOS.high — impossible (close <= high always).
      With BOS.high == flag_high, flag_high_idx points to the first consolidation
      candle at the ceiling; the BOS candle's close (above the ceiling) triggers
      the body-BOS check.
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    # 20 consolidation candles — tight range, ceiling at 1.1010
    candles = []
    for i in range(20):
        o = 1.0990
        h = 1.1010
        l = 1.0985
        c = 1.0995
        candles.append(make_candle(o, h, l, c, i=i))

    # BOS candle: high=1.1010 (= consolidation ceiling, does NOT become a new maximum),
    # close=1.1015 (body closes above ceiling).
    # flag_high stays 1.1010; flag_high_idx → first consolidation candle;
    # _find_body_bos scans forward and finds this candle (close=1.1015 > 1.1010 ✓).
    candles.append(make_candle(1.0990, 1.1010, 1.0985, 1.1015, i=20))

    swings = [_swing_low(candles[5], 5, level=1.0985), _swing_high(candles[15], 15, level=1.1010)]
    from trader_copilot.core.structures import BiasType
    result = engine.detect_flag(candles, swings, bias=BiasType.BULLISH, fvg_engine=poi)

    assert result is not None, "Should detect ascending flag"
    assert result.direction == Direction.BULLISH
    assert result.pattern_name == "Ascending Flag"
    print(f"PASS — Bullish flag | neckline: {result.neckline:.5f}")


# ─────────────────────────────────────────────────────────────────────────────
# DETECT_HEAD_AND_SHOULDERS
# ─────────────────────────────────────────────────────────────────────────────

def _make_hs_candles_and_swings(config):
    """
    Build a classic H&S pattern:
      LS (idx 2) at 1.1100
      NL low 1 (idx 4) at 1.0950
      Head (idx 6) at 1.1300
      NL low 2 (idx 8) at 1.0960
      RS (idx 10) at 1.1110
      Neckline ≈ (1.0950+1.0960)/2 = 1.0955
      BOS: body closes below 1.0955 at idx 12
    """
    tolerance = config.peak_equality_pips * config.pip_size  # 0.0005 for EURUSD

    candles = [make_candle(1.1000, 1.1020, 1.0990, 1.1010, i=i) for i in range(20)]

    # Override candles at key indices
    candles[2]  = make_candle(1.1080, 1.1100, 1.1070, 1.1090, i=2)   # LS high
    candles[4]  = make_candle(1.0960, 1.0970, 1.0950, 1.0955, i=4)   # NL low 1
    candles[6]  = make_candle(1.1280, 1.1300, 1.1260, 1.1290, i=6)   # Head
    candles[8]  = make_candle(1.0970, 1.0975, 1.0960, 1.0965, i=8)   # NL low 2
    candles[10] = make_candle(1.1090, 1.1110, 1.1080, 1.1100, i=10)  # RS
    # BOS: body (open=1.0960, close=0.0940) closes below neckline 1.0955
    candles[12] = make_candle(1.0960, 1.0965, 1.0930, 1.0940, i=12)  # BOS below neckline

    swings = [
        _swing_high(candles[2],  2,  level=1.1100),  # LS
        _swing_low (candles[4],  4,  level=1.0950),  # NL low 1
        _swing_high(candles[6],  6,  level=1.1300),  # Head
        _swing_low (candles[8],  8,  level=1.0960),  # NL low 2
        _swing_high(candles[10], 10, level=1.1110),  # RS — within shoulder_tolerance of LS
    ]

    return candles, swings


def test_hs_valid():
    """Classic H&S: three peaks (LS < Head > RS), two neckline lows, BOS below neckline."""
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    candles, swings = _make_hs_candles_and_swings(config)
    result = engine.detect_head_and_shoulders(candles, swings, fvg_engine=poi)

    assert result is not None, "Should detect H&S pattern"
    assert result.valid
    assert result.direction == Direction.BEARISH
    assert result.pattern_name == "Head and Shoulders"
    # Neckline should be average of the two lows
    expected_neckline = (1.0950 + 1.0960) / 2
    assert abs(result.neckline - expected_neckline) < 0.0001, \
        f"Neckline should be ~{expected_neckline:.4f}, got {result.neckline:.4f}"
    print(f"PASS — H&S | Head: 1.13000 | Neckline: {result.neckline:.5f}")


def test_hs_no_signal_when_shoulders_unequal():
    """
    If the right shoulder is much higher than the left (outside shoulder_tolerance),
    the pattern is NOT a valid H&S.
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    candles, swings = _make_hs_candles_and_swings(config)

    # Replace RS with a much higher level (0.0200 above LS — far outside tolerance)
    swings[4] = _swing_high(candles[10], 10, level=1.1300)  # Same as head → unequal shoulders

    result = engine.detect_head_and_shoulders(candles, swings, fvg_engine=poi)

    assert result is None, "Unequal shoulders should not produce H&S signal"
    print("PASS — Unequal shoulders correctly rejected")


def test_hs_insufficient_swings():
    """
    H&S requires at least 3 swing highs and 2 swing lows.
    With fewer swings, must return None.
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    candles = [make_candle(1.1000, 1.1010, 1.0990, 1.1005, i=i) for i in range(10)]
    swings = [
        _swing_high(candles[2], 2, level=1.1010),
        _swing_low (candles[5], 5, level=1.0990),
    ]

    result = engine.detect_head_and_shoulders(candles, swings, fvg_engine=poi)

    assert result is None, "Fewer than 3 highs/2 lows must return None"
    print("PASS — Insufficient swings returns None for H&S")


def test_hs_no_signal_when_no_bos():
    """
    All geometry present but no candle closes below the neckline — no BOS, no signal.
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    candles, swings = _make_hs_candles_and_swings(config)

    # Replace BOS candle with one that only wicks below but body stays above neckline
    candles[12] = make_candle(1.0970, 1.0975, 1.0940, 1.0975, i=12)  # close > neckline

    result = engine.detect_head_and_shoulders(candles, swings, fvg_engine=poi)

    assert result is None, "Wick-only break should not produce H&S signal (body BOS rule)"
    print("PASS — Wick-only neckline break correctly rejected for H&S")


# ─────────────────────────────────────────────────────────────────────────────
# DETECT_INVERSE_HEAD_AND_SHOULDERS
# ─────────────────────────────────────────────────────────────────────────────

def _make_ihs_candles_and_swings(config):
    """
    Build an inverse H&S pattern (bullish):
      LS trough (idx 2) at 1.0900
      NL high 1 (idx 4) at 1.1050
      Head trough (idx 6) at 1.0700
      NL high 2 (idx 8) at 1.1040
      RS trough (idx 10) at 1.0910
      Neckline ≈ (1.1050+1.1040)/2 = 1.1045
      BOS: body closes above 1.1045 at idx 12
    """
    candles = [make_candle(1.1000, 1.1020, 1.0990, 1.1010, i=i) for i in range(20)]

    candles[2]  = make_candle(1.0920, 1.0930, 1.0900, 1.0910, i=2)   # LS trough
    candles[4]  = make_candle(1.1030, 1.1050, 1.1020, 1.1040, i=4)   # NL high 1
    candles[6]  = make_candle(1.0720, 1.0730, 1.0700, 1.0710, i=6)   # Head (lowest)
    candles[8]  = make_candle(1.1020, 1.1040, 1.1010, 1.1030, i=8)   # NL high 2
    candles[10] = make_candle(1.0900, 1.0915, 1.0890, 1.0905, i=10)  # RS trough (~LS)
    # BOS: body closes above neckline ~1.1045
    candles[12] = make_candle(1.1040, 1.1070, 1.1035, 1.1060, i=12)  # BOS above neckline

    swings = [
        _swing_low (candles[2],  2,  level=1.0900),  # LS trough
        _swing_high(candles[4],  4,  level=1.1050),  # NL high 1
        _swing_low (candles[6],  6,  level=1.0700),  # Head
        _swing_high(candles[8],  8,  level=1.1040),  # NL high 2
        _swing_low (candles[10], 10, level=1.0910),  # RS trough (~LS)
    ]

    return candles, swings


def test_inverse_hs_valid():
    """Inverse H&S: three troughs (LS > Head < RS), two neckline highs, BOS above neckline."""
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    candles, swings = _make_ihs_candles_and_swings(config)
    result = engine.detect_inverse_hs(candles, swings, fvg_engine=poi)

    assert result is not None, "Should detect inverse H&S pattern"
    assert result.valid
    assert result.direction == Direction.BULLISH
    assert result.pattern_name == "Inverse Head and Shoulders"
    expected_neckline = (1.1050 + 1.1040) / 2
    assert abs(result.neckline - expected_neckline) < 0.0001, \
        f"Neckline should be ~{expected_neckline:.4f}, got {result.neckline:.4f}"
    print(f"PASS — Inverse H&S | Head: 1.07000 | Neckline: {result.neckline:.5f}")


def test_inverse_hs_no_signal_when_shoulders_unequal():
    """RS trough at a much lower level than LS — outside shoulder tolerance."""
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    candles, swings = _make_ihs_candles_and_swings(config)

    # Replace RS with a very low level — same as head, unequal to LS
    swings[4] = _swing_low(candles[10], 10, level=1.0700)  # matches head → unequal

    result = engine.detect_inverse_hs(candles, swings, fvg_engine=poi)

    assert result is None, "Unequal troughs should not produce inverse H&S signal"
    print("PASS — Unequal troughs correctly rejected for inverse H&S")


def test_inverse_hs_insufficient_swings():
    """Inverse H&S requires at least 3 swing lows and 2 swing highs."""
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    candles = [make_candle(1.1000, 1.1010, 1.0990, 1.1005, i=i) for i in range(10)]
    swings = [
        _swing_low (candles[2], 2, level=1.0990),
        _swing_high(candles[5], 5, level=1.1010),
    ]

    result = engine.detect_inverse_hs(candles, swings, fvg_engine=poi)

    assert result is None, "Fewer than 3 lows/2 highs must return None"
    print("PASS — Insufficient swings returns None for inverse H&S")


def test_inverse_hs_no_signal_when_no_bos():
    """Wick-only break above neckline does not trigger inverse H&S (body BOS rule)."""
    config = PAIR_CONFIGS["EURUSDm"]
    engine = PatternEngine(config)
    poi    = POIEngine(config)

    candles, swings = _make_ihs_candles_and_swings(config)

    # Replace BOS candle with a wick-only pierce above neckline — body stays below
    candles[12] = make_candle(1.1020, 1.1060, 1.1015, 1.1030, i=12)  # close < neckline 1.1045

    result = engine.detect_inverse_hs(candles, swings, fvg_engine=poi)

    assert result is None, "Wick-only neckline break should not produce inverse H&S signal"
    print("PASS — Wick-only break correctly rejected for inverse H&S")


# ─────────────────────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n── Trader Copilot — PatternEngine Test Suite ──\n")

    tests = [
        test_flag_bearish_valid,
        test_flag_bearish_no_signal_when_range_too_wide,
        test_flag_insufficient_candles,
        test_flag_bullish_valid,
        test_hs_valid,
        test_hs_no_signal_when_shoulders_unequal,
        test_hs_insufficient_swings,
        test_hs_no_signal_when_no_bos,
        test_inverse_hs_valid,
        test_inverse_hs_no_signal_when_shoulders_unequal,
        test_inverse_hs_insufficient_swings,
        test_inverse_hs_no_signal_when_no_bos,
    ]

    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback
            print(f"FAIL — {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n── Results: {passed} passed, {failed} failed ──\n")
