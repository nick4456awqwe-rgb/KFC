"""KFC — приложение для поиска лотов на МЭТС.

Запуск: KFC.exe (собранная версия, Python не нужен), ярлык «KFC Поиск лотов», KFC.bat или python app.py.
Окно с вкладками поисков и фильтрами как на сайте. Каждый запуск поиска — «процесс»:
его можно остановить, продолжить, сохранить в Excel то, что уже найдено, или удалить.
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
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mets.client import MetsClient
from mets.dicts import CATEGORY_GROUPS, REGIONS, STATUSES
from mets.paths import APP_DIR, DATA_DIR, OUTPUT_DIR, config_path
from mets.runner import run_search, save_excel
from mets.search import ConfigError
from mets.storage import LotCache, lots_from_json, lots_to_json

VERSION = 4
ROOT = APP_DIR
WEB = APP_DIR / "web"
DATA = DATA_DIR
RUNS_DIR = DATA / "runs"
STATE_FILE = DATA / "app_state.json"
RUNS_FILE = DATA / "runs.json"
LOG_FILE = DATA / "app.log"
PORT = 8765
IDLE_EXIT_SEC = 180  # окно закрыто и поиски не идут — через 3 минуты приложение завершается

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


def now_str():
    return datetime.now().strftime("%d.%m.%Y %H:%M")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def default_settings():
    with open(config_path(), "rb") as fh:
        return tomllib.load(fh)


def output_dir():
    return OUTPUT_DIR


CONFIG = default_settings()
CLIENT = MetsClient(delay=CONFIG.get("run", {}).get("delay_sec", 0.1))
CACHE = LotCache(DATA / "lots_cache.json", CONFIG.get("run", {}).get("cache_days", 7))


class Searches:
    """Вкладки: у каждой своё имя и свои фильтры."""

    def __init__(self):
        self.lock = threading.Lock()
        state = read_json(STATE_FILE, None)
        if not state or not state.get("searches"):
            old = read_json(DATA / "app_settings.json", None)  # настройки из первой версии приложения
            first = {"id": uuid.uuid4().hex[:8], "name": "Мой поиск", "settings": old or default_settings()}
            state = {"searches": [first], "active": first["id"]}
        self.state = state
        self._save()

    def _save(self):
        write_json(STATE_FILE, self.state)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def get(self, sid):
        with self.lock:
            return next((s for s in self.state["searches"] if s["id"] == sid), None)

    def new(self, copy_from=None):
        with self.lock:
            src = next((s for s in self.state["searches"] if s["id"] == copy_from), None)
            names = {s["name"] for s in self.state["searches"]}
            n = len(self.state["searches"]) + 1
            while f"Поиск {n}" in names:
                n += 1
            item = {"id": uuid.uuid4().hex[:8],
                    "name": f"{src['name']} (копия)" if src else f"Поиск {n}",
                    "settings": json.loads(json.dumps(src["settings"])) if src else default_settings()}
            self.state["searches"].append(item)
            self.state["active"] = item["id"]
            self._save()
            return item

    def update(self, sid, name=None, settings=None):
        with self.lock:
            item = next((s for s in self.state["searches"] if s["id"] == sid), None)
            if item is None:
                return None
            if name is not None and name.strip():
                item["name"] = name.strip()[:60]
            if settings is not None:
                item["settings"] = settings
            self._save()
            return item

    def activate(self, sid):
        with self.lock:
            if any(s["id"] == sid for s in self.state["searches"]):
                self.state["active"] = sid
                self._save()

    def delete(self, sid):
        with self.lock:
            self.state["searches"] = [s for s in self.state["searches"] if s["id"] != sid]
            if not self.state["searches"]:
                self.state["searches"] = [{"id": uuid.uuid4().hex[:8], "name": "Поиск 1", "settings": default_settings()}]
            if self.state["active"] not in {s["id"] for s in self.state["searches"]}:
                self.state["active"] = self.state["searches"][-1]["id"]
            self._save()


def with_tech_params(settings):
    """Технические параметры — общие из config.toml, а не из вкладки (там могли остаться старые)."""
    settings = json.loads(json.dumps(settings))
    run_cfg = settings.setdefault("run", {})
    for key in ("workers", "delay_sec", "cache_days", "output_dir", "fetch_details"):
        run_cfg.pop(key, None)
        if key in CONFIG.get("run", {}):
            run_cfg[key] = CONFIG["run"][key]
    run_cfg.pop("max_pages", None)
    return settings


class Runs:
    """Процессы поиска: идут, остановлены, готовы. Хранятся между запусками приложения."""

    def __init__(self):
        self.lock = threading.RLock()
        self.cancels = {}
        self.save_on_stop = set()
        self.runs = read_json(RUNS_FILE, [])
        for run in self.runs:  # приложение закрыли посреди поиска
            if run["status"] == "running":
                run["status"] = "stopped"
                run["log"].append([now_str(), "прерван: приложение было закрыто"])
        self._save()

    def _save(self):
        with self.lock:
            write_json(RUNS_FILE, self.runs)

    def _get(self, rid):
        return next((r for r in self.runs if r["id"] == rid), None)

    def _log(self, run, text):
        run["log"].append([now_str(), text])

    def list(self):
        with self.lock:
            return json.loads(json.dumps(self.runs))

    def any_running(self):
        with self.lock:
            return any(r["status"] == "running" for r in self.runs)

    def start(self, search, fresh=False):
        settings = json.loads(json.dumps(search["settings"]))
        run = {
            "id": uuid.uuid4().hex[:10],
            "search_id": search["id"],
            "name": search["name"],
            "settings": settings,
            "status": "running",
            "started": now_str(),
            "finished": None,
            "progress": {"text": "Запускаю…", "fraction": None},
            "stats": {},
            "file": None,
            "search_url": None,
            "log": [],
        }
        with self.lock:
            self.runs.insert(0, run)
            self._log(run, "запущен")
            self._launch(run, fresh)
        return run["id"]

    def _launch(self, run, fresh):
        cancel = threading.Event()
        self.cancels[run["id"]] = cancel
        run["status"] = "running"
        run["error"] = None
        self._save()
        threading.Thread(target=self._worker, args=(run["id"], cancel, fresh), daemon=True).start()

    def _worker(self, rid, cancel, fresh):
        with self.lock:
            run = self._get(rid)
            settings, name = run["settings"], run["name"]

        def progress(info):
            with self.lock:
                if (r := self._get(rid)) is not None:
                    r["progress"] = info

        try:
            result = run_search(with_tech_params(settings), DATA.parent, progress=progress, cancel=cancel,
                                client=CLIENT, cache=CACHE, fresh=fresh)
            (RUNS_DIR / f"{rid}.json").parent.mkdir(parents=True, exist_ok=True)
            (RUNS_DIR / f"{rid}.json").write_text(lots_to_json(result["lots"]), encoding="utf-8")
            s = result["stats"]
            with self.lock:
                run = self._get(rid)
                if run is None:  # удалили, пока шёл
                    return
                run.update(stats=s, search_url=result["search_url"], finished=now_str())
                self._log(run, f"{s['reason_text']}: просмотрено {s['viewed']}, подошло {s['found']}")
                keep_open = s["reason"] == "user" and rid not in self.save_on_stop
                if keep_open:
                    run["status"] = "stopped"
                    self._save()
                    return
            self._write_excel(rid, result)
        except ConfigError as e:
            self._fail(rid, str(e))
        except Exception as e:
            log(traceback.format_exc())
            self._fail(rid, f"Ошибка: {e}")
        finally:
            with self.lock:
                self.cancels.pop(rid, None)
                self.save_on_stop.discard(rid)
                self._save()

    def _write_excel(self, rid, result):
        with self.lock:
            run = self._get(rid)
            self._log(run, "сохранён в Excel")
            settings, name, log_rows = run["settings"], run["name"], list(run["log"])
        path = save_excel(result, settings, output_dir(), name, log=log_rows)
        with self.lock:
            run = self._get(rid)
            if run is not None:
                if run.get("file") and run["file"] != path.name:
                    (output_dir() / run["file"]).unlink(missing_ok=True)  # старая выгрузка этого же процесса
                run.update(status="done", file=path.name)
                self._save()

    def _fail(self, rid, text):
        with self.lock:
            run = self._get(rid)
            if run is not None:
                run.update(status="error", error=text, finished=now_str())
                self._log(run, text)
                self._save()

    def stop(self, rid, save=False):
        with self.lock:
            run = self._get(rid)
            if run is None or run["status"] != "running":
                return False
            if save:
                self.save_on_stop.add(rid)
            self._log(run, "завершён с сохранением в Excel" if save else "остановлен")
            run["progress"]["text"] = "Останавливаю…"
            self.cancels[rid].set()
            return True

    def resume(self, rid, fresh=False):
        with self.lock:
            run = self._get(rid)
            if run is None or run["status"] == "running":
                return False
            self._log(run, "продолжен")
            run["progress"] = {"text": "Продолжаю… уже проверенные лоты берутся из памяти", "fraction": None}
            self._launch(run, fresh)
            return True

    def lots(self, rid):
        path = RUNS_DIR / f"{rid}.json"
        return lots_from_json(path.read_text(encoding="utf-8")) if path.exists() else []

    def save(self, rid):
        with self.lock:
            run = self._get(rid)
            if run is None or run["status"] == "running":
                return False
            result = {"lots": self.lots(rid), "stats": run.get("stats") or {}, "search_url": run.get("search_url")}
        self._write_excel(rid, result)
        return True

    def delete(self, rid):
        with self.lock:
            run = self._get(rid)
            if run is None:
                return False
            if rid in self.cancels:
                self.cancels[rid].set()
            self.runs.remove(run)
            self._save()
        (RUNS_DIR / f"{rid}.json").unlink(missing_ok=True)
        if run.get("file"):
            (output_dir() / run["file"]).unlink(missing_ok=True)
        return True


SEARCHES = Searches()
RUNS = Runs()
LAST_SEEN = [time.monotonic()]

ROW_KEYS = ["number", "url", "title", "region", "categories", "trade_form", "status", "trade_start", "start_price",
            "price_now", "difference", "discount_now", "price_min", "discount_max", "period", "next_date",
            "next_price", "deadline", "days_left", "bids", "area", "price_per_m2", "is_new", "prev_price",
            "win_price_start", "win_price_min", "win_min_date", "win_discount"]


def lot_row(lot):
    return {k: (v.strftime("%d.%m.%Y %H:%M") if isinstance(v := lot.get(k), datetime) else v) for k in ROW_KEYS}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
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
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        LAST_SEEN[0] = time.monotonic()
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/ping":
            self._send(200, {"app": "kfc", "version": VERSION})
        elif url.path == "/api/options":
            self._send(200, {
                "regions": sorted(REGIONS, key=lambda r: r.removeprefix("г. ").lower()),
                "category_groups": CATEGORY_GROUPS,
                "statuses": list(STATUSES),
                "defaults": default_settings(),
            })
        elif url.path == "/api/state":
            runs = RUNS.list()
            for r in runs:
                r.pop("settings", None)
            self._send(200, {**SEARCHES.snapshot(), "runs": runs, "cache_size": CACHE.size()})
        elif url.path == "/api/run/rows":
            rid = query.get("id", [""])[0]
            run = next((r for r in RUNS.list() if r["id"] == rid), None)
            if run is None:
                return self._send(404, {"error": "Процесс не найден"})
            lots = RUNS.lots(rid)
            f = run["settings"].get("filter", {})
            self._send(200, {"rows": [lot_row(l) for l in lots[:500]], "total": len(lots),
                             "window": bool(f.get("window_from") or f.get("window_to"))})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        LAST_SEEN[0] = time.monotonic()
        path = urlparse(self.path).path
        body = self._body()
        ok = True
        if path == "/api/search/save":
            ok = SEARCHES.update(body["id"], body.get("name"), body.get("settings")) is not None
        elif path == "/api/search/new":
            return self._send(200, SEARCHES.new(body.get("copy_from")))
        elif path == "/api/search/activate":
            SEARCHES.activate(body["id"])
        elif path == "/api/search/delete":
            SEARCHES.delete(body["id"])
        elif path == "/api/run/start":
            if body.get("settings") is not None:
                SEARCHES.update(body["search_id"], settings=body["settings"])
            search = SEARCHES.get(body["search_id"])
            if search is None:
                return self._send(404, {"ok": False, "error": "Вкладка не найдена"})
            return self._send(200, {"ok": True, "id": RUNS.start(search, fresh=bool(body.get("fresh")))})
        elif path == "/api/run/stop":
            ok = RUNS.stop(body["id"], save=bool(body.get("save")))
        elif path == "/api/run/resume":
            ok = RUNS.resume(body["id"])
        elif path == "/api/run/save":
            ok = RUNS.save(body["id"])
        elif path == "/api/run/delete":
            ok = RUNS.delete(body["id"])
        elif path == "/api/open":
            folder = output_dir().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            target = folder
            if body.get("file"):
                target = (folder / Path(body["file"]).name).resolve()
                if target.parent != folder or not target.exists():
                    return self._send(404, {"ok": False, "error": "Файл не найден — возможно, его удалили"})
            os.startfile(target)
        elif path == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        else:
            return self._send(404, {"error": "not found"})
        self._send(200, {"ok": ok})


def open_window(url):
    """Окно без адресной строки (Edge/Chrome в режиме приложения), иначе — обычный браузер."""
    for exe in BROWSERS:
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1360,920"])
            return
    webbrowser.open(url)


def watchdog(server):
    while True:
        time.sleep(15)
        if not RUNS.any_running() and time.monotonic() - LAST_SEEN[0] > IDLE_EXIT_SEC:
            log("Окно закрыто — выхожу")
            server.shutdown()
            return


def _ping(url):
    try:
        with urllib.request.urlopen(url + "api/ping", timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def bind_server():
    """Порт 8765; если там уже KFC — своей версии открываем окно, старую просим закрыться."""
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else PORT
    url = f"http://127.0.0.1:{port}/"
    for _ in range(2):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler), url
        except OSError:
            info = _ping(url)
            if info and info.get("app") == "kfc" and info.get("version") == VERSION:
                return None, url
            if info and info.get("app") == "kfc":
                try:
                    urllib.request.urlopen(urllib.request.Request(url + "api/quit", data=b"{}", method="POST"), timeout=2)
                except Exception:
                    pass
                time.sleep(1.5)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # порт занят — берём свободный
    return server, f"http://127.0.0.1:{server.server_port}/"


def main():
    server, url = bind_server()
    no_window = "--no-window" in sys.argv
    if server is None:  # уже запущено — просто ещё одно окно
        if not no_window:
            open_window(url)
        return
    log(f"Запуск {url}")
    if sys.stdout:  # под pythonw консоли нет
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(f"KFC запущен: {url}\nЗакройте окно приложения, чтобы выйти.", flush=True)
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    if not no_window:
        threading.Timer(0.3, open_window, args=(url,)).start()
    server.serve_forever()
    CACHE.save()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        raise
