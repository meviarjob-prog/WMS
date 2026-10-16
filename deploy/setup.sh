#!/usr/bin/env bash
#
# Разворачивает WMS на чистом Ubuntu-сервере (22.04/24.04) целиком в одну
# команду: nginx + gunicorn + systemd + бесплатный настоящий HTTPS-сертификат
# (Let's Encrypt через certbot).
#
# По умолчанию домен вида <IP>.sslip.io — не требует покупки своего домена,
# sslip.io просто резолвит это имя в IP сервера:
#
#   curl -fsSL https://raw.githubusercontent.com/meviarjob-prog/WMS/claude/wms-system-python-t1db0u/deploy/setup.sh | bash -s -- you@example.com
#
# Email необязателен (нужен только для писем от Let's Encrypt об истечении
# сертификата, сам сертификат он не ограничивает):
#
#   curl -fsSL https://raw.githubusercontent.com/meviarjob-prog/WMS/claude/wms-system-python-t1db0u/deploy/setup.sh | bash
#
# Если есть свой домен, подключенный к Cloudflare (например, чтобы обойти
# блокировку IP хостинг-провайдера у некоторых операторов) — задайте
# WMS_DOMAIN и WMS_CF_API_TOKEN переменными окружения. Токен — с правом
# "Zone:DNS:Edit" на этот домен (dash.cloudflare.com → My Profile →
# API Tokens → Create Token → шаблон "Edit zone DNS", Zone Resources —
# выбрать нужный домен). Скрипт сам создаст/обновит A-запись на IP этого
# сервера (с включенным проксированием) и получит сертификат через
# DNS-01 challenge (не требует прямого доступа к порту 80 — работает,
# даже если сам порт 80 сервера кем-то заблокирован):
#
#   curl -fsSL .../setup.sh | WMS_DOMAIN=wms.example.com WMS_CF_API_TOKEN=xxxx bash -s -- you@example.com
#
# После этого в дашборде Cloudflare (SSL/TLS → Overview) один раз
# переключите режим шифрования на "Full" или "Full (strict)" — иначе
# при "Flexible" будет петля редиректов (см. README).
#
# Скрипт безопасно перезапускать повторно — например, чтобы обновить код
# (git pull) и перезапустить сервис после того, как вышло обновление.

set -euo pipefail

REPO_URL="https://github.com/meviarjob-prog/WMS.git"
# Ветка для установки; для тестового сервера можно задать другую:
#   curl -fsSL .../setup.sh | WMS_BRANCH=claude/project-thread-19dtys bash
BRANCH="${WMS_BRANCH:-claude/wms-system-python-t1db0u}"
APP_DIR="/opt/wms"
APP_USER="wms"
SERVICE_NAME="wms"
ENV_FILE="/etc/wms.env"
EMAIL="${1:-}"
DOMAIN="${WMS_DOMAIN:-}"
CF_API_TOKEN="${WMS_CF_API_TOKEN:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите скрипт от root (или через sudo)." >&2
  exit 1
fi

if [ -n "$DOMAIN" ] && [ -z "$CF_API_TOKEN" ]; then
  echo "WMS_DOMAIN задан, но WMS_CF_API_TOKEN не задан — нужен токен Cloudflare" >&2
  echo "с правом Zone:DNS:Edit для этого домена (см. комментарий в начале скрипта)." >&2
  exit 1
fi

# Проверяет, что ответ Cloudflare API — валидный JSON с success:true, и
# печатает значение по указанному пути (например, result.0.id). Останавливает
# скрипт (set -e) с понятной ошибкой при любом сбое API — без этой проверки
# ошибка Cloudflare (HTTP 200, но success:false) осталась бы незамеченной.
#
# Сам скрипт-парсер записан во временный файл, а не передан питону через
# heredoc на stdin — если сделать `python3 - <<PYEOF`, heredoc становится
# stdin ИМЕННО для чтения текста программы (из-за "-"), и на сам JSON,
# который должен туда же прийти через пайп (cf_extract вызывается как
# `echo "$JSON" | cf_extract path`), stdin уже не остается.
CF_EXTRACT_PY="$(mktemp)"
cat > "$CF_EXTRACT_PY" <<'PYEOF'
import sys, json
path = sys.argv[1]
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError as e:
    print(f"!! Cloudflare API: не-JSON ответ ({e})", file=sys.stderr)
    sys.exit(1)
if not data.get("success", False):
    print(f"!! Ошибка Cloudflare API: {data.get('errors')}", file=sys.stderr)
    sys.exit(1)
cur = data
for part in path.split("."):
    if isinstance(cur, list):
        cur = cur[int(part)] if part.isdigit() and int(part) < len(cur) else None
    elif isinstance(cur, dict):
        cur = cur.get(part)
    else:
        cur = None
    if cur is None:
        break
print(cur if cur is not None else "")
PYEOF

