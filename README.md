# Genuvia Edge

An 8-layer SMC/ICT signal pipeline that detects high-probability forex trade setups and delivers them to a prop firm trader via Telegram — in real time, with built-in risk enforcement.

Built for GoatFundedTrader prop firm accounts. Live on Railway.

---

## What It Does

Genuvia Edge scans multiple forex pairs every few minutes, runs each candle through a structured confluence stack, scores the result with a machine learning model, and fires a Telegram alert only when enough conditions align. A Risk Guard layer enforces prop firm rules before any signal is sent.

**Supported pairs:** XAUUSDm · EURUSDm · GBPUSDm · USDJPYm · GBPJPYm · CADJPYm

---

## Signal Pipeline (8 Layers)

```
MT5 Live Feed
    │
    ├── 1. Pattern Detection
    │       Breakout & Retest · Unicorn · Breaker + FVG · Sniper Head & Shoulders
    │
    ├── 2. Regime Classification
    │       Identifies whether the market is trending, ranging, or in distribution
    │
    ├── 3. Confluence Fusion
    │       Weights and combines pattern signals across timeframes
    │
    ├── 4. XGBoost Confidence Scoring
    │       ML model trained on historical SMC setups; filters low-probability signals
    │
    ├── 5. Qdrant Vector Memory
    │       Stores past setups; surfaces similar historical context at signal time
    │
    ├── 6. Risk Guard (PropFirmRuleEngine)
    │       Enforces GoatFundedTrader daily drawdown, max loss, and position rules
    │       Blocks signals when rules would be breached
    │
    ├── 7. Position Sizing (/size command)
    │       Calculates lot size based on account balance and risk %
    │
    └── 8. Telegram Delivery
            Alert includes: pair, direction, entry zone, SL, TP, confidence score
```

---

## Architecture

```
MT5 (live feed)
    │
    ▼
TwelveData API (market data + symbol mapping)
    │
    ▼
FastAPI Backend (Railway)
    ├── Signal Scanner (async, market-hours gated)
    ├── Risk Guard Engine
    ├── Trade Journal (auto-populated on signal fire)
    └── Session Debrief (17:00 UTC daily summary)
        │
        ▼
Telegram Bot (Sonkosi)
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Signal backend | Python · FastAPI |
| Market data | TwelveData API (dual-key rotation) |
| ML scoring | XGBoost |
| Vector memory | Qdrant Cloud |
| Deployment | Railway |
| Delivery | Telegram Bot API |
| MT5 bridge | Webhook EA |
| Testing | pytest · 64/64 passing |

---

## Test Coverage

```
pytest tests/
...................................................................
64 passed in 3.41s
```

All 64 tests cover the full signal pipeline — pattern detection, confluence scoring, Risk Guard rule enforcement, position sizing, and Telegram payload formatting.

---

## Key Features

- **Market-hours gate** — scanner does not run outside active sessions; no false signals on Sunday open
- **Dual-key TwelveData rotation** — handles free-tier rate limits without dropping scans
- **Non-blocking async write path** — signals fire without waiting on journal writes
- **Session debrief** — daily 17:00 UTC summary of signals fired, trades taken, and P&L
- **Risk Guard blocks before delivery** — prop firm rules enforced at the signal level, not after

---

## Telegram Commands

| Command | Description |
|---|---|
| `/signals` | Show today's fired signals |
| `/size [pair] [risk%]` | Calculate position size |
| `/journal` | View trade log |
| `/riskguard` | Check current prop firm rule status |
| `/debrief` | Trigger session summary |

---

## Local Setup

```bash
git clone https://github.com/Sekani-27/genuvia-edge
cd genuvia-edge
cp .env.example .env  # add your API keys
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Required environment variables:
```
TWELVEDATA_KEY_1=
TWELVEDATA_KEY_2=
QDRANT_URL=
QDRANT_API_KEY=
TELEGRAM_BOT_TOKEN=
```

---

## Project Context

Genuvia Edge is the trading intelligence layer of the [Genuvia](https://github.com/Sekani-27) platform. It is actively running on a live prop firm account. The north star metric is **trader survival rate at 90 days**.

Built and maintained by [Ntando Miya](https://github.com/Sekani-27) · Co-founder, Genuvia (Pty) Ltd
