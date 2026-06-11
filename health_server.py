"""
Genuvia Edge — Health Check Server
====================================
Lightweight FastAPI service running on port 8080 alongside run_multi.py.
Railway pings GET /health to confirm the container is alive.

Launched automatically by the Dockerfile CMD alongside the scanner via a
process supervisor, or called directly in run_multi.py as a background thread.

Endpoints
---------
    GET /health  →  {"status":"ok","pairs":16,"cycle":N,"last_scan":"...Z"}

State is shared via a module-level dict updated by run_multi.py through the
update_health() function exported from this module.
"""

import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from typing import List, Optional

# ── Path bootstrap — makes `trader_copilot` importable when health_server is
# started standalone (Railway) or imported before run_multi adds the root.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

# ── Trade event model ─────────────────────────────────────────────────────────

class TradeEvent(BaseModel):
    ticket: int
    symbol: str
    direction: str          # "BUY" or "SELL"
    lot_size: float
    entry_price: float
    sl: float
    tp: float
    event: str              # "OPEN" or "CLOSE"
    close_price: Optional[float] = None
    profit: Optional[float] = None
    timestamp: str          # ISO format


# ── Journal database ──────────────────────────────────────────────────────────
# Path is relative to this file so it works both locally and on Railway
# (Railway mounts /app/data; adjust DB_PATH via env var if needed).
_DB_PATH = os.environ.get(
    "JOURNAL_DB_PATH",
    os.path.join(os.path.dirname(__file__), "trader_copilot_journal.db"),
)

