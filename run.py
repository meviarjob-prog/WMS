import os
import socket

from wms import create_app

app = create_app()


def _detect_lan_ip():
    """Лучшая попытка определить LAN IP-адрес компьютера в локальной сети
    (для подсказки, какой адрес открывать на телефоне). Ничего никуда не
    отправляет — используется только для выбора сетевого интерфейса ОС."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    use_https = os.environ.get("WMS_HTTPS", "").strip().lower() in ("1", "true", "yes")
    ssl_context = "adhoc" if use_https else None
    scheme = "https" if use_https else "http"
    port = int(os.environ.get("WMS_PORT", "5000"))

    lan_ip = _detect_lan_ip()

    print("=" * 60)
    print(f"WMS запущен: {scheme}://localhost:{port}")
    if lan_ip:
        print(f"С телефона в этой же Wi-Fi сети: {scheme}://{lan_ip}:{port}")
    if not use_https:
        print(
            "Совет: для сканирования штрихкода камерой телефона браузеру "
            "нужен HTTPS. Запусти с WMS_HTTPS=1, чтобы включить "
            "самоподписанный сертификат для доступа с телефона."
        )
    else:
        print(
            "HTTPS включен (самоподписанный сертификат) — при первом "
            "заходе браузер покажет предупреждение о безопасности, это "
            "нормально: нажми 'Дополнительно' -> 'Все равно перейти'."
        )
    print("=" * 60)

    app.run(host="0.0.0.0", port=port, debug=True, ssl_context=ssl_context)
