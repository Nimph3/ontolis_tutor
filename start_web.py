"""Запуск OntoTutor из папки проекта и открытие браузера после готовности сервера."""
from __future__ import annotations
import threading
import time
import urllib.request
import webbrowser
import uvicorn

URL = 'http://127.0.0.1:8000'


def browser_when_ready():
    for _ in range(80):
        try:
            urllib.request.urlopen(URL, timeout=1).close()
            webbrowser.open(URL)
            return
        except OSError:
            time.sleep(.25)


if __name__ == '__main__':
    threading.Thread(target=browser_when_ready, daemon=True).start()
    uvicorn.run('app:app', host='127.0.0.1', port=8000, reload=False)
