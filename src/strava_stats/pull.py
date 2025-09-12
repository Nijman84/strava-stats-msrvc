#!/usr/bin/env python3
from __future__ import annotations

from .token_store import load_refresh_token, save_refresh_token

import argparse
import glob
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple

import pandas as pd
import requests

TOKEN_URL = "https://www.strava.com/api/v3/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
PER_PAGE_DEFAULT = 200  # Strava max


# ---------- OAuth ----------
def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> Tuple[str, int, int]:
    """Exchange refresh_token for access_token; persist rotated refresh_token if Strava returns one."""
    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if r.status_code == 400:
        # Typical when refresh token is revoked/expired/rotated away
        try:
            print("Refresh failed 400 payload:", json.dumps(r.json(), indent=2))
        except Exception:
            print("Refresh failed 400 payload (non-JSON):", r.text)
        raise SystemExit(
            "Refresh failed (likely invalid_grant). Run `make auth` to bootstrap a new refresh token."
        )
    r.raise_for_status()
    d = r.json()
    new_rt = d.get("refresh_token")
    if new_rt:
        # Strava rotates refresh tokens on refresh – save the NEW one
        save_refresh_token(new_rt, (d.get("athlete") or {}).get("id"), d.get("scope"))
    return d["access_token"], d.get("expires_at", 0), (d.get("athlete") or {}).get("id", 0)


