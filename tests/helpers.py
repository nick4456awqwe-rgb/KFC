"""Общее для тестов: сохранённые страницы МЭТС (обезличены) и подставной «сайт» без интернета."""

import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mets.client import Cancelled  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 9, 27, 21, 0)  # страницы сохранены 27.09.2026 вечером


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeClient:
    """Отвечает как m-ets.ru, но из файлов: 3 страницы выдачи по 20 лотов, любой лот — lot_public.html."""

    def __init__(self, pages=3, lot_html=None):
        self.pages = pages
        self.search_html = load("search_page.html")
        self.lot_html = lot_html or load("lot_public.html")
        self.search_calls = 0
        self.lot_calls = 0
        self.lot_urls = []

    def get(self, url, params=None, cancel=None):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if "/search?" in url:
            self.search_calls += 1
            page = int(re.search(r"[?&]page=(\d+)", url).group(1)) if "page=" in url else 1
            html = re.sub(r"([?&]page=)(\d+)", lambda m: f"{m.group(1)}{min(int(m.group(2)), self.pages)}", self.search_html)
            # у каждой страницы свои номера лотов, а цена в выдаче — как на странице лота
            html = re.sub(r'(itemprop="price" content=")[\d.]+', r"\g<1>3519450.00", html)
            return re.sub(r'href="(\d+)-(\d+)"', lambda m: f'href="{int(m.group(1)) + page * 1000}-{m.group(2)}"', html)
        self.lot_calls += 1
        self.lot_urls.append(url)
        lot_no = url.rstrip("/").rsplit("-", 1)[-1]
        return re.sub(r'(data-lotnumber=["\'])1(["\'])', rf"\g<1>{lot_no}\g<2>", self.lot_html, flags=re.I)
