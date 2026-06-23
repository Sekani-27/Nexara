"""
Trader Copilot — Multi-Pair Runner
===================================
Scans all configured pairs in a single process on a shared 30-minute heartbeat.
Supports multiple Telegram/Discord webhook recipients.
Usage:
    python -m trader_copilot.run_multi
    python -m trader_copilot.run_multi --pairs EURUSDm GBPUSDm
    python -m trader_copilot.run_multi --webhook URL1 --webhook URL2
    python -m trader_copilot.run_multi --interval 30
"""

import argparse
import time
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("trader_copilot.multi_runner")
# ─────────────────────────────────────────────
# PAIR ROUTING
# ─────────────────────────────────────────────
CURRENCY_PAIRS = {
    "EURUSDm",
    "GBPUSDm",
    "USDJPYm",
    "USDCHFm",
    "AUDUSDm",
    "USDCADm",
    "NZDUSDm",
    "EURGBPm",
    "EURJPYm",
    "EURCHFm",
    "EURAUDm",
    "EURCADm",
    "EURNZDm",
    "GBPJPYm",
    "GBPCHFm",
    "GBPAUDm",
    "GBPCADm",
    "GBPNZDm",
    "AUDJPYm",
    "CADJPYm",
    "CHFJPYm",
    "NZDJPYm",
}
COMMODITY_PAIRS = {"XAUUSDm", "USTEC_x100m"}
ALL_PAIRS = sorted(CURRENCY_PAIRS | COMMODITY_PAIRS)


# ─────────────────────────────────────────────
# DUPLICATE PREVENTION
# ─────────────────────────────────────────────
class AlertTracker:
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

    def tick(self):
        expired = [k for k, ttl in self._active.items() if ttl <= 1]
        for k in expired:
            del self._active[k]
        for k in self._active:
            self._active[k] -= 1


# ─────────────────────────────────────────────
# PAIR SCANNER
# ─────────────────────────────────────────────
def scan_pair(symbol, engine, mt5, config, tracker: AlertTracker) -> bool:
    try:
        if symbol in CURRENCY_PAIRS:
            candles = mt5.get_candles(symbol, "30M", count=300)
            if not candles:
                logger.warning(f"{symbol} — No 30M candles returned")
                return False
            signal = engine.analyse_currency_30m(candles)
        else:
            candles_htf = mt5.get_candles(symbol, config.structure_tf, count=300)
            candles_ltf = mt5.get_candles(symbol, config.entry_tf, count=300)
            if not candles_htf or not candles_ltf:
                logger.warning(f"{symbol} — No candles returned")
                return False
            signal = engine.analyse(candles_htf, candles_ltf)
        if not signal:
            logger.info(f"{symbol:14s} — No setup this cycle")
            return False
        if tracker.is_duplicate(symbol, signal.direction.value, signal.entry_price):
            logger.info(f"{symbol:14s} — Setup already alerted (duplicate suppressed)")
            return False
        engine.alert(signal)
        tracker.register(symbol, signal.direction.value, signal.entry_price)
        logger.info(
            f"{symbol:14s} — SIGNAL {signal.direction.value.upper()} | "
            f"Entry: {signal.entry_price:.5f} | "
            f"Score: {signal.confluence_score}/5 | "
            f"Pattern: {signal.pattern}"
        )
        return True
    except Exception as e:
        logger.error(f"{symbol} — Error during scan: {e}", exc_info=True)
        return False


# ─────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────
def run(pairs: list, interval_minutes: int, webhook_urls: Optional[List[str]] = None):
    from trader_copilot.engine import TraderCopilot
    from trader_copilot.utils.mt5_connector import MT5Connector
    from trader_copilot.config.pairs import PAIR_CONFIGS

    invalid = [p for p in pairs if p not in PAIR_CONFIGS]
    if invalid:
        logger.error(f"Unknown pairs: {invalid}. Supported: {ALL_PAIRS}")
        return
    mt5 = MT5Connector()
    if not mt5.connect():
        logger.error("Could not connect to MT5. Exiting.")
        return
    # Build engines — one per pair, broadcast to all webhook URLs
    engines = {}
    configs = {}
    for symbol in pairs:
        engines[symbol] = TraderCopilot(
            symbol=symbol,
            webhook_urls=webhook_urls or [],
            log_path=f"{symbol}_alerts.jsonl",
        )
        configs[symbol] = PAIR_CONFIGS[symbol]
    tracker = AlertTracker(ttl_cycles=5)
    webhook_display = f"{len(webhook_urls)} recipient(s)" if webhook_urls else "none"
    logger.info(f"{'═' * 55}")
    logger.info("  Trader Copilot — Multi-Pair Runner")
    logger.info(f"  Pairs     : {', '.join(pairs)}")
    logger.info(f"  Interval  : {interval_minutes} minutes")
    logger.info(f"  Webhook   : {webhook_display}")
    logger.info(f"{'═' * 55}\n")
    cycle = 0
    try:
        while True:
            cycle += 1
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            logger.info(f"── Cycle {cycle} | {now} | Scanning {len(pairs)} pairs ──")
            signals_fired = 0
            for symbol in pairs:
                fired = scan_pair(
                    symbol=symbol,
                    engine=engines[symbol],
                    mt5=mt5,
                    config=configs[symbol],
                    tracker=tracker,
                )
                if fired:
                    signals_fired += 1
            logger.info(
                f"── Cycle {cycle} complete | "
                f"{signals_fired} signal(s) fired | "
                f"Next scan in {interval_minutes} minutes ──\n"
            )
            tracker.tick()
            time.sleep(interval_minutes * 60)
    except KeyboardInterrupt:
        logger.info("Multi-pair runner stopped by user.")
    finally:
        mt5.disconnect()
        logger.info("MT5 disconnected.")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trader Copilot — Multi-Pair Runner")
    parser.add_argument("--pairs", nargs="+", default=ALL_PAIRS, choices=ALL_PAIRS)
    parser.add_argument("--interval", default=30, type=int)
    parser.add_argument(
        "--webhook",
        action="append",
        dest="webhooks",
        default=None,
        help="Webhook URL — repeat for multiple recipients",
    )
    args = parser.parse_args()
    run(
        pairs=args.pairs,
        interval_minutes=args.interval,
        webhook_urls=args.webhooks,
    )
