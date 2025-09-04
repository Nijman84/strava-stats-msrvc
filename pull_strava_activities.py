#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

import requests

# ---- Constants ----
TOKEN_URL = "https://www.strava.com/api/v3/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
PER_PAGE = int(os.environ.get("STRAVA_PER_PAGE", "200"))  # Strava max is 200

ATHLETE_URL = "https://www.strava.com/api/v3/athlete"

def get_athlete_id(access_token: str) -> int:
    r = requests.get(ATHLETE_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("id", 0)



# ---- Helpers ----
def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> Tuple[str, int, int]:
    """Return (access_token, expires_at, athlete_id)."""
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
    try:
        r.raise_for_status()
    except requests.HTTPError:
        print("Token refresh failed:", r.status_code, r.text)
        raise
    d = r.json()
    return d["access_token"], d.get("expires_at", 0), d.get("athlete", {}).get("id", 0)


def maybe_sleep_for_ratelimit(resp: requests.Response) -> None:
    """Back off if close to the 15-min rate limit."""
    lim = resp.headers.get("X-RateLimit-Limit", "")
    use = resp.headers.get("X-RateLimit-Usage", "")
    try:
        short_lim, _ = [int(x) for x in lim.split(",")]
        short_use, _ = [int(x) for x in use.split(",")]
        if short_lim - short_use <= 5:
            time.sleep(120)
    except Exception:
        pass


def fetch_page(access_token: str, page: int, per_page: int) -> List[Dict[str, Any]]:
    r = requests.get(
        ACTIVITIES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"page": page, "per_page": per_page},
        timeout=60,
    )
    if r.status_code == 401:
        try:
            print("401 payload:", json.dumps(r.json(), indent=2))
        except Exception:
            print("401 payload (non-JSON):", r.text)
        raise PermissionError(
            "401 Unauthorized. Re-authorize with scope=read,activity:read[,activity:read_all] "
            "and set STRAVA_REFRESH_TOKEN to the NEW value."
        )
    if r.status_code == 429:
        time.sleep(120)
        return fetch_page(access_token, page, per_page)
    r.raise_for_status()
    maybe_sleep_for_ratelimit(r)
    return r.json()


def fetch_all_activities(client_id: str, client_secret: str, refresh_token: str) -> Tuple[list[dict], int]:
    access_token, expires_at, athlete_id = refresh_access_token(client_id, client_secret, refresh_token)
    if not athlete_id:  # <-- add this
        athlete_id = get_athlete_id(access_token)
    print(f"Got access token (expires_at={expires_at}) for athlete_id={athlete_id}")

    all_items: list[dict] = []
    seen_ids: set[int] = set()
    page = 1

    while True:
        items = fetch_page(access_token, page=page, per_page=PER_PAGE)
        if not items:
            break
        for it in items:
            aid = it.get("id")
            if aid not in seen_ids:
                seen_ids.add(aid)
                all_items.append(it)
        if len(items) < PER_PAGE:
            break
        page += 1
        time.sleep(0.2)

    return all_items, athlete_id


# ---- Entry point ----
def main() -> None:
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        raise SystemExit("Set STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN in your environment.")

    activities, athlete_id = fetch_all_activities(client_id, client_secret, refresh_token)

    os.makedirs("output", exist_ok=True)
    output_path = f"output/strava_activities_{athlete_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(activities, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(activities)} activities to {output_path}")


if __name__ == "__main__":
    main()
