.PHONY: build ensure-dirs auth run run-all

SERVICE ?= pull
OUTPUT_DIR := output
DATA_DIR   := data
SECRETS_DIR := secrets
PER_PAGE  ?= 200

ensure-dirs:
	@mkdir -p $(OUTPUT_DIR) $(DATA_DIR)/activities $(SECRETS_DIR)

build:
	docker compose build

auth: ensure-dirs
	@test -f .env || (echo "Missing .env. Copy .env.example to .env"; exit 1)
	docker compose run --rm $(SERVICE) \
		python -m strava_stats.auth

run: ensure-dirs
	@test -f .env || (echo "Missing .env. Copy .env.example to .env"; exit 1)
	docker compose run --rm $(SERVICE) \
		python -m strava_stats.pull --per-page $(PER_PAGE)

run-all: ensure-dirs
	@test -f .env || (echo "Missing .env. Copy .env.example to .env"; exit 1)
	docker compose run --rm $(SERVICE) \
		python -m strava_stats.pull --all --per-page $(PER_PAGE)
