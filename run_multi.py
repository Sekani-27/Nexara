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
import json
import logging
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
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
_stderr_handler.setLevel(logging.WARNING)  # WARNING, ERROR, CRITICAL
_stderr_handler.setFormatter(_fmt)

logging.root.setLevel(logging.INFO)
logging.root.addHandler(_stdout_handler)
logging.root.addHandler(_stderr_handler)

logger = logging.getLogger("trader_copilot.multi_runner")


# ─────────────────────────────────────────────
# MARKET-OPEN GATE
# ─────────────────────────────────────────────


def is_forex_market_open() -> bool:
    now = datetime.now(timezone.utc)
    # Forex closed Saturday and Sunday before 21:00 UTC
    if now.weekday() == 5:  # Saturday
        return False
    if now.weekday() == 6 and now.hour < 21:  # Sunday before Sydney open
        return False
    return True


# ─────────────────────────────────────────────
# PAIR ROUTING
# ─────────────────────────────────────────────

CURRENCY_PAIRS = {
    # Original six
    "EURUSD",
    "GBPUSD",
    "EURAUD",
    "EURCAD",
    "CADJPY",
    "GBPCAD",
    # New additions
    "GBPJPY",
    "USDJPY",
    "USDZAR",
    "USDCHF",
    "AUDUSD",
    "NZDUSD",
    "USDCAD",
    "EURJPY",
}
COMMODITY_PAIRS = {"XAUUSD"}  # Gold — 4H/15M, price-unit pip precision
INDEX_PAIRS = {"NAS100"}  # Nasdaq-100 — 4H/15M, point-unit precision

# Explicit scan order — XAUUSD first so it hits TwelveData before the
# rate-limit window fills.  6 pairs active.
ALL_PAIRS = [
    "XAUUSD",  # Gold — scanned first, before rate-limit window fills
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "GBPJPY",
    "CADJPY",
]


# ─────────────────────────────────────────────
# DUPLICATE PREVENTION
# ─────────────────────────────────────────────


class AlertTracker:
    """
    Prevents the same setup firing as an alert on every poll cycle.

    Key: symbol + pattern + direction + candle-open-time (YYYYMMDDHHMM).
    Using the candle's fixed open timestamp instead of entry_price means the key
    is stable across all poll cycles — entry_price on a partial candle changes
    every tick (particularly visible on USTEC where the 15M close shifts by
    several points between polls, defeating a price-based key entirely).

    TTL default: 48 cycles ≈ 24 hours at 30-minute intervals.  This prevents
    the same setup from re-firing within the same trading day while still
    allowing a legitimately new setup on the same pair the next day.
    """

    STALE_HOURS = 2  # signals older than this are dropped before even checking

    def __init__(self, ttl_cycles: int = 48):
        self.ttl_cycles = ttl_cycles
        self._active: Dict[str, int] = {}

    @staticmethod
    def _normalise_ts(ts: datetime) -> datetime:
        """Return a UTC-aware datetime; treat naive datetimes as UTC."""
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts

    def _key(
        self, symbol: str, pattern: str, direction: str, candle_ts: datetime
    ) -> str:
        ts_str = self._normalise_ts(candle_ts).strftime("%Y%m%d%H%M")
        return f"{symbol}:{pattern}:{direction}:{ts_str}"

    def is_stale(self, candle_ts: datetime) -> bool:
        """Return True if the candle that generated the signal is too old to trade."""
        age = datetime.now(timezone.utc) - self._normalise_ts(candle_ts)
        return age > timedelta(hours=self.STALE_HOURS)

    def is_duplicate(
        self, symbol: str, pattern: str, direction: str, candle_ts: datetime
    ) -> bool:
        return self._key(symbol, pattern, direction, candle_ts) in self._active

    def register(self, symbol: str, pattern: str, direction: str, candle_ts: datetime):
        key = self._key(symbol, pattern, direction, candle_ts)
        self._active[key] = self.ttl_cycles
        logger.debug("Alert registered: %s | TTL: %d cycles", key, self.ttl_cycles)

    def tick(self):
        """Age all active keys by one cycle; remove expired ones."""
        expired = [k for k, ttl in self._active.items() if ttl <= 1]
        for k in expired:
            del self._active[k]
            logger.debug("Alert expired: %s", k)
        for k in self._active:
            self._active[k] -= 1


# ─────────────────────────────────────────────
# PAIR SCANNER
# ─────────────────────────────────────────────


