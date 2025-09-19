# Strava Stats Microservice

## What is it?
Dockerised, reproducible pipeline to:

### 1.  The Ingestion, ETL, Curation service: <span style="color:blue">**Flow**</span>
- **Pull** your Strava activities (incremental by default, with a configurable kudos lookback window)
- Runs in ephemeral containers. data persists on your host via bind mounts (but are not source-controlled for data protection)
- Land **JSON (bronze)** and **Parquet** shards
- **Compact** into a **DuckDB** warehouse (gold/views)
- **Enrich** activities with Strava’s DetailedActivity API (plus a lightweight **kudos sync** so details never lag the list view)

### 2. Cron Scheduler Sidecar service: <span style="color:blue">**Scheduler**</span>
- Scheduler sidecar (optional) for daily scheduling using local cron (5am Europe/London by default)
- Container persists until torn down or system restart


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
make compact         # upsert/merge into DuckDB warehouse and refresh views
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
make schedule-build    # build Docker image for the cron scheduler sidecar
make schedule-up       # spin up the Scheduler container with default 5am daily activity ingest
make schedule-down     # tear down Scheduler
make schedule-restart  # restart Scheduler container
make schedule-logs     # tail active Scheduler container logs
make schedule-ps       # check Scheduler container status
make schedule-exec     # adhoc ingest _flow_ NOW inside the Scheduler container
make schedule-time     # check Scheduler container's local time (TZ=Europe/London)
make schedule-doctor   # verify docker socket + compose in the scheduler
make schedule-shell    # shell into the scheduler container

# Convenience
make flow-now        # run full flow now (host) with logging)
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

OS-agnostic scheduler using its own Docker image. It runs your flow daily at **05:00 Europe/London** and writes logs into the repo.

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
- Edit `cron/strava.cron`. The default entry looks like:
  ```cron
  # run daily at 05:00 Europe/London, stream to scheduler log
  0 5 * * * HOST_REPO={{ABSOLUTE_PATH_ON_HOST}} /bin/sh -lc "/repo/scripts/flow.sh >> /repo/logs/scheduler/cron.log 2>&1"
  ```
  Replace `{{ABSOLUTE_PATH_ON_HOST}}` with your repo path (e.g., `/Users/you/code/strava-stats-msrvc` on macOS).  
  This lets the wrapper call **host** Docker Compose from inside the scheduler container so bind mounts resolve to your real filesystem.
- Apply changes with: `make schedule-restart`

**Useful checks**
```bash
make schedule-time     # the container's local time (TZ=Europe/London)
make schedule-doctor   # sanity-check docker & compose + /var/run/docker.sock
make schedule-shell    # interactive /bin/sh inside scheduler
```

**Notes**
- Scheduler runs with `restart: unless-stopped` (ensure Docker auto-starts at login).
- If the machine is asleep at 05:00, that run is skipped (no catch-up). For catch-up semantics, consider Airflow (`dags/` includes an example DAG).

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

## Project structure (high-level)

```
.
├── cron/
│   └── strava.cron               # supercronic schedule (05:00 Europe/London by default)
├── dags/
│   └── strava_stats_msrvc.example.py  # optional Airflow example
├── data/
│   ├── activities/               # Parquet shards (pull; incremental + kudos lookback)
│   ├── bronze/
│   │   ├── activities/           # SummaryActivity JSON (pull)
│   │   └── activity_details/     # DetailedActivity JSON (enrich)
│   └── warehouse/
│       └── strava.duckdb         # DuckDB (gold views + details/splits/efforts tables)
├── docker/
│   └── scheduler/
│       └── Dockerfile            # tiny scheduler image (supercronic + docker CLI)
├── logs/
│   ├── flow/                     # per-run human logs + JSONL
│   │   └── structured/
│   └── scheduler/                # cron driver log
├── scripts/
│   ├── dc.sh                     # docker compose wrapper (host CLI; certs/env friendly)
│   └── flow.sh                   # locking + logging wrapper around `make flow`
├── secrets/
│   ├── corp-root.pem             # (optional) corporate CA
│   ├── corp-bundle.pem           # (optional) corporate CA bundle
│   └── strava_token.json         # rotating refresh token (created by `make auth`)
├── src/
│   └── strava_stats/
│       ├── auth.py               # OAuth helper
│       ├── pull.py               # SummaryActivity → JSON/Parquet (bronze)
│       ├── compact.py            # shards → DuckDB (gold/views)
│       ├── enrich.py             # DetailedActivity + kudos sync
│       └── token_store.py
├── docker-compose.yml
├── Dockerfile
├── Makefile
├── requirements.txt
└── README.md
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

- **Scheduler can’t see your files or Docker**  
  On macOS/Windows, share your repo path in **Docker Desktop → Settings → Resources → File Sharing**.  
  Then run: `make schedule-doctor`

- **Stale reads in DuckDB**  
  DuckDB uses **snapshot isolation** per connection. After `make flow`, you might not see updates on an already-open connection until you `COMMIT` or **reconnect**.

- **401 / `invalid_grant`**  
  Refresh token is stale/revoked → `make auth` to re-seed `./secrets/strava_token.json`.

- **`ModuleNotFoundError: strava_stats.*`**  
  Rebuild after adding/changing modules: `make build`.

- **No data written**  
  Run from repo root so bind mounts map `./data`, `./secrets`, etc.

- **Windows line endings**  
  Ensure `cron/strava.cron` uses **LF** (not CRLF).

### `SSL: CERTIFICATE_VERIFY_FAILED` behind corporate proxy (Zscaler, etc.)
If you see:
```
requests.exceptions.SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate
```
…your network is likely doing HTTPS inspection (e.g., **Zscaler**). Export the CA(s) from **Chrome** and point Requests at them:

1. Open `https://www.strava.com` → padlock / slider on left of URL → **Connection is secure** → **Certificate is valid** → in **Certificate Hierarchy**, export the **top** item (e.g., **Zscaler Root CA**) as Base-64, and also export the **intermediate** directly under it.  
2. Convert to PEM if needed, then bundle:
   ```bash
   cat zscaler-root.pem zscaler-intermediate.pem > secrets/corp-bundle.pem
   ```
3. In `.env`:
   ```env
   REQUESTS_CA_BUNDLE=/app/secrets/corp-bundle.pem
   CURL_CA_BUNDLE=/app/secrets/corp-bundle.pem
   ```

*(Optional but recommended)* Keep public CAs + add corporate CA:
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

Retry `make auth` or `make flow`.

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
