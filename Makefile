PYTHON ?= python3
VENV   ?= .venv

.DEFAULT_GOAL := help

.PHONY: help install migrate migrate-status test test-verbose check-db seed demo dashboard clean reset-db fmt lint

help: ## Muestra esta ayuda
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Crea el venv e instala dependencias
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt -r requirements-dev.txt

migrate: ## Crea/actualiza la base de datos
	$(PYTHON) -m app.db.migrations

migrate-status: ## Muestra el estado del esquema
	$(PYTHON) -m app.db.migrations --status

test: ## Corre los tests
	$(PYTHON) -m unittest discover -s tests -t . -q

test-verbose: ## Tests con detalle
	$(PYTHON) -m unittest discover -s tests -t . -v

check-db: ## Verifica que la base existe y usa WAL
	@$(PYTHON) -c "from app.db.connection import journal_mode; m = journal_mode('data/app.db'); print('journal_mode =', m); raise SystemExit(0 if m == 'wal' else 1)"

seed: ## Genera logs sintéticos para el demo
	$(PYTHON) tools/generate_sample_logs.py

dashboard: ## Levanta el dashboard
	streamlit run app/ui/app.py

demo: migrate seed ## Prepara la base y genera datos de ejemplo
	@echo "Listo. Siguiente: make dashboard"

reset-db: ## Borra la base de datos (migraciones y datos)
	rm -f data/app.db data/app.db-wal data/app.db-shm
	@echo "Base eliminada. Ejecuta 'make migrate' para recrearla."

lint: ## Verifica sintaxis de todos los módulos
	$(PYTHON) -m compileall -q app tools tests

clean: ## Elimina cachés
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage