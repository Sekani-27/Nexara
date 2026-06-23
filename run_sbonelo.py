"""
run_sbonelo.py — Risk Guard runner for Sbonelo's GOAT funded account.
=====================================================================
All configuration (firm, account size, DB path, Telegram credentials)
is read exclusively from .env.sbonelo — os.environ is never modified,
so this can run alongside the default .env instance in the same process
or machine without any credential cross-contamination.

Usage
-----
    python run_sbonelo.py                  # print config and exit
    python run_sbonelo.py --monitor        # start live equity polling loop
    python run_sbonelo.py --check          # run a sample Fence 1 check

The equity fetcher below is a placeholder. Wire it to MT5 or a broker
API before running --monitor in production:

    def _equity_fetcher() -> float:
        from trader_copilot.utils.mt5_connector import MT5Connector
        mt5 = MT5Connector()
        return mt5.get_current_price("XAUUSD") * account_contracts  # example
"""

import sys
import asyncio
from pathlib import Path

ENV_FILE = ".env.sbonelo"

# ─────────────────────────────────────────────────────────────────────────────
# Guard: env file must exist
# ─────────────────────────────────────────────────────────────────────────────
if not Path(ENV_FILE).exists():
    print(f"ERROR: {ENV_FILE} not found in the project root.")
    print("Create it from the template in README or the project spec.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Load Risk Guard from Sbonelo's env file
# ─────────────────────────────────────────────────────────────────────────────
from risk_guard import RiskGuard
from risk_guard.monitor import RiskGuardMonitor
from risk_guard.models import TradeProposal, DecisionStatus
from risk_guard.alerts import format_clear, format_warn, format_block

rg = RiskGuard(env_file=ENV_FILE)


# ─────────────────────────────────────────────────────────────────────────────
# Print loaded configuration
# ─────────────────────────────────────────────────────────────────────────────
def _banner():
    c = rg.config
    s = rg.state
    print("\n" + "═" * 60)
    print("  Risk Guard — Sbonelo Instance")
    print("  Loaded from:", ENV_FILE)
    print("═" * 60)
    print(f"  Firm             : {c.name.upper()}")
    print(f"  Account size     : ${s.account_size:,.2f}")
    print(f"  DB path          : {rg.state_db.db_path}")
    print(
        f"  Telegram token   : {'configured' if rg.telegram_token and '<' not in rg.telegram_token else 'PLACEHOLDER — update .env.sbonelo'}"
    )
    print(f"  Telegram chat ID : {rg.telegram_chat_id}")
    print("─" * 60)
    print(
        f"  Daily DD limit   : {c.daily_dd_pct}%   (${c.daily_dd_pct/100*s.account_size:,.2f})"
    )
    print(
        f"  Soft stop        : {c.internal_soft_stop_pct}%  (${c.internal_soft_stop_pct/100*s.account_size:,.2f})"
    )
    print(
        f"  Hard stop        : {c.internal_hard_stop_pct}%  (${c.internal_hard_stop_pct/100*s.account_size:,.2f})"
    )
    print(
        f"  Max open risk    : {c.max_open_risk_pct}%  (${c.max_open_risk_pct/100*s.account_size:,.2f})"
    )
    print(f"  Max trades/day   : {c.max_trades_per_day}")
    print(f"  Min hold         : {c.min_hold_seconds}s")
    print(f"  Rollover         : {c.rollover_time} {c.rollover_tz}")
    print("─" * 60)
    print(f"  Session date     : {s.session_date}")
    print(f"  Daily PnL        : ${s.daily_pnl:+,.2f}")
    print(f"  Trades today     : {s.trades_today}/{c.max_trades_per_day}")
    print(f"  Valid days       : {s.valid_trading_days}")
    print(f"  Session locked   : {s.session_locked}")
    print("═" * 60 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Sample Fence 1 check + Telegram send
# ─────────────────────────────────────────────────────────────────────────────
def _run_check():
    proposal = TradeProposal(
        firm=rg.config.name,
        account_size=rg.state.account_size,
        proposed_risk_dollars=75.0,  # 0.5% of 15k
        current_daily_pnl=rg.state.daily_pnl,
        open_risk_dollars=0.0,
        trades_today=rg.state.trades_today,
        estimated_hold_minutes=45.0,
    )
    decision = rg.check_trade(proposal)
    print(f"  Fence 1 result   : {decision.status.value}")
    print(f"  Reason           : {decision.reason}")
    print(f"  Risk budget left : ${decision.remaining_daily_risk:,.2f}")
    print(f"  Trades remaining : {decision.trades_remaining}\n")
    return decision


def _format_and_send(decision):
    """Format the appropriate alert and send it via Sbonelo's Telegram."""
    signal_body = (
        "🟢 GBPUSD — SELL\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Pattern:    Bearish Breakout & Retest\n"
        "Session:    London\n"
        "Entry:      1.2681\n"
        "Stop Loss:  1.2714\n"
        "Take Profit:1.2615\n"
        "RR:         1 : 2.0\n"
        "Score:      4/5  (HIGH)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "[SBONELO — Risk Guard smoke test]"
    )

    if decision.status == DecisionStatus.BLOCK:
        message = format_block(decision)
    elif decision.status == DecisionStatus.WARN:
        message = format_warn(signal_body, decision)
    else:
        message = format_clear(signal_body, decision)

    print("═" * 60)
    print("FORMATTED MESSAGE SENT TO SBONELO'S TELEGRAM")
    print("═" * 60)
    print(message)
    print("═" * 60 + "\n")

    asyncio.run(rg.send_alert(message))
    print("✅  Message sent to Sbonelo's chat (ID:", rg.telegram_chat_id, ")\n")


# ─────────────────────────────────────────────────────────────────────────────
# Monitor with placeholder equity fetcher
# ─────────────────────────────────────────────────────────────────────────────
def _equity_fetcher() -> float:
    """
    PLACEHOLDER — replace with a real broker/MT5 equity call.
    Returns the current session starting balance as a safe default.
    """
    return rg.state.starting_balance + rg.state.daily_pnl


def _run_monitor():
    import asyncio

    async def _on_block(decision):
        msg = format_block(decision)
        print(msg)
        await rg.send_alert(msg)

    async def _on_warn(decision):
        msg = format_warn("", decision)
        print(msg)
        await rg.send_alert(msg)

    def on_block(d):
        asyncio.run(_on_block(d))

    def on_warn(d):
        asyncio.run(_on_warn(d))

    monitor = RiskGuardMonitor(
        config=rg.config,
        state_db=rg.state_db,
        equity_fetcher=_equity_fetcher,
        account_size=rg.state.account_size,
        poll_interval=30,
        on_block=on_block,
        on_warn=on_warn,
        env_file=ENV_FILE,
    )
    print("  Starting monitor loop (Ctrl-C to stop)...\n")
    monitor.run(rg.state)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _banner()

    if "--monitor" in sys.argv:
        _run_monitor()
    elif "--check" in sys.argv:
        print("  Running sample Fence 1 check...")
        _run_check()
    else:
        print("  Pass --check to run a sample trade proposal through Fence 1.")
        print("  Pass --monitor to start the live equity polling loop.")
        print("  (Running Fence 1 check + Telegram send by default)\n")
        decision = _run_check()
        _format_and_send(decision)
