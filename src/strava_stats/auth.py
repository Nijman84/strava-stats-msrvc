from __future__ import annotations
import os, sys
from urllib.parse import urlencode, urlparse, parse_qs
import requests

from .token_store import save_refresh_token
TOKEN_URL = "https://www.strava.com/api/v3/oauth/token"

def main():
    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")
    if not (client_id and client_secret):
        sys.exit("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env")

    # Use your configured redirect in the Strava app (same as you used manually)
    redirect_uri = os.getenv("STRAVA_REDIRECT_URI", "http://localhost/exchange_token")
    scope = os.getenv("STRAVA_SCOPE", "read,activity:read,activity:read_all")

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "force",
        "scope": scope,
    }
    url = "https://www.strava.com/oauth/authorize?" + urlencode(params)
    print("\n1) Open this URL in your browser and authorize:\n")
    print(url, "\n")
    pasted = input("2) Paste the FULL redirect URL *or* just the code here: ").strip()
    code = pasted
    try:
        q = parse_qs(urlparse(pasted).query)
        code = q.get("code", [pasted])[0]
    except Exception:
        pass
    if not code:
        sys.exit("No code provided.")

    # Exchange code for tokens
    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if r.status_code >= 400:
        print("Token exchange failed:", r.status_code, r.text)
        sys.exit(1)
    d = r.json()
    refresh_token = d.get("refresh_token")
    athlete_id = (d.get("athlete") or {}).get("id")
    scope_resp = d.get("scope")
    if not refresh_token:
        sys.exit("No refresh_token returned. Check your scopes and redirect URI.")

    path = save_refresh_token(refresh_token, athlete_id, scope_resp)
    print(f"\n✅ Saved refresh token to {path}\nNow you can run `make run` or `make run-all`.")

if __name__ == "__main__":
    main()
