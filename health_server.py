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
from datetime import datetime, date, timezone
from typing import List, Optional

# ── Path bootstrap — makes `trader_copilot` importable when health_server is
# started standalone (Railway) or imported before run_multi adds the root.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fastapi import FastAPI, Query
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
# Accepts TRADE_JOURNAL_DB_PATH (Railway) or JOURNAL_DB_PATH (legacy local).
_DB_PATH = (
    os.environ.get("TRADE_JOURNAL_DB_PATH")
    or os.environ.get("JOURNAL_DB_PATH")
    or os.path.join(os.path.dirname(__file__), "trader_copilot_journal.db")
)

_RG_DB_PATH = os.environ.get(
    "RISK_GUARD_DB_PATH",
    os.path.join(os.path.dirname(__file__), "risk_guard.db"),
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


# ── DB table-discovery helpers ────────────────────────────────────────────────

def _list_tables(db_path: str) -> list[str]:
    """Return all user table names in the given SQLite database."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _resolve_table(db_path: str, candidates: list[str]) -> Optional[str]:
    """
    Return the first candidate table name that actually exists in the DB.
    Falls back to None if none match (endpoint will surface a clear error).
    """
    existing = set(_list_tables(db_path))
    for name in candidates:
        if name in existing:
            return name
    return None


# Known aliases for each logical table — ordered most-likely-first.
# If Railway uses a different name, add it here.
_RG_SESSION_CANDIDATES   = ["session_state", "rg_session_state", "risk_guard_session"]
_JOURNAL_SIGNAL_CANDIDATES = ["trades", "signals", "trade_signals", "journal_trades"]


# ── GET /debug/tables ─────────────────────────────────────────────────────────

@app.get("/debug/tables")
def debug_tables() -> JSONResponse:
    """
    Diagnostic: list every table in both SQLite databases plus the resolved paths.
    Use this to confirm the correct table names on Railway.
    """
    def _describe(db_path: str) -> dict:
        tables = {}
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                names = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                for (name,) in names:
                    cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
                    tables[name] = [c["name"] for c in cols]
            finally:
                conn.close()
            return {"path": db_path, "exists": True, "tables": tables}
        except Exception as exc:
            return {"path": db_path, "exists": False, "error": str(exc), "tables": {}}

    return JSONResponse({
        "risk_guard_db":  _describe(_RG_DB_PATH),
        "journal_db":     _describe(_DB_PATH),
    })


# ── GET /risk-guard/state ─────────────────────────────────────────────────────

@app.get("/risk-guard/state")
def risk_guard_state() -> JSONResponse:
    """
    Return the current account state from risk_guard.db.
    Derives gate status (ALLOW/WARN/BLOCK) and any active prop-firm violations
    from today's session_state row (most recent firm if multiple exist).
    """
    tbl = _resolve_table(_RG_DB_PATH, _RG_SESSION_CANDIDATES)
    if tbl is None:
        actual = _list_tables(_RG_DB_PATH)
        return JSONResponse(
            {"error": "session_state table not found",
             "db_path": _RG_DB_PATH,
             "tables_found": actual,
             "hint": "Call GET /debug/tables to inspect the database"},
            status_code=500,
        )

    try:
        conn = sqlite3.connect(_RG_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                f"SELECT * FROM {tbl} ORDER BY last_updated DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return JSONResponse({"error": f"DB read failed: {exc}"}, status_code=500)

    if row is None:
        return JSONResponse({"error": "No session state found"}, status_code=404)

    row = dict(row)
    account_size     = row["account_size"]
    starting_balance = row["starting_balance"]
    daily_pnl        = row["daily_pnl"]
    balance          = round(starting_balance + daily_pnl, 2)
    daily_loss_pct   = round(daily_pnl / account_size * 100.0, 4) if account_size else 0.0

    violations: list[str] = []
    if row["session_locked"]:
        gate_status = "BLOCK"
        violations.append("session_locked: hard stop triggered this session")
    else:
        revenge_until = row.get("revenge_locked_until")
        if revenge_until:
            try:
                unlock_dt = datetime.fromisoformat(revenge_until)
                if datetime.utcnow() < unlock_dt:
                    gate_status = "BLOCK"
                    violations.append(f"revenge_lock_active: trading resumes after {revenge_until}")
                else:
                    gate_status = "ALLOW"
            except (ValueError, TypeError):
                gate_status = "ALLOW"
        else:
            gate_status = "ALLOW"

    if row.get("session_ended_via_hard_stop"):
        violations.append("prior_hard_stop: previous session ended via hard stop")

    return JSONResponse({
        "firm":               row["firm"],
        "session_date":       row["session_date"],
        "account_size":       account_size,
        "balance":            balance,
        "daily_pnl":          round(daily_pnl, 2),
        "daily_loss_pct":     daily_loss_pct,
        "equity_high":        row["equity_high"],
        "trades_today":       row["trades_today"],
        "cumulative_pnl":     round(row["cumulative_pnl"], 2),
        "valid_trading_days": row["valid_trading_days"],
        "gate_status":        gate_status,
        "prop_firm_violations": violations,
        "last_updated":       row["last_updated"],
        "_table":             tbl,
    })


# ── GET /signals ──────────────────────────────────────────────────────────────

@app.get("/signals")
def get_signals(
    symbol: Optional[str] = Query(default=None, description="Filter by symbol, e.g. EURUSD"),
    date: Optional[str]   = Query(default=None, description="Filter by signal date YYYY-MM-DD"),
    limit: int            = Query(default=20, ge=1, le=500, description="Max rows to return"),
) -> JSONResponse:
    """Return recent signals from the journal database with optional filters."""
    tbl = _resolve_table(_DB_PATH, _JOURNAL_SIGNAL_CANDIDATES)
    if tbl is None:
        actual = _list_tables(_DB_PATH)
        return JSONResponse(
            {"error": "signals/trades table not found",
             "db_path": _DB_PATH,
             "tables_found": actual,
             "hint": "Call GET /debug/tables to inspect the database"},
            status_code=500,
        )

    conditions: list[str] = []
    params: list = []

    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol.upper())
    if date:
        conditions.append("DATE(signal_time) = ?")
        params.append(date)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT * FROM {tbl} {where} ORDER BY signal_time DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return JSONResponse({"error": f"DB read failed: {exc}"}, status_code=500)

    return JSONResponse([dict(r) for r in rows])


# ── GET /journal/summary ──────────────────────────────────────────────────────

@app.get("/journal/summary")
def journal_summary() -> JSONResponse:
    """
    Return session summary for today: trade counts, win/loss/BE, setups, avg RR.
    'Taken' trades only (taken=1).
    """
    tbl = _resolve_table(_DB_PATH, _JOURNAL_SIGNAL_CANDIDATES)
    if tbl is None:
        actual = _list_tables(_DB_PATH)
        return JSONResponse(
            {"error": "signals/trades table not found",
             "db_path": _DB_PATH,
             "tables_found": actual,
             "hint": "Call GET /debug/tables to inspect the database"},
            status_code=500,
        )

    today_str = date.today().isoformat()

    # 'taken' column may not exist on all Railway DB versions — check columns
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            col_names = {c[1] for c in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
            if "taken" in col_names:
                rows = conn.execute(
                    f"SELECT outcome, pattern, pnl_rr FROM {tbl} "
                    "WHERE taken = 1 AND DATE(signal_time) = ?",
                    (today_str,),
                ).fetchall()
            else:
                # Fall back: count all resolved signals for today
                rows = conn.execute(
                    f"SELECT outcome, pattern, pnl_rr FROM {tbl} "
                    "WHERE DATE(signal_time) = ?",
                    (today_str,),
                ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return JSONResponse({"error": f"DB read failed: {exc}"}, status_code=500)

    wins = losses = breakevens = 0
    rr_values: list[float] = []
    setups: dict[str, int] = {}

    for r in rows:
        outcome = (r["outcome"] or "").lower()
        if outcome in ("tp_hit", "manual_win"):
            wins += 1
        elif outcome in ("sl_hit", "manual_loss"):
            losses += 1
        elif outcome == "breakeven":
            breakevens += 1

        if r["pnl_rr"] is not None:
            rr_values.append(r["pnl_rr"])

        pat = r["pattern"] or "unknown"
        setups[pat] = setups.get(pat, 0) + 1

    total = len(rows)
    avg_rr = round(sum(rr_values) / len(rr_values), 3) if rr_values else None

    return JSONResponse({
        "session_date":     today_str,
        "total_trades":     total,
        "wins":             wins,
        "losses":           losses,
        "breakevens":       breakevens,
        "pending":          total - wins - losses - breakevens,
        "setups_triggered": setups,
        "average_rr":       avg_rr,
        "_table":           tbl,
    })


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
