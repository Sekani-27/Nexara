"""
Trader Copilot — Main Orchestrator (smc_core_v1)
Full pipeline: candles → structure → pattern → POI → signal → alert

Usage:
    from trader_copilot.engine import TraderCopilot
    from trader_copilot.config.pairs import PAIR_CONFIGS

    engine = TraderCopilot("XAUUSD")
    signal = engine.analyse(candles_htf, candles_ltf)
    if signal:
        engine.alert(signal)

    # Currency pairs — Breakout & Retest model (30M only)
    engine = TraderCopilot("EURUSD")
    signal = engine.analyse_currency_30m(candles_30m)
    if signal:
        engine.alert(signal)
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from .core.structures import Candle, Direction, BiasType, TradeSignal

logger = logging.getLogger(__name__)
from .core.structure_engine import StructureEngine
from .core.poi_engine import POIEngine
from .core.signal_generator import SignalGenerator
from .patterns.pattern_engine import PatternEngine
from .patterns.breakout_retest_engine import BreakoutRetestEngine       # ← NEW
from .alerts.alert_engine import AlertEngine
from .config.pairs import PAIR_CONFIGS, PairConfig


class TraderCopilot:

    def __init__(
        self,
        symbol: str,
        webhook_urls: Optional[List[str]] = None,
        log_path: Optional[str] = "trader_copilot_alerts.jsonl",
        backtest_mode: bool = False,
    ):
        if symbol not in PAIR_CONFIGS:
            raise ValueError(f"Unknown symbol: {symbol}. Supported: {list(PAIR_CONFIGS.keys())}")

        self.symbol        = symbol
        self.backtest_mode = backtest_mode
        self.config: PairConfig = PAIR_CONFIGS[symbol]

        self.structure_engine       = StructureEngine(self.config)
        self.poi_engine             = POIEngine(self.config)
        self.pattern_engine         = PatternEngine(self.config)
        self.signal_generator       = SignalGenerator(self.config, backtest_mode=backtest_mode)
        self.breakout_retest_engine = BreakoutRetestEngine(self.config)
        self.alert_engine           = AlertEngine(webhook_urls=webhook_urls, log_path=log_path)

    # ─────────────────────────────────────────────
    # ORIGINAL PIPELINE (XAUUSD / NAS100)
    # ─────────────────────────────────────────────

    def analyse(
        self,
        candles_htf: List[Candle],   # Structure timeframe (e.g. 4H for XAUUSD)
        candles_ltf: List[Candle],   # Entry timeframe (e.g. 15M for XAUUSD)
    ) -> Optional[TradeSignal]:
        """
        Full pipeline:
        1. Detect swings on HTF
        2. Determine bias
        3. Scan all patterns on HTF swings
        4. Detect sweep + OB on LTF
        5. Find FVG at neckline on LTF
        6. Detect iCHoCH retest on LTF
        7. Score confluence → generate signal
        """

        # ── Step 1: HTF structure ──
        swings_htf = self.structure_engine.detect_swings(candles_htf, left=3, right=3)
        if len(swings_htf) < 4:
            return None

        bias = self.structure_engine.determine_bias(swings_htf)
        if bias == BiasType.RANGING:
            return None

        # ── Step 2: Pattern detection on HTF ──
        pattern = None

        if bias == BiasType.BEARISH:
            pattern = (
                self.pattern_engine.detect_double_top(candles_htf, swings_htf, self.poi_engine) or
                self.pattern_engine.detect_head_and_shoulders(candles_htf, swings_htf, self.poi_engine) or
                self.pattern_engine.detect_flag(candles_htf, swings_htf, bias, self.poi_engine)
            )
        elif bias == BiasType.BULLISH:
            pattern = (
                self.pattern_engine.detect_double_bottom(candles_ltf, swings_htf, self.poi_engine) or
                self.pattern_engine.detect_inverse_hs(candles_htf, swings_htf, self.poi_engine) or
                self.pattern_engine.detect_flag(candles_htf, swings_htf, bias, self.poi_engine)
            )

        if not pattern or not pattern.valid:
            return None

        # ── Step 3: LTF — sweep confirmation ──
        swings_ltf = self.structure_engine.detect_swings(candles_ltf, left=2, right=2)

        sweep = self.structure_engine.detect_sweep(
            candles=candles_ltf,
            level=pattern.sweep_level,
            direction=pattern.direction,
            index=min(pattern.sweep_candle_index, len(candles_ltf) - 1),
        )

        # ── Step 4: Order Block on LTF ──
        ob = self.poi_engine.detect_order_block(
            candles=candles_ltf,
            direction=pattern.direction,
            sweep_index=min(pattern.sweep_candle_index, len(candles_ltf) - 1),
        )

        # ── Step 5: FVG at neckline on LTF ──
        fvg = self.poi_engine.detect_fvg(
            candles=candles_ltf,
            direction=pattern.direction,
            around_index=min(pattern.bos_candle_index, len(candles_ltf) - 1),
            search_range=3,
        )
        if fvg and self.poi_engine.fvg_near_level(fvg, pattern.neckline):
            pattern.fvg = fvg  # Attach confirmed LTF FVG to pattern

        # ── Step 6: iCHoCH retest ──
        retest_index = self.poi_engine.detect_retest(
            candles=candles_ltf,
            level=pattern.neckline,
            direction=pattern.direction,
            bos_index=min(pattern.bos_candle_index, len(candles_ltf) - 1),
            fvg=pattern.fvg,
        )

        if retest_index is None:
            return None

        entry_candle = candles_ltf[retest_index]

        # ── Step 7: Score and generate signal ──
        signal = self.signal_generator.generate_signal(
            pattern=pattern,
            entry_candle=entry_candle,
            bias=bias,
            sweep=sweep,
            ob=ob,
            candles_after_bos=candles_ltf[pattern.bos_candle_index:],
        )

        return signal

    # ─────────────────────────────────────────────
    # CURRENCY PIPELINE — BREAKOUT & RETEST (30M)
    # ─────────────────────────────────────────────

    def analyse_currency_30m(
        self,
        candles_30m: List[Candle],
    ) -> Optional[TradeSignal]:
        """
        Currency-specific pipeline using the Breakout & Retest model.
        Operates exclusively on 30M candles.

        Usage:
            engine = TraderCopilot("EURUSD")
            signal = engine.analyse_currency_30m(candles_30m)

        Returns a TradeSignal if a confirmed retest is found.
        Returns None but fires a pending alert if setup is valid but retest not yet confirmed.
        """

        # ── Regime gate ───────────────────────────────────────────────────────────
        # Breakout & Retest only works in trending (directional channel) conditions.
        # Ranging / choppy structure produces false breakouts and absurd R:R values.
        # Uses the same determine_bias() check the original HTF pipeline already does.
        swings_30m = self.structure_engine.detect_swings(candles_30m, left=3, right=3)
        bias_30m   = self.structure_engine.determine_bias(swings_30m)
        if bias_30m == BiasType.RANGING:
            if not self.backtest_mode:
                logger.info(
                    "[%s] 30M bias=RANGING — Breakout & Retest blocked "
                    "(pattern requires trending structure)", self.symbol
                )
                return None
            logger.debug(
                "[%s] 30M bias=RANGING — regime gate bypassed (backtest_mode)", self.symbol
            )

        # Run Breakout & Retest detection
        result = self.breakout_retest_engine.analyse(candles_30m)

        if not result or not result.valid:
            return None

        # Retest not yet confirmed — fire pending alert and return None
        if result.retest_index is None:
            self.alert_engine.log_pending(
                symbol=self.symbol,
                pattern=result.pattern_name,
                entry_price=result.entry_price,
                stop_loss=result.stop_loss,
                notes=result.notes,
                direction=result.direction,
            )
            return None

        # Calculate take profit — opposite channel boundary
        if result.direction == Direction.BEARISH:
            take_profit = result.channel_low
        else:
            take_profit = result.channel_high

        signal = TradeSignal(
            symbol=self.symbol,
            direction=result.direction,
            entry_price=result.entry_price,
            stop_loss=result.stop_loss,
            take_profit=take_profit,
            timeframe="30M",
            timestamp=datetime.now(timezone.utc),
            pattern=result.pattern_name,
            confluence_score=self._score_breakout_retest(result),
            fvg_present=result.fvg is not None,
            ob_present=True,
            killzone_active=self._is_killzone_active(),
            notes=result.notes
        )

        return signal

    # ─────────────────────────────────────────────
    # SCORING — BREAKOUT & RETEST
    # ─────────────────────────────────────────────

    def _score_breakout_retest(self, result) -> int:
        """
        Confluence scoring for Breakout & Retest (1–5 scale).

        Score components:
        - Base (channel + wedge + breakout confirmed) : +1
        - Retest confirmed                            : +1
        - FVG present at breakout displacement        : +1
        - Killzone active                             : +1
        - R:R >= 2.0                                  : +1
        """
        score = 1  # Base: channel + wedge + breakout all confirmed

        if result.retest_index is not None:
            score += 1

        if result.fvg is not None:
            score += 1

        if self._is_killzone_active():
            score += 1

        # R:R check against opposite channel boundary
        risk = abs(result.entry_price - result.stop_loss)
        reward = abs(
            (result.channel_low if result.direction == Direction.BEARISH else result.channel_high)
            - result.entry_price
        )
        if risk > 0 and (reward / risk) >= 2.0:
            score += 1

        return min(score, 5)

    # ─────────────────────────────────────────────
    # KILLZONE CHECK
    # ─────────────────────────────────────────────

    def _is_killzone_active(self) -> bool:
        """Check if current UTC time falls within any configured killzone window."""
        now = datetime.now(timezone.utc).time()
        for kz_start, kz_end in self.config.killzone_windows:
            if kz_start <= now <= kz_end:
                return True
        return False

    # ─────────────────────────────────────────────
    # DISPATCH
    # ─────────────────────────────────────────────

    def alert(self, signal: TradeSignal):
        """Dispatch alert for a confirmed signal."""
        self.alert_engine.dispatch(signal)

    def run(self, candles_htf: List[Candle], candles_ltf: List[Candle]):
        """Convenience: analyse and alert in one call (original pipeline)."""
        signal = self.analyse(candles_htf, candles_ltf)
        if signal:
            self.alert(signal)
        return signal

    def run_currency(self, candles_30m: List[Candle]):
        """Convenience: analyse and alert in one call (currency 30M pipeline)."""
        signal = self.analyse_currency_30m(candles_30m)
        if signal:
            self.alert(signal)
        return signal
