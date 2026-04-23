# Trader Copilot — smc_core_v1

Rules-based SMC trading alert engine built on Ntando's framework.

---

## Architecture

```
trader_copilot/
├── engine.py                  # Main orchestrator — full pipeline
├── config/
│   └── pairs.py               # Per-pair config: TFs, sessions, tolerances
├── core/
│   ├── structures.py          # Data models: Candle, SwingPoint, FVG, OB, TradeSignal
│   ├── structure_engine.py    # Swing detection, bias, BOS, iCHoCH, sweep
│   ├── poi_engine.py          # FVG detection, OB detection, retest detection
│   └── signal_generator.py   # Confluence scorer + signal builder
├── patterns/
│   └── pattern_engine.py      # Flag, Double Top/Bottom, H&S, IH&S detection
├── alerts/
│   └── alert_engine.py        # Alert formatter + dispatcher (console/webhook/log)
└── tests/
    └── test_engine.py         # Core rule validation tests
```

---

## The Rules Engine (Ntando's Framework)

### Market Structure
- **Selling bias**: LH → LL sequence on HTF
- **Buying bias**: HH → HL sequence on HTF
- **Ranging**: filtered out — no trades

### Liquidity Sweep
- Wick must pierce the level by at least `sweep_wick_pips`
- Candle **body** must close back on the other side
- Wick-only rejections do not count

### BOS / iCHoCH Rule (non-negotiable)
- Candle **body** must close above/below the neckline
- Intrabar / wick break does NOT confirm BOS
- This is enforced in every pattern's execution logic

### Pattern Execution Rules

| Pattern | Execution trigger |
|---|---|
| Ascending/Descending Flag | CHoCH — body closes through high/low |
| Double Top / Bottom | Neckline break — body closes leaving **imbalance (FVG)** |
| H&S / Inverse H&S | Neckline break — body closes above/below high/low |

### Entry Logic
- **Base entry**: retest of iCHoCH / neckline level
- **Boosted entry**: FVG present at the retest zone (same entry, higher conviction)

### Confluence Scoring (1–5)

| Factor | Points |
|---|---|
| HTF bias matches pattern direction | +1 |
| Liquidity sweep confirmed | +1 |
| Pattern BOS confirmed (body close) | +1 |
| FVG at neckline | +1 |
| Order Block at retest zone | +1 |
| Unicorn (OB + FVG together) | +1 (capped at 5) |

Minimum score to generate alert: **3**
XAUUSD / NAS100: additionally requires **killzone active**

### SL / TP
- **SL**: above/below the sweep wick + 1.5× buffer
- **TP**: 2× risk (minimum — manage manually from there)

---

## Pair Configuration

| Pair | Structure TF | Entry TF | Sessions |
|---|---|---|---|
| EURUSDm | 30M | 5M | London, New York |
| GBPUSDm | 30M | 5M | London, New York |
| XAUUSDm | 4H | 15M | London Open, NY Open |
| USTEC_x100m | 4H | 15M | NY Open only |

---

## Quick Start

```python
from trader_copilot import TraderCopilot, Candle
from datetime import datetime

# Initialise for XAUUSDm
engine = TraderCopilot(
    symbol="XAUUSDm",
    log_path="alerts.jsonl",                        # optional: log all signals
    webhook_urls=["https://...", "https://..."]     # optional: one or more Telegram / Discord URLs
)

# Feed candles (your data source)
# candles_htf = list of Candle objects on the 4H timeframe
# candles_ltf = list of Candle objects on the 15M timeframe

signal = engine.run(candles_htf, candles_ltf)

if signal:
    print(f"Signal: {signal.direction.value} {signal.symbol}")
    print(f"Entry: {signal.entry_price} | SL: {signal.stop_loss} | TP: {signal.take_profit}")
    print(f"R:R 1:{signal.risk_reward} | Score: {signal.confluence_score}/5")
```

---

## Connecting a Data Source

The engine expects `List[Candle]` for both HTF and LTF. You can feed it from any source:

```python
from trader_copilot import Candle
from datetime import datetime

# From a broker API / CSV / OHLCV array
def build_candles(ohlcv_rows, timeframe="15M") -> list:
    return [
        Candle(
            timestamp=datetime.fromisoformat(row["time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0)),
            timeframe=timeframe,
        )
        for row in ohlcv_rows
    ]
```

Compatible with: MetaTrader 5 (via `mt5` Python library), CCXT, yfinance, any CSV export.

---

## Running Tests

```bash
cd /path/to/project
PYTHONPATH=. python trader_copilot/tests/test_engine.py
```

Expected output:
```
── Trader Copilot smc_core_v1 — Test Suite ──

PASS — BOS requires body close, not wick
PASS — Sweep requires wick + body rejection
PASS — FVG detected: top=1.1, bottom=1.099, size=0.00100
PASS — Equal highs detected within 20.0pt tolerance
PASS — Bias determination from swing sequence
PASS — Killzone filter working for XAUUSD

── All tests passed ──
```

---

## Phase 2 Roadmap

- [ ] MT5 live data connector
- [ ] Confidence scoring ML layer (trained on trade journal)
- [ ] Telegram alert bot integration
- [ ] Trade journal logger (entry, result, score, notes)
- [ ] Backtesting harness against historical OHLCV
- [ ] Adaptive geometry tolerance (learns from labelled trades)
- [ ] News event filter (avoid high-impact releases)

---

## Phase 2 — Trade Journal & Backtesting

### Trade Journal

Every signal fired is automatically logged to a SQLite database.

```python
from trader_copilot.journal.trade_journal import TradeJournal, Outcome

journal = TradeJournal("my_journal.db")

# Log a signal (called automatically by the engine)
trade_id = journal.log_signal(signal)

# Record outcome after trade closes
journal.record_outcome(trade_id, Outcome.TP_HIT, close_price=2320.00)

# Get performance stats
stats = journal.get_stats(symbol="XAUUSD")
print(stats["win_rate_pct"])    # e.g. 68.0
print(stats["fvg_present_wr"]) # FVG confirmation win rate
print(stats["total_r"])         # Total R banked
```

### Dashboard

```bash
# Overall performance
python -m trader_copilot.journal.dashboard

# Filter by pair
python -m trader_copilot.journal.dashboard --symbol XAUUSD

# Show pending trades
python -m trader_copilot.journal.dashboard --pending
```

### Backtesting

Export historical data from MT5 as CSV (OHLCV format), then:

```python
from trader_copilot.backtest.backtester import Backtester

bt = Backtester("XAUUSD")
results = bt.run(
    htf_csv="xauusd_4h.csv",
    ltf_csv="xauusd_15m.csv",
    min_score=3,
)
bt.print_report(results)
bt.export_report_csv(results, "xauusd_backtest.csv")
```

### MT5 CSV Export (How to get your data)

In MT5:
1. Open the pair chart on the required timeframe
2. File → Save As → select CSV
3. Export both the structure TF (4H) and entry TF (15M)
4. Feed both files into the backtester

---

## Phase 3 Roadmap (next)
- [ ] ML confidence layer trained on journal outcomes
- [ ] Telegram alert bot
- [ ] Adaptive geometry tolerances
- [ ] News event filter
- [ ] Multi-pair simultaneous scanning