def _fetch(
    primary, fallback, symbol: str, timeframe: str, count: int = 300, twelvedata=None
) -> list:
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
    massive=None,  # MassiveConnector (Polygon.io) — second in chain
    twelvedata=None,  # TwelveDataConnector — third in chain
) -> bool:
    """
    Runs one scan cycle for a single pair using its correct pipeline.
    Data chain: MT5 → Massive (Polygon.io) → TwelveData.
    Returns True if a new (non-duplicate) signal was generated.
    """
    try:
        if symbol in CURRENCY_PAIRS:
            candles = _fetch(
                mt5, massive, symbol, "30M", count=300, twelvedata=twelvedata
            )
            if not candles:
                logger.warning(f"{symbol:10s} — No 30M candles from any source")
                return False
            signal = engine.analyse_currency_30m(candles)

        else:
            candles_htf = _fetch(
                mt5,
                massive,
                symbol,
                config.structure_tf,
                count=300,
                twelvedata=twelvedata,
            )
            candles_ltf = _fetch(
                mt5, massive, symbol, config.entry_tf, count=300, twelvedata=twelvedata
            )
            if not candles_htf or not candles_ltf:
                logger.warning(f"{symbol:10s} — No candles from any source")
                return False
            signal = engine.analyse(candles_htf, candles_ltf)

        if not signal:
            logger.info(f"{symbol:10s} — No setup this cycle")
            return False

        # ── Bug 2: Staleness gate ─────────────────────────────────────────
        # signal.timestamp is the candle's open time (fixed), not wall-clock.
        # If the candle that produced this setup is older than STALE_HOURS,
        # the market has moved on — do not send an alert for a dead setup.
        if tracker.is_stale(signal.timestamp):
            age_h = (
                datetime.now(timezone.utc)
                - AlertTracker._normalise_ts(signal.timestamp)
            ).total_seconds() / 3600
            logger.warning(
                f"{symbol:10s} — STALE signal dropped "
                f"(candle {signal.timestamp} is {age_h:.1f}h old, "
                f"limit={AlertTracker.STALE_HOURS}h)"
            )
            return False

        # ── Bug 1: Duplicate gate ─────────────────────────────────────────
        # Key = symbol:pattern:direction:candle_open_time — stable across every
        # poll cycle regardless of how the live close price fluctuates.
        if tracker.is_duplicate(
            symbol, signal.pattern, signal.direction.value, signal.timestamp
        ):
            logger.info(
                f"{symbol:10s} — Duplicate suppressed "
                f"({signal.pattern} {signal.direction.value.upper()} "
                f"candle@{signal.timestamp})"
            )
            return False

        # ── Signal confirmed ──────────────────────────────────────────────
        logger.info(
            f"{symbol:10s} — SIGNAL {signal.direction.value.upper():7s} | "
            f"Entry: {signal.entry_price:.5f} | "
            f"SL: {signal.stop_loss:.5f} | "
            f"Score: {signal.confluence_score}/5 | "
            f"Pattern: {signal.pattern} | "
            f"Candle: {signal.timestamp}"
        )

        # ── Price-distance staleness gate ─────────────────────────────────
        # If the current market price has already moved more than 15 pips
        # past the entry level in the trade direction, the entry opportunity
        # is gone — log STALE and skip the Telegram alert.
        # Pip size: JPY pairs = 0.01, Gold (XAUUSD) = 0.10, others = 0.0001.
        if symbol in COMMODITY_PAIRS:
            pip = 0.10
        elif symbol.endswith("JPY"):
            pip = 0.01
        else:
            pip = 0.0001
        _pip_threshold = 15 * pip

        # Try Massive first (cheaper, no shared rate-limit bucket with candle
        # fetches), fall back to TwelveData, log clearly if both return None.
        current_price = None
        if massive is not None and massive.is_available():
            current_price = massive.get_current_price(symbol)
            if current_price is not None:
                logger.debug(
                    f"{symbol:10s} — price-gate: current price from Massive: {current_price:.5f}"
                )
        if (
            current_price is None
            and twelvedata is not None
            and twelvedata.is_available()
        ):
            current_price = twelvedata.get_current_price(symbol)
            if current_price is not None:
                logger.debug(
                    f"{symbol:10s} — price-gate: current price from TwelveData: {current_price:.5f}"
                )

        if current_price is None:
            logger.warning(
                f"{symbol:10s} — price-gate: could not fetch current price from any source "
                f"(Massive={'available' if massive and massive.is_available() else 'unavailable'}, "
                f"TwelveData={'available' if twelvedata and twelvedata.is_available() else 'unavailable'}) "
                f"— staleness check SKIPPED, alert will fire"
            )
        else:
            direction_up = signal.direction.value.lower() == "buy"
            # For a BUY, price above entry means the level was already triggered.
            # For a SELL, price below entry means the level was already triggered.
            overshoot = (
                (current_price - signal.entry_price)
                if direction_up
                else (signal.entry_price - current_price)
            )
            overshoot_pips = overshoot / pip
            logger.info(
                f"{symbol:10s} — price-gate: entry={signal.entry_price:.5f} "
                f"current={current_price:.5f} direction={signal.direction.value.upper()} "
                f"overshoot={overshoot_pips:.1f} pips (threshold=15) "
                f"STALE={'True' if overshoot_pips > 15 else 'False'}"
            )
            if overshoot_pips > 15:
                logger.warning(
                    f"{symbol:10s} — STALE alert suppressed | "
                    f"entry={signal.entry_price:.5f} current={current_price:.5f} "
                    f"overshoot={overshoot_pips:.1f} pips | STALE=True"
                )
                tracker.register(
                    symbol, signal.pattern, signal.direction.value, signal.timestamp
                )
                return False

        if not dry_run:
            engine.alert(signal)
        else:
            logger.info(f"{symbol:10s} — [DRY-RUN] Alert suppressed")

        tracker.register(
            symbol, signal.pattern, signal.direction.value, signal.timestamp
        )
        return True

    except Exception as exc:
        logger.error(f"{symbol:10s} — Scan error: {exc}", exc_info=True)
        return False


