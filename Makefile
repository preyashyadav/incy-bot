.DEFAULT_GOAL := help
PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help venv install db-up db-down db-reset migrate revision index dev api worker slack lint fmt type test test-live check clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	python3 -m venv .venv

install: venv ## Install the package and dev dependencies
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"

db-up: ## Start Postgres
	docker compose up -d postgres
	@echo "waiting for postgres…"
	@until docker compose exec -T postgres pg_isready -U copilot -d copilot >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready"

db-down: ## Stop Postgres
	docker compose down

db-reset: ## Destroy and recreate the database volume
	docker compose down -v
	$(MAKE) db-up
	$(MAKE) migrate
	$(MAKE) index

migrate: ## Apply migrations
	$(PY) -m alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add incidents"
	$(PY) -m alembic revision --autogenerate -m "$(m)"

index: ## Rebuild the runbook and incident-history search index
	$(PY) -m incident_copilot.retrieval

# awk with an explicit fflush, not sed: sed block-buffers whenever its output is not a
# terminal, which makes `make dev | tee` or a redirected log look dead for the first few seconds.
dev: ## Run api + worker + slack together, prefixed output (Ctrl-C stops all)
	@echo "api :8000 · worker · slack (socket mode) — Ctrl-C stops all"
	@trap 'kill 0' EXIT INT TERM; \
	  PYTHONUNBUFFERED=1 $(PY) -m uvicorn incident_copilot.api.main:app --port 8000 2>&1 | awk '{ print "[api]   ", $$0; fflush() }' & \
	  PYTHONUNBUFFERED=1 $(PY) -m incident_copilot.jobs.worker 2>&1 | awk '{ print "[worker]", $$0; fflush() }' & \
	  PYTHONUNBUFFERED=1 $(PY) -m incident_copilot.slack.app 2>&1 | awk '{ print "[slack] ", $$0; fflush() }' & \
	  wait

api: ## Run the API with reload
	$(PY) -m uvicorn incident_copilot.api.main:app --reload --port 8000

worker: ## Run the job worker
	$(PY) -m incident_copilot.jobs.worker

slack: ## Run the Slack app in Socket Mode (needs SLACK_APP_TOKEN)
	$(PY) -m incident_copilot.slack.app

lint: ## Lint
	$(PY) -m ruff check incident_copilot tests

fmt: ## Format
	$(PY) -m ruff format incident_copilot tests
	$(PY) -m ruff check --fix incident_copilot tests

type: ## Type-check
	$(PY) -m mypy

test: ## Run tests (excludes live-API tests)
	$(PY) -m pytest -q

test-live: ## Run the live-model tests (costs money, needs ANTHROPIC_API_KEY)
	$(PY) -m pytest -m live -v

check: lint type test ## Everything CI runs

clean: ## Remove caches
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
