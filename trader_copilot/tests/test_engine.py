"""
Trader Copilot — Test Suite
Validates core rules against synthetic candle data.
Run: python -m pytest tests/test_engine.py -v
  or: python tests/test_engine.py
"""

from datetime import datetime, timedelta
from trader_copilot.core.structures import Candle, Direction, BiasType, StructureType
from trader_copilot.core.structure_engine import StructureEngine
from trader_copilot.core.poi_engine import POIEngine
from trader_copilot.config.pairs import PAIR_CONFIGS


def make_candle(open_, high, low, close, i=0, tf="5M") -> Candle:
    return Candle(
        timestamp=datetime(2024, 1, 1, 0, 0) + timedelta(minutes=5 * i),
        open=open_, high=high, low=low, close=close,
        timeframe=tf
    )


def test_bos_requires_body_close():
    """
    Core rule: candle BODY must close below neckline.
    A wick through is NOT a BOS.
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = StructureEngine(config)

    neckline = 1.10000

    # Candle that wicks below but body stays above — NOT a BOS
    wick_only = make_candle(1.10050, 1.10100, 1.09900, 1.10020, i=0)
    assert wick_only.close > neckline, "Wick candle close should be above neckline"
    assert wick_only.body_low > neckline, "Wick candle body should stay above neckline"

    # Candle whose body closes below neckline — VALID BOS
    body_break = make_candle(1.10050, 1.10080, 1.09850, 1.09950, i=1)
    assert body_break.close < neckline, "Body-close candle should close below neckline"
    assert body_break.body_low < neckline, "Body low should be below neckline"

    candles = [make_candle(1.1010, 1.1015, 1.1005, 1.1012, i=0), wick_only, body_break]
    result = engine.detect_bos(candles, neckline, Direction.BEARISH, start_index=0)

    assert result is not None, "Should detect BOS on body-close candle"
    assert result[1] == 2, "BOS should be at index 2 (body_break), not the wick candle"
    print("PASS — BOS requires body close, not wick")


def test_sweep_requires_body_rejection():
    """
    Sweep rule: wick pierces level AND body closes back.
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = StructureEngine(config)

    level = 1.10000

    # Valid sweep: wick above level, body closes below
    candles = [make_candle(1.09980, 1.10060, 1.09950, 1.09970, i=0)]
    sweep = engine.detect_sweep(candles, level, Direction.BEARISH, 0)
    assert sweep is not None, "Should detect valid sweep"
    assert sweep.body_rejected, "Body should be rejected back below level"

    # Invalid: wick above but body also closes above
    candles2 = [make_candle(1.09980, 1.10060, 1.09990, 1.10020, i=0)]
    sweep2 = engine.detect_sweep(candles2, level, Direction.BEARISH, 0)
    assert sweep2 is None, "Should NOT detect sweep when body stays above level"

    print("PASS — Sweep requires wick + body rejection")


def test_fvg_detection():
    """
    FVG: 3-candle imbalance. Gap between prev.low and next.high (bearish).
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = POIEngine(config)

    # Create 3 candles with a clear bearish FVG
    # prev.low = 1.1000, next.high = 1.0990 → gap = 0.0010 = 10 pips
    candles = [
        make_candle(1.1020, 1.1030, 1.1000, 1.1005, i=0),  # prev: low=1.1000
        make_candle(1.1005, 1.1010, 1.0970, 1.0975, i=1),  # displacement
        make_candle(1.0975, 1.0990, 1.0960, 1.0965, i=2),  # next: high=1.0990
    ]

    fvg = engine.detect_fvg(candles, Direction.BEARISH, around_index=1, search_range=2)
    assert fvg is not None, "Should detect bearish FVG"
    assert fvg.top == 1.1000, f"FVG top should be 1.1000, got {fvg.top}"
    assert fvg.bottom == 1.0990, f"FVG bottom should be 1.0990, got {fvg.bottom}"
    assert fvg.size >= config.fvg_min_pips * config.pip_size, "FVG size should meet minimum"

    print(f"PASS — FVG detected: top={fvg.top}, bottom={fvg.bottom}, size={fvg.size:.5f}")


def test_equal_highs_detection():
    """
    USTEC_x100m / double top edge: equal highs within tolerance.
    """
    config = PAIR_CONFIGS["USTEC_x100m"]
    engine = StructureEngine(config)

    candles = [make_candle(18000, 18100 + i * 50, 17900, 18050, i=i) for i in range(20)]
    swings = engine.detect_swings(candles, left=2, right=2)

    # Manually inject two equal highs
    from trader_copilot.core.structures import SwingPoint
    swing1 = SwingPoint(candle=candles[5],  swing_type=StructureType.HH, level=19000.0, index=5)
    swing2 = SwingPoint(candle=candles[15], swing_type=StructureType.EQH, level=19015.0, index=15)  # 15pt gap < 20pt tolerance

    eq_pairs = engine.find_equal_levels([swing1, swing2], level_type="high")
    assert len(eq_pairs) > 0, "Should find equal high pair within USTEC_x100m tolerance"
    print(f"PASS — Equal highs detected within {config.peak_equality_pips}pt tolerance")


def test_bias_determination():
    """
    Bearish bias from LH/LL sequence, bullish from HH/HL.
    """
    config = PAIR_CONFIGS["EURUSDm"]
    engine = StructureEngine(config)

    from trader_copilot.core.structures import SwingPoint, StructureType

    c = make_candle(1.10, 1.11, 1.09, 1.105)

    bearish_swings = [
        SwingPoint(candle=c, swing_type=StructureType.LH, level=1.1100, index=0),
        SwingPoint(candle=c, swing_type=StructureType.LL, level=1.0900, index=1),
        SwingPoint(candle=c, swing_type=StructureType.LH, level=1.1050, index=2),
        SwingPoint(candle=c, swing_type=StructureType.LL, level=1.0850, index=3),
    ]
    assert engine.determine_bias(bearish_swings) == BiasType.BEARISH

    bullish_swings = [
        SwingPoint(candle=c, swing_type=StructureType.HH, level=1.1100, index=0),
        SwingPoint(candle=c, swing_type=StructureType.HL, level=1.0950, index=1),
        SwingPoint(candle=c, swing_type=StructureType.HH, level=1.1200, index=2),
        SwingPoint(candle=c, swing_type=StructureType.HL, level=1.1000, index=3),
    ]
    assert engine.determine_bias(bullish_swings) == BiasType.BULLISH
    print("PASS — Bias determination from swing sequence")


def test_killzone_filter():
    """
    XAUUSDm should only fire during NY open (13:00–14:30 UTC).
    """
    from trader_copilot.core.signal_generator import SignalGenerator

    config = PAIR_CONFIGS["XAUUSDm"]
    gen = SignalGenerator(config)

    inside_kz  = datetime(2024, 1, 2, 13, 30)  # 13:30 UTC — NY open
    outside_kz = datetime(2024, 1, 2, 10, 0)   # 10:00 UTC — outside for XAUUSDm

    assert gen.is_killzone_active(inside_kz)  == True,  "13:30 UTC should be in NY open KZ"
    assert gen.is_killzone_active(outside_kz) == False, "10:00 UTC should be outside XAUUSDm KZ"
    print("PASS — Killzone filter working for XAUUSDm")


if __name__ == "__main__":
    print("\n── Trader Copilot smc_core_v1 — Test Suite ──\n")
    test_bos_requires_body_close()
    test_sweep_requires_body_rejection()
    test_fvg_detection()
    test_equal_highs_detection()
    test_bias_determination()
    test_killzone_filter()
    print("\n── All tests passed ──\n")
