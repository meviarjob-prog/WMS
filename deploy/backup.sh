#!/usr/bin/env bash
#
# Резервная копия базы WMS (sqlite) на сервере.
#
# Пока сервис работает, файл instance/wms.db может быть открыт "на запись" —
# обычный cp/scp в этот момент рискует скопировать файл в противоречивом
# состоянии (посреди транзакции). Поэтому копируем через встроенный в python3
# sqlite3.Connection.backup() — тот же механизм, что использует официальная
# команда `sqlite3 .backup`: он безопасен при параллельной записи и не
# требует останавливать сервис.
#
# Использование (на сервере, от root или из-под пользователя wms):
#   bash deploy/backup.sh
#
# Ежедневный автозапуск — добавить в crontab (`crontab -e` от root):
#   0 3 * * * /opt/wms/deploy/backup.sh >> /var/log/wms-backup.log 2>&1

set -euo pipefail

APP_DIR="/opt/wms"
DB_PATH="$APP_DIR/instance/wms.db"
BACKUP_DIR="$APP_DIR/backups"
KEEP_DAYS=30

if [ ! -f "$DB_PATH" ]; then
  echo "!! База не найдена: $DB_PATH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
DEST="$BACKUP_DIR/wms_${STAMP}.db"

python3 - "$DB_PATH" "$DEST" <<'PYEOF'
import sqlite3
import sys

src_path, dest_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
dest = sqlite3.connect(dest_path)
with dest:
    src.backup(dest)
src.close()
dest.close()
PYEOF

echo "Резервная копия создана: $DEST ($(du -h "$DEST" | cut -f1))"

# Удаляем копии старше KEEP_DAYS дней — иначе backups/ будет расти бесконечно.
find "$BACKUP_DIR" -name 'wms_*.db' -type f -mtime "+$KEEP_DAYS" -delete

echo "Хранится копий: $(find "$BACKUP_DIR" -name 'wms_*.db' -type f | wc -l) (за последние $KEEP_DAYS дн.)"
