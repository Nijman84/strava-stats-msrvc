# Strava Stats
## Author: Neal Hayes @Nijman84


# Usage
```sh
export STRAVA_CLIENT_ID=xxxxx
export STRAVA_CLIENT_SECRET=xxxxx
export STRAVA_REFRESH_TOKEN=xxxxx   # from OAuth exchange
# optional:
export STRAVA_PER_PAGE=200

python3 pull_strava_activities.py
```






# Notes

## Refresh OAuth token
```sh
curl -X POST https://www.strava.com/api/v3/oauth/token \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET \
  -d grant_type=refresh_token \
  -d refresh_token=YOUR_REFRESH_TOKEN
```
