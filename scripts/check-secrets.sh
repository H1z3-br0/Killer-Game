#!/usr/bin/env bash
# Грубая проверка на секреты — на случай, если gitleaks недоступен
# (например, в локальном GitLab без доступа в интернет).
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

patterns=(
  'BEGIN (RSA|OPENSSH|DSA|EC|PGP) PRIVATE KEY'
  'AKIA[0-9A-Z]{16}'                      # ключ AWS
  'ghp_[A-Za-z0-9]{30,}'                  # токен GitHub
  'xox[baprs]-[A-Za-z0-9-]{10,}'          # токен Slack
  '[0-9]{8,10}:AA[A-Za-z0-9_-]{33}'       # токен Telegram
  '(password|passwd|secret|token|api_key)[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"']{8,}'
)

found=0
for p in "${patterns[@]}"; do
  hits=$(git grep -InE "$p" -- ':!scripts/check-secrets.sh' ':!tests/' || true)
  if [ -n "$hits" ]; then
    echo "Похоже на секрет ($p):"
    echo "$hits"
    found=1
  fi
done

[ "$found" -eq 0 ] && echo "секретов не найдено"
exit "$found"
