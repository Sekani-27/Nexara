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
import threading
from datetime import datetime, timezone
from typing import List, Optional

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


def update_health(pairs: int, cycle: int):
    """Call this from run_multi.py after each scan cycle completes."""
    _health["status"]    = "ok"
    _health["pairs"]     = pairs
    _health["cycle"]     = cycle
    _health["last_scan"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Genuvia Edge Health")


_trade_events: List[dict] = []


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
