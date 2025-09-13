# Strava Stats Microservice

Dockerised, reproducible pipeline to:
- **Pull** your Strava activities (incremental by default)
- Land **JSON** (bronze) & **Parquet** shards
- **Compact** into a single **DuckDB** warehouse table
- **Enrich** missing activities with Strava’s DetailedActivity API

Everything runs in ephemeral containers; data persists on your host via bind mounts.

---

## Quick start

### 1) Prereqs
- Docker + Docker Compose
- A Strava API application (get **Client ID** and **Client Secret**)

### 2) Configure `.env`
```bash
cp .env.example .env
# Edit with your STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET
# (STRAVA_REDIRECT_URI is optional; defaults to http://localhost/exchange_token)
```

### 3) Build & Authorise once
```bash
make build
make auth   # opens an auth URL; paste back the redirect URL or code
# refresh token is saved to ./secrets/strava_token.json and auto-rotated on each run
```

### 4) Run the pipeline
```bash
# Incremental pull (default), then compact, then enrich:
make flow

# Or step-by-step:
make run          # pull (incremental; uses watermark from existing Parquet)
make compact      # upsert/merge into DuckDB warehouse
make enrich       # fetch details for missing activities (throttled)
```

### 5) Full backfill (once, if needed)
```bash
make run-all      # ignore watermark, pull everything
make compact
make enrich
```

---

## Repo & data layout

```
src/strava_stats/
  pull.py                 # pulls SummaryActivity → JSON/Parquet
  compact.py              # compacts shards → DuckDB table
  enrich.py               # fetches DetailedActivity for missing ids
  auth.py                 # one-time OAuth helper
  token_store.py          # persists rotating refresh token

output/                   # JSON drops (bronze)
data/
  activities/             # Parquet shards (bronze)
warehouse/
  strava.duckdb           # DuckDB file (gold + details)
secrets/
  strava_token.json       # rotating refresh token (do not commit)

Dockerfile
docker-compose.yml
Makefile
requirements.txt
```

---

## Commands (Make targets)

```bash
make build         # build the Docker image

make auth          # one-time OAuth; saves rotating refresh token to ./secrets

make run           # incremental pull (per-page configurable)
make run-all       # full backfill (ignores existing Parquet)

make compact       # dedupe & upsert all shards into DuckDB

make enrich        # fetch details for activities missing in warehouse (throttled)

make flow          # run → compact → enrich (in that order)
```

### Tunables

- `PER_PAGE` (default `200`): `make run PER_PAGE=150`
- `ENRICH_LIMIT` (default `100`): `make enrich ENRICH_LIMIT=50`
- `STRAVA_ENRICH_SLEEP_MS` (default `200`): set in `.env` to throttle detail calls
- `STRAVA_WAREHOUSE` (default `warehouse/strava.duckdb`): override in `.env` if desired

Environment variables consumed (via `.env`):
```
STRAVA_CLIENT_ID=...
STRAVA_CLIENT_SECRET=...
# optional:
STRAVA_REDIRECT_URI=http://localhost/exchange_token
STRAVA_SCOPE=read,activity:read,activity:read_all
STRAVA_ENRICH_SLEEP_MS=200
STRAVA_WAREHOUSE=warehouse/strava.duckdb
STRAVA_ENRICH_MAX=100   # legacy; use ENRICH_LIMIT with make if set
```

---

## What gets created

**Bronze**
- `output/strava_activities_<athleteId>_<yyyymmddhhmmss>.json`
- `data/activities/activities_<athleteId>_<yyyymmddhhmmss>.parquet`

**Warehouse (DuckDB)**
- `strava.activities` — merged/deduped activities from all shards
- `strava.activity_details` — one JSON payload per activity (created by `enrich`)

Open the warehouse with the DuckDB CLI if you have it installed:
```bash
duckdb warehouse/strava.duckdb
```

Example queries:
```sql
SELECT id, start_date, name, sport_type, distance
FROM strava.activities
ORDER BY start_date DESC
LIMIT 10;

SELECT a.id, a.start_date, (d.json->>'kudos_count')::INT AS kudos
FROM strava.activities a
LEFT JOIN strava.activity_details d ON d.id = a.id
ORDER BY a.start_date DESC
LIMIT 20;
```

---

## How incremental works

- `pull.py` scans existing `data/activities/*.parquet` and uses the **max(start_date)** as a watermark (Strava `after=` param).
- `compact.py` loads all shards, deduplicates by `id` (latest by `start_date`), and **upserts** into `strava.activities`.
- `enrich.py` asks the warehouse for **activities missing details** and fetches those, upserting into `strava.activity_details`.

---

## Scheduling (later)

This repo is ready for cron/GitHub Actions/etc. For cron, call the ephemeral job:
```
0 6 * * * cd /path/to/strava-stats-msrvc &&   make run && make compact && make enrich >> logs/pull.log 2>&1
```

---

## Troubleshooting

- **401 / invalid_grant** on pull/enrich  
  Your refresh token is stale/revoked. Run `make auth` again (one-time), which re-seeds `./secrets/strava_token.json`.

- **`ModuleNotFoundError: strava_stats.*`**  
  Rebuild the image after adding new files: `make build`.

- **No data written**  
  Ensure you’re running from repo root so bind mounts map `./output` and `./data` into the container.

- **Compose warns: `version is obsolete`**  
  We intentionally omit the `version:` key; the warning goes away once removed.

---

## Privacy

- Tokens live only under `./secrets/strava_token.json` (not committed).
- All data lands under your local `./output`, `./data`, and `./warehouse` directories.

---

## Roadmap

- Add metrics/views (weekly/monthly rollups) in DuckDB  
- Optional kudos/HR refresh windows  
- CLI flags for targeted enrich (ids/date ranges)
