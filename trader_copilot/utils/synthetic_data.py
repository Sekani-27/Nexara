"""
Trader Copilot — Synthetic Market Data Generator
Generates realistic OHLCV candle sequences for backtesting.

Behaviour modelled:
- Realistic price ranges per pair (XAUUSDm ~2300-2400, USTEC_x100m ~17000-19000)
- Trending phases (impulse) + consolidation phases (flag/range)
- Liquidity sweeps at swing highs/lows
- Volatility clustering (quiet → explosive)
- Session-aware volume weighting
- SMC-like structure: HH/HL uptrend, LH/LL downtrend, reversals
"""

import random
import math
import csv
import os
from datetime import datetime, timedelta
from typing import List, Tuple
from dataclasses import dataclass


@dataclass
class OHLCVRow:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


# ─────────────────────────────────────────────
# PAIR SPECS
# ─────────────────────────────────────────────

PAIR_SPECS = {
    "XAUUSDm": {
        "start_price":   2320.0,
        "pip_size":      0.01,
        "atr_4h":        8.0,      # Average 4H candle range in $
        "atr_15m":       2.5,      # Average 15M candle range
        "trend_strength": 0.55,    # % of candles in trend direction
        "sweep_freq":    0.08,     # Probability of sweep per swing
        "volatility_cluster": True,
    },
    "USTEC_x100m": {
        "start_price":   17800.0,
        "pip_size":      1.0,
        "atr_4h":        120.0,
        "atr_15m":       35.0,
        "trend_strength": 0.54,
        "sweep_freq":    0.07,
        "volatility_cluster": True,
    },
    "EURUSDm": {
        "start_price":   1.08500,
        "pip_size":      0.0001,
        "atr_4h":        0.0035,
        "atr_15m":       0.0010,
        "trend_strength": 0.53,
        "sweep_freq":    0.06,
        "volatility_cluster": False,
    },
    "GBPUSDm": {
        "start_price":   1.26500,
        "pip_size":      0.0001,
        "atr_4h":        0.0045,
        "atr_15m":       0.0013,
        "trend_strength": 0.53,
        "sweep_freq":    0.06,
        "volatility_cluster": False,
    },
}


# ─────────────────────────────────────────────
# MARKET REGIME
# ─────────────────────────────────────────────

class MarketRegime:
    BULLISH_TREND   = "bullish_trend"
    BEARISH_TREND   = "bearish_trend"
    CONSOLIDATION   = "consolidation"
    BULLISH_IMPULSE = "bullish_impulse"
    BEARISH_IMPULSE = "bearish_impulse"


# ─────────────────────────────────────────────
# GENERATOR
# ─────────────────────────────────────────────

