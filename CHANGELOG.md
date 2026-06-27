# Changelog

## 0.1.0 (2026-06-27)


### Features

* add /size command for position size calculation to Telegram bot ([f4a9bab](https://github.com/Sekani-27/Nexara/commit/f4a9babca48f6a2cbd15659b29e2ec0d45291dfe))
* add conversational intelligence to telegram_bot.py ([dfe47b2](https://github.com/Sekani-27/Nexara/commit/dfe47b29c0988a5542dd89c7bc1cd88780f33ac7))
* add daily 17:00 UTC session debrief thread with journal DB + risk guard state ([a8c81f5](https://github.com/Sekani-27/Nexara/commit/a8c81f5b0df6924103b667e04edd58e36cadaf16))
* call record_outcome() on signal journal when CLOSE webhook received ([8218b03](https://github.com/Sekani-27/Nexara/commit/8218b03c4fc24859518f91c29a168e3328884ac7))
* expand scanner to 16 pairs — add GBPJPYm USDJPYm USDZARm USDCHFm AUDUSDm NZDUSDm USDCADm EURJPYm, 4s TD delay ([db23722](https://github.com/Sekani-27/Nexara/commit/db237229fe3dfe9b3fbb06678d2f6b6141711957))
* gate Telegram polling behind ENABLE_TELEGRAM_POLLING env var ([8461d4e](https://github.com/Sekani-27/Nexara/commit/8461d4e12d9ccea452c697d6ead36d34560d0d7c))
* live dashboard — dashboard_server.py FastAPI + MT5 backend, HTML cleaned (journal/settings/bars removed), fetchState() live polling, MT5 indicator ([61998c7](https://github.com/Sekani-27/Nexara/commit/61998c7fbebc0417052b8c1e48a1fbde1a81e3fb))
* MassiveConnector (Polygon.io) fallback — MT5 -&gt; Massive chain in scan_pair ([24a4ebf](https://github.com/Sekani-27/Nexara/commit/24a4ebf0ee2578feaf17a096e3ed3aebcc1086c6))
* persist MT5 webhook trade events to journal DB (mt5_trades table) ([1d1ddb7](https://github.com/Sekani-27/Nexara/commit/1d1ddb7b5ccc911f1380b6babfa9e32ab3c3cb7e))
* persistent storage, journal wiring, taken column ([002344b](https://github.com/Sekani-27/Nexara/commit/002344be97107cce1dd8ec69b68438e6ce73dd25))
* Railway deployment — Dockerfile, railway.toml, non-fatal MT5 on Linux ([46e0fc3](https://github.com/Sekani-27/Nexara/commit/46e0fc3b76bd4c2929a3c274c55a9ea4efd41ca6))
* reduce scan interval to 5min; add 15-pip price-distance staleness gate ([aa73e31](https://github.com/Sekani-27/Nexara/commit/aa73e31b615332f08f8de685e0183254026e269a))
* Risk Guard module, 47 tests passing, dual trader setup (Ntando + Sbonelo) ([1755a12](https://github.com/Sekani-27/Nexara/commit/1755a12cdf39c7e218951407d6d1ed2bbd6e7d56))
* split COMMODITY_PAIRS and INDEX_PAIRS — XAUUSDm and USTEC_x100m in ALL_PAIRS ([7330a5a](https://github.com/Sekani-27/Nexara/commit/7330a5a93368be66d6213ffd3eff39d9931af2a9))
* TwelveData dual-key alternation — 2s delay, key1/key2 split, all 16 pairs confirmed ([3f66792](https://github.com/Sekani-27/Nexara/commit/3f667921de5aff9e3fc6f07e475d2934ea4a184f))
* TwelveDataConnector — MT5-&gt;Massive-&gt;TwelveData chain, all 8 pairs confirmed live ([d8d80ca](https://github.com/Sekani-27/Nexara/commit/d8d80cadec8fe7d0f4d6e0f89dd7c1163e159ac0))
* update risk_guard state in _state on every trade OPEN/CLOSE webhook event ([63da9a6](https://github.com/Sekani-27/Nexara/commit/63da9a65de1b74db07b69eb07df3c74ebfec79fb))
* wire start_polling_thread() into run_multi.py ([b94c2f4](https://github.com/Sekani-27/Nexara/commit/b94c2f441859bc5b7c4367249bdc68f4d976407a))


### Bug Fixes

* 3s cold-start delay + 4s inter-request delay — eliminates Cycle 1 TwelveData 429s ([6642e94](https://github.com/Sekani-27/Nexara/commit/6642e945745057e4a3351d34827b8e61ce3338ea))
* add 0.5s delay after each TwelveData call — respect 8 req/min free-tier limit ([795c329](https://github.com/Sekani-27/Nexara/commit/795c3295658a8fd0b4a1f55d732ff5d7de292d1e))
* add bare-symbol aliases to TwelveData and Massive SYMBOL_MAP ([ebc855f](https://github.com/Sekani-27/Nexara/commit/ebc855f665fe2318d10f5088c7be5e9838170ef0))
* clean pair symbols, reduce scan sleep, add market-open gate, use candle timestamps ([c99b331](https://github.com/Sekani-27/Nexara/commit/c99b331e173fad22732bfcfa6654dbf50d28f055))
* duplicate suppression, staleness gate, pending expiry ([7e8a1f4](https://github.com/Sekani-27/Nexara/commit/7e8a1f4a4570ca738bc61d2bdd909f51d851ed45))
* gate Telegram polling on POLLING_ENABLED env var to prevent 409 conflicts ([d79f206](https://github.com/Sekani-27/Nexara/commit/d79f206acf2db21c0479adba2477c4f40b92a71d))
* MT5 'not installed' message prints once at import, not per candle call ([9f557d0](https://github.com/Sekani-27/Nexara/commit/9f557d07786d31390e81410901b9056eb29861fb))
* price-gate fallback to Massive + unconditional debug logging for staleness check ([d0a370b](https://github.com/Sekani-27/Nexara/commit/d0a370b61ba46cf28a03e2c467925743d977ebab))
* read ACCOUNT_SIZE from os.environ as fallback in RiskGuard ([d2bc1e1](https://github.com/Sekani-27/Nexara/commit/d2bc1e1fc5ef2c4bc102d5642d4ef60b7b30a0cd))
* remove AUDCADm from default pairs — no PAIR_CONFIGS entry ([5924593](https://github.com/Sekani-27/Nexara/commit/5924593e1e0645908856ccbebd42c4aae073a753))
* replace app.run_polling() with manual async loop in daemon thread ([52c1ffd](https://github.com/Sekani-27/Nexara/commit/52c1ffdd5ab8d8afceccde9f0a6974567cbe19d2))
* replace broken % counter with itertools.cycle — key rotates on every call unconditionally ([ed9eb53](https://github.com/Sekani-27/Nexara/commit/ed9eb53d114f4e49431e066ada1150d9cdb5d038))
* route INFO/DEBUG to stdout, WARNING+ to stderr — stops Railway marking scan logs as errors ([f45cf6e](https://github.com/Sekani-27/Nexara/commit/f45cf6ebc83f7fc063535b54ca937868088df273))
* TwelveData rate limit — 8s delay + XAUUSDm moved to scan position 2 ([d8f3f48](https://github.com/Sekani-27/Nexara/commit/d8f3f48f850ddac1a29fc7e3ebd0403dfd0e0f33))
* wire Telegram Bot API into dispatch() — alerts were never sent ([634f028](https://github.com/Sekani-27/Nexara/commit/634f0281b6e71ef74df05447131001317a7ea454))
