"""HTTP-клиент для m-ets.ru: одна сессия, пауза между запросами, повторы при ошибках.

Один клиент можно делить между несколькими поисками: пауза общая, поэтому сайт
не получает больше запросов, чем задано, сколько бы поисков ни шло одновременно.
"""

import threading
import time

import requests
from requests.adapters import HTTPAdapter

BASE_URL = "https://m-ets.ru"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class Cancelled(Exception):
    """Поиск остановлен пользователем."""


class MetsClient:
    def __init__(self, delay=0.15, retries=3, timeout=25):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=24)
        self.session.mount("https://", adapter)
        self._lock = threading.Lock()
        self._last_request = 0.0

    def _wait_turn(self):
        with self._lock:
            pause = self.delay - (time.monotonic() - self._last_request)
            if pause > 0:
                time.sleep(pause)
            self._last_request = time.monotonic()

    def get(self, path, params=None, cancel=None):
        """cancel — threading.Event поиска: если выставлен, запрос не отправляется (Cancelled)."""
        url = path if path.startswith("http") else f"{BASE_URL}/{path.lstrip('/')}"
        last_error = None
        for attempt in range(1, self.retries + 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            self._wait_turn()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                resp.encoding = "utf-8"
                return resp.text
            except requests.RequestException as e:
                last_error = e
                if cancel is not None and cancel.wait(2 * attempt):
                    raise Cancelled()
                if cancel is None:
                    time.sleep(2 * attempt)
        raise RuntimeError(f"Не удалось загрузить {url}: {last_error}")
