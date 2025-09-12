# src/strava_stats/enrich.py
from __future__ import annotations

import argparse
import glob
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import requests

# Re-use your existing refresh-token helpers
try:
    from .token_store import load_refresh_token, save_refresh_token  # noqa: F401
except Exception:
    raise

# --------------------------------------------------------------------------------------
# Config & paths
# --------------------------------------------------------------------------------------
BASE = Path("data")
WAREHOUSE_DB = BASE / "warehouse" / "strava.duckdb"

BRONZE_DETAILS_DIR = BASE / "bronze" / "activity_details"
BRONZE_DETAILS_DIR.mkdir(parents=True, exist_ok=True)

TOKEN_URL = "https://www.strava.com/api/v3/oauth/token"
DETAIL_URL_TMPL = "https://www.strava.com/api/v3/activities/{id}"

CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")

# --------------------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def next_quarter_hour(now: Optional[datetime] = None) -> datetime:
    """Return the next quarter-hour boundary in UTC."""
    now = now or utc_now()
    qmins = ((now.minute // 15) + 1) * 15
    hour = now.hour + (1 if qmins == 60 else 0)
    minute = 0 if qmins == 60 else qmins
    day = now
    if hour == 24:
        day = now + timedelta(days=1)
        hour = 0
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def sleep_until(dt: datetime, jitter_seconds: int = 3) -> None:
    """Sleep until datetime dt (UTC) + small jitter."""
    now = utc_now()
    seconds = max(0.0, (dt - now).total_seconds()) + jitter_seconds
    time.sleep(seconds)


def parse_limit_header(v: str) -> Tuple[int, int]:
    """Parse Strava X-RateLimit-* headers like '100,1000' -> (per_15min, per_day)."""
    parts = [p.strip() for p in (v or "").split(",")]
    if len(parts) != 2:
        return (0, 0)
    try:
        return (int(parts[0]), int(parts[1]))
    except Exception:
        return (0, 0)


class RateBudget:
    """Tracks live rate-limit budget from response headers and decides pacing."""

    def __init__(self, cushion_15min: int = 10, cushion_daily: int = 10):
        self.limit_15 = 100
        self.limit_day = 1000
        self.used_15 = 0
        self.used_day = 0
        self.cushion_15 = cushion_15min
        self.cushion_day = cushion_daily

    def update_from_headers(self, headers: Dict[str, str]) -> None:
        lim15, limday = parse_limit_header(headers.get("X-RateLimit-Limit", "") or headers.get("x-ratelimit-limit", ""))
        use15, useday = parse_limit_header(headers.get("X-RateLimit-Usage", "") or headers.get("x-ratelimit-usage", ""))
        if lim15 and limday:
            self.limit_15, self.limit_day = lim15, limday
        if use15 or useday:
            self.used_15, self.used_day = use15, useday

    def would_exceed_next_call(self) -> Tuple[bool, str]:
        if self.limit_15 and (self.used_15 + 1) > (self.limit_15 - self.cushion_15):
            return True, f"15-min window nearly exhausted ({self.used_15}/{self.limit_15})"
        if self.limit_day and (self.used_day + 1) > (self.limit_day - self.cushion_day):
            return True, f"Daily window nearly exhausted ({self.used_day}/{self.limit_day})"
        return False, ""

# --------------------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------------------
def get_access_token() -> str:
    """Refresh access token via stored refresh_token."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Missing STRAVA_CLIENT_ID or STRAVA_CLIENT_SECRET in environment.")
    refresh_token = load_refresh_token()
    if not refresh_token:
        raise RuntimeError("No refresh token found. Run `make auth` first.")

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    new_refresh = payload.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        try:
            save_refresh_token(new_refresh)
        except Exception:
            pass
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("Failed to acquire access token from Strava OAuth response.")
    return access_token

# --------------------------------------------------------------------------------------
# DuckDB schema
# --------------------------------------------------------------------------------------
DDL = r"""
CREATE TABLE IF NOT EXISTS activity_details (
    activity_id           BIGINT PRIMARY KEY,
    name                  TEXT,
    type                  TEXT,
    sport_type            TEXT,
    start_date            TIMESTAMP,
    start_date_local      TIMESTAMP,
    timezone              TEXT,
    utc_offset_seconds    INTEGER,
    moving_time_seconds   INTEGER,
    elapsed_time_seconds  INTEGER,
    distance_m            DOUBLE,
    total_elevation_gain  DOUBLE,
    elev_high             DOUBLE,
    elev_low              DOUBLE,
    average_speed         DOUBLE,
    max_speed             DOUBLE,
    average_cadence       DOUBLE,
    average_heartrate     DOUBLE,
    max_heartrate         DOUBLE,
    average_watts         DOUBLE,
    max_watts             DOUBLE,
    device_watts          BOOLEAN,
    calories              DOUBLE,
    commute               BOOLEAN,
    trainer               BOOLEAN,
    manual                BOOLEAN,
    private               BOOLEAN,
    gear_id               TEXT,
    device_name           TEXT,
    description           TEXT,
    has_kudoed            BOOLEAN,
    kudos_count           INTEGER,
    comment_count         INTEGER,
    photo_count           INTEGER,
    map_summary_polyline  TEXT,
    fetched_at            TIMESTAMP
);

CREATE TABLE IF NOT EXISTS activity_splits_metric (
    activity_id           BIGINT,
    split_index           INTEGER,
    distance_m            DOUBLE,
    elapsed_time_seconds  INTEGER,
    moving_time_seconds   INTEGER,
    average_speed         DOUBLE,
    elevation_difference  DOUBLE,
    pace_zone             INTEGER,
    PRIMARY KEY (activity_id, split_index)
);

CREATE TABLE IF NOT EXISTS activity_splits_standard (
    activity_id           BIGINT,
    split_index           INTEGER,
    distance_m            DOUBLE,
    elapsed_time_seconds  INTEGER,
    moving_time_seconds   INTEGER,
    average_speed         DOUBLE,
    elevation_difference  DOUBLE,
    pace_zone             INTEGER,
    PRIMARY KEY (activity_id, split_index)
);

CREATE TABLE IF NOT EXISTS activity_segment_efforts (
    effort_id             BIGINT PRIMARY KEY,
    activity_id           BIGINT,
    segment_id            BIGINT,
    name                  TEXT,
    elapsed_time_seconds  INTEGER,
    moving_time_seconds   INTEGER,
    distance_m            DOUBLE,
    start_date            TIMESTAMP,
    pr_rank               INTEGER,
    kom_rank              INTEGER,
    average_heartrate     DOUBLE,
    max_heartrate         DOUBLE
);
"""

def _exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(con.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [name]).fetchone()[0])

def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(DDL)
    # one-time, safe migration: drop legacy raw_json if present
    cols = con.execute("PRAGMA table_info('activity_details')").fetchdf()
    if "raw_json" in set(cols["name"].astype(str)):
        con.execute("ALTER TABLE activity_details DROP COLUMN raw_json")

# --------------------------------------------------------------------------------------
# Backlog selection (activities -> parquet fallback)
# --------------------------------------------------------------------------------------
def select_backlog_ids(
    con: duckdb.DuckDBPyConnection,
    fetch_all: bool,
    since_days: Optional[int],
    explicit_ids: Optional[List[int]],
) -> List[int]:
    if explicit_ids:
        q = """
            WITH ids AS (SELECT UNNEST(?::BIGINT[]) AS activity_id)
            SELECT i.activity_id
            FROM ids i
            LEFT JOIN activity_details d USING (activity_id)
            WHERE d.activity_id IS NULL
            ORDER BY i.activity_id DESC
        """
        return [r[0] for r in con.execute(q, [explicit_ids]).fetchall()]

    def exists(name: str) -> bool:
        return bool(con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [name]
        ).fetchone()[0])

    source = "activities" if exists("activities") else None

    # Build filter
    filt_sql, params = "", []
    if not fetch_all:
        if since_days:
            # DuckDB: parameters cannot appear inside INTERVAL literal → use scalar * INTERVAL 1 DAY
            filt_sql = "WHERE start_date >= NOW() - (? * INTERVAL 1 DAY)"
            params = [since_days]
        else:
            filt_sql = "WHERE start_date >= NOW() - INTERVAL 30 DAY"

    if source:
        q = f"""
            WITH cand AS (
                SELECT CAST(id AS BIGINT) AS activity_id, start_date
                FROM {source}
                {filt_sql}
            )
            SELECT c.activity_id
            FROM cand c
            LEFT JOIN activity_details d USING (activity_id)
            WHERE d.activity_id IS NULL
            ORDER BY c.start_date DESC NULLS LAST
        """
        return [r[0] for r in con.execute(q, params).fetchall()]

    # Parquet fallback
    pq_glob = str((BASE / "activities" / "activities_*.parquet").resolve())
    if glob.glob(pq_glob):
        q = f"""
            WITH raw AS (
                SELECT CAST(id AS BIGINT) AS activity_id,
                       TRY_CAST(start_date AS TIMESTAMP) AS start_date
                FROM read_parquet(?)
            ),
            cand AS (
                SELECT activity_id, start_date FROM raw
                {filt_sql}
            )
            SELECT c.activity_id
            FROM cand c
            LEFT JOIN activity_details d USING (activity_id)
            WHERE d.activity_id IS NULL
            ORDER BY c.start_date DESC NULLS LAST
        """
        return [r[0] for r in con.execute(q, [pq_glob] + params).fetchall()]

    print("[WARN] No 'activities' view and no Parquet shards found; nothing to enrich.")
    return []


# --------------------------------------------------------------------------------------
# Fetch + upsert
# --------------------------------------------------------------------------------------
def fetch_detail(session: requests.Session, token: str, activity_id: int, include_efforts: bool) -> Tuple[dict, Dict[str, str], int]:
    url = DETAIL_URL_TMPL.format(id=activity_id)
    resp = session.get(
        url,
        params={"include_all_efforts": "true" if include_efforts else "false"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    status = resp.status_code
    if status == 429:
        return {}, resp.headers, status
    resp.raise_for_status()
    return resp.json(), resp.headers, status


def json_get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if cur is None or not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def upsert_detail(con: duckdb.DuckDBPyConnection, activity: dict) -> None:
    aid = int(activity["id"])
    start_date = activity.get("start_date")
    start_date_local = activity.get("start_date_local")
    tz = activity.get("timezone")
    tzoffset = activity.get("utc_offset")  # seconds (can be None)
    map_poly = json_get(activity, "map", "summary_polyline")

    con.execute(
        """
        INSERT OR REPLACE INTO activity_details
        SELECT
            ?::BIGINT              AS activity_id,
            ?::TEXT                AS name,
            ?::TEXT                AS type,
            ?::TEXT                AS sport_type,
            ?::TIMESTAMP           AS start_date,
            ?::TIMESTAMP           AS start_date_local,
            ?::TEXT                AS timezone,
            ?::INTEGER             AS utc_offset_seconds,
            ?::INTEGER             AS moving_time_seconds,
            ?::INTEGER             AS elapsed_time_seconds,
            ?::DOUBLE              AS distance_m,
            ?::DOUBLE              AS total_elevation_gain,
            ?::DOUBLE              AS elev_high,
            ?::DOUBLE              AS elev_low,
            ?::DOUBLE              AS average_speed,
            ?::DOUBLE              AS max_speed,
            ?::DOUBLE              AS average_cadence,
            ?::DOUBLE              AS average_heartrate,
            ?::DOUBLE              AS max_heartrate,
            ?::DOUBLE              AS average_watts,
            ?::DOUBLE              AS max_watts,
            ?::BOOLEAN             AS device_watts,
            ?::DOUBLE              AS calories,
            ?::BOOLEAN             AS commute,
            ?::BOOLEAN             AS trainer,
            ?::BOOLEAN             AS manual,
            ?::BOOLEAN             AS private,
            ?::TEXT                AS gear_id,
            ?::TEXT                AS device_name,
            ?::TEXT                AS description,
            ?::BOOLEAN             AS has_kudoed,
            ?::INTEGER             AS kudos_count,
            ?::INTEGER             AS comment_count,
            ?::INTEGER             AS photo_count,
            ?::TEXT                AS map_summary_polyline,
            NOW()                  AS fetched_at
        """,
        [
            aid,
            activity.get("name"),
            activity.get("type"),
            activity.get("sport_type"),
            start_date,
            start_date_local,
            tz,
            tzoffset if isinstance(tzoffset, (int, float)) else None,
            activity.get("moving_time"),
            activity.get("elapsed_time"),
            activity.get("distance"),
            activity.get("total_elevation_gain"),
            activity.get("elev_high"),
            activity.get("elev_low"),
            activity.get("average_speed"),
            activity.get("max_speed"),
            activity.get("average_cadence"),
            activity.get("average_heartrate"),
            activity.get("max_heartrate"),
            activity.get("average_watts"),
            activity.get("max_watts"),
            activity.get("device_watts"),
            activity.get("calories"),
            activity.get("commute"),
            activity.get("trainer"),
            activity.get("manual"),
            activity.get("private"),
            activity.get("gear_id"),
            activity.get("device_name"),
            activity.get("description"),
            activity.get("has_kudoed"),
            activity.get("kudos_count"),
            activity.get("comment_count"),
            activity.get("total_photo_count") or activity.get("photo_count"),
            map_poly,
        ],
    )

    # Splits (metric)
    splits_metric = activity.get("splits_metric") or []
    if splits_metric:
        con.execute("DELETE FROM activity_splits_metric WHERE activity_id = ?", [aid])
        for i, s in enumerate(splits_metric, start=1):
            con.execute(
                """
                INSERT OR REPLACE INTO activity_splits_metric
                (activity_id, split_index, distance_m, elapsed_time_seconds, moving_time_seconds,
                 average_speed, elevation_difference, pace_zone)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    aid,
                    i,
                    s.get("distance"),
                    s.get("elapsed_time"),
                    s.get("moving_time"),
                    s.get("average_speed"),
                    s.get("elevation_difference"),
                    s.get("pace_zone"),
                ],
            )

    # Splits (standard / miles)
    splits_std = activity.get("splits_standard") or []
    if splits_std:
        con.execute("DELETE FROM activity_splits_standard WHERE activity_id = ?", [aid])
        for i, s in enumerate(splits_std, start=1):
            con.execute(
                """
                INSERT OR REPLACE INTO activity_splits_standard
                (activity_id, split_index, distance_m, elapsed_time_seconds, moving_time_seconds,
                 average_speed, elevation_difference, pace_zone)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    aid,
                    i,
                    s.get("distance"),
                    s.get("elapsed_time"),
                    s.get("moving_time"),
                    s.get("average_speed"),
                    s.get("elevation_difference"),
                    s.get("pace_zone"),
                ],
            )

    # Segment efforts (optional)
    seg_efforts = activity.get("segment_efforts") or []
    if seg_efforts:
        for e in seg_efforts:
            con.execute(
                """
                INSERT OR REPLACE INTO activity_segment_efforts
                (effort_id, activity_id, segment_id, name, elapsed_time_seconds, moving_time_seconds,
                 distance_m, start_date, pr_rank, kom_rank, average_heartrate, max_heartrate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    e.get("id"),
                    aid,
                    json_get(e, "segment", "id"),
                    json_get(e, "name") or json_get(e, "segment", "name"),
                    e.get("elapsed_time"),
                    e.get("moving_time"),
                    e.get("distance"),
                    e.get("start_date"),
                    e.get("pr_rank"),
                    e.get("kom_rank"),
                    e.get("average_heartrate"),
                    e.get("max_heartrate"),
                ],
            )


