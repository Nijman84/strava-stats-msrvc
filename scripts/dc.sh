#!/bin/sh
# scripts/dc.sh - robust wrapper that prefers `docker compose` and falls back to `docker-compose`
set -e
if docker compose version >/dev/null 2>&1; then
  exec docker compose "$@"
elif command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose "$@"
else
  echo "ERROR: Docker Compose not found (neither 'docker compose' nor 'docker-compose')." >&2
  exit 127
fi
