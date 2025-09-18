# Strava Stats Microservice

## What is it
Dockerised, reproducible pipeline to:

- **Pull** your Strava activities (incremental by default, with a configurable kudos lookback window)
- Land **JSON (bronze)** and **Parquet** shards
- **Compact** into a **DuckDB** warehouse (gold/views)
- **Enrich** activities with Strava’s DetailedActivity API (plus a lightweight **kudos sync** so details never lag the list view)

Everything runs in ephemeral containers; data persists on your host via bind mounts.

---

## Quick start

### 1) Prereqs
- Docker + Docker Compose (the repo uses `./scripts/dc.sh` as a wrapper)
- A Strava API application (get your **Client ID** and **Client Secret**)

### 2) Configure `.env`
```bash
cp .env.example .env
# Edit with your STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET
# (STRAVA_REDIRECT_URI is optional; defaults to http://localhost/exchange_token)
```

### 3) Build & authorise once
```bash
make build
make auth   # opens an auth URL; paste back the redirect URL or just the code
# refresh token is saved to ./secrets/strava_token.json and auto-rotated
```

### 4) Run the pipeline
```bash
# End-to-end daily-style:
make flow           # pull (incremental) → compact (refresh gold/views) → enrich (recent + kudos sync)

# Or step-by-step:
make run            # pull (uses watermark + kudos lookback window DAYS)
make compact        # upsert/merge into DuckDB warehouse and refresh views
make enrich         # fetch details for recent/missing activities + kudos sync (defaults to last DAYS)
```

### 5) Full backfill (one-time or occasional)
```bash
# Pull EVERYTHING → compact → enrich ALL missing details:
make flow-backfill

# (or just fill all missing details without re-pulling:)
make enrich-all
```

> **Tip:** For very large accounts, you can pace enrich during a backfill:
> ```bash
> make enrich-all ENRICH_ALL_ARGS="--all --cushion-15min 5 --cushion-daily 25 --max-calls 800"
> ```

---

## Make targets (cheat sheet)

```bash
make help            # list targets

# Build & auth
make build
make auth

# Pipeline
make run             # incremental pull (uses DAYS for kudos lookback)
make run-lite        # pull without kudos lookback (fast path)
make run-all         # full pull back to day zero
make compact
make recompact       # re-run compaction only
make enrich          # enrich recent window (defaults to --since-days $(DAYS))
make enrich-all      # enrich ALL missing details (activities.id NOT IN activity_details)
make flow            # run → compact → enrich (recent)
make flow-backfill   # run-all → compact → enrich-all (full backfill)
make refresh         # build → run → compact (one-shot convenience)

# DuckDB
make duck            # open CLI against data/warehouse/strava.duckdb
make sql-"SELECT count(*) FROM activities;"

# Scheduler (cron-in-a-container)
make schedule-build
make schedule-up
make schedule-down
make schedule-restart
make schedule-logs
make schedule-ps
make schedule-exec
make schedule-time

# Convenience
make flow-now        # run full flow now (host) with logging
make flow-latest     # tail the most recent flow log
make clean-logs      # wipe logs (plain + JSONL + scheduler)
```

### Tunables
- `PER_PAGE` (default `200`): e.g. `make run PER_PAGE=150`
- `DAYS` (default `21`): used by `run` (kudos lookback) and as the default window for `enrich`
- `ENRICH_ARGS` default (from the Makefile):  
  `--since-days $(DAYS) --cushion-15min 0 --cushion-daily 0`  
  - Zero cushions ensures recent details refresh even if a row already exists  
  - Override per run:  
    `make enrich ENRICH_ARGS="--since-days 7 --include-efforts"`
- `ENRICH_ALL_ARGS` default:  
  `--all --cushion-15min 0 --cushion-daily 0`  
  - Override per run:  
    `make enrich-all ENRICH_ALL_ARGS="--all --max-calls 500"`

---

## Scheduling (cron-in-a-container)

OS-agnostic scheduler using a tiny Docker image. It runs your flow daily at **05:00 Europe/London** and writes logs into the repo.

**One-time start**
```bash
make schedule-build
make schedule-up
```

**Verify**
```bash
make schedule-ps
make schedule-logs
```

**Manual triggers**
```bash
make flow-now        # run the full flow now (on host, with logging)
make schedule-exec   # run one flow inside the scheduler container
```

**Change the schedule**
- Edit `cron/strava.cron` (default: `0 5 * * * /bin/sh -lc "/repo/scripts/flow.sh >> /repo/logs/scheduler/cron.log 2>&1"`)
- Apply with: `make schedule-restart`

**Notes**
- Scheduler container runs with `TZ=Europe/London` and `restart: unless-stopped` (ensure Docker auto-starts).
- If the machine is asleep at 05:00, that run is skipped (no catch-up). For catch-up semantics, consider Airflow.

---

## Logs & observability

The wrapper `scripts/flow.sh` writes both human logs and JSON Lines:

- **Human logs (per run):** `logs/flow/YYYY/MM/DD/flow_<timestamp>.log`
- **Latest symlink:** `logs/flow/latest.log`
- **Structured JSONL:** `logs/flow/structured/YYYY/MM/DD/flow_<timestamp>.jsonl`
- **Scheduler log:** `logs/scheduler/cron.log`

Retention (handled by the script):
- Plain logs pruned after **14 days**
- JSONL pruned after **30 days**

---

## Repo & data layout

