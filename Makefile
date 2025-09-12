.DEFAULT_GOAL := help

.PHONY: help build ensure-dirs auth run run-lite run-all compact refresh recompact duck sql-% enrich

# ---------- Variables ----------
SERVICE      ?= pull
DATA_DIR     := data
SECRETS_DIR  := secrets
PER_PAGE     ?= 200     # override: make run PER_PAGE=100
DAYS         ?= 21      # kudos lookback window (days). override: make refresh DAYS=7
ENRICH_ARGS  ?=

# ---------- Help ----------
help: ## Show this help (lists targets with their descriptions)
	@awk 'BEGIN {FS = ":.*##"; \
		printf "\nUsage: make \033[36m<TARGET>\033[0m [VAR=val]\n"} \
	/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0,5); next} \
	/^[a-zA-Z0-9_.-]+:.*##/ {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' \
	$(MAKEFILE_LIST)

##@ Build & Auth
build: ## Build image(s)
	docker compose build

ensure-dirs: ## Create local data/secrets dirs if missing
	@mkdir -p $(DATA_DIR)/activities $(DATA_DIR)/bronze/activities $(DATA_DIR)/bronze/activity_details $(DATA_DIR)/warehouse $(SECRETS_DIR)

auth: ensure-dirs ## Bootstrap/refresh Strava OAuth (writes secrets/strava_token.json)
	@test -f .env || (echo "Missing .env. Copy .env.example to .env"; exit 1)
	docker compose run --rm $(SERVICE) \
		python -m strava_stats.auth

##@ Pull
run: ensure-dirs ## Pull new/updated activities (incl. kudos lookback)
	@test -f .env || (echo "Missing .env. Copy .env.example to .env"; exit 1)
	docker compose run --rm $(SERVICE) \
		python -m strava_stats.pull --per-page $(PER_PAGE) --refresh-kudos-days $(DAYS)

run-lite: ensure-dirs ## Pull without kudos lookback (fast path)
	@test -f .env || (echo "Missing .env. Copy .env.example to .env"; exit 1)
	docker compose run --rm $(SERVICE) \
		python -m strava_stats.pull --refresh-kudos-days 0 --per-page $(PER_PAGE)

run-all: ensure-dirs ## Full refresh (ignore existing Parquet, fetch everything)
	@test -f .env || (echo "Missing .env. Copy .env.example to .env"; exit 1)
	docker compose run --rm $(SERVICE) \
		python -m strava_stats.pull --all --per-page $(PER_PAGE)

##@ Enrichment
enrich: ensure-dirs ## Enrich activities with DetailedActivity (flags via ENRICH_ARGS)
	@test -f .env || (echo "Missing .env. Copy .env.example to .env"; exit 1)
	docker compose run --rm $(SERVICE) \
		python -m strava_stats.enrich $(ENRICH_ARGS)

##@ Compaction
compact: ensure-dirs ## Dedupe & partition shards into gold + refresh DuckDB view
	@mkdir -p $(DATA_DIR)/warehouse
	@test -f .env || (echo "Missing .env. Copy .env.example to .env"; exit 1)
	docker compose run --rm $(SERVICE) \
		python -m strava_stats.compact

recompact: ## Re-run compaction only (useful after tweaking compact.py)
	@$(MAKE) compact

##@ Convenience
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
