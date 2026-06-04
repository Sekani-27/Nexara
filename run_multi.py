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
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

# ── Logging: INFO/DEBUG → stdout, WARNING+ → stderr ──────────────────────────
# Railway (and most log aggregators) treat anything written to stderr as an
# error and highlights it red. Normal scan messages are INFO — route them to
# stdout so only genuine warnings/errors appear as errors in the Railway UI.
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setLevel(logging.DEBUG)
_stdout_handler.addFilter(lambda r: r.levelno < logging.WARNING)  # INFO + DEBUG only
_stdout_handler.setFormatter(_fmt)

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.WARNING)   # WARNING, ERROR, CRITICAL
_stderr_handler.setFormatter(_fmt)

logging.root.setLevel(logging.INFO)
logging.root.addHandler(_stdout_handler)
logging.root.addHandler(_stderr_handler)

logger = logging.getLogger("trader_copilot.multi_runner")


# ─────────────────────────────────────────────
# PAIR ROUTING
# ─────────────────────────────────────────────

CURRENCY_PAIRS  = {
    # Original six
    "EURUSDm", "GBPUSDm", "EURAUDm", "EURCADm", "CADJPYm", "GBPCADm",
    # New additions
    "GBPJPYm", "USDJPYm", "USDZARm", "USDCHFm",
    "AUDUSDm", "NZDUSDm", "USDCADm", "EURJPYm",
}
COMMODITY_PAIRS = {"XAUUSDm"}           # Gold — 4H/15M, price-unit pip precision
INDEX_PAIRS     = {"USTEC_x100m"}       # Nasdaq-100 — 4H/15M, point-unit precision

# Explicit scan order — commodity/index pairs placed early so they hit
# TwelveData before the rate-limit window fills.  16 pairs total.
ALL_PAIRS = [
    "EURUSDm",
    "XAUUSDm",      # Gold — scanned second, before rate-limit window fills
    "USTEC_x100m",  # Nasdaq — scanned third
    "GBPUSDm",
    "USDJPYm",
    "GBPJPYm",
    "EURJPYm",
    "USDCHFm",
    "AUDUSDm",
    "NZDUSDm",
    "USDCADm",
    "USDZARm",
    "EURAUDm",
    "EURCADm",
    "CADJPYm",
    "GBPCADm",
]


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

def _fetch(primary, fallback, symbol: str, timeframe: str,
           count: int = 300, twelvedata=None) -> list:
    """
    Three-tier data chain: MT5 → Massive (Polygon.io) → TwelveData.
    Returns the first non-empty result and logs which source delivered it.
    """
    candles = primary.get_candles(symbol, timeframe, count=count)
    if candles:
        return candles

    if fallback is not None and fallback.is_available():
        logger.info("%s %s — MT5 empty, trying Massive (Polygon.io)", symbol, timeframe)
        candles = fallback.get_candles(symbol, timeframe, count=count)
        if candles:
            return candles

    if twelvedata is not None and twelvedata.is_available():
        logger.info("%s %s — Massive empty, trying TwelveData", symbol, timeframe)
        candles = twelvedata.get_candles(symbol, timeframe, count=count)

    return candles


def scan_pair(
    symbol: str,
    engine,
    mt5,
    config,
    tracker: AlertTracker,
    dry_run: bool = False,
    massive=None,       # MassiveConnector (Polygon.io) — second in chain
    twelvedata=None,    # TwelveDataConnector — third in chain
) -> bool:
    """
    Runs one scan cycle for a single pair using its correct pipeline.
    Data chain: MT5 → Massive (Polygon.io) → TwelveData.
    Returns True if a new (non-duplicate) signal was generated.
    """
    try:
        if symbol in CURRENCY_PAIRS:
            candles = _fetch(mt5, massive, symbol, "30M",
                             count=300, twelvedata=twelvedata)
            if not candles:
                logger.warning(f"{symbol:10s} — No 30M candles from any source")
                return False
            signal = engine.analyse_currency_30m(candles)

        else:
            candles_htf = _fetch(mt5, massive, symbol, config.structure_tf,
                                 count=300, twelvedata=twelvedata)
            candles_ltf = _fetch(mt5, massive, symbol, config.entry_tf,
                                 count=300, twelvedata=twelvedata)
            if not candles_htf or not candles_ltf:
                logger.warning(f"{symbol:10s} — No candles from any source")
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
    from trader_copilot.utils.massive_connector import MassiveConnector
    from trader_copilot.utils.twelvedata_connector import TwelveDataConnector
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
            "MT5 connection unavailable — will use Massive (Polygon.io) fallback "
            "if MASSIVE_API_KEY is configured."
        )

    # Massive (Polygon.io) — second in chain
    massive = MassiveConnector()
    if massive.is_available():
        logger.info("Massive connector ready — Polygon.io fallback active.")
    else:
        logger.info("MASSIVE_API_KEY not set — Massive fallback disabled.")

    # TwelveData — third in chain
    twelvedata = TwelveDataConnector()
    if twelvedata.is_available():
        logger.info("TwelveData connector ready — third-tier fallback active.")
    else:
        logger.info("TWELVEDATA_API_KEY not set — TwelveData fallback disabled.")

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

    # Cold-start pause — give TwelveData rate-limit buckets time to settle
    # before Cycle 1 fires.  Without this, a fresh container restart hammers
    # both keys simultaneously from a standing start and hits the 429 window.
    logger.info("Cold-start delay: 3s before first scan cycle…")
    time.sleep(3)

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
                    massive=massive,
                    twelvedata=twelvedata,
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