class SyntheticDataGenerator:

    def __init__(self, symbol: str, seed: int = 42):
        if symbol not in PAIR_SPECS:
            raise ValueError(f"Unknown symbol: {symbol}")
        self.symbol = symbol
        self.spec   = PAIR_SPECS[symbol]
        random.seed(seed)

    def generate_4h(self, n_candles: int = 500, start_date: datetime = None) -> List[OHLCVRow]:
        """Generate HTF (4H) candles with realistic trend structure."""
        if start_date is None:
            start_date = datetime(2024, 1, 1, 0, 0)
        return self._generate(
            n_candles=n_candles,
            start_date=start_date,
            interval_hours=4,
            atr=self.spec["atr_4h"],
        )

    def generate_15m(self, n_candles: int = 2000, start_date: datetime = None) -> List[OHLCVRow]:
        """Generate LTF (15M) candles."""
        if start_date is None:
            start_date = datetime(2024, 1, 1, 0, 0)
        return self._generate(
            n_candles=n_candles,
            start_date=start_date,
            interval_hours=0.25,
            atr=self.spec["atr_15m"],
        )

    def generate_30m(self, n_candles: int = 1500, start_date: datetime = None) -> List[OHLCVRow]:
        """Generate 30M candles for EURUSDm/GBPUSDm structure TF."""
        if start_date is None:
            start_date = datetime(2024, 1, 1, 0, 0)
        return self._generate(
            n_candles=n_candles,
            start_date=start_date,
            interval_hours=0.5,
            atr=self.spec["atr_4h"] * 0.5,
        )

    def _generate(
        self,
        n_candles: int,
        start_date: datetime,
        interval_hours: float,
        atr: float,
    ) -> List[OHLCVRow]:

        price     = self.spec["start_price"]
        rows      = []
        regime    = MarketRegime.BULLISH_TREND
        regime_bars_left = random.randint(20, 60)

        # Track recent swing for sweep injection
        recent_swing_high = price * 1.002
        recent_swing_low  = price * 0.998
        swing_high_age    = 0
        swing_low_age     = 0

        volatility_mult = 1.0

        for i in range(n_candles):
            ts = start_date + timedelta(hours=interval_hours * i)

            # Skip weekends (forex closes)
            if ts.weekday() >= 5:
                continue

            # Session volatility multiplier
            hour = ts.hour
            if 7 <= hour < 10:      # London open
                sess_mult = 1.3
            elif 13 <= hour < 16:   # NY open
                sess_mult = 1.4
            elif 10 <= hour < 13:   # London/NY overlap
                sess_mult = 1.2
            elif 0 <= hour < 3:     # Asian
                sess_mult = 0.7
            else:
                sess_mult = 0.9

            # Volatility clustering
            if self.spec["volatility_cluster"] and random.random() < 0.05:
                volatility_mult = random.uniform(1.5, 2.5)
            volatility_mult = max(0.6, volatility_mult * 0.97 + random.uniform(-0.02, 0.05))

            effective_atr = atr * sess_mult * volatility_mult

            # Regime management
            regime_bars_left -= 1
            if regime_bars_left <= 0:
                regime, regime_bars_left = self._next_regime(regime)

            # Directional bias
            if regime == MarketRegime.BULLISH_TREND:
                bias = 0.58
            elif regime == MarketRegime.BEARISH_TREND:
                bias = 0.42
            elif regime == MarketRegime.BULLISH_IMPULSE:
                bias = 0.75
            elif regime == MarketRegime.BEARISH_IMPULSE:
                bias = 0.25
            else:  # consolidation
                bias = 0.50

            # Build candle
            is_bull = random.random() < bias
            body_size = random.uniform(0.2, 0.7) * effective_atr
            wick_top  = random.uniform(0.05, 0.35) * effective_atr
            wick_bot  = random.uniform(0.05, 0.35) * effective_atr

            if is_bull:
                open_  = price - random.uniform(0, body_size * 0.3)
                close_ = open_ + body_size
                high_  = close_ + wick_top
                low_   = open_  - wick_bot
            else:
                open_  = price + random.uniform(0, body_size * 0.3)
                close_ = open_ - body_size
                high_  = open_  + wick_top
                low_   = close_ - wick_bot

            # Liquidity sweep injection
            swing_high_age += 1
            swing_low_age  += 1

            if swing_high_age > 8 and regime in (MarketRegime.BEARISH_TREND, MarketRegime.BEARISH_IMPULSE):
                if random.random() < self.spec["sweep_freq"]:
                    # Sweep the swing high with a wick, body closes below
                    sweep_wick = atr * random.uniform(0.3, 0.8)
                    high_   = recent_swing_high + sweep_wick
                    close_  = recent_swing_high - atr * random.uniform(0.1, 0.4)
                    open_   = recent_swing_high - atr * random.uniform(0.05, 0.2)
                    low_    = close_ - atr * random.uniform(0.1, 0.3)
                    swing_high_age = 0

            if swing_low_age > 8 and regime in (MarketRegime.BULLISH_TREND, MarketRegime.BULLISH_IMPULSE):
                if random.random() < self.spec["sweep_freq"]:
                    sweep_wick = atr * random.uniform(0.3, 0.8)
                    low_    = recent_swing_low - sweep_wick
                    close_  = recent_swing_low + atr * random.uniform(0.1, 0.4)
                    open_   = recent_swing_low + atr * random.uniform(0.05, 0.2)
                    high_   = close_ + atr * random.uniform(0.1, 0.3)
                    swing_low_age = 0

            # Clamp and round
            prec = len(str(self.spec["pip_size"]).rstrip("0").split(".")[-1]) if "." in str(self.spec["pip_size"]) else 0
            high_  = round(max(open_, close_, high_), max(2, prec + 2))
            low_   = round(min(open_, close_, low_),  max(2, prec + 2))
            open_  = round(open_,  max(2, prec + 2))
            close_ = round(close_, max(2, prec + 2))

            # Track swings
            if high_ > recent_swing_high:
                recent_swing_high = high_
                swing_high_age = 0
            if low_ < recent_swing_low:
                recent_swing_low = low_
                swing_low_age = 0

            volume = random.uniform(800, 3000) * sess_mult

            rows.append(OHLCVRow(
                timestamp=ts,
                open=open_,
                high=high_,
                low=low_,
                close=close_,
                volume=round(volume),
            ))

            price = close_

        return rows

    def _next_regime(self, current: str) -> Tuple[str, int]:
        """Transition between market regimes."""
        transitions = {
            MarketRegime.BULLISH_TREND:   [(MarketRegime.CONSOLIDATION, 0.4),
                                           (MarketRegime.BULLISH_IMPULSE, 0.3),
                                           (MarketRegime.BEARISH_TREND, 0.3)],
            MarketRegime.BEARISH_TREND:   [(MarketRegime.CONSOLIDATION, 0.4),
                                           (MarketRegime.BEARISH_IMPULSE, 0.3),
                                           (MarketRegime.BULLISH_TREND, 0.3)],
            MarketRegime.CONSOLIDATION:   [(MarketRegime.BULLISH_TREND, 0.35),
                                           (MarketRegime.BEARISH_TREND, 0.35),
                                           (MarketRegime.BULLISH_IMPULSE, 0.15),
                                           (MarketRegime.BEARISH_IMPULSE, 0.15)],
            MarketRegime.BULLISH_IMPULSE: [(MarketRegime.CONSOLIDATION, 0.5),
                                           (MarketRegime.BEARISH_TREND, 0.3),
                                           (MarketRegime.BULLISH_TREND, 0.2)],
            MarketRegime.BEARISH_IMPULSE: [(MarketRegime.CONSOLIDATION, 0.5),
                                           (MarketRegime.BULLISH_TREND, 0.3),
                                           (MarketRegime.BEARISH_TREND, 0.2)],
        }

        options = transitions.get(current, [(MarketRegime.CONSOLIDATION, 1.0)])
        rand = random.random()
        cumulative = 0.0
        for regime, prob in options:
            cumulative += prob
            if rand <= cumulative:
                duration = {
                    MarketRegime.BULLISH_TREND:   random.randint(30, 80),
                    MarketRegime.BEARISH_TREND:   random.randint(30, 80),
                    MarketRegime.CONSOLIDATION:   random.randint(15, 40),
                    MarketRegime.BULLISH_IMPULSE: random.randint(8, 20),
                    MarketRegime.BEARISH_IMPULSE: random.randint(8, 20),
                }[regime]
                return regime, duration

        return MarketRegime.CONSOLIDATION, 30

    # ─────────────────────────────────────────────
    # CSV EXPORT
    # ─────────────────────────────────────────────

    def to_csv(self, rows: List[OHLCVRow], filepath: str):
        """Write candles to CSV in MT5-compatible format."""
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "open", "high", "low", "close", "volume"])
            for r in rows:
                writer.writerow([
                    r.timestamp.strftime("%Y.%m.%d %H:%M"),
                    r.open, r.high, r.low, r.close, r.volume
                ])
        print(f"Saved {len(rows)} candles → {filepath}")

    def to_candles(self, rows: List[OHLCVRow], timeframe: str):
        """Convert OHLCVRow list directly to Candle objects."""
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from trader_copilot.core.structures import Candle
        return [
            Candle(
                timestamp=r.timestamp,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
                timeframe=timeframe,
            )
            for r in rows
        ]
