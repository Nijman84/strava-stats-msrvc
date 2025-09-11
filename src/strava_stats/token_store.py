import json, os, time
from pathlib import Path

STORE_PATH = Path(os.environ.get("STRAVA_TOKEN_STORE", "/app/secrets/strava_token.json"))

def load_refresh_token() -> str | None:
    try:
        return json.loads(STORE_PATH.read_text()).get("refresh_token")
    except Exception:
        return None

def save_refresh_token(refresh_token: str, athlete_id: int | None = None, scope: str | None = None) -> str:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "refresh_token": refresh_token,
        "athlete_id": athlete_id,
        "scope": scope,
        "saved_at": int(time.time()),
    }
    STORE_PATH.write_text(json.dumps(payload, indent=2))
    return str(STORE_PATH)
