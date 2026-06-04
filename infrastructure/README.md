# Genuvia Edge — Infrastructure

## Files

| File | Purpose |
|------|---------|
| `env.template` | All environment variables for both services. Copy to Railway's variable editor for each service. |
| `setup.sh` | One-shot Railway CLI script — creates both services in the project. |
| `../railway.toml` | Service definition for **nexara-scanner** (`run_multi.py`, 16-pair scan loop). |
| `../railway.sbonelo.toml` | Service definition for **nexara-sbonelo** (`run_sbonelo.py`, Sbonelo's dedicated service). |

## Using the setup script

```bash
# 1. Install Railway CLI and authenticate
npm i -g @railway/cli
railway login

# 2. Run from the project root
bash infrastructure/setup.sh
```

The script creates both services. Then in the Railway dashboard for each service:
- Set environment variables from `env.template`
- Connect the GitHub repo and select the branch
- Railway will use `railway.toml` / `railway.sbonelo.toml` for build and deploy config automatically

## Adding a third trader service

1. Create a new entry-point script, e.g. `run_alice.py`, modelled on `run_sbonelo.py`.
2. Create `railway.alice.toml` with the appropriate `startCommand = "python run_alice.py"`.
3. Add a `railway service create nexara-alice` line to `setup.sh`.
4. In the Railway dashboard, link the new service to the repo and point it at `railway.alice.toml` via the service's **Config file path** setting.
5. Copy the required env vars from `env.template` into the new service's variable editor.

## Health check

The nexara-scanner service exposes `GET /health` on port 8080 (served by `health_server.py`).
Railway pings this endpoint every 30 s (configured in `railway.toml`).

Response shape:
```json
{"status": "ok", "pairs": 16, "cycle": 5, "last_scan": "2026-06-04T18:00:00Z"}
```
