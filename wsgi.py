"""Точка входа для production WSGI-сервера (gunicorn).

Локальная разработка по-прежнему запускается через `python run.py`
(со своим dev-сервером и подсказками про LAN/HTTPS для телефона).
На сервере используется gunicorn:

    gunicorn --workers 2 --bind 127.0.0.1:8000 wsgi:app

см. deploy/setup.sh — разворачивает это автоматически за nginx с HTTPS.
"""

from wms import create_app

app = create_app()
