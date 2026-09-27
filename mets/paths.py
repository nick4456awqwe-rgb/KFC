"""Где лежат файлы программы и данные пользователя.

Запуск из исходников (python app.py): всё рядом с кодом — data/ и output/ в папке проекта.
Собранный KFC.exe: файлы программы (окно, настройки по умолчанию) лежат внутри exe, а данные —
в папках пользователя, куда всегда можно писать:
  память лотов, вкладки, процессы — %LOCALAPPDATA%\\KFC
  выгрузки Excel                   — Документы\\KFC
Переменные окружения KFC_DATA_DIR и KFC_OUTPUT_DIR переопределяют эти папки (для проверок).
"""

import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
# Файлы программы: web/, config.toml, kfc.ico
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _documents():
    """Папка «Документы» (может быть перенесена, например в OneDrive)."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:  # 5 — «Документы»
            return Path(buf.value)
    except Exception:
        pass
    return Path.home() / "Documents"


if FROZEN:
    _data = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "KFC"
    _output = _documents() / "KFC"
else:
    _data = APP_DIR / "data"
    _output = APP_DIR / "output"

DATA_DIR = Path(os.environ.get("KFC_DATA_DIR") or _data)
OUTPUT_DIR = Path(os.environ.get("KFC_OUTPUT_DIR") or _output)


def config_path():
    """Настройки: свои в папке данных (если пользователь их положил), иначе встроенные."""
    own = DATA_DIR / "config.toml"
    return own if FROZEN and own.exists() else APP_DIR / "config.toml"