```
scripts/flow.sh                 # logging wrapper: human + JSONL, lock, retention
cron/strava.cron               # 05:00 Europe/London schedule
docker/scheduler/Dockerfile    # tiny scheduler image

src/strava_stats/
  auth.py                      # OAuth helper (writes secrets/strava_token.json)
  pull.py                      # SummaryActivity → JSON/Parquet (bronze)
  compact.py                   # shards → DuckDB (gold/views)
  enrich.py                    # DetailedActivity + kudos sync (details/splits/efforts)

data/
  bronze/
    activities/                # SummaryActivity JSON (pull)
    activity_details/          # DetailedActivity JSON (enrich)
  activities/                  # Parquet shards (pull; incremental + kudos lookback)
  warehouse/
    strava.duckdb              # DuckDB (gold views + details/splits/efforts tables)

secrets/
  strava_token.json            # rotating refresh token (do not commit)

docker-compose.yml
Makefile
requirements.txt
```

---

## DuckDB usage & example queries

Open the warehouse:
```bash
duckdb data/warehouse/strava.duckdb
```

Sample queries:
```sql
-- 10 most recent activities
SELECT id, start_date, name, sport_type, distance, kudos_count
FROM activities
ORDER BY start_date DESC
LIMIT 10;

-- Verify kudos are aligned for the last week
SELECT
  a.id,
  a.start_date,
  a.kudos_count        AS kudos_activities,
  d.kudos_count        AS kudos_details,
  d.fetched_at
FROM activities a
LEFT JOIN activity_details d
  ON d.activity_id = a.id
WHERE a.start_date >= now() - INTERVAL 7 DAY
ORDER BY a.start_date DESC;

-- Which activities still lack details info (recent window)
SELECT a.id, a.start_date, a.name
FROM activities a
LEFT JOIN activity_details d ON d.activity_id = a.id
WHERE a.start_date >= now() - INTERVAL 21 DAY
  AND d.activity_id IS NULL
ORDER BY a.start_date DESC;
```

---

## How incremental works

- `pull.py` scans existing `data/activities/*.parquet` to set a **watermark** (`after=`) and applies a **kudos lookback window** of `DAYS` to refresh recent list metrics.
- `compact.py` loads shards, **dedupes by `id`**, and upserts into `activities` (and refreshes any dependent views).
- `enrich.py` selects recent/missing activities and fetches **DetailedActivity**, upserting into `activity_details`, `activity_splits_*`, and `activity_segment_efforts`.  
  It also performs a **kudos sync** so `activity_details.kudos_count` catches up to `activities.kudos_count` within the recent window.

### Full backfill
- `make flow-backfill` runs:
  1) `run-all` (pull everything), then
  2) `compact`, then
  3) `enrich-all` (enrich ALL missing details across your entire history).

### Kudos sync
If `activities.kudos_count` is higher than `activity_details.kudos_count` in the recent window, `enrich` updates the details table to match—preventing the details view from lagging behind the list refresh when new kudos arrive.

---

## Troubleshooting

- **Stale reads in DuckDB**  
  DuckDB uses **snapshot isolation** per connection. After `make flow`, you might not see updates on an already-open connection until you `COMMIT` or **reconnect**.

- **401 / `invalid_grant`**  
  Refresh token is stale/revoked → `make auth` to re-seed `./secrets/strava_token.json`.

- **`ModuleNotFoundError: strava_stats.*`**  
  Rebuild after adding/changing modules: `make build`.

- **No data written**  
  Run from repo root so bind mounts map `./data`, `./secrets`, etc.

- **macOS: Xcode/CLT licence prompt** (exit code 69)  
  Accept once: `sudo xcodebuild -license accept`

- **Windows line endings**  
  Ensure `cron/strava.cron` uses **LF** (not CRLF).

### `SSL: CERTIFICATE_VERIFY_FAILED` behind corporate proxy (Zscaler, etc.)
If you see:
```
requests.exceptions.SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate
```
…your network is likely doing HTTPS inspection (e.g., **Zscaler**). Export the CA(s) from **Chrome** and point Requests at them:

1. Open `https://www.strava.com` → padlock → **Certificate** → in **Certificate Hierarchy**, export the **top** item (e.g., **Zscaler Root CA**) as Base-64, and also export the **intermediate** directly under it.  
2. Convert to PEM if needed, then bundle:
   ```bash
   cat zscaler-root.pem zscaler-intermediate.pem > secrets/corp-bundle.pem
   ```
3. In `.env`:
   ```env
   REQUESTS_CA_BUNDLE=/app/secrets/corp-bundle.pem
   CURL_CA_BUNDLE=/app/secrets/corp-bundle.pem
   ```
4. (Recommended) Keep public CAs + add corporate CA:
   ```bash
   ./scripts/dc.sh run --rm pull sh -lc '
     python - <<PY > /app/secrets/ca-bundle.pem
   import certifi, sys
   sys.stdout.write(open(certifi.where(),"r").read())
   sys.stdout.write(open("/app/secrets/corp-bundle.pem","r").read())
   PY
   '
   ```
   Then set:
   ```env
   REQUESTS_CA_BUNDLE=/app/secrets/ca-bundle.pem
   ```
5. Retry `make auth`.

---

## Privacy

- Tokens are stored only under `./secrets/strava_token.json` (never committed).
- Your activity data stays local under `./data` and `./data/warehouse`.

---

## Roadmap

- Metrics/views (weekly/monthly rollups) in DuckDB  
- Optional structured log shipping (Loki/OpenSearch/CloudWatch)  
- Targeted enrich (ids/date ranges) & per-stage timings

---

## License
Code: Apache License 2.0 — see [LICENSE](./LICENSE).  
Attribution: see [NOTICE](./NOTICE).
