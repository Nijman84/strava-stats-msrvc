# Strava Stats Microservice

Tiny, dockerised pipeline that pulls your Strava activities, writes **bronze** Parquet shards, compacts/dedupes them into a clean **silver** dataset, and materialises a **gold** DuckDB table for easy querying.

- Incremental pulls by default (uses your existing shards to find the “after” watermark).
- A **21-day sliding window** refresh keeps **`kudos_count`** and other evolving fields up to date without a full backfill.
- One-shot `make refresh` builds → pulls → compacts.

---

## Quick start

### 1) Prereqs
- Docker + Docker Compose
- A Strava API application (free)

### 2) Create a Strava API app
1. Go to **Strava → Settings → API** (on your account) and create an application.  
2. Note your **Client ID** and **Client Secret**.  
3. For local/dev, the callback URL you enter in Strava can be anything; we use the refresh-token grant flow and store tokens locally.

### 3) Configure environment
Copy the example env file and populate it:
```bash
cp .env.example .env
# then edit .env
STRAVA_CLIENT_ID=xxxxxxxx
STRAVA_CLIENT_SECRET=yyyyyyyy
# optional bootstrap only:
# STRAVA_REFRESH_TOKEN=... (if you already have one)
```

### 4) Authenticate (first run only)
This seeds/rotates your long-lived refresh token in `secrets/strava_token.json`:
```bash
make build
make auth
```

### 5) Run the full flow
```bash
make refresh
# build → run (incl. 21-day kudos lookback) → compact
```

Open DuckDB to query the warehouse:
```bash
make duck
-- inside duckdb:
SELECT COUNT(*) FROM strava_activities;
```

---

## Makefile targets

> Tip: `make help` prints this list with descriptions.

### Build & Auth
- `make build` — Build image(s).
- `make auth` — Bootstrap/refresh Strava OAuth (writes `secrets/strava_token.json`).

### Pull
- `make run` — Pull new/updated activities (**includes** kudos lookback).  
  Override defaults: `make run PER_PAGE=100 DAYS=7`
- `make run-all` — Full refresh (ignore existing Parquet).  
- `make run-lite` — Pull without kudos lookback (fast path).

### Compaction
- `make compact` — Dedupe & partition bronze → silver and refresh the DuckDB **gold** table & view.
- `make recompact` — Re-run compaction only (useful after tweaking `compact.py`).

### Convenience
- `make refresh` — **Build → run (with lookback) → compact** in one command.

### DuckDB helpers
- `make duck` — Open DuckDB against `data/warehouse/strava.duckdb`.
- `make sql-"<SQL...>"` — Run a one-liner.  
  Example: `make sql-"SELECT COUNT(*) FROM strava_activities;"`

---

## How it works (Medallion)

```
bronze/ (raw shards)        silver/ (curated Parquet)       gold/ (warehouse & view)
-----------------------     -----------------------------    ---------------------------------
data/activities/*.parquet   data/silver/strava_activities   data/warehouse/strava.duckdb
+ output/*.json             (partitioned by athlete_id       - table: gold.strava_activities
                              when available)                - view:  strava_activities
```

### Bronze
- `make run` hits `GET /athlete/activities` with paging and writes:
  - JSON snapshots → `output/strava_activities_<id>_<ts>.json`
  - Parquet shards → `data/activities/activities_<id>_<ts>.parquet`
- Each row includes **`ingestion_ts`** (UTC, TZ-less string: `YYYY-MM-DD HH:MM:SS`) so the newest record “wins” in compaction.

### Kudos refresh (near-realtime)
- After the incremental pull, we **re-pull the last N days** (default **21**) and write another bronze shard.  
- Because **`kudos_count` can change after the activity day**, this lightweight lookback keeps it current without a full backfill.
- You can tweak the window on the fly: `make refresh DAYS=7`.

> You noticed this in action: someone kudoed your morning run, a quick rerun showed the increment. ✅

