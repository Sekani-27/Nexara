"""
Genuvia Edge — Live Dashboard Backend
======================================
FastAPI server that bridges MT5 account data to the browser dashboard.

Usage
-----
    python dashboard_server.py
    # Then open http://localhost:8000

Environment (.env)
------------------
    MT5_LOGIN      — MT5 account number
    MT5_PASSWORD   — MT5 trading password
    MT5_SERVER     — MT5 broker server name
    ACCOUNT_SIZE   — notional account size in USD
    ACTIVE_FIRM    — firm name (e.g. GOAT)
    ACCOUNT_STEP   — step/phase number (e.g. 1)
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dashboard")

# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", 8000))
ACTIVE_FIRM  = os.getenv("ACTIVE_FIRM",  "GOAT")
ACCOUNT_STEP = os.getenv("ACCOUNT_STEP", "1")
MT5_LOGIN    = int(os.getenv("MT5_LOGIN", 0))  or None
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER   = os.getenv("MT5_SERVER")

# GOAT firm limits (applied to all firms until per-firm YAML lookup is wired in)
DAILY_DD_PCT   = 0.05     # 5%  — firm daily draw-down
SOFT_STOP_PCT  = 0.025    # 2.5% — internal soft stop
HARD_STOP_PCT  = 0.0375   # 3.75% — internal hard stop
TARGET_PCT     = 0.10     # 10% — profit target

# ── In-memory state ───────────────────────────────────────────────────────────
_equity_high: float = ACCOUNT_SIZE

_state: dict = {
    "equity":             ACCOUNT_SIZE,
    "balance":            ACCOUNT_SIZE,
    "daily_pnl":          0.0,
    "equity_high":        ACCOUNT_SIZE,
    "daily_floor":        round(ACCOUNT_SIZE - DAILY_DD_PCT  * ACCOUNT_SIZE, 2),
    "soft_stop_level":    round(ACCOUNT_SIZE - SOFT_STOP_PCT * ACCOUNT_SIZE, 2),
    "hard_stop_level":    round(ACCOUNT_SIZE - HARD_STOP_PCT * ACCOUNT_SIZE, 2),
    "trades_today":       0,
    "open_positions":     [],
    "open_risk":          0.0,
    "target":             round(ACCOUNT_SIZE * TARGET_PCT, 2),
    "target_progress_pct": 0.0,
    "session_locked":     False,
    "soft_stop_active":   False,
    "mt5_connected":      False,
    "account_size":       ACCOUNT_SIZE,
    "firm":               ACTIVE_FIRM.upper(),
    "step":               f"Step {ACCOUNT_STEP}",
}


# ── MT5 polling ───────────────────────────────────────────────────────────────

def _today_midnight_utc() -> datetime:
    """Return today's midnight as a timezone-aware UTC datetime."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _poll_once():
    """
    Single synchronous MT5 poll.  Called from the async loop via
    asyncio.get_event_loop().run_in_executor(None, _poll_once).
    Returns the updated state dict; raises on failure.
    """
    global _equity_high

    import MetaTrader5 as mt5  # only reachable on Windows

    info = mt5.account_info()
    if info is None:
        raise RuntimeError(f"account_info() returned None: {mt5.last_error()}")

    equity   = float(info.equity)
    balance  = float(info.balance)

    # Rolling equity high
    if equity > _equity_high:
        _equity_high = equity

    daily_floor    = round(_equity_high - DAILY_DD_PCT  * ACCOUNT_SIZE, 2)
    soft_stop_lvl  = round(_equity_high - SOFT_STOP_PCT * ACCOUNT_SIZE, 2)
    hard_stop_lvl  = round(_equity_high - HARD_STOP_PCT * ACCOUNT_SIZE, 2)

    # Open positions
    positions = mt5.positions_get() or []
    open_pnl  = 0.0
    open_risk = 0.0
    pos_list  = []
    for p in positions:
        profit = float(p.profit)
        open_pnl += profit
        # Approximate open risk = SL distance × lot × contract size × pip value
        # Use 1% of account as fallback if SL not set
        sl_risk = abs(p.price_open - p.sl) * p.volume * 100_000 * 0.0001 if p.sl else ACCOUNT_SIZE * 0.01
        open_risk += sl_risk
        pos_list.append({
            "symbol":  p.symbol,
            "profit":  round(profit, 2),
            "volume":  p.volume,
            "type":    "buy" if p.type == 0 else "sell",
        })

    # Today's closed deals
    midnight   = _today_midnight_utc()
    now_utc    = datetime.now(timezone.utc)
    deals      = mt5.history_deals_get(midnight, now_utc) or []
    closed_pnl = sum(float(d.profit) for d in deals if d.entry == 1)  # 1 = deal out (close)
    trades_today = sum(1 for d in deals if d.entry == 0)               # 0 = deal in (open)

    daily_pnl  = round(closed_pnl + open_pnl, 2)
    earned     = round(balance - ACCOUNT_SIZE, 2)
    target     = round(ACCOUNT_SIZE * TARGET_PCT, 2)
    progress   = round(max(0.0, earned / target * 100), 2) if target > 0 else 0.0

    return {
        "equity":             round(equity, 2),
        "balance":            round(balance, 2),
        "daily_pnl":          daily_pnl,
        "equity_high":        round(_equity_high, 2),
        "daily_floor":        daily_floor,
        "soft_stop_level":    soft_stop_lvl,
        "hard_stop_level":    hard_stop_lvl,
        "trades_today":       trades_today,
        "open_positions":     pos_list,
        "open_risk":          round(open_risk, 2),
        "target":             target,
        "target_progress_pct": progress,
        "session_locked":     daily_pnl <= -(HARD_STOP_PCT * ACCOUNT_SIZE),
        "soft_stop_active":   daily_pnl <= -(SOFT_STOP_PCT * ACCOUNT_SIZE),
        "mt5_connected":      True,
        "account_size":       ACCOUNT_SIZE,
        "firm":               ACTIVE_FIRM.upper(),
        "step":               f"Step {ACCOUNT_STEP}",
    }


async def _poll_loop():
    """Background task: poll MT5 every 5 seconds, update _state in place."""
    global _state
    while True:
        await asyncio.sleep(5)
        try:
            loop    = asyncio.get_event_loop()
            updated = await loop.run_in_executor(None, _poll_once)
            _state.update(updated)
            log.debug(
                "Poll OK — equity=%.2f  daily_pnl=%.2f  trades=%d",
                updated["equity"], updated["daily_pnl"], updated["trades_today"],
            )
        except ImportError:
            # MetaTrader5 not installed (Linux/Railway) — cached state, mt5_connected=False
            _state["mt5_connected"] = False
        except Exception as exc:
            log.warning("MT5 poll error: %s", exc)
            _state["mt5_connected"] = False


# ── Startup ───────────────────────────────────────────────────────────────────

def _connect_mt5() -> bool:
    """Try to connect to MT5. Returns True on success."""
    try:
        import MetaTrader5 as mt5
        if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
            ok = mt5.initialize(
                login=MT5_LOGIN,
                password=MT5_PASSWORD,
                server=MT5_SERVER,
            )
        else:
            ok = mt5.initialize()
        if ok:
            log.info("MT5 connected — %s  account %s", MT5_SERVER, MT5_LOGIN)
        else:
            log.warning("MT5 connect failed: %s", mt5.last_error())
        return ok
    except ImportError:
        log.info("MetaTrader5 not installed — running in cached/demo mode")
        return False
    except Exception as exc:
        log.warning("MT5 connect error: %s", exc)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    connected = _connect_mt5()
    _state["mt5_connected"] = connected
    task = asyncio.create_task(_poll_loop())
    log.info("Dashboard backend started — http://localhost:8000")
    yield
    task.cancel()
    try:
        import MetaTrader5 as mt5
        mt5.shutdown()
    except Exception:
        pass


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Genuvia Edge Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000",
                   "http://localhost:5500", "http://127.0.0.1:5500",
                   "null"],   # file:// origin
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/state")
def get_state() -> dict:
    """Return current account state as JSON."""
    return _state


@app.get("/")
def serve_dashboard():
    """Serve the dashboard HTML file."""
    html_path = os.path.join(os.path.dirname(__file__), "genuvia_edge_full.html")
    if not os.path.exists(html_path):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "genuvia_edge_full.html not found"}, status_code=404)
    return FileResponse(html_path, media_type="text/html")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard_server:app", host="0.0.0.0", port=8000, reload=False)
