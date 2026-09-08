#!/usr/bin/env bash
#
# Как backup.sh, но дополнительно отправляет свежий снепшот в отдельный
# приватный git-репозиторий — офсайт-копия, переживающая потерю самого
# сервера (просто локальные файлы в /opt/wms/backups этого не дают).
#
# ВАЖНО: снепшоты — это реальные складские данные, поэтому репозиторий
# должен быть ПРИВАТНЫМ и отдельным от репозитория кода WMS — не пушьте
# базу в тот же репозиторий, откуда деплоится приложение.
#
# Разовая настройка (один раз, на сервере, от root):
#
#   1. Создайте на GitHub новый ПРИВАТНЫЙ пустой репозиторий, например
#      wms-backups (без README/.gitignore — просто пустой).
#
#   2. Сгенерируйте ключ, которым сервер будет писать только в этот
#      репозиторий (отдельный от любых других ключей на сервере):
#        ssh-keygen -t ed25519 -f /root/.ssh/wms_backup_key -N "" -C "wms-backup"
#
#   3. Добавьте публичный ключ в репозиторий: на GitHub — Settings этого
#      репозитория -> Deploy keys -> Add deploy key -> вставьте содержимое
#      /root/.ssh/wms_backup_key.pub -> обязательно поставьте галку
#      "Allow write access".
#
#   4. Склонируйте репозиторий на сервер этим ключом и настройте автора
#      коммитов и сам ключ для этого репозитория (чтобы дальше пуш работал
#      без лишних переменных окружения):
#        GIT_SSH_COMMAND="ssh -i /root/.ssh/wms_backup_key -o IdentitiesOnly=yes" \
#          git clone git@github.com:ВАШ_АККАУНТ/wms-backups.git /opt/wms/backups-git
#        git -C /opt/wms/backups-git config core.sshCommand "ssh -i /root/.ssh/wms_backup_key -o IdentitiesOnly=yes"
#        git -C /opt/wms/backups-git config user.email "wms-backup@localhost"
#        git -C /opt/wms/backups-git config user.name "WMS backup"
#
# После разовой настройки запускайте этот скрипт вместо backup.sh (в т.ч.
# по cron — см. пример в README) — он сделает всё то же, что и backup.sh,
# плюс отправит копию в репозиторий.

set -euo pipefail

APP_DIR="/opt/wms"
GIT_BACKUP_DIR="$APP_DIR/backups-git"
# В git-репозитории храним меньше копий, чем локально в backups/ — иначе
# репозиторий будет расти без остановки: git не умеет сжимать разные
# бинарные снепшоты между собой, каждый коммит добавляет полный вес файла.
KEEP_IN_GIT=14

bash "$APP_DIR/deploy/backup.sh"

if [ ! -d "$GIT_BACKUP_DIR/.git" ]; then
  echo "!! $GIT_BACKUP_DIR не настроен как git-репозиторий — см. инструкцию в начале этого файла." >&2
  echo "!! Локальная копия на сервере создана (backup.sh отработал), но в git не отправлена." >&2
  exit 1
fi

LATEST="$(ls -t "$APP_DIR"/backups/wms_*.db | head -1)"
cp "$LATEST" "$GIT_BACKUP_DIR/$(basename "$LATEST")"

cd "$GIT_BACKUP_DIR"
ls -t wms_*.db 2>/dev/null | tail -n "+$((KEEP_IN_GIT + 1))" | xargs -r rm -f

git add -A
if git diff --cached --quiet; then
  echo "Изменений нет — пуш не нужен."
  exit 0
fi
git commit -m "backup $(basename "$LATEST" .db)" --quiet
git push --quiet
echo "Отправлено в git: $(basename "$LATEST")"
