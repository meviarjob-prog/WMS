"""Единая точка входа для desktop-версии WMS (запускается как .exe).

Делает две вещи одновременно:
1. Поднимает сам WMS-сервер локально.
2. Запускает Cloudflare Tunnel (готовый бинарник cloudflared) и печатает
   выданную им настоящую HTTPS-ссылку — без неё браузер не даст доступ к
   камере телефона для сканирования.

По умолчанию — quick tunnel (https://....trycloudflare.com), адрес
меняется при каждом перезапуске программы, настройка не нужна. Если
рядом с .exe лежит файл cloudflared_token.txt (токен именованного
туннеля из дашборда Cloudflare) — используется он, и ссылка становится
постоянной (не меняется между запусками). См. deploy/build_exe.md,
раздел «Постоянная ссылка».

Обычный `python run.py` (или собранный из него .exe) для этого не подходит:
там нужно самому поднимать второй процесс (cloudflared) в отдельном окне.
Здесь всё в одном: двойной клик по .exe — и сразу готовая ссылка с камерой.

Сборка в .exe — см. deploy/build_exe.md.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import urlparse

from wms import create_app

TUNNEL_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


def _find_cloudflared():
    """Ищет бинарник cloudflared — сначала среди файлов, вшитых в .exe,
    потом рядом со скриптом (при обычном запуске из исходников), потом
    в PATH (если пользователь установил его сам, например через winget)."""
    exe_name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    candidates = []

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, exe_name))
        candidates.append(os.path.join(os.path.dirname(sys.executable), exe_name))
    else:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), exe_name))

    found_in_path = shutil.which(exe_name)
    if found_in_path:
        candidates.append(found_in_path)

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _read_sidecar_file(filename):
    """Ищет файл рядом с .exe (или со скриптом при запуске из исходников)
    и возвращает его содержимое (одна строка) без пробелов, либо None."""
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    path = os.path.join(base_dir, filename)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        value = f.read().strip()
    return value or None


def _find_tunnel_token():
    """Токен именованного (постоянного) туннеля Cloudflare — если задан,
    ссылка не меняется при перезапуске программы (в отличие от обычного
    quick tunnel). Берется из переменной окружения WMS_TUNNEL_TOKEN или
    файла cloudflared_token.txt рядом с .exe (см. deploy/build_exe.md)."""
    return os.environ.get("WMS_TUNNEL_TOKEN") or _read_sidecar_file("cloudflared_token.txt")


def _find_public_url():
    """Адрес, который выводится в консоли и открывается в браузере при
    использовании именованного туннеля (сам cloudflared его не печатает,
    в отличие от quick tunnel — адрес настраивается один раз в дашборде
    Cloudflare). Из WMS_PUBLIC_URL или файла cloudflared_url.txt."""
    return os.environ.get("WMS_PUBLIC_URL") or _read_sidecar_file("cloudflared_url.txt")


def _wait_for_dns(url, timeout=15):
    """cloudflared печатает ссылку в консоль в момент, когда туннель уже
    поднят у него самого, но публичный DNS для этого только что созданного
    поддомена начинает её резолвить с задержкой в несколько секунд — иначе
    браузер при автооткрытии сразу показывает "Не удалось найти IP-адрес
    сервера" (ERR_NAME_NOT_RESOLVED), хотя ссылка на самом деле рабочая.
    Ждем, пока имя реально начнет резолвиться, прежде чем открывать браузер."""
    hostname = urlparse(url).hostname
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.gethostbyname(hostname)
            return True
        except socket.gaierror:
            time.sleep(1)
    return False


def _run_server(app, host, port):
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


def _start_tunnel(cloudflared_path, local_port, on_url_found):
    """Запускает cloudflared quick tunnel и в фоновом потоке вызывает
    on_url_found(url), как только адрес появится в его логах. Не блокирует
    вызывающий поток — процесс cloudflared работает (и пишет логи) все
    время, пока программа запущена, поэтому чтение его вывода должно жить
    в отдельном потоке, а не останавливать основной."""
    process = subprocess.Popen(
        [cloudflared_path, "tunnel", "--url", f"http://127.0.0.1:{local_port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def _read_output():
        found = False
        for line in process.stdout:
            if not found:
                match = TUNNEL_URL_RE.search(line)
                if match:
                    found = True
                    on_url_found(match.group(0))

    threading.Thread(target=_read_output, daemon=True).start()
    return process


def _start_named_tunnel(cloudflared_path, token):
    """Запускает постоянный (именованный) туннель Cloudflare по токену —
    в отличие от quick tunnel, адрес заранее настроен в дашборде
    Cloudflare (Zero Trust → Networks → Tunnels) и не меняется между
    запусками программы."""
    return subprocess.Popen(
        [cloudflared_path, "tunnel", "run", "--token", token],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    port = int(os.environ.get("WMS_PORT", "5000"))
    app = create_app()

    server_thread = threading.Thread(target=_run_server, args=(app, "0.0.0.0", port), daemon=True)
    server_thread.start()
    time.sleep(1)  # даем серверу подняться перед тем, как к нему подключится туннель

    print("=" * 60)
    print(f"WMS запущен локально: http://localhost:{port}")

    cloudflared_path = _find_cloudflared()
    if not cloudflared_path:
        print(
            "!! cloudflared не найден — публичная HTTPS-ссылка с камерой "
            "недоступна. Работает только локальный адрес выше (без камеры "
            "с телефона, если он не в этой же Wi-Fi сети)."
        )
        print("=" * 60)
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
        return

    token = _find_tunnel_token()
    if token:
        public_url = _find_public_url()
        print("Поднимаю постоянную ссылку (именованный Cloudflare Tunnel)...")
        print("=" * 60)
        tunnel_process = _start_named_tunnel(cloudflared_path, token)
        time.sleep(3)  # даем туннелю время подключиться перед проверкой/открытием
        print("=" * 60)
        print("Готово! Ссылка постоянная — не меняется при перезапуске программы.")
        if public_url:
            print(f"  {public_url}")
            _wait_for_dns(public_url)
            try:
                webbrowser.open(public_url)
            except Exception:  # noqa: BLE001
                pass
        else:
            print("Откройте адрес, настроенный для этого туннеля в дашборде Cloudflare")
            print("(Zero Trust → Networks → Tunnels → Public Hostname).")
        print("=" * 60)
        try:
            tunnel_process.wait()
        except KeyboardInterrupt:
            tunnel_process.terminate()
        return

    print("Поднимаю защищенную ссылку (Cloudflare Tunnel)...")
    print("=" * 60)

    tunnel_url = {}
    tunnel_ready = threading.Event()

    def on_url_found(url):
        tunnel_url["url"] = url
        tunnel_ready.set()

    tunnel_process = _start_tunnel(cloudflared_path, port, on_url_found)

    if tunnel_ready.wait(timeout=20):
        url = tunnel_url["url"]
        print("=" * 60)
        print("Готово! Открой на любом устройстве (компьютер, телефон):")
        print(f"  {url}")
        print()
        print("Эта ссылка с настоящим HTTPS — камера на телефоне заработает")
        print("без предупреждений браузера. Ссылка меняется при каждом")
        print("перезапуске программы — раздавай актуальную ссылку заново.")
        print()
        print("Если сразу после запуска браузер покажет ошибку вида")
        print("«не удалось найти IP-адрес сервера» — подождите 10-15 секунд")
        print("и обновите страницу: свежей ссылке нужно немного времени,")
        print("чтобы стать видимой всем DNS-серверам в интернете.")
        print("=" * 60)
        _wait_for_dns(url)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    else:
        print("!! Не удалось получить ссылку от cloudflared за 20 секунд.")
        print(f"   Локальный адрес всё еще доступен: http://localhost:{port}")

    try:
        tunnel_process.wait()
    except KeyboardInterrupt:
        tunnel_process.terminate()


if __name__ == "__main__":
    main()