# ─────────────────────────────────────────────
# SESSION DEBRIEF — fires daily at 17:00 UTC
# ─────────────────────────────────────────────

_DEBRIEF_HOUR = 17  # UTC hour to fire

_DEBRIEF_DB_PATH = os.environ.get(
    "JOURNAL_DB_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "trader_copilot_journal.db"
    ),
)

_TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def _query_signals_today(today_str: str) -> dict:
    """
    Query trader_copilot_journal.db for today's signal counts.
    Returns dict with fired/taken/ignored; falls back to zeros on any error.
    """
    result = {"fired": 0, "taken": 0, "ignored": 0}
    try:
        conn = sqlite3.connect(_DEBRIEF_DB_PATH)
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*)                          AS fired,
                    SUM(CASE WHEN taken = 1 THEN 1 ELSE 0 END) AS taken
                FROM trades
                WHERE DATE(created_at) = ?
                """,
                (today_str,),
            ).fetchone()
            if row:
                fired = row[0] or 0
                taken = row[1] or 0
                result = {"fired": fired, "taken": taken, "ignored": fired - taken}
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Debrief: could not query signals from DB — %s", exc)
    return result


def _get_risk_guard_state() -> dict:
    """
    Read live risk guard state from health_server._state.
    Returns safe defaults if health_server isn't imported yet.
    """
    try:
        import health_server  # imported after start_health_server() runs

        rg = health_server._state.get("risk_guard", {})
        return {
            "open_positions": rg.get("open_positions", []),
            "open_trade_count": rg.get("open_trade_count", 0),
            "total_exposure": rg.get("total_exposure", 0.0),
            "realized_pnl_today": rg.get("realized_pnl_today", 0.0),
        }
    except Exception as exc:
        logger.warning("Debrief: could not read risk_guard state — %s", exc)
        return {
            "open_positions": [],
            "open_trade_count": 0,
            "total_exposure": 0.0,
            "realized_pnl_today": 0.0,
        }


def _build_debrief_message(today_str: str) -> str:
    signals = _query_signals_today(today_str)
    rg = _get_risk_guard_state()

    pnl = rg["realized_pnl_today"]
    pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
    open_count = rg["open_trade_count"]
    exposure = rg["total_exposure"]

    # Risk guard status line
    if open_count == 0:
        rg_status = "CLEAR"
    else:
        syms = ", ".join(p["symbol"] for p in rg["open_positions"])
        rg_status = f"OPEN — {syms}"

    return (
        "╔══════════════════════════════════════╗\n"
        "  GENUVIA EDGE — SESSION DEBRIEF 📊\n"
        "╚══════════════════════════════════════╝\n"
        "\n"
        f"  Date        : {today_str}\n"
        f"  Session     : London/NY Close (17:00 UTC)\n"
        "\n"
        "  SIGNALS\n"
        f"  Fired Today : {signals['fired']}\n"
        f"  Taken       : {signals['taken']}\n"
        f"  Ignored     : {signals['ignored']}\n"
        "\n"
        "  POSITIONS\n"
        f"  Open Trades : {open_count}\n"
        f"  Exposure    : {exposure:.2f} lots\n"
        "\n"
        "  P&L\n"
        f"  Realized    : {pnl_str}\n"
        "\n"
        "  RISK GUARD\n"
        f"  Status      : {rg_status}\n"
        "══════════════════════════════════════════"
    )


def _send_debrief_telegram(text: str) -> None:
    """POST debrief message directly to the Telegram Bot API via urllib."""
    if not _TELEGRAM_TOKEN or not _TELEGRAM_CHAT_ID:
        logger.warning(
            "Debrief: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping send."
        )
        return
    url = f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": _TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Debrief sent — HTTP %s", resp.status)
    except Exception as exc:
        logger.error("Debrief: Telegram send failed — %s", exc)


def _debrief_loop() -> None:
    """
    Daemon loop: wakes every 60 s, fires debrief once at _DEBRIEF_HOUR UTC.
    A date tracker prevents double-firing if the process restarts mid-minute.
    """
    last_fired: Optional[str] = None
    logger.info(
        "Debrief thread started — will fire daily at %02d:00 UTC.", _DEBRIEF_HOUR
    )

    while True:
        time.sleep(60)
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")

        if now.hour != _DEBRIEF_HOUR or now.minute != 0:
            continue
        if last_fired == today_str:
            continue

        last_fired = today_str
        logger.info("Debrief: building session debrief for %s…", today_str)
        try:
            message = _build_debrief_message(today_str)
            logger.info("Debrief message:\n%s", message)
            _send_debrief_telegram(message)
        except Exception as exc:
            logger.error("Debrief: unexpected error — %s", exc, exc_info=True)


def start_debrief_thread() -> threading.Thread:
    """Launch the daily debrief loop as a daemon thread."""
    t = threading.Thread(target=_debrief_loop, name="session-debrief", daemon=True)
    t.start()
    return t


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
    from health_server import start_health_server, update_health

    # Start health endpoint in background (Railway healthcheck pings /health)
    start_health_server(port=8080)

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
    configs = {}
    for symbol in pairs:
        engines[symbol] = TraderCopilot(
            symbol=symbol,
            webhook_urls=[webhook_url] if webhook_url else None,
            log_path=f"{symbol}_alerts.jsonl",
        )
        configs[symbol] = PAIR_CONFIGS[symbol]

    tracker = AlertTracker(ttl_cycles=5)

    logger.info(f"{'═' * 60}")
    logger.info("  Trader Copilot — Multi-Pair Runner")
    logger.info(f"  Pairs     : {', '.join(pairs)}")
    logger.info(f"  Interval  : {interval_minutes} min")
    logger.info(f"  Webhook   : {'configured' if webhook_url else 'none'}")
    logger.info(f"  Dry-run   : {'YES — alerts logged only' if dry_run else 'no'}")
    logger.info(f"{'═' * 60}\n")

    # Start Telegram polling in a background daemon thread so inbound messages
    # (keyword queries and Groq fallback) are handled while the scan loop runs.
    from trader_copilot.telegram_bot import start_polling_thread

    start_polling_thread()

    # Daily session debrief at 17:00 UTC.
    start_debrief_thread()

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

            if not is_forex_market_open():
                logger.info(
                    f"── Cycle {cycle} | Market closed — skipping scan. "
                    f"Next check in {interval_minutes} min ──\n"
                )
                tracker.tick()
                time.sleep(interval_minutes * 60)
                continue

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
                time.sleep(2)  # rate-limit buffer between pair scans

            update_health(pairs=len(pairs), cycle=cycle)
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
        epilog=f"Supported pairs: {', '.join(ALL_PAIRS)}",
    )
    parser.add_argument(
        "--pairs",
        nargs="+",
        default=ALL_PAIRS,
        choices=ALL_PAIRS,
        metavar="SYMBOL",
        help="Pairs to scan (default: all). "
        "Currency pairs use the 30M Breakout & Retest engine; "
        "XAUUSD/NAS100 use the HTF/LTF structure engine.",
    )
    parser.add_argument(
        "--interval",
        default=5,
        type=int,
        metavar="MINUTES",
        help="Poll interval in minutes (default: 5)",
    )
    parser.add_argument(
        "--webhook",
        default=None,
        metavar="URL",
        help="Telegram or Discord webhook URL for alert delivery",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log signals without dispatching alerts (useful for forward-testing)",
    )
    args = parser.parse_args()

    run(
        pairs=args.pairs,
        interval_minutes=args.interval,
        webhook_url=args.webhook,
        dry_run=args.dry_run,
    )
