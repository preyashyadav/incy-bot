.DEFAULT_GOAL := help
PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help venv install db-up db-down db-reset migrate revision api worker lint fmt type test check clean

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

migrate: ## Apply migrations
	$(PY) -m alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add incidents"
	$(PY) -m alembic revision --autogenerate -m "$(m)"

api: ## Run the API with reload
	$(PY) -m uvicorn incident_copilot.api.main:app --reload --port 8000

worker: ## Run the job worker
	$(PY) -m incident_copilot.jobs.worker

lint: ## Lint
	$(PY) -m ruff check incident_copilot tests

fmt: ## Format
	$(PY) -m ruff format incident_copilot tests
	$(PY) -m ruff check --fix incident_copilot tests

type: ## Type-check
	$(PY) -m mypy

test: ## Run tests (excludes live-API tests)
	$(PY) -m pytest -q

check: lint type test ## Everything CI runs

clean: ## Remove caches
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