_DB_LOCK = threading.Lock()   # sqlite3 is not thread-safe across connections


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create mt5_trades if it doesn't exist. Never touches the existing trades table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mt5_trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket       INTEGER NOT NULL,
            symbol       TEXT    NOT NULL,
            direction    TEXT    NOT NULL,
            lot_size     REAL    NOT NULL,
            entry_price  REAL    NOT NULL,
            sl           REAL    NOT NULL,
            tp           REAL    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'open',
            opened_at    TEXT    NOT NULL,
            close_price  REAL,
            profit       REAL,
            closed_at    TEXT,
            created_at   TEXT    NOT NULL
        )
    """)
    conn.commit()


# ── Shared state (written by run_multi, read by /health) ─────────────────────
_health: dict = {
    "status":    "starting",
    "pairs":     0,
    "cycle":     0,
    "last_scan": None,
}

# ── Risk Guard state (updated by webhook trade events) ────────────────────────
_state: dict = {
    "risk_guard": {
        "open_positions":    [],   # list of dicts, one per open trade
        "open_trade_count":  0,
        "total_exposure":    0.0,  # sum of lot_size across open positions
        "realized_pnl_today": 0.0,
        "last_updated":      None,
    }
}

_STATE_LOCK = threading.Lock()


def _ensure_risk_guard() -> None:
    """Guarantee _state["risk_guard"] has all expected keys (safe to call anytime)."""
    rg = _state.setdefault("risk_guard", {})
    rg.setdefault("open_positions",     [])
    rg.setdefault("open_trade_count",   0)
    rg.setdefault("total_exposure",     0.0)
    rg.setdefault("realized_pnl_today", 0.0)
    rg.setdefault("last_updated",       None)


def _recalculate_exposure() -> None:
    """Recompute open_trade_count and total_exposure from open_positions."""
    rg = _state["risk_guard"]
    rg["open_trade_count"] = len(rg["open_positions"])
    rg["total_exposure"]   = round(
        sum(p["lot_size"] for p in rg["open_positions"]), 2
    )


def update_health(pairs: int, cycle: int):
    """Call this from run_multi.py after each scan cycle completes."""
    _health["status"]    = "ok"
    _health["pairs"]     = pairs
    _health["cycle"]     = cycle
    _health["last_scan"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Genuvia Edge Health")


_trade_events: List[dict] = []


def _link_to_signal_journal(payload: "TradeEvent") -> None:
    """
    On CLOSE: find the most recent matching pending signal in the trades table
    and call record_outcome() so taken is set to 1 and the outcome is recorded.
    Runs after the mt5_trades write — any error here is logged but never raises.
    """
    try:
        from trader_copilot.journal.trade_journal import TradeJournal, Outcome
    except ImportError as exc:
        print(f"[JOURNAL LINK] Could not import TradeJournal — {exc}")
        return

    # Infer Outcome from profit (we don't know if TP or SL was hit from the webhook)
    profit = payload.profit or 0.0
    if profit > 0:
        outcome = Outcome.MANUAL_WIN
    elif profit < 0:
        outcome = Outcome.MANUAL_LOSS
    else:
        outcome = Outcome.BREAKEVEN

    # Parse close_time from ISO string (strip trailing Z for fromisoformat compat)
    try:
        close_time = datetime.fromisoformat(payload.timestamp.replace("Z", "+00:00"))
    except ValueError:
        close_time = datetime.now(timezone.utc)

    # Find the most recent pending signal for this symbol that hasn't been taken
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT id FROM trades
                WHERE symbol = ?
                  AND taken   = 0
                  AND outcome = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (payload.symbol,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[JOURNAL LINK] DB lookup failed for ticket={payload.ticket} — {exc}")
        return

    if row is None:
        print(
            f"[JOURNAL LINK] No matching pending signal found for "
            f"symbol={payload.symbol} ticket={payload.ticket} — skipping record_outcome()"
        )
        return

    trade_id = row["id"]
    try:
        journal = TradeJournal(db_path=_DB_PATH)
        journal.record_outcome(
            trade_id=trade_id,
            outcome=outcome,
            close_price=payload.close_price or 0.0,
            close_time=close_time,
            taken=True,
        )
        print(
            f"[JOURNAL LINK] record_outcome() called — "
            f"trades.id={trade_id} symbol={payload.symbol} "
            f"outcome={outcome.value} ticket={payload.ticket}"
        )
    except Exception as exc:
        print(f"[JOURNAL LINK] record_outcome() failed for trade_id={trade_id} — {exc}")


@app.post("/webhook/trade")
def receive_trade_event(payload: TradeEvent) -> dict:
    """Ingest a trade event pushed from MT5 and persist it to the journal DB."""
    _trade_events.append(payload.model_dump())
    print(
        f"[TRADE EVENT] ticket={payload.ticket} symbol={payload.symbol} "
        f"direction={payload.direction} event={payload.event}"
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    journal_id: int

    with _DB_LOCK:
        conn = _get_conn()
        try:
            _ensure_table(conn)

            if payload.event.upper() == "OPEN":
                cur = conn.execute(
                    """
                    INSERT INTO mt5_trades
                        (ticket, symbol, direction, lot_size, entry_price,
                         sl, tp, status, opened_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                    """,
                    (
                        payload.ticket, payload.symbol, payload.direction,
                        payload.lot_size, payload.entry_price,
                        payload.sl, payload.tp,
                        payload.timestamp, now,
                    ),
                )
                conn.commit()
                journal_id = cur.lastrowid

            else:  # CLOSE
                # Try to update an existing open row for this ticket.
                cur = conn.execute(
                    """
                    UPDATE mt5_trades
                       SET status      = 'closed',
                           close_price = ?,
                           profit      = ?,
                           closed_at   = ?
                     WHERE ticket = ?
                       AND status = 'open'
                    """,
                    (payload.close_price, payload.profit, payload.timestamp, payload.ticket),
                )
                conn.commit()

                if cur.rowcount > 0:
                    # Fetch the id of the row we just updated.
                    row = conn.execute(
                        "SELECT id FROM mt5_trades WHERE ticket = ? AND status = 'closed' ORDER BY id DESC LIMIT 1",
                        (payload.ticket,),
                    ).fetchone()
                    journal_id = row["id"]
                else:
                    # No open entry found — insert a fully-closed record.
                    cur = conn.execute(
                        """
                        INSERT INTO mt5_trades
                            (ticket, symbol, direction, lot_size, entry_price,
                             sl, tp, status, opened_at, close_price, profit,
                             closed_at, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?)
                        """,
                        (
                            payload.ticket, payload.symbol, payload.direction,
                            payload.lot_size, payload.entry_price,
                            payload.sl, payload.tp,
                            payload.timestamp,
                            payload.close_price, payload.profit,
                            payload.timestamp, now,
                        ),
                    )
                    conn.commit()
                    journal_id = cur.lastrowid

        finally:
            conn.close()

    # ── Signal journal linkage (CLOSE only) ───────────────────────────────────
    if payload.event.upper() == "CLOSE":
        _link_to_signal_journal(payload)

    # ── Risk Guard state update ───────────────────────────────────────────────
    with _STATE_LOCK:
        _ensure_risk_guard()
        rg = _state["risk_guard"]

        if payload.event.upper() == "OPEN":
            rg["open_positions"].append({
                "ticket":      payload.ticket,
                "symbol":      payload.symbol,
                "direction":   payload.direction,
                "lot_size":    payload.lot_size,
                "entry_price": payload.entry_price,
                "sl":          payload.sl,
                "tp":          payload.tp,
                "opened_at":   payload.timestamp,
            })
        else:  # CLOSE
            rg["open_positions"] = [
                p for p in rg["open_positions"] if p["ticket"] != payload.ticket
            ]
            rg["realized_pnl_today"] = round(
                rg["realized_pnl_today"] + (payload.profit or 0.0), 2
            )

        _recalculate_exposure()
        rg["last_updated"] = now

    return {"status": "received", "ticket": payload.ticket, "journal_id": journal_id}


@app.get("/webhook/trades")
def get_trade_events() -> list:
    """Return all stored trade events."""
    return _trade_events


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(_health)


# ── Background thread launcher ────────────────────────────────────────────────

def start_health_server(port: int = 8080):
    """
    Start the health server in a daemon background thread.
    Call once at the top of run_multi.run() before the polling loop.

        from health_server import start_health_server, update_health
        start_health_server()
        # ... in the cycle loop:
        update_health(pairs=len(pairs), cycle=cycle)
    """
    def _run():
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ── Standalone entry point ────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("health_server:app", host="0.0.0.0", port=8080, reload=False)
