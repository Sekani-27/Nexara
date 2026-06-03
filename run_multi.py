"""
Trader Copilot — Multi-Pair Runner  (project-root entry point)
===============================================================
Scans all configured pairs in a single process on a shared heartbeat.
Each pair is routed to its correct pipeline automatically:
  • Currency pairs  → Breakout & Retest 30M engine
  • Commodity/Index → HTF/LTF structure engine

Duplicate-alert suppression built in: the same setup won't fire more than
once per TTL window (~2.5 hours at default 30-minute intervals).

Usage (run from the project root — trader_copilot_phase3_complete/):
    # Scan all configured pairs
    python run_multi.py

    # Specific pairs only
    python run_multi.py --pairs EURUSD EURAUD CADJPY

    # With Telegram / Discord webhook
    python run_multi.py --webhook https://your-webhook-url

    # Custom poll interval
    python run_multi.py --interval 15

    # Dry-run (log only, no alerts dispatched)
    python run_multi.py --dry-run
"""

import sys
import os

# ── Path bootstrap ────────────────────────────────────────────────────────────
# Ensures `import trader_copilot` works regardless of where Python is invoked.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import time
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("trader_copilot.multi_runner")


# ─────────────────────────────────────────────
# PAIR ROUTING
# ─────────────────────────────────────────────

CURRENCY_PAIRS  = {"EURUSDm", "GBPUSDm", "EURAUDm", "EURCADm", "AUDCADm", "CADJPYm", "GBPCADm"}
COMMODITY_PAIRS = {"XAUUSDm", "USTEC_x100m"}
ALL_PAIRS       = sorted(CURRENCY_PAIRS | COMMODITY_PAIRS)


# ─────────────────────────────────────────────
# DUPLICATE PREVENTION
# ─────────────────────────────────────────────

class AlertTracker:
    """
    Prevents the same setup firing as an alert on every poll cycle.

    A setup is keyed by: symbol + direction + entry_price (rounded to 4dp).
    Each registered key expires after `ttl_cycles` poll cycles.
    Default ttl_cycles=5 at 30-minute intervals ≈ 2.5-hour suppression window.
    """

    def __init__(self, ttl_cycles: int = 5):
        self.ttl_cycles = ttl_cycles
        self._active: Dict[str, int] = {}

    def _key(self, symbol: str, direction: str, entry_price: float) -> str:
        return f"{symbol}:{direction}:{round(entry_price, 4)}"

    def is_duplicate(self, symbol: str, direction: str, entry_price: float) -> bool:
        return self._key(symbol, direction, entry_price) in self._active

    def register(self, symbol: str, direction: str, entry_price: float):
        key = self._key(symbol, direction, entry_price)
        self._active[key] = self.ttl_cycles
        logger.debug(f"Alert registered: {key} | TTL: {self.ttl_cycles} cycles")

    def tick(self):
        """Age all active keys by one cycle; remove expired ones."""
        expired = [k for k, ttl in self._active.items() if ttl <= 1]
        for k in expired:
            del self._active[k]
            logger.debug(f"Alert expired: {k}")
        for k in self._active:
            self._active[k] -= 1


# ─────────────────────────────────────────────
# PAIR SCANNER
# ─────────────────────────────────────────────

def scan_pair(
    symbol: str,
    engine,
    mt5,
    config,
    tracker: AlertTracker,
    dry_run: bool = False,
) -> bool:
    """
    Runs one scan cycle for a single pair using its correct pipeline.
    Returns True if a new (non-duplicate) signal was generated.
    """
    try:
        if symbol in CURRENCY_PAIRS:
            candles = mt5.get_candles(symbol, "30M", count=300)
            if not candles:
                logger.warning(f"{symbol:10s} — No 30M candles returned")
                return False
            signal = engine.analyse_currency_30m(candles)

        else:
            candles_htf = mt5.get_candles(symbol, config.structure_tf, count=300)
            candles_ltf = mt5.get_candles(symbol, config.entry_tf,     count=300)
            if not candles_htf or not candles_ltf:
                logger.warning(f"{symbol:10s} — No candles returned")
                return False
            signal = engine.analyse(candles_htf, candles_ltf)

        if not signal:
            logger.info(f"{symbol:10s} — No setup this cycle")
            return False

        if tracker.is_duplicate(symbol, signal.direction.value, signal.entry_price):
            logger.info(f"{symbol:10s} — Duplicate suppressed "
                        f"({signal.direction.value.upper()} @ {signal.entry_price:.5f})")
            return False

        # ── Signal confirmed ──────────────────────────────────────────────
        logger.info(
            f"{symbol:10s} — SIGNAL {signal.direction.value.upper():7s} | "
            f"Entry: {signal.entry_price:.5f} | "
            f"SL: {signal.stop_loss:.5f} | "
            f"Score: {signal.confluence_score}/5 | "
            f"Pattern: {getattr(signal, 'pattern', getattr(signal, 'pattern_name', '—'))}"
        )

        if not dry_run:
            engine.alert(signal)
        else:
            logger.info(f"{symbol:10s} — [DRY-RUN] Alert suppressed")

        tracker.register(symbol, signal.direction.value, signal.entry_price)
        return True

    except Exception as exc:
        logger.error(f"{symbol:10s} — Scan error: {exc}", exc_info=True)
        return False


