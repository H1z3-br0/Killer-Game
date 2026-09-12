# Короткие команды для разработки и эксплуатации.
.PHONY: help install install-dev run test lint audit secrets ci check backup clean hooks

PY := .venv/bin/python
PORT ?= 8000

help:           ## Показать список команд
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ —/' | sort

install:        ## Окружение для сервера (только то, что нужно в бою)
	python3 -m venv .venv
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install --quiet -r requirements.txt

install-dev:    ## Окружение разработчика: плюс тесты, линтер, аудит
	python3 -m venv .venv
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install --quiet -r requirements-dev.txt

run:            ## Запустить сервис (PORT=8000 по умолчанию)
	PORT=$(PORT) ./run.sh

test:           ## Прогнать тесты (149 штук)
	$(PY) -m pytest tests/ -q

test-v:         ## Тесты с именами и временем
	$(PY) -m pytest tests/ -v --durations=10

lint:           ## Линтер (ruff): стиль, ошибки, безопасность
	$(PY) -m ruff check .

lint-fix:       ## Линтер с автоисправлением
	$(PY) -m ruff check . --fix

audit:          ## Уязвимости в зависимостях
	$(PY) -m pip_audit --requirement requirements.txt --strict

secrets:        ## Поиск секретов и лишних файлов в репозитории
	@bash scripts/check-secrets.sh
	@bash scripts/check-repo.sh

ci:             ## Всё, что гоняет конвейер: линтер, тесты, секреты, аудит
	@$(MAKE) lint
	@$(MAKE) test
	@$(MAKE) secrets
	@$(MAKE) audit

check:          ## Быстрая проверка синтаксиса всех модулей
	$(PY) -m compileall -q app tests migrations >/dev/null && echo "синтаксис в порядке"

backup:         ## Снять копию базы прямо сейчас
	@mkdir -p data/backups
	@$(PY) -c "import sqlite3, pathlib, datetime; \
	src = pathlib.Path('data/killer.db'); \
	dst = pathlib.Path('data/backups') / ('killer-manual-' + datetime.date.today().isoformat() + '.db'); \
	s = sqlite3.connect(src); d = sqlite3.connect(dst); s.backup(d); d.close(); s.close(); \
	print('копия:', dst)"

hooks:          ## Поставить git-хук, гоняющий тесты перед коммитом
	@cp scripts/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
	@echo "хук установлен: тесты будут гоняться перед каждым коммитом"

clean:          ## Убрать кеши Python
	find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	@echo "кеши убраны"
