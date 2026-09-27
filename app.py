"""KFC — приложение для поиска лотов на МЭТС.

Запуск: двойной клик по KFC.bat (или python app.py).
Открывается окно с фильтрами как на сайте; кнопка «Найти» собирает лоты и сохраняет Excel.
Всё работает локально на этом компьютере. Окно закрыли — приложение само завершится.
"""

import json
import os
import subprocess
import sys
import threading
import time
import tomllib
import traceback
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mets.client import Cancelled
from mets.dicts import CATEGORY_GROUPS, REGIONS, STATUSES
from mets.runner import run_search
from mets.search import ConfigError

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
SETTINGS_FILE = DATA / "app_settings.json"
LOG_FILE = DATA / "app.log"
PORT = 8765
IDLE_EXIT_SEC = 180  # окно закрыто и поиск не идёт — через 3 минуты приложение завершается

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def log(message):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


def output_dir(settings=None):
    run = (settings or load_settings()).get("run", {})
    return ROOT / run.get("output_dir", "output")


def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    with open(ROOT / "config.toml", "rb") as fh:
        return tomllib.load(fh)


def save_settings(settings):
    DATA.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")


def _fmt_dt(value):
    return value.strftime("%d.%m.%Y %H:%M") if isinstance(value, datetime) else value


ROW_KEYS = ["number", "url", "title", "region", "categories", "form_short", "status", "start_price", "price_now",
            "difference", "discount_now", "price_min", "discount_max", "period", "next_date", "next_price",
            "deadline", "days_left", "area", "price_per_m2", "is_new", "prev_price"]


def lot_row(lot):
    return {k: _fmt_dt(lot.get(k)) for k in ROW_KEYS}


class Job:
    """Один поиск в фоне; окно спрашивает его состояние раз в секунду."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.state = {"status": "idle"}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def update(self, **kw):
        with self.lock:
            self.state.update(kw)

    @property
    def running(self):
        return self.snapshot().get("status") == "running"

    def start(self, settings, fresh=False):
        with self.lock:
            if self.state.get("status") == "running":
                return False
            self.cancel = threading.Event()
            self.state = {"status": "running", "stage": "search", "done": 0, "total": 0, "text": "Запускаю поиск…"}
        threading.Thread(target=self._run, args=(settings, fresh), daemon=True).start()
        return True

    def _run(self, settings, fresh):
        log(f"Поиск: {json.dumps(settings, ensure_ascii=False)}")
        try:
            result = run_search(settings, ROOT, progress=self._progress, cancel=self.cancel, fresh=fresh)
            self.update(status="done", file=result["file"].name, stats=result["stats"],
                        search_url=result["search_url"], rows=[lot_row(l) for l in result["lots"][:500]],
                        text="Готово")
            log(f"Готово: {result['file'].name} {result['stats']}")
        except Cancelled:
            self.update(status="cancelled",
                        text="Поиск остановлен. Уже открытые лоты сохранены — следующий поиск продолжит с них.")
        except ConfigError as e:
            self.update(status="error", text=str(e))
        except Exception as e:
            log(traceback.format_exc())
            self.update(status="error", text=f"Ошибка: {e}")

    def _progress(self, stage, done, total, text):
        self.update(stage=stage, done=done, total=total, text=text)


JOB = Job()
LAST_SEEN = [time.monotonic()]


def list_files():
    folder = output_dir()
    if not folder.exists():
        return []
    files = sorted(folder.glob("lots_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
    return [{"name": p.name, "date": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m.%Y %H:%M"),
             "size_kb": round(p.stat().st_size / 1024)} for p in files]


def cache_size():
    path = DATA / "lots_cache.json"
    if not path.exists():
        return 0
    try:
        return sum(1 for v in json.loads(path.read_text(encoding="utf-8")).values() if "lot" in v)
    except (OSError, json.JSONDecodeError):
        return 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # не засоряем вывод
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def do_GET(self):
        LAST_SEEN[0] = time.monotonic()
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/ping":
            self._send(200, {"app": "kfc"})
        elif path == "/api/options":
            self._send(200, {
                "regions": sorted(REGIONS, key=lambda r: r.removeprefix("г. ").lower()),
                "category_groups": CATEGORY_GROUPS,
                "statuses": list(STATUSES),
                "settings": load_settings(),
                "cache_size": cache_size(),
            })
        elif path == "/api/status":
            self._send(200, JOB.snapshot())
        elif path == "/api/files":
            self._send(200, list_files())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        LAST_SEEN[0] = time.monotonic()
        path = self.path.split("?", 1)[0]
        body = self._body()
        if path == "/api/settings":
            save_settings(body["settings"])
            self._send(200, {"ok": True})
        elif path == "/api/search":
            save_settings(body["settings"])
            ok = JOB.start(body["settings"], fresh=bool(body.get("fresh")))
            self._send(200, {"ok": ok, "error": None if ok else "Поиск уже идёт"})
        elif path == "/api/stop":
            JOB.cancel.set()
            self._send(200, {"ok": True})
        elif path == "/api/open":
            folder = output_dir().resolve()
            target = folder
            if body.get("file"):
                target = (folder / Path(body["file"]).name).resolve()
                if target.parent != folder or not target.exists():
                    return self._send(404, {"ok": False, "error": "Файл не найден"})
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(target)
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})


def open_window(url):
    """Окно без адресной строки (Edge/Chrome в режиме приложения), иначе — обычный браузер."""
    for exe in BROWSERS:
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1320,900"])
            return
    webbrowser.open(url)


def watchdog(server):
    while True:
        time.sleep(15)
        if not JOB.running and time.monotonic() - LAST_SEEN[0] > IDLE_EXIT_SEC:
            log("Окно закрыто — выхожу")
            server.shutdown()
            return


def main():
    url = f"http://127.0.0.1:{PORT}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        try:  # приложение уже запущено — просто открываем ещё одно окно
            with urllib.request.urlopen(url + "api/ping", timeout=2) as resp:
                if json.loads(resp.read()).get("app") == "kfc":
                    open_window(url)
                    return
        except Exception:
            pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # порт занят чем-то другим — берём свободный
        url = f"http://127.0.0.1:{server.server_port}/"

    log(f"Запуск {url}")
    if sys.stdout:  # под pythonw консоли нет
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(f"KFC запущен: {url}\nЗакройте окно приложения, чтобы выйти.", flush=True)
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    if "--no-window" not in sys.argv:
        threading.Timer(0.3, open_window, args=(url,)).start()
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        raise