def write_detail_json(activity: dict) -> Path:
    """Write DetailedActivity payload to bronze with athlete + activity in filename."""
    aid = int(activity["id"])
    athlete_id = str(activity.get("athlete", {}).get("id", os.getenv("STRAVA_ATHLETE_ID", "unknown")))
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    fname = f"strava_detailed_activity_{athlete_id}_{aid}_{ts}.json"
    out = BRONZE_DETAILS_DIR / fname
    with out.open("w", encoding="utf-8") as f:
        json.dump(activity, f, ensure_ascii=False)
    return out

# --------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------
def run(
    all_: bool,
    since_days: Optional[int],
    include_efforts: bool,
    max_calls: Optional[int],
    ids_csv: Optional[str],
    sleep_floor_seconds: int,
    dry_run: bool,
    cushion_15min: int,
    cushion_daily: int,
) -> None:
    con = duckdb.connect(str(WAREHOUSE_DB))
    ensure_schema(con)

    explicit_ids = [int(x) for x in ids_csv.split(",")] if ids_csv else None
    backlog = select_backlog_ids(con, all_, since_days, explicit_ids)
    if not backlog:
        print("Nothing to enrich. ✓")
        con.close()
        return

    token = get_access_token()
    session = requests.Session()
    budget = RateBudget(cushion_15min=cushion_15min, cushion_daily=cushion_daily)

    if dry_run:
        print(f"[DRY-RUN] Backlog candidates: {len(backlog)}")
        print("[DRY-RUN] Will pace using live headers during execution.")
        con.close()
        return

    processed = 0
    target = len(backlog)
    hard_cap = max_calls if max_calls is not None else float("inf")

    i = 0
    while i < len(backlog) and processed < hard_cap:
        aid = backlog[i]

        should_sleep, reason = budget.would_exceed_next_call()
        if should_sleep:
            print(f"[PAUSE] {reason}. Sleeping to next 15-min window…")
            sleep_until(next_quarter_hour())

        payload, headers, status = fetch_detail(session, token, aid, include_efforts)
        budget.update_from_headers(headers)

        if status == 429:
            retry_after = headers.get("Retry-After")
            if retry_after:
                try:
                    secs = int(retry_after)
                except Exception:
                    secs = 60
                secs = max(secs, sleep_floor_seconds)
                print(f"[429] Rate limited. Sleeping {secs}s…")
                time.sleep(secs)
            else:
                print("[429] Rate limited. Sleeping until next quarter-hour…")
                sleep_until(next_quarter_hour())
            continue  # retry same aid on next loop

        # Persist
        write_detail_json(payload)
        upsert_detail(con, payload)

        processed += 1
        i += 1

        u15, l15 = budget.used_15, budget.limit_15
        uday, lday = budget.used_day, budget.limit_day
        print(f"[{processed}/{target}] activity_id={aid} | 15m {u15}/{l15} | day {uday}/{lday}")

    print(f"Done. Processed {processed} activities.")
    try:
        con.execute("CHECKPOINT")
    except Exception:
        pass
    con.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Enrich Strava activities with DetailedActivity.")
    p.add_argument("--all", dest="all_", action="store_true", help="Backfill all activities without date filter.")
    p.add_argument("--since-days", type=int, default=None, help="Enrich only last N days (default 30 if not set).")
    p.add_argument("--include-efforts", action="store_true", help="Also fetch segment_efforts (heavier payload).")
    p.add_argument("--max-calls", type=int, default=None, help="Hard upper bound for calls this run.")
    p.add_argument("--ids", type=str, default=None, help="Comma-separated activity IDs for surgical enrich.")
    p.add_argument("--sleep-floor-seconds", type=int, default=5, help="Minimum sleep when backing off on 429.")
    p.add_argument("--dry-run", action="store_true", help="Plan only; do not call the API.")
    p.add_argument("--cushion-15min", type=int, default=10, help="Leave this many calls unused per 15-min window.")
    p.add_argument("--cushion-daily", type=int, default=10, help="Leave this many calls unused per day.")
    args = p.parse_args()

    run(
        all_=args.all_,
        since_days=args.since_days,
        include_efforts=args.include_efforts,
        max_calls=args.max_calls,
        ids_csv=args.ids,
        sleep_floor_seconds=args.sleep_floor_seconds,
        dry_run=args.dry_run,
        cushion_15min=args.cushion_15min,
        cushion_daily=args.cushion_daily,
    )


if __name__ == "__main__":
    main()