# ─────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────

def run(
    pairs: List[str],
    interval_minutes: int,
    webhook_url: Optional[str] = None,
    dry_run: bool = False,
):
    from trader_copilot.engine import TraderCopilot
    from trader_copilot.utils.mt5_connector import MT5Connector
    from trader_copilot.config.pairs import PAIR_CONFIGS

    # Validate requested pairs
    invalid = [p for p in pairs if p not in PAIR_CONFIGS]
    if invalid:
        logger.error(f"Unknown pairs: {invalid}. Supported: {', '.join(ALL_PAIRS)}")
        return

    # Connect to MetaTrader 5
    # On Linux / Railway (no MT5 terminal), connect() returns False but the
    # polling loop keeps running — get_candles() returns [] gracefully so
    # scan_pair() logs "No candles returned" and moves on without crashing.
    mt5 = MT5Connector()
    if not mt5.connect():
        logger.warning(
            "MT5 connection unavailable — running without live data feed. "
            "Signals will not fire until an MT5 terminal is reachable. "
            "Continuing so the process stays alive on Railway/cloud."
        )

    # Build one engine instance per pair
    engines: Dict[str, TraderCopilot] = {}
    configs  = {}
    for symbol in pairs:
        engines[symbol] = TraderCopilot(
            symbol=symbol,
            webhook_urls=[webhook_url] if webhook_url else None,
            log_path=f"{symbol}_alerts.jsonl"
        )
        configs[symbol] = PAIR_CONFIGS[symbol]

    tracker = AlertTracker(ttl_cycles=5)

    logger.info(f"{'═' * 60}")
    logger.info(f"  Trader Copilot — Multi-Pair Runner")
    logger.info(f"  Pairs     : {', '.join(pairs)}")
    logger.info(f"  Interval  : {interval_minutes} min")
    logger.info(f"  Webhook   : {'configured' if webhook_url else 'none'}")
    logger.info(f"  Dry-run   : {'YES — alerts logged only' if dry_run else 'no'}")
    logger.info(f"{'═' * 60}\n")

    cycle = 0

    try:
        while True:
            cycle += 1
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            logger.info(f"── Cycle {cycle} | {now} | Scanning {len(pairs)} pair(s) ──")

            signals_fired = 0
            for symbol in pairs:
                if scan_pair(
                    symbol=symbol,
                    engine=engines[symbol],
                    mt5=mt5,
                    config=configs[symbol],
                    tracker=tracker,
                    dry_run=dry_run,
                ):
                    signals_fired += 1

            logger.info(
                f"── Cycle {cycle} done | {signals_fired} signal(s) fired | "
                f"Next scan in {interval_minutes} min ──\n"
            )

            tracker.tick()
            time.sleep(interval_minutes * 60)

    except KeyboardInterrupt:
        logger.info("Multi-pair runner stopped by user.")
    finally:
        mt5.disconnect()
        logger.info("MT5 disconnected.")


# ─────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Trader Copilot — Multi-Pair Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Supported pairs: {', '.join(ALL_PAIRS)}"
    )
    parser.add_argument(
        "--pairs",
        nargs="+",
        default=ALL_PAIRS,
        choices=ALL_PAIRS,
        metavar="SYMBOL",
        help="Pairs to scan (default: all). "
             f"Currency pairs use the 30M Breakout & Retest engine; "
             f"XAUUSD/NAS100 use the HTF/LTF structure engine."
    )
    parser.add_argument(
        "--interval",
        default=30,
        type=int,
        metavar="MINUTES",
        help="Poll interval in minutes (default: 30)"
    )
    parser.add_argument(
        "--webhook",
        default=None,
        metavar="URL",
        help="Telegram or Discord webhook URL for alert delivery"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log signals without dispatching alerts (useful for forward-testing)"
    )
    args = parser.parse_args()

    run(
        pairs=args.pairs,
        interval_minutes=args.interval,
        webhook_url=args.webhook,
        dry_run=args.dry_run,
    )
