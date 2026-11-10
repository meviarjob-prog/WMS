#!/usr/bin/env bash
#
# Настраивает ОТДЕЛЬНЫЙ чистый Ubuntu-сервер (22.04/24.04) под статичный
# лендинг: nginx + бесплатный HTTPS-сертификат (Let's Encrypt). Ничего общего
# с боевой WMS — запускать на другом сервере (см. docs/commercial/07-publish-landing.md).
#
# Порядок:
#   1. В DNS домена создайте A-запись на IP нового сервера (и, если нужен
#      адрес с www, вторую запись www -> тот же IP). Дождитесь, пока она
#      заработает (обычно несколько минут), иначе сертификат не выдастся.
#   2. С вашего компьютера загрузите файлы лендинга (папку landing/ из репозитория):
#        scp -r landing/* root@IP_СЕРВЕРА:/var/www/landing/
#      (папку на сервере создаст этот скрипт; если scp ругается на отсутствие
#      папки, сначала выполните на сервере: mkdir -p /var/www/landing)
#   3. На сервере от root:
#        bash landing_setup.sh ваш-домен.ru you@example.com
#      Для домена вместе с www:  WWW=1 bash landing_setup.sh ваш-домен.ru you@example.com
#
# Скрипт безопасно запускать повторно (например, после обновления файлов
# сертификат и конфиг не пересоздаются без необходимости).

set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
WWW="${WWW:-0}"
SITE_DIR="/var/www/landing"
SITE_NAME="landing"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите скрипт от root (или через sudo)." >&2
  exit 1
fi
if [ -z "$DOMAIN" ]; then
  echo "Использование: bash landing_setup.sh ваш-домен.ru [email]" >&2
  exit 1
fi

echo "==> Устанавливаю nginx и certbot..."
apt-get update -qq
apt-get install -y nginx certbot python3-certbot-nginx ufw curl

mkdir -p "$SITE_DIR"
if [ ! -f "$SITE_DIR/index.html" ]; then
  echo "!! В $SITE_DIR нет index.html. Сначала загрузите файлы лендинга:" >&2
  echo "   scp -r landing/* root@IP_СЕРВЕРА:$SITE_DIR/" >&2
  exit 1
fi

SERVER_NAMES="$DOMAIN"
CERT_DOMAINS=(-d "$DOMAIN")
if [ "$WWW" = "1" ]; then
  SERVER_NAMES="$DOMAIN www.$DOMAIN"
  CERT_DOMAINS+=(-d "www.$DOMAIN")
fi

echo "==> Настраиваю nginx для $SERVER_NAMES ..."
cat > "/etc/nginx/sites-available/$SITE_NAME" <<EOF
server {
    listen 80;
    server_name $SERVER_NAMES;
    root $SITE_DIR;
    index index.html;

    gzip on;
    gzip_types text/css application/javascript image/svg+xml;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    location / {
        try_files \$uri \$uri/ =404;
    }
    location /img/ {
        expires 7d;
        add_header Cache-Control "public";
    }
}
EOF
ln -sf "/etc/nginx/sites-available/$SITE_NAME" "/etc/nginx/sites-enabled/$SITE_NAME"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx || systemctl restart nginx

echo "==> Открываю порты в файрволе..."
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 80/tcp >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

echo "==> Получаю бесплатный HTTPS-сертификат (Let's Encrypt)..."
CERTBOT_CMD=(certbot --nginx "${CERT_DOMAINS[@]}" --non-interactive --agree-tos --redirect)
if [ -n "$EMAIL" ]; then
  CERTBOT_CMD+=(-m "$EMAIL")
else
  CERTBOT_CMD+=(--register-unsafely-without-email)
fi
if ! "${CERTBOT_CMD[@]}"; then
  echo "!! Сертификат не выдан. Чаще всего причина — DNS-запись домена ещё не указывает на этот сервер." >&2
  echo "   Проверьте:  dig +short $DOMAIN   (должен вернуть IP этого сервера), затем запустите скрипт снова." >&2
  exit 1
fi

echo
echo "Готово: https://$DOMAIN"
echo "Обновить сайт позже: scp -r landing/* root@IP_СЕРВЕРА:$SITE_DIR/  (перезапуск не нужен)"
