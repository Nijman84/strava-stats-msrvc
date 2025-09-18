#!/usr/bin/env bash
# scripts/flow.sh
# Runs `make flow`, writes timestamped logs (human + JSONL), and prevents overlapping runs.
# Cron-safe: avoids heredocs/complex substitutions in command substitution.

set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# --- ensure compose runs against the *host* repo (not /repo inside the scheduler)
# When HOST_REPO is set (by the scheduler service), force docker compose to use the host path.
MAKE="make"
if [[ -n "${HOST_REPO:-}" ]]; then
  export DOCKER_COMPOSE="docker compose -f ${HOST_REPO}/docker-compose.yml --project-directory ${HOST_REPO}"
  MAKE="make -e"  # honour env overrides even if the Makefile assigns defaults
fi

# --- iso-8601 timestamp (UTC)
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# --- safe git sha probe (never emits prompts/noise)
get_git_sha() {
  if [ -d .git ] && command -v git >/dev/null 2>&1; then
    local sha
    sha="$(git rev-parse --short HEAD 2>/dev/null || true)"
    if [ -n "$sha" ]; then
      printf "%s" "$sha"
      return 0
    fi
  fi
  return 1
}

# --- paths
LOG_ROOT="$REPO_DIR/logs/flow"
STAMP="$(date +%Y%m%d_%H%M%S)"
DATE_PATH="$(date +%Y/%m/%d)"
LOG_DIR="$LOG_ROOT/$DATE_PATH"
PLAIN_LOG="$LOG_DIR/flow_${STAMP}.log"
JSONL_DIR="$REPO_DIR/logs/flow/structured/$DATE_PATH"
JSONL_LOG="$JSONL_DIR/flow_${STAMP}.jsonl"
LATEST_LINK="$LOG_ROOT/latest.log"
LOCK_DIR="$REPO_DIR/.locks/flow.lock"

mkdir -p "$LOG_DIR" "$JSONL_DIR" "$REPO_DIR/.locks"

# --- simple JSON logger (no external deps; strings kept simple)
jlog() {
  # usage: jlog level event key=value ...
  local level="$1"; shift
  local event="$1"; shift

  local now json k v
  now="$(ts)"
  json="{\"ts\":\"$now\",\"level\":\"$level\",\"event\":\"$event\""
  while (($#)); do
    k="${1%%=*}"
    v="${1#*=}"
    v="${v//\\/\\\\}"
    v="${v//\"/\\\"}"
    json+=",\"$k\":\"$v\""
    shift
  done
  json+="}"
  echo "$json" >> "$JSONL_LOG"
}

# --- overlap guard
cleanup() { rmdir "$LOCK_DIR" >/dev/null 2>&1 || true; }
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  msg="Another flow run appears to be in progress; exiting."
  echo "[$(ts)] $msg" | tee -a "$PLAIN_LOG"
  jlog info lock_skipped reason="$msg"
  exit 0
fi
trap cleanup EXIT

# --- robust RUN_ID
if command -v uuidgen >/dev/null 2>&1; then
  RUN_ID="$(uuidgen)"
elif [ -r /proc/sys/kernel/random/uuid ]; then
  RUN_ID="$(cat /proc/sys/kernel/random/uuid)"
else
  RUN_ID="no-uuid-$STAMP-$RANDOM"
fi

GIT_SHA="$(get_git_sha || echo 'n/a')"

{
  echo "=== strava-stats-msrvc flow run $(ts) ==="
  echo "repo: $REPO_DIR"
  echo "git:  $GIT_SHA"
  echo "user: $(id -un)  host: $(hostname)"
  [[ -n "${HOST_REPO:-}" ]] && echo "host_repo: $HOST_REPO"
  echo "run_id: $RUN_ID"
  echo "---- begin make flow ----"
} | tee -a "$PLAIN_LOG"

jlog info start run_id="$RUN_ID" repo="$REPO_DIR" git="$GIT_SHA" host_repo="${HOST_REPO:-}"

START_EPOCH="$(date +%s)"

# Optional: ensure dirs if target exists
if $MAKE -q ensure-dirs >/dev/null 2>&1; then
  echo "[$(ts)] make ensure-dirs" | tee -a "$PLAIN_LOG"
  jlog info step name="ensure-dirs" status="started"
  if $MAKE ensure-dirs |& tee -a "$PLAIN_LOG"; then
    jlog info step name="ensure-dirs" status="succeeded"
  else
    jlog error step name="ensure-dirs" status="failed"
  fi
fi

# Run the flow (pass through any args)
echo "[$(ts)] $MAKE flow $*" | tee -a "$PLAIN_LOG"
jlog info step name="flow" status="started" args="$*"

set +e
$MAKE flow "$@" |& tee -a "$PLAIN_LOG"
status=${PIPESTATUS[0]}
set -e

if [[ "$status" -eq 0 ]]; then
  jlog info step name="flow" status="succeeded"
else
  jlog error step name="flow" status="failed" exit_code="$status"
fi

END_EPOCH="$(date +%s)"
DURATION="$((END_EPOCH - START_EPOCH))"

{
  echo "---- end make flow ----"
  echo "exit_code: $status"
  echo "finished:  $(ts)"
} | tee -a "$PLAIN_LOG"

ln -sf "$PLAIN_LOG" "$LATEST_LINK"

# Retention: delete *plain* logs older than 14 days; keep JSONL for 30 days
find "$LOG_ROOT" -type f -name "*.log"   -mtime +14 -delete 2>/dev/null || true
find "$REPO_DIR/logs/flow/structured" -type f -name "*.jsonl" -mtime +30 -delete 2>/dev/null || true

jlog info end run_id="$RUN_ID" exit_code="$status" duration_s="$DURATION"
exit "$status"
