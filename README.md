# Strava Stats Microservice

Tiny, dockerised pipeline that pulls your Strava activities, lands **bronze** JSON + **Parquet**, compacts to a single **gold** `activities` table in DuckDB, and (optionally) enriches with Strava’s **DetailedActivity** API.

- Incremental pulls by default (watermark from your existing shards).
- A **21‑day sliding window** refresh keeps `kudos_count` etc. up‑to‑date without full backfills.
- Enrichment is **rate‑limit aware** and writes detailed payloads to bronze alongside flat tables.

---

## Quick start

### 1) Prereqs
- Docker + Docker Compose
- A Strava API application (free): note your **Client ID** and **Client Secret**

### 2) Configure `.env`
Copy and fill:
```bash
cp .env.example .env
# add your STRAVA_* values
```

### 3) Authorise once
```bash
make build
make auth   # opens device flow; stores refresh token at secrets/strava_token.json
```

### 4) Pull → Compact
```bash
make run         # incremental pull + 21d kudos refresh
make compact     # materialises gold.activities and view activities
```

### 5) Optional: Enrich with DetailedActivity
```bash
# enrich everything missing details
make enrich ENRICH_ARGS="--all"

# or: only last N days
make enrich ENRICH_ARGS="--since-days 30"

# or: surgical
make enrich ENRICH_ARGS="--ids 1234567890,1234567891"

# include segment efforts (heavier)
make enrich ENRICH_ARGS="--all --include-efforts"
```

**Rate‑limit safety knobs** (defaults shown):
```bash
make enrich ENRICH_ARGS="--all --cushion-15min 10 --cushion-daily 10 --max-calls 400"
```

---

## Directory layout

```
data/
  activities/                     # Parquet shards from pulls
  bronze/
    activities/                   # Raw SummaryActivity batches (JSON)
      strava_activities_<athleteId>_<yyyymmddhhmmss>.json
    activity_details/             # Raw DetailedActivity payloads (JSON)
      strava_detailed_activity_<athleteId>_<activityId>_<yyyymmddhhmmss>.json
  warehouse/
    strava.duckdb                 # DuckDB file (gold + views)
secrets/
  strava_token.json               # refresh token persisted after 'make auth'
```

---

## What gets created

**Gold**
- `gold.activities` (table) — deduped, typed rollup of Parquet shards
- `activities` (view) — simple `SELECT * FROM gold.activities`

**Details**
- `activity_details` — flattened subset of DetailedActivity (one row per activity)
- `activity_splits_metric` — metric splits (1km etc.)
- `activity_splits_standard` — imperial splits (1mi etc.)
- `activity_segment_efforts` — only when `--include-efforts`

---

## Common queries

Open DuckDB:
```bash
make duck
```

Examples:
```sql
-- Newest 10 activities
SELECT id, start_date, name, sport_type, distance
FROM activities
ORDER BY start_date DESC
LIMIT 10;

-- Join details
SELECT a.id, a.start_date, a.distance, d.kudos_count, d.average_heartrate
FROM activities a
LEFT JOIN activity_details d ON d.activity_id = a.id
ORDER BY a.start_date DESC
LIMIT 20;

-- Splits for an activity
SELECT split_index, distance_m, elapsed_time_seconds, average_speed
FROM activity_splits_metric
WHERE activity_id = 1234567890
ORDER BY split_index;
```

---

## Tips & Troubleshooting

- **“Nothing to enrich”** right after pull? Run `make compact` first so the `activities` view exists; or pass `--ids` to enrich directly.
- **DuckDB CLI not seeing new rows** created by a running job? Either reconnect the CLI, or run `PRAGMA disable_object_cache;` once per session.
- **429 rate limits** during enrich are handled automatically. You can reduce concurrency via `--max-calls` and increase cushions.
- This project intentionally has **no `strava_activities` view**. The canonical surface is `activities`.

---

## Makefile targets

> `make help` prints this list with descriptions.

### Build & Auth
- `make build` — Build image(s)
- `make auth` — Device-code OAuth; persists refresh token to `secrets/strava_token.json`

### Pull
- `make run` — Incremental pull **with** kudos lookback (21d).
  Override: `make run PER_PAGE=100 DAYS=7`
- `make run-lite` — Pull without kudos lookback
- `make run-all` — Full backfill (ignores Parquet watermark)

### Enrich
- `make enrich ENRICH_ARGS="..."` — See examples above

### Compact
- `make compact` — Dedup shards → `gold.activities`; creates `activities` view

### Convenience
- `make refresh` — `build → run → compact`

### DuckDB
- `make duck` — Open DuckDB shell
- `make sql-"SELECT count(*) FROM activities;"` — One-liner SQL

---

## Privacy

Tokens live only in `secrets/strava_token.json`. Data lands only under `./data/**` on your machine.

---

## Roadmap / Ideas

- Materialised views for common roll‑ups (week/month summaries)
- Lightweight UI on top of DuckDB
