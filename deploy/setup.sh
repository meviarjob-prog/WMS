#!/usr/bin/env bash
#
# Разворачивает WMS на чистом Ubuntu-сервере (22.04/24.04) целиком в одну
# команду: nginx + gunicorn + systemd + бесплатный настоящий HTTPS-сертификат
# (Let's Encrypt через certbot, для домена вида <IP>.sslip.io — не требует
# покупки своего домена, sslip.io просто резолвит это имя в IP сервера).
#
# Запуск на свежем сервере (одной командой, по SSH, от root):
#
#   curl -fsSL https://raw.githubusercontent.com/meviarjob-prog/wms/claude/wms-system-python-t1db0u/deploy/setup.sh | bash -s -- you@example.com
#
# Email необязателен (нужен только для писем от Let's Encrypt об истечении
# сертификата, сам сертификат он не ограничивает):
#
#   curl -fsSL https://raw.githubusercontent.com/meviarjob-prog/wms/claude/wms-system-python-t1db0u/deploy/setup.sh | bash
#
# Скрипт безопасно перезапускать повторно — например, чтобы обновить код
# (git pull) и перезапустить сервис после того, как вышло обновление.

set -euo pipefail

REPO_URL="https://github.com/meviarjob-prog/wms.git"
BRANCH="claude/wms-system-python-t1db0u"
APP_DIR="/opt/wms"
APP_USER="wms"
SERVICE_NAME="wms"
ENV_FILE="/etc/wms.env"
EMAIL="${1:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите скрипт от root (или через sudo)." >&2
  exit 1
fi

echo "==> Устанавливаю системные пакеты..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx ufw curl

echo "==> Определяю публичный IP сервера..."
PUBLIC_IP="$(curl -fsSL https://api.ipify.org || curl -fsSL https://ifconfig.me)"
if [ -z "$PUBLIC_IP" ]; then
  echo "Не удалось определить публичный IP сервера." >&2
  exit 1
fi
HOSTNAME_FQDN="${PUBLIC_IP}.sslip.io"
echo "    IP сервера: $PUBLIC_IP"
echo "    Адрес WMS:  https://$HOSTNAME_FQDN"

echo "==> Создаю системного пользователя $APP_USER..."
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> Получаю код приложения..."
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  rm -rf "$APP_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "==> Устанавливаю зависимости Python..."
if [ ! -x "$APP_DIR/.venv/bin/python3" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --upgrade pip --quiet
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" -r "$APP_DIR/deploy/requirements-server.txt"

mkdir -p "$APP_DIR/instance"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Настраиваю переменные окружения..."
if [ ! -f "$ENV_FILE" ]; then
  SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  cat > "$ENV_FILE" <<EOF
WMS_SECRET_KEY=$SECRET_KEY
WMS_BEHIND_PROXY=1
WMS_FORCE_SECURE_COOKIES=1
EOF
  chmod 600 "$ENV_FILE"
  echo "    Создан $ENV_FILE со случайным секретным ключом."
else
  echo "    $ENV_FILE уже существует, оставляю как есть."
fi

echo "==> Настраиваю systemd-сервис..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=WMS (gunicorn)
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8000 --timeout 60 wsgi:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "==> Настраиваю nginx..."
cat > "/etc/nginx/sites-available/${SERVICE_NAME}" <<EOF
server {
    listen 80;
    server_name $HOSTNAME_FQDN;
    client_max_body_size 25m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
ln -sf "/etc/nginx/sites-available/${SERVICE_NAME}" "/etc/nginx/sites-enabled/${SERVICE_NAME}"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx || systemctl restart nginx

echo "==> Открываю порты в файрволе..."
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 80/tcp >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

echo "==> Запускаю WMS..."
systemctl restart "$SERVICE_NAME"
sleep 2

echo "==> Получаю бесплатный HTTPS-сертификат (Let's Encrypt)..."
if [ -n "$EMAIL" ]; then
  certbot --nginx -d "$HOSTNAME_FQDN" --non-interactive --agree-tos --redirect -m "$EMAIL" || \
    echo "!! Не удалось получить сертификат автоматически — сайт пока доступен по http://$HOSTNAME_FQDN. Проверьте, что порт 80 открыт на сервере, и запустите скрипт еще раз."
else
  certbot --nginx -d "$HOSTNAME_FQDN" --non-interactive --agree-tos --redirect --register-unsafely-without-email || \
    echo "!! Не удалось получить сертификат автоматически — сайт пока доступен по http://$HOSTNAME_FQDN. Проверьте, что порт 80 открыт на сервере, и запустите скрипт еще раз."
fi

echo
echo "============================================================"
echo "Готово! WMS доступен по адресу:"
echo "  https://$HOSTNAME_FQDN"
echo
echo "Пароль администратора (показывается только при первом запуске):"
journalctl -u "$SERVICE_NAME" --no-pager 2>/dev/null | grep -A2 "Пароль:" | tail -3 || \
  echo "  (не найден в логах — вероятно, сервис уже запускался раньше; см. journalctl -u $SERVICE_NAME)"
echo "============================================================"
