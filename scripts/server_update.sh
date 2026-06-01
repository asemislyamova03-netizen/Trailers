#!/usr/bin/env bash
# Обновление Trailers на сервере. Запускать с локальной машины, где настроен SSH:
#   bash scripts/server_update.sh
#   bash scripts/server_update.sh /var/www/Trailers trailers.service

set -euo pipefail

if [[ -f "${SSH_KEY:-}" ]]; then
  :
elif [[ -f "/c/keys/lightsail_frankfurt.pem" ]]; then
  SSH_KEY="/c/keys/lightsail_frankfurt.pem"
elif [[ -f "$HOME/../keys/lightsail_frankfurt.pem" ]]; then
  SSH_KEY="$HOME/../keys/lightsail_frankfurt.pem"
else
  echo "Укажите ключ: SSH_KEY=/path/to/lightsail_frankfurt.pem"
  exit 1
fi
SSH_OPTS=(-i "$SSH_KEY" -o ConnectTimeout=25 -o StrictHostKeyChecking=accept-new)
SERVER="${SERVER:-ubuntu@3.67.82.83}"
REMOTE_DIR="${1:-}"
SERVICE_NAME="${2:-}"
BRANCH="${BRANCH:-crm-roles-production-logistics}"

run_remote() {
  ssh "${SSH_OPTS[@]}" "$SERVER" "$@"
}

if [[ -z "$REMOTE_DIR" ]]; then
  echo "Ищем каталог проекта на сервере..."
  REMOTE_DIR="$(run_remote 'for d in /var/www/Trailers /opt/Trailers /root/Trailers /home/*/Trailers; do
    [ -f "$d/views.py" ] && echo "$d" && exit 0
  done
  find /var/www /opt /root -maxdepth 3 -name views.py 2>/dev/null | head -1 | xargs dirname 2>/dev/null')" || true
fi

if [[ -z "$REMOTE_DIR" ]]; then
  echo "Не найден каталог проекта. Укажите вручную:"
  echo "  bash scripts/server_update.sh /path/to/Trailers [service-name]"
  exit 1
fi

echo "Сервер: $SERVER"
echo "Каталог: $REMOTE_DIR"
echo "Ветка: $BRANCH"

if [[ -z "$SERVICE_NAME" ]]; then
  SERVICE_NAME="$(run_remote "systemctl list-units --type=service --all 2>/dev/null | grep -ioE '[a-z0-9@.-]*trail[a-z0-9@.-]*\.service' | head -1 | sed 's/\.service//'" || true)"
fi

run_remote bash -s <<REMOTE_SCRIPT
set -euo pipefail
cd "$REMOTE_DIR"

if [ -f trailers.db ]; then
  mkdir -p backups
  cp trailers.db "backups/trailers_before_update_\$(date +%Y%m%d_%H%M%S).db"
  echo "Backup БД создан в backups/"
fi

if [ -d .git ]; then
  git fetch origin
  git checkout "$BRANCH"
  git pull origin "$BRANCH"
else
  echo "WARNING: нет .git — обновите views.py вручную (git pull невозможен)"
  exit 1
fi

if [ -d venv ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
elif [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if [ -f requirements.txt ]; then
  pip install -q -r requirements.txt
fi

export FLASK_APP=app.py
echo "Миграции:"
flask db current || true
flask db heads || true
flask db upgrade

SERVICE="$SERVICE_NAME"
if [ -n "\$SERVICE" ] && systemctl list-unit-files "\$SERVICE.service" &>/dev/null; then
  systemctl restart "\$SERVICE"
  systemctl --no-pager status "\$SERVICE" | head -15
else
  echo "Сервис не указан или не найден. Перезапустите приложение вручную."
  echo "Подсказка: systemctl list-units --type=service | grep -i trail"
fi

echo "Готово. Коммит на сервере:"
git log -1 --oneline
REMOTE_SCRIPT

echo "Проверьте сайт в браузере."
