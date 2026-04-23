"""
Trader Copilot — Live Runner
Polls MT5 every N minutes and fires alerts when setups confirm.

Usage:
    # Original pipeline (XAUUSDm / USTEC_x100m)
    python run_live.py --symbol XAUUSDm --interval 15
    python run_live.py --symbol USTEC_x100m --interval 15 --webhook https://...

    # Currency Breakout & Retest pipeline (30M)
    python run_live.py --symbol EURUSDm --interval 30
    python run_live.py --symbol EURAUDm --interval 30 --webhook https://...
"""

import argparse
import time
import logging
from datetime import datetime
from typing import List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("trader_copilot.runner")


# Pairs that use the Breakout & Retest 30M pipeline (Exness demo — `m` suffix)
CURRENCY_PAIRS = {
    "EURUSDm", "GBPUSDm", "USDJPYm", "USDCHFm", "AUDUSDm", "USDCADm", "NZDUSDm",
    "EURGBPm", "EURJPYm", "EURCHFm", "EURAUDm", "EURCADm", "EURNZDm",
    "GBPJPYm", "GBPCHFm", "GBPAUDm", "GBPCADm", "GBPNZDm",
    "AUDJPYm", "CADJPYm", "CHFJPYm", "NZDJPYm",
}

# Pairs that use the original HTF/LTF pipeline (Exness demo — `m` suffix)
COMMODITY_PAIRS = {"XAUUSDm", "USTEC_x100m"}

ALL_PAIRS = sorted(CURRENCY_PAIRS | COMMODITY_PAIRS)


def run(symbol: str, interval_minutes: int, webhook_urls: Optional[List[str]] = None):
    from trader_copilot.engine import TraderCopilot
    from trader_copilot.utils.mt5_connector import MT5Connector
    from trader_copilot.config.pairs import PAIR_CONFIGS

    config = PAIR_CONFIGS[symbol]
    engine = TraderCopilot(
        symbol=symbol,
        webhook_urls=webhook_urls,
        log_path=f"{symbol}_alerts.jsonl"
    )
    mt5 = MT5Connector()

    if not mt5.connect():
        logger.error("Could not connect to MT5. Exiting.")
        return

    is_currency = symbol in CURRENCY_PAIRS

    logger.info(f"Trader Copilot live | {symbol} | "
                f"Pipeline: {'Breakout & Retest (30M)' if is_currency else f'HTF/LTF ({config.structure_tf}/{config.entry_tf})'}")
    logger.info(f"Killzones : {config.killzones}")
    logger.info(f"Polling every {interval_minutes} minutes\n")

    try:
        while True:
            logger.info(f"[{datetime.utcnow().strftime('%H:%M UTC')}] Scanning {symbol}...")

            if is_currency:
                # ── Currency: Breakout & Retest pipeline (30M only) ──────────
                candles_30m = mt5.get_candles(symbol, "30M", count=300)

                if not candles_30m:
                    logger.warning("No 30M candles returned — skipping this cycle")
                else:
                    signal = engine.analyse_currency_30m(candles_30m)
                    if signal:
                        logger.info(
                            f"SIGNAL — {signal.direction.value.upper()} | "
                            f"Entry: {signal.entry_price:.5f} | "
                            f"Score: {signal.confluence_score}/5"
                        )
                        engine.alert(signal)
                    else:
                        logger.info("No confirmed setup this cycle.")

            else:
                # ── Commodities/Indices: original HTF/LTF pipeline ───────────
                candles_htf = mt5.get_candles(symbol, config.structure_tf, count=300)
                candles_ltf = mt5.get_candles(symbol, config.entry_tf, count=300)

                if not candles_htf or not candles_ltf:
                    logger.warning("No candles returned — skipping this cycle")
                else:
                    signal = engine.analyse(candles_htf, candles_ltf)
                    if signal:
                        logger.info(
                            f"SIGNAL — {signal.direction.value.upper()} | "
                            f"Entry: {signal.entry_price:.5f} | "
                            f"Score: {signal.confluence_score}/5"
                        )
                        engine.alert(signal)
                    else:
                        logger.info("No setup confirmed this cycle.")

            time.sleep(interval_minutes * 60)

    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        mt5.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trader Copilot Live Runner")
    parser.add_argument(
        "--symbol",
        default="XAUUSDm",
        choices=ALL_PAIRS,
        help=f"Trading pair to scan. Currency pairs use 30M Breakout & Retest pipeline. "
             f"Supported: {', '.join(ALL_PAIRS)}"
    )
    parser.add_argument(
        "--interval",
        default=30,
        type=int,
        help="Poll interval in minutes (use 30 for currency pairs, 15 for XAUUSDm/USTEC_x100m)"
    )
    parser.add_argument(
        "--webhook",
        action="append",
        dest="webhooks",
        default=None,
        help="Telegram or Discord webhook URL — repeat the flag for multiple recipients"
    )
    args = parser.parse_args()

    run(
        symbol=args.symbol,
        interval_minutes=args.interval,
        webhook_urls=args.webhooks
    )
