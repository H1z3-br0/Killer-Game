#!/usr/bin/env bash
# Проверяет, что в репозиторий не попало то, чему там не место:
# база с игроками, окружение, кеши, локальные настройки.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

forbidden='(^|/)(\.venv/|data/killer|\.env$|__pycache__/|\.DS_Store$)|\.(db|db-wal|db-shm|sqlite3|log)$'
found=$(git ls-files | grep -nE "$forbidden" || true)

if [ -n "$found" ]; then
  echo "В репозитории есть лишние файлы:"
  echo "$found"
  echo
  echo "База игроков, окружение и логи не версионируются — см. .gitignore"
  exit 1
fi

# Шрифты нужны (сервер офлайн), но следим, чтобы репозиторий не разбухал.
# Ошибки du не важны: это оценка, а не проверка целостности.
# head закрывает пайп раньше времени, поэтому pipefail здесь мешает.
set +o pipefail
size_kb=$(git ls-files -z | xargs -0 du -ck 2>/dev/null | tail -1 | cut -f1)
set -o pipefail
size_kb=${size_kb:-0}
if [ "${size_kb:-0}" -gt 20000 ]; then
  echo "Репозиторий вырос до ${size_kb} КБ — проверьте, что попало внутрь"
  exit 1
fi

echo "репозиторий чист: $(git ls-files | wc -l | tr -d ' ') файлов, ${size_kb} КБ"
