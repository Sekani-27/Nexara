"""
seed_railway.py
---------------
Sends a minimal POST /webhook/trade to Railway to trigger _ensure_table(),
which creates mt5_trades (and implicitly touches the journal DB).
Then GETs /debug/tables to confirm the schema is populated.

Usage:
    python seed_railway.py
"""

import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

BASE_URL = "https://nexara-production-6da6.up.railway.app"

PAYLOAD = {
    "ticket": 99999,
    "symbol": "EURUSD",
    "direction": "BUY",
    "lot_size": 0.01,
    "entry_price": 1.08500,
    "sl": 1.08000,
    "tp": 1.09000,
    "event": "OPEN",
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}


def post_json(url: str, data: dict) -> tuple[int, dict]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get_json(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main():
    # ── Step 1: seed the journal DB via the webhook ───────────────────────────
    webhook_url = f"{BASE_URL}/webhook/trade"
    print(f"POST {webhook_url}")
    print(f"     payload: {json.dumps(PAYLOAD, indent=2)}\n")

    status, body = post_json(webhook_url, PAYLOAD)
    print(f"Response {status}: {json.dumps(body, indent=2)}\n")

    if status not in (200, 201):
        print(
            "WARNING: webhook returned a non-2xx status — tables may not have been created."
        )

    # ── Step 2: verify schema via /debug/tables ───────────────────────────────
    debug_url = f"{BASE_URL}/debug/tables"
    print(f"GET {debug_url}\n")

    status2, tables = get_json(debug_url)
    print(f"Response {status2}:")
    print(json.dumps(tables, indent=2))

    # ── Step 3: human-readable verdict ───────────────────────────────────────
    print("\n-- Verdict ----------------------------------------------------------")
    for db_key in ("risk_guard_db", "journal_db"):
        info = tables.get(db_key, {})
        path = info.get("path", "?")
        found_tables = list(info.get("tables", {}).keys())
        if not info.get("exists"):
            print(f"  {db_key}: DB NOT FOUND at {path}")
        elif not found_tables:
            print(f"  {db_key}: DB exists but still EMPTY — {path}")
        else:
            print(f"  {db_key}: OK — tables: {found_tables}")


if __name__ == "__main__":
    main()
