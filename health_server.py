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

import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
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
    """Ingest a trade event pushed from MT5."""
    _trade_events.append(payload.model_dump())
    print(
        f"[TRADE EVENT] ticket={payload.ticket} symbol={payload.symbol} "
        f"direction={payload.direction} event={payload.event}"
    )
    return {"status": "received", "ticket": payload.ticket}


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
