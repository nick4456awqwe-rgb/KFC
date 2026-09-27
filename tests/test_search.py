"""Поисковый запрос к МЭТС и разбор выдачи."""

import base64
import json
import unittest
from urllib.parse import unquote

from helpers import load

from mets.search import ConfigError, build_query, encode_query, parse_search_page, search_url


class SearchPage(unittest.TestCase):
    def test_cards(self):
        cards, total, last_page = parse_search_page(load("search_page.html"))
        self.assertEqual(len(cards), 20)
        self.assertEqual(total, 2996)
        self.assertEqual(last_page, 150)
        first = cards[0]
        self.assertEqual(first.lot_id, "236613-1")
        self.assertEqual(first.url, "https://m-ets.ru/236613-1")
        self.assertEqual(first.price_now, 121_398_953)
        self.assertEqual(first.region, "г. Москва")


class Query(unittest.TestCase):
    def test_names_to_codes(self):
        q = build_query({"regions": ["Краснодарский", "г. Москва"], "categories": ["Квартира", "Нежилое здание"],
                         "statuses": ["Объявленные торги"], "trade_forms": ["публичное предложение"]})
        self.assertEqual(q["xregion[]"], ["23", "77"])
        self.assertEqual(q["search_category"], "36,38")
        self.assertEqual(q["stat[]"], ["0"])
        self.assertEqual(q["ispub"], "on")
        self.assertNotIn("isauk", q)

    def test_dates_and_sort(self):
        q = build_query({"apps_start_from": "2026-10-01", "apps_start_to": "01.10.2026", "site_sort": "ending"})
        self.assertEqual(q["date_nach_ot"], "01.10.2026")
        self.assertEqual(q["date_nach_do"], "01.10.2026")
        self.assertEqual(q["sortby"], "2")

    def test_exclude_words_are_filtered_locally(self):
        # С исключениями сайт отвечает в ~20 раз медленнее — их проверяем сами
        q = build_query({"exclude_words": ["доля"]})
        self.assertNotIn("iskl", q)

    def test_discount_filter_narrows_search_on_site(self):
        # «Подешевел на 30 %» возможно только у лотов, которые по графику падают минимум на 30 %
        q = build_query({}, {"min_discount_now_pct": 30})
        self.assertEqual(q["proc_snij_ot"], "30")
        q = build_query({"max_drop_pct_from": 50}, {"min_discount_now_pct": 30})
        self.assertEqual(q["proc_snij_ot"], "50")
        q = build_query({}, {"min_window_discount_pct": 40, "max_window_price": 500_000})
        self.assertEqual(q["proc_snij_ot"], "40")
        self.assertEqual(q["cena_min_do"], "500000")

    def test_bad_names(self):
        with self.assertRaises(ConfigError):
            build_query({"regions": ["Нарния"]})
        with self.assertRaises(ConfigError):
            build_query({"regions": ["область"]})  # подходит слишком много
        with self.assertRaises(ConfigError):
            build_query({"apps_start_from": "первое октября"})

    def test_url_roundtrip(self):
        q = build_query({"keywords": "квартира", "regions": ["Москва"]})
        encoded = unquote(search_url(q).split("q=", 1)[1])
        self.assertEqual(encoded, encode_query(q))
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
        self.assertEqual(json.loads(raw), q)


if __name__ == "__main__":
    unittest.main()
