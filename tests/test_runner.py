"""Движок поиска без интернета: лимиты, остановка, память лотов, исключения, Excel."""

import tempfile
import threading
import unittest
from pathlib import Path

from helpers import FakeClient, load
from openpyxl import load_workbook

from mets.runner import run_search, save_excel
from mets.storage import LotCache


def settings(**parts):
    s = {"search": {"trade_forms": ["публичное предложение"]}, "filter": {}, "run": {"workers": 4, "delay_sec": 0}}
    for section, values in parts.items():
        s[section].update(values)
    return s


class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = LotCache(self.root / "cache.json")
        self.client = FakeClient(pages=3)  # 60 лотов, у каждого снижение 25 %

    def tearDown(self):
        self.tmp.cleanup()

    def run_search(self, s, **kw):
        return run_search(s, self.root, client=self.client, cache=self.cache, **kw)

    def test_whole_results(self):
        r = self.run_search(settings())
        self.assertEqual(r["stats"]["viewed"], 60)
        self.assertEqual(r["stats"]["found"], 60)
        self.assertIsNone(r["stats"]["reason"])

    def test_view_limit(self):
        r = self.run_search(settings(run={"max_lots": 15}))
        self.assertEqual(r["stats"]["viewed"], 15)
        self.assertEqual(r["stats"]["reason"], "limit")
        self.assertLessEqual(self.client.search_calls, 2)  # дальше первой страницы почти не ходили

    def test_stop_after_found(self):
        r = self.run_search(settings(filter={"min_discount_now_pct": 20}, run={"stop_after_found": 5}))
        self.assertEqual(r["stats"]["found"], 5)
        self.assertEqual(r["stats"]["reason"], "found")
        self.assertLess(r["stats"]["viewed"], 60)

    def test_discount_filter(self):
        self.assertEqual(self.run_search(settings(filter={"min_discount_now_pct": 20}))["stats"]["found"], 60)
        self.assertEqual(self.run_search(settings(filter={"min_discount_now_pct": 30}))["stats"]["found"], 0)

    def test_stop_button_returns_found_so_far(self):
        cancel = threading.Event()

        def progress(info):
            if info.get("checked", 0) >= 10:
                cancel.set()

        r = self.run_search(settings(), progress=progress, cancel=cancel)
        self.assertEqual(r["stats"]["reason"], "user")
        self.assertGreaterEqual(r["stats"]["found"], 10)
        self.assertLess(r["stats"]["viewed"], 60)

    def test_second_search_uses_memory(self):
        self.run_search(settings())
        opened = self.client.lot_calls
        self.assertEqual(opened, 60)
        self.run_search(settings())
        self.assertEqual(self.client.lot_calls, opened)  # второй раз лоты не открываются

    def test_new_and_seen(self):
        first = self.run_search(settings())
        self.assertEqual(first["stats"]["new"], 60)
        second = self.run_search(settings())
        self.assertEqual(second["stats"]["new"], 0)
        only_new = self.run_search(settings(run={"only_new": True}))
        self.assertEqual(only_new["stats"]["found"], 0)

    def test_exclude_words(self):
        r = self.run_search(settings(search={"exclude_words": ["дебиторская"]}))
        self.assertEqual(r["stats"]["excluded"], 3)  # по одной «дебиторке» на каждой из 3 страниц
        self.assertEqual(r["stats"]["viewed"], 57)

    def test_multi_lot_trade_opened_once(self):
        # В выдаче 8 лотов торгов 236596 — их страница общая, качать её нужно один раз
        self.client = FakeClient(pages=1, lot_html=load("lot_multi.html"))
        r = self.run_search(settings())
        trade_urls = [u for u in self.client.lot_urls if "/237596-" in u]
        self.assertEqual(len(trade_urls), 1)
        found = {l["lot_id"] for l in r["lots"]}
        self.assertTrue({"237596-4", "237596-8", "237596-15"} <= found)
        zil = next(l for l in r["lots"] if l["lot_id"] == "237596-15")
        self.assertTrue(zil["title"].startswith("Седельный тягач ЗИЛ"))

    def test_excel(self):
        s = settings(filter={"window_from": "2026-10-01", "window_to": "2026-10-15"}, run={"max_lots": 5})
        r = self.run_search(s)
        path = save_excel(r, s, self.root, "Тест", log=[("27.09.2026 21:00", "запущен")])
        wb = load_workbook(path)
        self.assertEqual(wb.sheetnames, ["Лоты", "График цен", "Что искали"])
        headers = [c.value for c in wb["Лоты"][1]]
        self.assertIn("Мин. цена в периоде, ₽", headers)
        self.assertEqual(wb["Лоты"].max_row, 6)
        about = [tuple(r) for r in wb["Что искали"].iter_rows(values_only=True)]
        self.assertIn(("Цена в период", "с 01.10.2026 по 15.10.2026"), about)
        self.assertIn(("27.09.2026 21:00", "запущен"), about)


if __name__ == "__main__":
    unittest.main()
