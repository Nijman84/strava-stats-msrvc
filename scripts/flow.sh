#!/usr/bin/env bash
# scripts/flow.sh
# Runs `make flow`, writes timestamped logs (human + JSONL), and prevents overlapping runs.

set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# --- iso-8601 timestamp (portable, UTC)
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# --- safe git sha probe (never emits prompts/noise)
get_git_sha() {
  if [ -d .git ] && command -v git >/dev/null 2>&1; then
    # suppress stderr to avoid any Xcode/CLT license spew; empty means "n/a"
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

  local now; now="$(ts)"
  local json="{\"ts\":\"$now\",\"level\":\"$level\",\"event\":\"$event\""
  while (($#)); do
    local k v
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

cleanup() { rmdir "$LOCK_DIR" >/dev/null 2>&1 || true; }
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  mkdir -p "$LOG_DIR"
  msg="Another flow run appears to be in progress; exiting."
  echo "[$(ts)] $msg" | tee -a "$PLAIN_LOG"
  jlog info lock_skipped reason="$msg"
  exit 0
fi
trap cleanup EXIT

RUN_ID="$(uuidgen 2>/dev/null || python3 - <<'PY' || echo "no-uuid-$STAMP"
import uuid; print(uuid.uuid4())
PY
)"

GIT_SHA="$(get_git_sha || echo 'n/a')"

{
  echo "=== strava-stats-msrvc flow run $(ts) ==="
  echo "repo: $REPO_DIR"
  echo "git:  $GIT_SHA"
  echo "user: $(id -un)  host: $(hostname)"
  echo "run_id: $RUN_ID"
  echo "---- begin make flow ----"
} | tee -a "$PLAIN_LOG"

jlog info start run_id="$RUN_ID" repo="$REPO_DIR" git="$GIT_SHA"

START_EPOCH="$(date +%s)"

# Optional: ensure dirs if the target exists
if make -q ensure-dirs >/dev/null 2>&1; then
  echo "[$(ts)] make ensure-dirs" | tee -a "$PLAIN_LOG"
  jlog info step name="ensure-dirs" status="started"
  if make ensure-dirs |& tee -a "$PLAIN_LOG"; then
    jlog info step name="ensure-dirs" status="succeeded"
  else
    jlog error step name="ensure-dirs" status="failed"
  fi
fi

# Run the flow (pass-through any args)
echo "[$(ts)] make flow $*" | tee -a "$PLAIN_LOG"
jlog info step name="flow" status="started" args="$*"

# Capture exit separately (PIPESTATUS is bash-only; we're in bash)
set +e
make flow "$@" |& tee -a "$PLAIN_LOG"
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
find "$LOG_ROOT" -type f -name "*.log" -mtime +14 -delete 2>/dev/null || true
find "$REPO_DIR/logs/flow/structured" -type f -name "*.jsonl" -mtime +30 -delete 2>/dev/null || true

jlog info end run_id="$RUN_ID" exit_code="$status" duration_s="$DURATION"
exit "$status"
