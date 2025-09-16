.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

.PHONY: help build ensure-dirs env-ok check \
        auth run run-lite run-all compact recompact enrich refresh flow flow-now \
        duck sql-% \
        schedule-build schedule-up schedule-down schedule-restart schedule-logs schedule-ps \
        schedule-exec schedule-time schedule-init flow-latest clean-logs

# ---------- Variables ----------
DOCKER_COMPOSE ?= docker compose
SERVICE        ?= pull
DATA_DIR       := data
SECRETS_DIR    := secrets
PER_PAGE       ?= 200     # override: make run PER_PAGE=100
DAYS           ?= 21      # kudos lookback window (days). override: make refresh DAYS=7

# Default enrich behaviour: refresh recent details and don't skip due to cushions
# (your enrich.py supports --since-days, --cushion-15min, --cushion-daily)
ENRICH_ARGS    ?= --since-days $(DAYS) --cushion-15min 0 --cushion-daily 0

CRON_FILE      ?= cron/strava.cron
SCHEDULER_SVC  ?= scheduler

# convenience
COMPOSE_RUN := $(DOCKER_COMPOSE) run --rm $(SERVICE)
PYMOD       := python -m strava_stats

# ---------- Help ----------
help: ## Show this help (lists targets with their descriptions)
	@awk 'BEGIN {FS = ":.*##"; \
		printf "\nUsage: make \033[36m<TARGET>\033[0m [VAR=val]\n"} \
	/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0,5); next} \
	/^[a-zA-Z0-9_.-]+:.*##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' \
	$(MAKEFILE_LIST)

##@ Build & Setup
build: ## Build image(s)
	$(DOCKER_COMPOSE) build

ensure-dirs: ## Create local data/secrets dirs if missing
	mkdir -p $(DATA_DIR)/activities \
	         $(DATA_DIR)/bronze/activities \
	         $(DATA_DIR)/bronze/activity_details \
	         $(DATA_DIR)/warehouse \
	         $(SECRETS_DIR)

env-ok: ## Verify .env exists
	@test -f .env || (echo "Missing .env. Copy .env.example to .env"; exit 1)

check: ensure-dirs env-ok ## Pre-flight for most targets
	@true

##@ Auth
auth: check ## Bootstrap/refresh Strava OAuth (writes secrets/strava_token.json)
	$(COMPOSE_RUN) $(PYMOD).auth

##@ Pull
run: check ## Pull new/updated activities (incl. kudos lookback)
	$(COMPOSE_RUN) $(PYMOD).pull --per-page $(PER_PAGE) --refresh-kudos-days $(DAYS)

run-lite: check ## Pull without kudos lookback (fast path)
	$(COMPOSE_RUN) $(PYMOD).pull --refresh-kudos-days 0 --per-page $(PER_PAGE)

run-all: check ## Full refresh (ignore existing Parquet, fetch everything)
	$(COMPOSE_RUN) $(PYMOD).pull --all --per-page $(PER_PAGE)

##@ Compaction
compact: check ## Dedupe & partition shards into gold + refresh DuckDB view
	$(COMPOSE_RUN) $(PYMOD).compact

recompact: ## Re-run compaction only (useful after tweaking compact.py)
	@$(MAKE) compact

##@ Enrichment
enrich: check ## Enrich activities with DetailedActivity (uses ENRICH_ARGS default window + zero cushions)
	@echo "→ Enrich args: $(ENRICH_ARGS)"
	$(COMPOSE_RUN) $(PYMOD).enrich $(ENRICH_ARGS)

##@ Flow
flow: run compact ## Run the entire flow (pull -> compact -> enrich recent details)
	@$(MAKE) enrich

FLOW_ARGS ?=
flow-now: ## Run the full flow now (host) with logging; e.g. make flow-now FLOW_ARGS="--since-days 1"
	./scripts/flow.sh $(FLOW_ARGS)

refresh: ## Build -> run (with kudos lookback) -> compact (one-shot)
	@$(MAKE) build
	@echo "→ Running pull (per_page=$(PER_PAGE), kudos lookback $(DAYS)d)"
	@$(MAKE) run
	@$(MAKE) compact

##@ DuckDB
duck: ## Open DuckDB against the warehouse file
	duckdb $(DATA_DIR)/warehouse/strava.duckdb

sql-%: ## Run a one-liner SQL against the warehouse (usage: make sql-"SELECT count(*) FROM activities;")
	duckdb $(DATA_DIR)/warehouse/strava.duckdb -c $(subst ",\",$*)

##@ Scheduler (cron-in-a-container)
schedule-build: ## Build the scheduler image
	$(DOCKER_COMPOSE) build $(SCHEDULER_SVC)

schedule-up: schedule-init ## Start the scheduler (cron @ 05:00 Europe/London)
	$(DOCKER_COMPOSE) up -d $(SCHEDULER_SVC)

schedule-down: ## Stop the scheduler
	$(DOCKER_COMPOSE) stop $(SCHEDULER_SVC)

schedule-restart: ## Restart the scheduler (use after editing $(CRON_FILE))
	$(DOCKER_COMPOSE) restart $(SCHEDULER_SVC)

schedule-logs: ## Tail scheduler container logs
	$(DOCKER_COMPOSE) logs -f $(SCHEDULER_SVC)

schedule-ps: ## Show scheduler container status
	$(DOCKER_COMPOSE) ps $(SCHEDULER_SVC)

schedule-exec: ## Run one flow inside the scheduler container NOW
	$(DOCKER_COMPOSE) exec $(SCHEDULER_SVC) /bin/sh -lc "/repo/scripts/flow.sh $(FLOW_ARGS)"

schedule-time: ## Print the container's idea of local time (checks TZ)
	$(DOCKER_COMPOSE) exec $(SCHEDULER_SVC) date

schedule-init: ## One-time: create host log dirs so cron appends cleanly
	mkdir -p logs/scheduler

flow-latest: ## Tail the most recent flow log
	tail -f logs/flow/latest.log

clean-logs: ## Prune all logs (plain+JSONL+scheduler)
	rm -rf logs/flow logs/scheduler || true