### Silver
- `make compact` reads all bronze shards, chooses a **dedupe key**:
  - `athlete_id, id` if both exist  
  - else `id`  
  - else `source_file` (last resort)
- **Best/latest** row per key is picked using (in order):  
  `updated_at` (desc), `resource_state` (desc), **`ingestion_ts` (desc)**, presence of polyline, then `source_file` tie-break.
- Writes clean Parquet to `data/silver/strava_activities/` using a staging dir and **atomic swap**; staging is always cleaned.

### Gold (DuckDB)
- Compaction materialises `gold.strava_activities` and a convenience view `strava_activities`.  
- We `ANALYZE` the table for better planning and keep an **ORDER BY** that helps zone-map pruning.

---

## File & directory layout

```
output/                                   # JSON snapshots (human-friendly; may be empty if no new incrementals)
data/activities/*.parquet                 # bronze shards
data/silver/strava_activities/*           # silver Parquet (partitioned when athlete_id exists)
data/warehouse/strava.duckdb              # DuckDB file (gold)
secrets/strava_token.json                 # long-lived refresh token + metadata
.env                                      # your STRAVA_CLIENT_ID/SECRET, etc.
```

---

## Environment & knobs

- `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET` — from your Strava app (in `.env`).
- Refresh token lives in `secrets/strava_token.json` (rotated automatically).
- Make vars (override per-call):
  - `PER_PAGE` (default `200`)
  - `DAYS` (default `21`) — kudos lookback window used by `make run`/`make refresh`.

Examples:
```bash
make refresh DAYS=30
make run PER_PAGE=100
```

---

## Example DuckDB queries

Open:
```bash
make duck
```

Queries:
```sql
-- 1) Recent activities (10 most recent)
SELECT id, start_date, sport_type, name, distance, kudos_count
FROM strava_activities
ORDER BY start_date DESC
LIMIT 10;

-- 2) Today’s activities (UTC)
SELECT id, name, kudos_count
FROM strava_activities
WHERE CAST(start_date AS DATE) = CURRENT_DATE
ORDER BY start_date DESC;

-- 3) Weekly distance by sport
SELECT strftime(start_date, '%Y-%W') AS year_week,
       sport_type,
       ROUND(SUM(distance)/1000.0, 2) AS km
FROM strava_activities
GROUP BY 1,2
ORDER BY 1 DESC, 2;

-- 4) Long runs in last 90 days (>15km)
SELECT start_date, name, ROUND(distance/1000.0, 2) AS km, kudos_count
FROM strava_activities
WHERE sport_type IN ('Run', 'TrailRun')
  AND start_date >= now() - INTERVAL '90 days'
  AND distance >= 15000
ORDER BY start_date DESC;

-- 5) Top-kudoed activities (all-time)
SELECT id, name, sport_type, kudos_count
FROM strava_activities
ORDER BY kudos_count DESC
LIMIT 20;
```

> Note: If you’ve just adopted the `ingestion_ts` column, older shards might not have it. Compaction still works fine; the column will appear once newer shards are present.

---

## Troubleshooting

- **Empty JSON files in `output/`**  
  That means the incremental pull found nothing new; it’s normal. The **kudos lookback** still writes a bronze Parquet shard in `data/activities/`.

- **Kudos didn’t change**  
  Run `make refresh` again; the lookback re-pull is cheap. You can also `curl` the activity id to confirm Strava shows the new count.

- **429 rate-limit**  
  The client sleeps & retries. Keep `PER_PAGE=200` and the default `DAYS=21` for fewest calls.

- **Changed code but nothing happens**  
  `make build` before running to rebuild the image.

- **Clean staging dirs**  
  Compaction uses a staging folder with atomic swap and always removes staging (and orphans) on completion.

---

## Notes on privacy

Your tokens are stored **locally** in `secrets/strava_token.json`. The repository writes data only to your local `data/` and `output/` directories.

---

## What’s next?

- Add a small dashboard notebook or lightweight UI on top of DuckDB.  
- Optional: materialise views for common rollups (weekly km, VO2-ish metrics, etc.).  
