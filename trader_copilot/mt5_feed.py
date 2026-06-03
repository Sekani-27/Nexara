#!/usr/bin/env python3
"""
mt5_feed.py  —  Trader Copilot Live MT5 Feed
=============================================
Polls MT5 every 30 seconds for newly CLOSED candles across all watched symbols,
routes them through the correct signal-engine pipeline, and enriches confirmed
alerts with the top-3 most similar historical setups from Qdrant trade memory.

Symbols : EURUSD GBPUSD USDCAD USDJPY AUDNZD CADJPY AUDCAD GBPCAD
          EURNZD EURAUD XAUUSD US30 US100 US500 GER40

Pipeline routing (matches existing architecture in engine.py / run_multi.py):
  • Currency / Index pairs → analyse_currency_30m(candles_30m)   [30M only]
  • XAUUSD               → analyse(candles_4H, candles_15M)      [4H + 15M]

Usage:
    python mt5_feed.py
    python mt5_feed.py --suffix ""          # plain symbol names (non-Exness brokers)
    python mt5_feed.py --suffix m           # Exness demo (default)
    python mt5_feed.py --no-memory          # skip Qdrant enrichment
    python mt5_feed.py --login 12345 --password secret --server Exness-MT5Real
    python mt5_feed.py --symbols EURUSD GBPUSD XAUUSD  # watch a subset only
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

# ── Telegram alert layer (silent no-op when .env creds are missing) ───────────
try:
    from telegram_bot import send_alert as _tg_send_alert
    from telegram_bot import send_startup_message as _tg_send_startup
    _TG_AVAILABLE = True
except ImportError:
    _TG_AVAILABLE = False

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("trader_copilot.mt5_feed")

# ── Constants ─────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS  = 30    # How often to check for new closed candles
CANDLE_COUNT           = 300   # Candles to fetch per request
MAX_RECONNECT_ATTEMPTS = 5     # MT5 reconnect attempts before giving up
RECONNECT_BASE_DELAY   = 10    # Seconds — exponential backoff base
MEMORY_TOP_K           = 3     # Qdrant similar setups to attach to each alert

# Timeframe durations (minutes) — used for closed-candle detection
TF_MINUTES: Dict[str, int] = {
    "5M": 5, "15M": 15, "30M": 30, "1H": 60, "4H": 240, "D": 1440,
}

# ─────────────────────────────────────────────────────────────────────────────
# SYMBOL CATALOG
# ─────────────────────────────────────────────────────────────────────────────

# Clean user-facing symbol names requested for this feed
WATCH_SYMBOLS: List[str] = [
    "EURUSD", "GBPUSD", "USDCAD", "USDJPY", "NZDUSD",
    "AUDNZD", "CADJPY", "AUDCAD", "GBPCAD",
    "EURNZD", "EURAUD",
    "XAUUSD",
    "US30", "US100", "US500", "GER40",
]

# Pipeline type per clean symbol.
# "currency_30m" → engine.analyse_currency_30m(candles_30m)
# "htf_ltf"      → engine.analyse(candles_htf, candles_ltf)
PIPELINE: Dict[str, str] = {
    "EURUSD": "currency_30m",
    "GBPUSD": "currency_30m",
    "USDCAD": "currency_30m",
    "USDJPY": "currency_30m",
    "NZDUSD": "currency_30m",
    "AUDNZD": "currency_30m",
    "CADJPY": "currency_30m",
    "AUDCAD": "currency_30m",
    "GBPCAD": "currency_30m",
    "EURNZD": "currency_30m",
    "EURAUD": "currency_30m",
    "XAUUSD": "htf_ltf",      # 4H structure + 15M entry (per PairConfig)
    "US30":   "currency_30m",
    "US100":  "currency_30m",
    "US500":  "currency_30m",
    "GER40":  "currency_30m",
}

# Maps clean name → existing PAIR_CONFIGS key (broker-suffixed)
# Only entries that already exist in config/pairs.py.
# Missing ones are injected at runtime (see inject_missing_configs).
EXISTING_CONFIG_MAP: Dict[str, str] = {
    "EURUSD": "EURUSDm",
    "GBPUSD": "GBPUSDm",
    "USDCAD": "USDCADm",
    "USDJPY": "USDJPYm",
    "NZDUSD": "NZDUSDm",
    "CADJPY": "CADJPYm",
    "GBPCAD": "GBPCADm",
    "EURNZD": "EURNZDm",
    "EURAUD": "EURAUDm",
    "XAUUSD": "XAUUSDm",
    # AUDNZD, AUDCAD, US30, US100, US500, GER40 → injected below
}

# ─────────────────────────────────────────────────────────────────────────────
# INJECT MISSING PAIR CONFIGS
# ─────────────────────────────────────────────────────────────────────────────

def inject_missing_configs(suffix: str) -> None:
    """
    Adds PairConfig entries for symbols absent from config/pairs.py into
    the live PAIR_CONFIGS dict.  Called once at startup before engines are built.

    The engine's __init__ validates against PAIR_CONFIGS, so all symbols must
    be present before TraderCopilot is instantiated.
    """
    from trader_copilot.config.pairs import PAIR_CONFIGS, PairConfig

    missing: Dict[str, dict] = {
        # AUD/NZD — Pacific cross, similar tolerances to EURNZD
        f"AUDNZD{suffix}": dict(
            symbol=f"AUDNZD{suffix}",
            structure_tf="30M", entry_tf="5M",
            killzones=["london", "sydney"],
            sweep_wick_pips=3.0, fvg_min_pips=2.0,
            peak_equality_pips=5.0, pip_size=0.0001,
        ),
        # AUD/CAD — similar to GBPCAD
        f"AUDCAD{suffix}": dict(
            symbol=f"AUDCAD{suffix}",
            structure_tf="30M", entry_tf="5M",
            killzones=["london", "sydney"],
            sweep_wick_pips=3.0, fvg_min_pips=2.0,
            peak_equality_pips=5.0, pip_size=0.0001,
        ),
        # US30 (Dow Jones) — index, quoted in dollars
        f"US30{suffix}": dict(
            symbol=f"US30{suffix}",
            structure_tf="30M", entry_tf="5M",
            killzones=["new_york", "new_york_open"],
            sweep_wick_pips=15.0, fvg_min_pips=8.0,
            peak_equality_pips=20.0, pip_size=1.0,
        ),
        # US100 (Nasdaq 100) — index
        f"US100{suffix}": dict(
            symbol=f"US100{suffix}",
            structure_tf="30M", entry_tf="5M",
            killzones=["new_york", "new_york_open"],
            sweep_wick_pips=20.0, fvg_min_pips=10.0,
            peak_equality_pips=30.0, pip_size=1.0,
        ),
        # US500 (S&P 500) — index
        f"US500{suffix}": dict(
            symbol=f"US500{suffix}",
            structure_tf="30M", entry_tf="5M",
            killzones=["new_york", "new_york_open"],
            sweep_wick_pips=5.0, fvg_min_pips=3.0,
            peak_equality_pips=8.0, pip_size=0.1,
        ),
        # GER40 (DAX) — European index
        f"GER40{suffix}": dict(
            symbol=f"GER40{suffix}",
            structure_tf="30M", entry_tf="5M",
            killzones=["london", "london_open"],
            sweep_wick_pips=15.0, fvg_min_pips=8.0,
            peak_equality_pips=20.0, pip_size=1.0,
        ),
    }

    for key, kwargs in missing.items():
        if key not in PAIR_CONFIGS:
            PAIR_CONFIGS[key] = PairConfig(**kwargs)
            logger.debug(f"[config] Injected PairConfig for {key}")


# ─────────────────────────────────────────────────────────────────────────────
# CLOSED CANDLE DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def current_candle_open_time(tf: str, now: Optional[datetime] = None) -> datetime:
    """
    Returns the UTC open time of the candle currently forming on `tf`.
    e.g. if now=08:47 UTC and tf="30M", returns 08:30 UTC.
    """
    now = now or datetime.now(timezone.utc)
    tf_secs = TF_MINUTES[tf] * 60
    epoch   = int(now.timestamp())
    period_start = (epoch // tf_secs) * tf_secs
    return datetime.fromtimestamp(period_start, tz=timezone.utc)


def last_closed_candle_open_time(tf: str, now: Optional[datetime] = None) -> datetime:
    """
    Returns the UTC open time of the most recently FULLY CLOSED candle on `tf`.
    e.g. if now=08:47 and tf="30M", the last closed candle opened at 08:00.
    """
    current_open = current_candle_open_time(tf, now)
    return current_open - timedelta(minutes=TF_MINUTES[tf])


class ClosedCandleTracker:
    """
    Tracks the timestamp of the last candle we passed to the engine,
    per (symbol, timeframe).  Prevents the same candle being processed twice
    during multiple 30-second poll cycles.
    """

    def __init__(self):
        # (symbol, tf) → datetime (UTC) of last processed candle open
        self._last: Dict[Tuple[str, str], datetime] = {}

    def is_new(self, symbol: str, tf: str, candle_open_time: datetime) -> bool:
        key = (symbol, tf)
        last = self._last.get(key)
        return last is None or candle_open_time > last

    def mark(self, symbol: str, tf: str, candle_open_time: datetime) -> None:
        self._last[(symbol, tf)] = candle_open_time


# ─────────────────────────────────────────────────────────────────────────────
# DUPLICATE ALERT SUPPRESSION  (same pattern as run_multi.py)
# ─────────────────────────────────────────────────────────────────────────────

class AlertTracker:
    """Suppresses re-alerting the same entry price for TTL cycles."""

    def __init__(self, ttl_cycles: int = 8):
        self.ttl_cycles = ttl_cycles
        self._active: Dict[str, int] = {}

    def _key(self, symbol: str, direction: str, entry_price: float) -> str:
        return f"{symbol}:{direction}:{round(entry_price, 4)}"

    def is_duplicate(self, symbol: str, direction: str, entry_price: float) -> bool:
        return self._key(symbol, direction, entry_price) in self._active

    def register(self, symbol: str, direction: str, entry_price: float) -> None:
        self._active[self._key(symbol, direction, entry_price)] = self.ttl_cycles

    def tick(self) -> None:
        expired = [k for k, ttl in self._active.items() if ttl <= 1]
        for k in expired:
            del self._active[k]
        for k in self._active:
            self._active[k] -= 1


# ─────────────────────────────────────────────────────────────────────────────
# MT5 RECONNECT LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def try_connect(
    connector,
    max_attempts: int = MAX_RECONNECT_ATTEMPTS,
    base_delay: float = RECONNECT_BASE_DELAY,
) -> bool:
    """
    Attempt to connect (or reconnect) to MT5 with exponential backoff.
    Returns True if connected, False if all attempts exhausted.
    """
    for attempt in range(1, max_attempts + 1):
        logger.info(f"[MT5] Connection attempt {attempt}/{max_attempts} …")
        if connector.connect():
            logger.info("[MT5] Connected successfully.")
            return True
        delay = base_delay * (2 ** (attempt - 1))   # 10s, 20s, 40s, 80s, 160s
        logger.warning(f"[MT5] Connection failed. Retrying in {delay:.0f}s …")
        time.sleep(delay)
    logger.error("[MT5] All reconnect attempts exhausted. Feed stopping.")
    return False


def ensure_connected(connector) -> bool:
    """
    Check if MT5 is still alive; attempt to reconnect if not.
    Uses the connector's internal _connected flag and a lightweight ping.
    """
    try:
        import MetaTrader5 as mt5
        info = mt5.terminal_info()
        if info is not None:
            return True
    except Exception:
        pass

    logger.warning("[MT5] Connection lost — attempting reconnect …")
    connector._connected = False
    return try_connect(connector)


# ─────────────────────────────────────────────────────────────────────────────
# TRADE MEMORY ENRICHMENT
# ─────────────────────────────────────────────────────────────────────────────

def _active_session(now_utc: datetime) -> str:
    """
    Returns the name of the trading session active at *now_utc*.
    Mirrors the killzone windows in config/pairs.py.
    """
    h, m = now_utc.hour, now_utc.minute
    t = h * 60 + m   # minutes since midnight UTC
    if   7 * 60 <= t < 10 * 60:  return "london"
    elif 12 * 60 <= t < 16 * 60: return "new_york"
    elif 0 * 60 <= t <  3 * 60:  return "tokyo"
    elif 22 * 60 <= t <= 24 * 60: return "sydney"
    else:                          return "off_hours"


def build_memory_query(signal, clean_symbol: str) -> str:
    """
    Constructs a free-text query string from a TradeSignal to pass to
    retrieve_similar.  Mirrors the text format used during ingestion so
    the embedding space is consistent.
    """
    session = _active_session(signal.timestamp)
    confluences = []
    if signal.fvg_present:      confluences.append("fvg")
    if signal.ob_present:       confluences.append("order block")
    if signal.killzone_active:  confluences.append("killzone")

    return (
        f"{clean_symbol} {signal.direction.value} on {signal.timeframe} "
        f"in {session} session. Pattern: {signal.pattern.replace('_', ' ')}. "
        f"Confluences: {', '.join(confluences) or 'base retest'}. "
        f"Notes: {signal.notes}"
    )


def fetch_similar_setups(
    query: str,
    use_memory: bool,
    regime: Optional[str] = None,
) -> Optional[List[dict]]:
    """
    Calls retrieve_similar.retrieve_similar() and returns top-K results.
    Silently returns None if Qdrant is unavailable — the feed continues.

    regime : "trending" | "ranging" | None
        When set, only Qdrant records whose ``regime`` payload field matches
        are returned.  Pass None to skip filtering (backwards-compatible).
    """
    if not use_memory:
        return None
    try:
        from trader_copilot.retrieve_similar import retrieve_similar
        results = retrieve_similar(query, top_k=MEMORY_TOP_K, regime=regime)
        logger.info(
            "[memory] retrieve_similar returned %d result(s) "
            "(regime_filter=%s): %s",
            len(results) if results else 0, regime, results
        )
        return results
    except Exception:
        logger.exception("[memory] Qdrant query failed — full traceback:")
        return None


def format_memory_block(matches: Optional[List[dict]]) -> str:
    """
    Formats the top-K similar historical setups for terminal output.
    Appended below the main alert box.
    """
    if not matches:
        return ""

    lines = [
        "",
        "  ── Similar historical setups ──────────────────────",
    ]
    for i, m in enumerate(matches, start=1):
        sid    = m.get("setup_id", "?")
        instr  = m.get("instrument", "?").upper()
        dir_   = m.get("direction", "?")
        out    = m.get("outcome", "?")
        grade  = m.get("quality_grade", "?")
        score  = m.get("similarity_score", 0.0)
        notes  = m.get("vision_notes", "")

        # Truncate long notes for terminal readability
        if len(notes) > 100:
            notes = notes[:97] + "…"

        lines.append(f"  #{i}  [{sid}]  {instr} {dir_}  |  outcome={out}  grade={grade}  sim={score:.3f}")
        lines.append(f"       {notes}")

    lines.append("─" * 48)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CANDLE FETCHING  (closed-only, excludes the currently forming bar)
# ─────────────────────────────────────────────────────────────────────────────

def get_closed_candles(connector, broker_symbol: str, tf: str, count: int = CANDLE_COUNT):
    """
    Fetches candles from MT5 and strips the currently forming bar.

    MT5's copy_rates_from_pos(0) includes the bar currently being built.
    We identify it by comparing the last candle's timestamp to the expected
    open time of the current period and drop it if it matches.

    Returns List[Candle] containing only fully closed bars.
    """
    candles = connector.get_candles(broker_symbol, tf, count=count)
    if not candles:
        return []

    # The candle currently forming should have its open == current period start
    forming_open = current_candle_open_time(tf)

    # MT5 timestamps are tz-naive UTC — make them tz-aware for comparison
    last = candles[-1]
    last_ts = last.timestamp
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)

    forming_ts = forming_open.replace(tzinfo=timezone.utc)

    if last_ts >= forming_ts:
        candles = candles[:-1]   # drop the still-open bar

    return candles


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM ALERT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _score_to_confidence(confluence_score: float) -> int:
    """
    Map confluence_score (0–5 int from engine) → 0–100 confidence int.
    Scale:  0→45  1→55  2→65  3→75  4→85  5→95
    """
    return min(95, 45 + int(round(confluence_score)) * 10)


def _memory_summary(similar_setups: Optional[List[dict]]) -> str:
    """
    Compress the top-K Qdrant results into a one-liner for the Telegram alert.
    e.g.  "3 similar — 2W 1L. Best match: BOS+FVG entry. Outcome: win."
    """
    if not similar_setups:
        return "No similar setups in memory."

    wins   = sum(1 for m in similar_setups if str(m.get("outcome", "")).lower() in ("win", "w", "tp"))
    losses = sum(1 for m in similar_setups if str(m.get("outcome", "")).lower() in ("loss", "l", "sl"))
    total  = len(similar_setups)
    wl_str = f"{wins}W {losses}L" if (wins + losses) else f"{total} results"

    # Pull notes from the closest (first) match
    best_notes = str(similar_setups[0].get("vision_notes", "")).strip()
    if len(best_notes) > 80:
        best_notes = best_notes[:77] + "…"
    note_str = f" Closest match: {best_notes}" if best_notes else ""

    return f"{total} similar — {wl_str}.{note_str}"


def _build_telegram_alert_dict(
    signal,
    clean_symbol: str,
    similar_setups: Optional[List[dict]],
) -> dict:
    """
    Convert a TradeSignal + Qdrant memory results into the alert_dict
    expected by telegram_bot.send_alert().
    """
    entry = float(signal.entry_price)
    sl    = float(signal.stop_loss)
    tp    = float(signal.take_profit)

    risk   = abs(sl - entry)
    reward = abs(tp - entry)
    if risk <= 0:
        rr_str = "—"
    else:
        rr = reward / risk
        if rr > 5.0:
            # Suspiciously wide R:R — cap display and flag for manual review.
            # Root cause is usually a near-zero SL on an index instrument.
            rr_str = f">5.0 ⚠️ check SL manually"
        else:
            rr_str = f"{rr:.1f}"

    session_raw = _active_session(signal.timestamp)
    session_fmt = session_raw.replace("_", " ").title()

    pattern_fmt = signal.pattern.replace("_", " ").title() if signal.pattern else "—"

    return {
        "symbol":     clean_symbol,
        "direction":  signal.direction.value.upper(),
        "pattern":    pattern_fmt,
        "session":    session_fmt,
        "confidence": _score_to_confidence(signal.confluence_score),
        "entry":      entry,
        "stop_loss":  sl,
        "take_profit": tp,
        "rr":         rr_str,
        "why":        signal.notes or "—",
        "memory":     _memory_summary(similar_setups),
        # news_warning intentionally omitted — add if you integrate a news feed
    }


def _fire_telegram_alert(alert_dict: dict) -> None:
    """Synchronous wrapper — safe to call from the feed's sync loop."""
    if not _TG_AVAILABLE:
        return
    try:
        asyncio.run(_tg_send_alert(alert_dict))
    except Exception:
        logger.exception("[telegram] send_alert failed — full traceback:")


