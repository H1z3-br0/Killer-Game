# Короткие команды для разработки и эксплуатации.
.PHONY: help install run test check backup clean hooks

PY := .venv/bin/python
PORT ?= 8000

help:           ## Показать список команд
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ —/' | sort

install:        ## Создать окружение и поставить зависимости
	python3 -m venv .venv
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install --quiet -r requirements.txt

run:            ## Запустить сервис (PORT=8000 по умолчанию)
	PORT=$(PORT) ./run.sh

test:           ## Прогнать оба набора тестов
	$(PY) tests/test_flow.py
	$(PY) tests/test_service.py

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
