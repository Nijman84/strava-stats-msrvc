# Strava Stats Microservice
## What is it
Dockerised, reproducible pipeline to:

- **Pull** your Strava activities (incremental by default, with a configurable kudos lookback window)
- Land **JSON (bronze)** and **Parquet** shards
- **Compact** into a **DuckDB** warehouse (gold/views)
- **Enrich** recent/missing activities with Strava’s DetailedActivity API (and perform a lightweight **kudos sync** so details never lag the list view)

Everything runs in ephemeral containers; data persists on your host via bind mounts.

---

## Quick start

### 1) Prereqs
- Docker + Docker Compose
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
make auth   # opens an auth URL; paste back the redirect URL or code
# refresh token is saved to ./secrets/strava_token.json and auto-rotated on each run
```

### 4) Run the pipeline
```bash
# End-to-end:
make flow           # pull (incremental) → compact (refresh gold/views) → enrich (recent + kudos sync)

# Or step-by-step:
make run            # pull (uses watermark + kudos lookback window DAYS)
make compact        # upsert/merge into DuckDB warehouse and refresh views
make enrich         # fetch details for recent/missing activities + kudos sync
```

### 5) Full backfill (optional, one-time)
```bash
make run-all
make compact
make enrich
```

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
make run-all         # full backfill
make compact
make recompact       # rerun compaction only
make enrich
make flow            # run → compact → enrich
make refresh         # build → run → compact (one-shot)

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
- `ENRICH_ARGS` default (from the Makefile): `--since-days $(DAYS) --cushion-15min 0 --cushion-daily 0`  
  - Zero cushions make `enrich` refresh recent details even if a row already exists  
  - Override per run, e.g. `make enrich ENRICH_ARGS="--since-days 7 --include-efforts"`

---

## Scheduling (cron-in-a-container)

OS‑agnostic scheduler using a tiny Docker image. It runs your flow daily at **05:00 Europe/London** and writes logs into the repo.

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

### Kudos sync
If `activities.kudos_count` is higher than `activity_details.kudos_count` in the recent window, `enrich` updates the details table to match. This prevents the details table from lagging behind the list refresh when someone kudos your activity after the last details fetch.

---

## Troubleshooting

- **Stale reads in DuckDB**
  DuckDB uses **snapshot isolation** per connection. If you re-query on an already-open connection after `make flow`, you may not see the new rows until you `COMMIT` or **reconnect**.

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