def _fire_telegram_startup(symbols: List[str]) -> None:
    """Synchronous wrapper — safe to call from the feed's sync loop."""
    if not _TG_AVAILABLE:
        return
    try:
        asyncio.run(_tg_send_startup(symbols))
    except Exception:
        logger.exception("[telegram] send_startup_message failed — full traceback:")


# ─────────────────────────────────────────────────────────────────────────────
# PER-SYMBOL SCAN
# ─────────────────────────────────────────────────────────────────────────────

def scan_symbol(
    clean_symbol:   str,
    broker_symbol:  str,
    engine,
    connector,
    pipeline:       str,
    candle_tracker: ClosedCandleTracker,
    alert_tracker:  AlertTracker,
    use_memory:     bool,
) -> bool:
    """
    Runs one scan cycle for a single symbol:
      1. Fetch closed candles for the trigger timeframe
      2. Check whether the most recent closed candle is new (not yet processed)
      3. If new, run the correct engine pipeline
      4. If signal fires: enrich with trade memory, print, suppress duplicates
    Returns True if a signal was fired.
    """

    # ── Determine trigger timeframe ──────────────────────────────────────────
    if pipeline == "htf_ltf":
        # XAUUSD: trigger on new 15M candle; always fetch fresh 4H too
        trigger_tf = "15M"
        htf_tf     = "4H"
    else:
        trigger_tf = "30M"

    # ── Fetch closed candles for the trigger timeframe ───────────────────────
    trigger_candles = get_closed_candles(connector, broker_symbol, trigger_tf)
    if not trigger_candles:
        logger.debug(f"[{clean_symbol}] No {trigger_tf} candles returned — skipping")
        return False

    last_closed_open = trigger_candles[-1].timestamp
    if last_closed_open.tzinfo is None:
        last_closed_open = last_closed_open.replace(tzinfo=timezone.utc)

    # ── New-candle gate: only process each closed candle once ─────────────────
    if not candle_tracker.is_new(clean_symbol, trigger_tf, last_closed_open):
        return False   # This candle was already processed — wait for the next one

    candle_tracker.mark(clean_symbol, trigger_tf, last_closed_open)
    logger.info(
        f"[{clean_symbol}] New {trigger_tf} candle closed at "
        f"{last_closed_open.strftime('%Y-%m-%d %H:%M UTC')} — running engine …"
    )

    # ── Run the engine pipeline ───────────────────────────────────────────────
    try:
        if pipeline == "htf_ltf":
            candles_htf = get_closed_candles(connector, broker_symbol, htf_tf)
            if not candles_htf:
                logger.warning(f"[{clean_symbol}] No {htf_tf} candles — skipping")
                return False
            signal = engine.analyse(candles_htf, trigger_candles)
        else:
            # For currency/index pairs: 5M candles are fetched here for the
            # confirmation timeframe. The current engine passes only 30M, but
            # the 5M data is available to wire in once the LTF engine step is
            # extended to support it.
            candles_5m = get_closed_candles(connector, broker_symbol, "5M")
            if not candles_5m:
                logger.debug(f"[{clean_symbol}] No 5M confirmation candles (non-fatal)")

            signal = engine.analyse_currency_30m(trigger_candles)

    except Exception as e:
        logger.error(f"[{clean_symbol}] Engine error: {e}", exc_info=True)
        return False

    if not signal:
        logger.info(f"[{clean_symbol}] No confirmed setup this candle.")
        return False

    # ── Duplicate suppression ─────────────────────────────────────────────────
    if alert_tracker.is_duplicate(clean_symbol, signal.direction.value, signal.entry_price):
        logger.info(f"[{clean_symbol}] Duplicate alert suppressed ({signal.direction.value} @ {signal.entry_price:.5f})")
        return False

    alert_tracker.register(clean_symbol, signal.direction.value, signal.entry_price)

    # ── Trade memory enrichment ───────────────────────────────────────────────
    # regime="trending" is safe here: the engine regime gate already blocks
    # any signal that originates from a ranging / choppy structure.
    memory_query   = build_memory_query(signal, clean_symbol)
    similar_setups = fetch_similar_setups(memory_query, use_memory, regime="trending")
    memory_block   = format_memory_block(similar_setups)

    # ── Dispatch alert ────────────────────────────────────────────────────────
    engine.alert(signal)
    if memory_block:
        print(memory_block)

    # ── Telegram notification ─────────────────────────────────────────────────
    tg_dict = _build_telegram_alert_dict(signal, clean_symbol, similar_setups)
    logger.info("[DEBUG] About to fire Telegram alert for %s", clean_symbol)
    _fire_telegram_alert(tg_dict)
    logger.info("[DEBUG] Telegram alert fired for %s", clean_symbol)

    logger.info(
        f"[{clean_symbol}] SIGNAL FIRED — {signal.direction.value.upper()} | "
        f"Entry: {signal.entry_price:.5f} | SL: {signal.stop_loss:.5f} | "
        f"TP: {signal.take_profit:.5f} | Score: {signal.confluence_score}/5"
    )
    return True