cf_extract() {
  python3 "$CF_EXTRACT_PY" "$1"
}

echo "==> Устанавливаю системные пакеты..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx python3-certbot-dns-cloudflare ufw curl

echo "==> Определяю публичный IP сервера..."
PUBLIC_IP="$(curl -fsSL https://api.ipify.org || curl -fsSL https://ifconfig.me)"
if [ -z "$PUBLIC_IP" ]; then
  echo "Не удалось определить публичный IP сервера." >&2
  exit 1
fi
echo "    IP сервера: $PUBLIC_IP"

if [ -n "$DOMAIN" ]; then
  HOSTNAME_FQDN="$DOMAIN"
  echo "    Адрес WMS:  https://$HOSTNAME_FQDN (собственный домен через Cloudflare)"

  echo "==> Настраиваю DNS-запись в Cloudflare для $DOMAIN..."
  ZONE_NAME="$(echo "$DOMAIN" | awk -F. '{print $(NF-1)"."$NF}')"
  ZONE_ID="$(curl -fsSL "https://api.cloudflare.com/client/v4/zones?name=$ZONE_NAME" \
    -H "Authorization: Bearer $CF_API_TOKEN" | cf_extract 'result.0.id')"
  if [ -z "$ZONE_ID" ]; then
    echo "!! Не нашел зону $ZONE_NAME в Cloudflare для этого токена — проверьте токен и что домен добавлен в аккаунт." >&2
    exit 1
  fi

  EXISTING_RECORD_ID="$(curl -fsSL "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records?type=A&name=$DOMAIN" \
    -H "Authorization: Bearer $CF_API_TOKEN" | cf_extract 'result.0.id')"

  RECORD_PAYLOAD="{\"type\":\"A\",\"name\":\"$DOMAIN\",\"content\":\"$PUBLIC_IP\",\"ttl\":1,\"proxied\":true}"
  if [ -n "$EXISTING_RECORD_ID" ]; then
    curl -fsSL -X PUT "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records/$EXISTING_RECORD_ID" \
      -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" \
      --data "$RECORD_PAYLOAD" | cf_extract 'success' >/dev/null
    echo "    Обновил A-запись $DOMAIN -> $PUBLIC_IP (проксирование включено)"
  else
    curl -fsSL -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records" \
      -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" \
      --data "$RECORD_PAYLOAD" | cf_extract 'success' >/dev/null
    echo "    Создал A-запись $DOMAIN -> $PUBLIC_IP (проксирование включено)"
  fi

  mkdir -p /etc/letsencrypt
  cat > /etc/letsencrypt/cloudflare.ini <<EOF
dns_cloudflare_api_token = $CF_API_TOKEN
EOF
  chmod 600 /etc/letsencrypt/cloudflare.ini
else
  HOSTNAME_FQDN="${PUBLIC_IP}.sslip.io"
  echo "    Адрес WMS:  https://$HOSTNAME_FQDN"
fi

echo "==> Создаю системного пользователя $APP_USER..."
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> Получаю код приложения..."
# После первого запуска $APP_DIR принадлежит системному пользователю wms
# (chown ниже), а git-команды здесь выполняются от root — начиная с
# git 2.35.2 это considered "dubious ownership" и git отказывается
# работать с репозиторием без явного разрешения. Разрешаем один раз;
# повторный вызов --add идемпотентен (не дублирует запись).
git config --global --add safe.directory "$APP_DIR"
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
if [ -n "$DOMAIN" ]; then
  CERTBOT_CMD=(certbot -i nginx -a dns-cloudflare
    --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini
    --dns-cloudflare-propagation-seconds 15
    -d "$HOSTNAME_FQDN" --non-interactive --agree-tos --redirect)
else
  CERTBOT_CMD=(certbot --nginx -d "$HOSTNAME_FQDN" --non-interactive --agree-tos --redirect)
fi
if [ -n "$EMAIL" ]; then
  CERTBOT_CMD+=(-m "$EMAIL")
else
  CERTBOT_CMD+=(--register-unsafely-without-email)
fi

"${CERTBOT_CMD[@]}" || \
  echo "!! Не удалось получить сертификат автоматически — сайт пока доступен по http://$HOSTNAME_FQDN. Проверьте DNS/токен Cloudflare (для своего домена) или что порт 80 открыт на сервере (для sslip.io), и запустите скрипт еще раз."

echo
echo "============================================================"
echo "Готово! WMS доступен по адресу:"
echo "  https://$HOSTNAME_FQDN"
echo
echo "Пароль администратора (показывается только при первом запуске):"
journalctl -u "$SERVICE_NAME" --no-pager 2>/dev/null | grep -A2 "Пароль:" | tail -3 || \
  echo "  (не найден в логах — вероятно, сервис уже запускался раньше; см. journalctl -u $SERVICE_NAME)"
echo "============================================================"
