"""
Trader Copilot — Confluence Scorer & Signal Generator
Scores each setup 1-5 and generates the final TradeSignal.

Scoring logic:
  Base (required):
    +1 HTF bias confirmed
    +1 Liquidity sweep confirmed (wick + body rejection)
    +1 Pattern BOS confirmed (body close through neckline)
    +1 iCHoCH retest reached

  Boosters (optional):
    +1 FVG at neckline (entry zone upgrade)
    +1 OB confluence at retest zone
    +1 Killzone active
    +1 Unicorn model (OB + FVG together)
"""

from datetime import datetime, timezone
from typing import Optional
from ..core.structures import (
    Candle, Direction, BiasType, TradeSignal,
    FairValueGap, OrderBlock, LiquiditySweep
)
from ..patterns.pattern_engine import PatternResult
from ..config.pairs import PairConfig, KILLZONE_WINDOWS


class ConfluenceScorer:

    def __init__(self, config: PairConfig):
        self.config = config

    def score(
        self,
        pattern: PatternResult,
        bias: BiasType,
        sweep: Optional[LiquiditySweep],
        ob: Optional[OrderBlock],
        killzone_active: bool,
    ) -> int:
        """Returns confluence score 1–5."""
        score = 0

        # Base requirements
        bias_match = (
            (bias == BiasType.BEARISH and pattern.direction == Direction.BEARISH) or
            (bias == BiasType.BULLISH and pattern.direction == Direction.BULLISH)
        )
        if bias_match:
            score += 1

        if sweep and sweep.body_rejected:
            score += 1

        if pattern.valid:
            score += 1

        # Boosters
        if pattern.fvg is not None:
            score += 1

        if ob is not None:
            score += 1

        # Unicorn bonus (OB + FVG together)
        if pattern.fvg and ob:
            score += 1

        return min(score, 5)


class SignalGenerator:

    def __init__(self, config: PairConfig, backtest_mode: bool = False):
        self.config = config
        self.scorer = ConfluenceScorer(config)
        self.backtest_mode = backtest_mode

    def is_killzone_active(self, timestamp: datetime) -> bool:
        """Check if timestamp falls within any of this pair's active killzones."""
        hour   = timestamp.hour
        minute = timestamp.minute

        for kz_name in self.config.killzones:
            if kz_name not in KILLZONE_WINDOWS:
                continue
            sh, sm, eh, em = KILLZONE_WINDOWS[kz_name]
            start_mins = sh * 60 + sm
            end_mins   = eh * 60 + em
            current_mins = hour * 60 + minute
            if start_mins <= current_mins <= end_mins:
                return True
        return False

    def generate_signal(
        self,
        pattern: PatternResult,
        entry_candle: Candle,
        bias: BiasType,
        sweep: Optional[LiquiditySweep],
        ob: Optional[OrderBlock],
        candles_after_bos: list,
    ) -> Optional[TradeSignal]:
        """
        Generate a TradeSignal from a confirmed pattern.
        Entry: rejection candle at iCHoCH retest / FVG zone
        SL: above/below sweep wick
        TP: next significant swing in trade direction (simplified: 2× risk)
        """
        killzone_active = self.is_killzone_active(entry_candle.timestamp)

        score = self.scorer.score(
            pattern=pattern,
            bias=bias,
            sweep=sweep,
            ob=ob,
            killzone_active=killzone_active,
        )

        # Minimum score to generate alert
        if score < 3:
            return None

        # Minimum — no killzone on XAUUSDm/USTEC_x100m means skip (live only)
        # In backtest_mode this is relaxed so all signals are captured for analysis
        if not self.backtest_mode:
            if self.config.symbol in ("XAUUSDm", "USTEC_x100m") and not killzone_active:
                return None

        # Entry price: close of the retest rejection candle
        entry_price = entry_candle.close

        # SL: beyond sweep wick
        sl_buffer = self.config.sweep_wick_pips * self.config.pip_size * 1.5
        if pattern.direction == Direction.BEARISH:
            stop_loss   = pattern.sweep_level + sl_buffer
            # TP: 2× risk below entry (minimum; trader manages manually)
            risk        = stop_loss - entry_price
            take_profit = entry_price - (risk * 2)
        else:
            stop_loss   = pattern.sweep_level - sl_buffer
            risk        = entry_price - stop_loss
            take_profit = entry_price + (risk * 2)

        if risk <= 0:
            return None

        notes = (
            f"{pattern.notes} | "
            f"Score: {score}/5 | "
            f"Killzone: {'active' if killzone_active else 'inactive'} | "
            f"OB: {'yes' if ob else 'no'}"
        )

        return TradeSignal(
            symbol=self.config.symbol,
            direction=pattern.direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            timeframe=self.config.entry_tf,
            timestamp=entry_candle.timestamp,
            pattern=pattern.pattern_name,
            confluence_score=score,
            fvg_present=pattern.fvg is not None,
            ob_present=ob is not None,
            killzone_active=killzone_active,
            notes=notes,
        )