# ─────────────────────────────────────────────────────────────────────────────
# MAIN FEED LOOP
# ─────────────────────────────────────────────────────────────────────────────

def run(
    symbols:    List[str],
    suffix:     str,
    login:      Optional[int],
    password:   Optional[str],
    server:     Optional[str],
    use_memory: bool,
) -> None:
    from trader_copilot.engine import TraderCopilot
    from trader_copilot.utils.mt5_connector import MT5Connector
    from trader_copilot.config.pairs import PAIR_CONFIGS

    # ── 1. Inject PairConfigs for symbols missing from pairs.py ──────────────
    inject_missing_configs(suffix)

    # ── 2. Resolve broker symbol names and validate configs ───────────────────
    # Map: clean_name → broker_name (e.g. "EURUSD" → "EURUSDm")
    broker_name: Dict[str, str] = {}
    for sym in symbols:
        if sym in EXISTING_CONFIG_MAP:
            # Use the pre-mapped name (handles the canonical m-suffix pairs)
            bname = EXISTING_CONFIG_MAP[sym]
        else:
            # Fall back to clean_name + suffix (handles injected configs)
            bname = f"{sym}{suffix}"

        if bname not in PAIR_CONFIGS:
            logger.error(
                f"No PairConfig for '{bname}'. "
                f"Known configs: {sorted(PAIR_CONFIGS.keys())}"
            )
            sys.exit(1)
        broker_name[sym] = bname

    # ── 3. Connect to MT5 ─────────────────────────────────────────────────────
    connector = MT5Connector(login=login, password=password, server=server)
    if not try_connect(connector):
        sys.exit(1)

    # ── 4. Build engines — one per symbol ─────────────────────────────────────
    engines: Dict[str, TraderCopilot] = {}
    for sym in symbols:
        bname = broker_name[sym]
        engines[sym] = TraderCopilot(
            symbol=bname,
            webhook_urls=None,            # Terminal output only for now
            log_path=f"{sym}_alerts.jsonl",
        )
        logger.info(
            f"  {sym:10s} → broker={bname:14s}  pipeline={PIPELINE[sym]}"
        )

    candle_tracker = ClosedCandleTracker()
    alert_tracker  = AlertTracker(ttl_cycles=8)

    # ── 5. Print startup banner ───────────────────────────────────────────────
    logger.info("═" * 60)
    logger.info("  Trader Copilot — Live MT5 Feed")
    logger.info(f"  Symbols   : {', '.join(symbols)}")
    logger.info(f"  Suffix    : '{suffix}' (broker symbol suffix)")
    logger.info(f"  Poll      : every {POLL_INTERVAL_SECONDS}s (triggers on closed candle)")
    logger.info(f"  Memory    : {'Qdrant (top ' + str(MEMORY_TOP_K) + ' similar setups)' if use_memory else 'disabled'}")
    logger.info("═" * 60)

    # ── Telegram startup announcement ─────────────────────────────────────────
    _fire_telegram_startup(symbols)

    cycle = 0
    try:
        while True:
            cycle += 1

            # ── Reconnect guard ───────────────────────────────────────────────
            if not ensure_connected(connector):
                logger.error("MT5 connection could not be restored. Stopping feed.")
                break

            now_utc = datetime.now(timezone.utc)
            logger.debug(
                f"── Poll {cycle} | {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} ──"
            )

            signals_this_cycle = 0
            for sym in symbols:
                fired = scan_symbol(
                    clean_symbol=sym,
                    broker_symbol=broker_name[sym],
                    engine=engines[sym],
                    connector=connector,
                    pipeline=PIPELINE[sym],
                    candle_tracker=candle_tracker,
                    alert_tracker=alert_tracker,
                    use_memory=use_memory,
                )
                if fired:
                    signals_this_cycle += 1

            if signals_this_cycle:
                logger.info(
                    f"── Poll {cycle} complete | "
                    f"{signals_this_cycle} signal(s) fired this cycle ──\n"
                )

            # ── Decay duplicate tracker every cycle ───────────────────────────
            alert_tracker.tick()

            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("\nFeed stopped by user (Ctrl+C).")
    finally:
        connector.disconnect()
        logger.info("MT5 disconnected. Goodbye.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Trader Copilot — Live MT5 Feed\n"
            "Polls MT5 every 30s for closed candles and fires alerts."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--symbols", nargs="+",
        default=WATCH_SYMBOLS,
        choices=WATCH_SYMBOLS,
        metavar="SYMBOL",
        help=(
            f"Symbols to watch (default: all {len(WATCH_SYMBOLS)}). "
            f"Choices: {', '.join(WATCH_SYMBOLS)}"
        ),
    )
    parser.add_argument(
        "--suffix", default="m",
        help=(
            "Broker symbol suffix appended to build the MT5 name "
            "(default: 'm' for Exness demo, use '' for live or other brokers)."
        ),
    )
    parser.add_argument(
        "--login", type=int, default=None,
        help="MT5 account number (leave blank to use the already-logged-in terminal).",
    )
    parser.add_argument(
        "--password", default=None,
        help="MT5 account password.",
    )
    parser.add_argument(
        "--server", default=None,
        help="MT5 broker server name (e.g. 'Exness-MT5Real').",
    )
    parser.add_argument(
        "--no-memory", action="store_true", dest="no_memory",
        help=(
            "Disable Qdrant trade-memory enrichment. "
            "Use this if Qdrant is not running or trade_memory hasn't been ingested yet."
        ),
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        symbols=args.symbols,
        suffix=args.suffix,
        login=args.login,
        password=args.password,
        server=args.server,
        use_memory=not args.no_memory,
    )