def get_athlete_id(access_token: str) -> int:
    r = requests.get(
        "https://www.strava.com/api/v3/athlete",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    r.raise_for_status()
    return int(r.json().get("id", 0))


# ---------- Incremental state (from Parquet) ----------
def compute_after_from_parquet() -> int | None:
    """Look at existing Parquet shards and return max start_date as Unix seconds (UTC)."""
    files = glob.glob("data/activities/*.parquet")
    if not files:
        return None
    latest = None
    for f in files:
        try:
            s = pd.read_parquet(f, columns=["start_date"])["start_date"]
            s = pd.to_datetime(s, utc=True, errors="coerce")
            m = s.max()
            if pd.isna(m):
                continue
            if latest is None or m > latest:
                latest = m
        except Exception:
            continue
    return int(latest.timestamp()) if latest is not None else None


# ---------- API paging ----------
def fetch_page(access_token: str, page: int, per_page: int, after: int | None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"page": page, "per_page": per_page}
    if after is not None:
        params["after"] = after
    r = requests.get(
        ACTIVITIES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=60,
    )
    if r.status_code == 401:
        try:
            print("401 payload:", json.dumps(r.json(), indent=2))
        except Exception:
            print("401 payload (non-JSON):", r.text)
        raise PermissionError(
            "401 Unauthorized. Re-authorize with scope=read,activity:read[,activity:read_all] "
            "and run `make auth` to seed a fresh refresh token."
        )
    if r.status_code == 429:
        time.sleep(120)
        return fetch_page(access_token, page, per_page, after)
    r.raise_for_status()
    return r.json()


def fetch_activities(access_token: str, per_page: int, after: int | None) -> list[dict]:
    all_items: list[dict] = []
    seen_ids: set[int] = set()
    page = 1
    while True:
        items = fetch_page(access_token, page=page, per_page=per_page, after=after)
        if not items:
            break
        for it in items:
            aid = it.get("id")
            if aid not in seen_ids:
                seen_ids.add(aid)
                all_items.append(it)
        if len(items) < per_page:
            break
        page += 1
        time.sleep(0.2)
    return all_items


# ---------- Landers (Bronze JSON + Parquet) ----------
def write_json_shard(activities: list[dict], athlete_id: int) -> str:
    """
    Write SummaryActivity batch to bronze landing:
      data/bronze/activities/strava_activities_<athleteId>_<yyyymmddhhmmss>.json
    """
    os.makedirs("data/bronze/activities", exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    path = f"data/bronze/activities/strava_activities_{athlete_id}_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(activities, f, ensure_ascii=False, indent=2)
    return path


def to_frame(activities: list[dict]) -> pd.DataFrame:
    if not activities:
        return pd.DataFrame()
    rows = []
    for a in activities:
        m = a.get("map") or {}
        rows.append(
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "sport_type": a.get("sport_type") or a.get("type"),
                "type": a.get("type"),
                "distance": a.get("distance"),
                "moving_time": a.get("moving_time"),
                "elapsed_time": a.get("elapsed_time"),
                "total_elevation_gain": a.get("total_elevation_gain"),
                "start_date": a.get("start_date"),  # ISO8601 UTC
                "start_date_local": a.get("start_date_local"),  # ISO8601 local
                "timezone": a.get("timezone"),
                "utc_offset": a.get("utc_offset"),
                "achievement_count": a.get("achievement_count"),
                "kudos_count": a.get("kudos_count"),
                "average_speed": a.get("average_speed"),
                "max_speed": a.get("max_speed"),
                "average_heartrate": a.get("average_heartrate"),
                "max_heartrate": a.get("max_heartrate"),
                "suffer_score": a.get("suffer_score"),
                "commute": bool(a.get("commute")),
                "manual": bool(a.get("manual")),
                "visibility": a.get("visibility"),
                "gear_id": a.get("gear_id"),
                "location_city": a.get("location_city"),
                "location_state": a.get("location_state"),
                "location_country": a.get("location_country"),
                "map_id": m.get("id"),
                "polyline": m.get("polyline"),
                "summary_polyline": m.get("summary_polyline"),
                # drives “newest wins” in compaction when kudos change
                "ingestion_ts": a.get("ingestion_ts"),
            }
        )
    df = pd.DataFrame(rows)
    for col in ["start_date", "start_date_local"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    # Keep ingestion_ts as string to avoid tz gymnastics; compaction casts if needed
    return df


def write_parquet_shard(df: pd.DataFrame, athlete_id: int) -> str | None:
    if df.empty:
        return None
    os.makedirs("data/activities", exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    path = f"data/activities/activities_{athlete_id}_{ts}.parquet"
    df.to_parquet(path, index=False)  # requires pyarrow
    return path


# ---------- Sliding-window kudos refresh ----------
def refresh_recent_kudos(access_token: str, athlete_id: int, days: int = 21) -> Tuple[int, str | None]:
    """
    Re-pull recent activities (last N days) to refresh kudos_count (and any other evolving fields).
    Writes a bronze Parquet shard; compaction will pick latest via ingestion_ts ordering.
    Returns (row_count, parquet_path or None).
    """
    if days <= 0:
        return (0, None)

    after = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    per_page, page = PER_PAGE_DEFAULT, 1
    rows: list[dict] = []

    while True:
        batch = fetch_page(access_token, page=page, per_page=per_page, after=after)
        if not batch:
            break
        # TZ-less UTC so DuckDB parses deterministically
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for a in batch:
            a["ingestion_ts"] = now_iso  # ensure “newest wins” in compaction
        rows.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
        time.sleep(0.2)

    if not rows:
        print(f"[kudos-refresh] No recent activities in last {days} days.")
        return (0, None)

    df = to_frame(rows)
    pq_path = write_parquet_shard(df, athlete_id)
    print(f"[kudos-refresh] Wrote {len(df)} rows to {pq_path}")
    return (len(df), pq_path)


# ---------- CLI ----------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pull Strava activities to JSON + Parquet (incremental by default)."
    )
    p.add_argument(
        "--all", action="store_true", help="Full refresh (ignore existing Parquet and fetch everything)."
    )
    p.add_argument("--per-page", type=int, default=PER_PAGE_DEFAULT, help="Items per page (max 200).")
    p.add_argument(
        "--refresh-kudos-days",
        type=int,
        default=21,
        help="Also re-pull the last N days to refresh kudos_count without a full backfill (0 disables).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise SystemExit("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in your environment (.env).")

    # Prefer the persisted refresh token (rotated each run). Fallback to .env only for first bootstrap.
    rt = load_refresh_token() or os.environ.get("STRAVA_REFRESH_TOKEN")
    if not rt:
        raise SystemExit("No refresh token found. Run `make auth` once to seed it, or set STRAVA_REFRESH_TOKEN.")

    access_token, expires_at, athlete_id = refresh_access_token(client_id, client_secret, rt)
    if not athlete_id:
        # Some refresh responses omit athlete; fetch explicitly.
        athlete_id = get_athlete_id(access_token)

    after_ts = None
    if not args.all:
        after_ts = compute_after_from_parquet()
        if after_ts:
            dt = datetime.fromtimestamp(after_ts, tz=timezone.utc)
            print(f"Incremental pull since {dt.isoformat().replace('+00:00','Z')} (after={after_ts})")
        else:
            print("No existing Parquet found; doing full pull this time.")

    per_page = min(max(args.per_page, 1), 200)

    # Normal pull (incremental or full)
    activities = fetch_activities(access_token, per_page=per_page, after=after_ts)
    # Stamp ingestion_ts so compaction prefers this batch over older duplicates
    now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for a in activities:
        a["ingestion_ts"] = now_iso

    json_path = write_json_shard(activities, athlete_id)
    df = to_frame(activities)
    pq_path = write_parquet_shard(df, athlete_id)

    print(f"Wrote {len(activities)} activities.")
    print(f"JSON  -> {json_path}")
    if pq_path:
        print(f"PARQUET -> {pq_path}")
    else:
        print("PARQUET -> (nothing new to write)")

    # Sliding-window kudos refresh (skip if doing --all)
    if not args.all and args.refresh_kudos_days > 0:
        n, kudos_pq = refresh_recent_kudos(
            access_token, athlete_id=athlete_id, days=args.refresh_kudos_days
        )
        if n > 0:
            print(f"KUDOS REFRESH -> {kudos_pq} ({n} rows)")
        else:
            print("KUDOS REFRESH -> (no recent activities to refresh)")

if __name__ == "__main__":
    main()
