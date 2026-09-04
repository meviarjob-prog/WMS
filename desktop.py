"""Единая точка входа для desktop-версии WMS (запускается как .exe).

Делает две вещи одновременно:
1. Поднимает сам WMS-сервер локально.
2. Запускает Cloudflare Tunnel (готовый бинарник cloudflared) и печатает
   выданную им настоящую HTTPS-ссылку (https://....trycloudflare.com) —
   без неё браузер не даст доступ к камере телефона для сканирования.

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
