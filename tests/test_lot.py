"""Разбор страницы лота и расчёт разницы — на сохранённых страницах МЭТС."""

import unittest
from datetime import datetime

from helpers import NOW, load

from mets.lot import apply_window, parse_lot


class PublicOfferLot(unittest.TestCase):
    """232477-1: публичное предложение в 6-м периоде из 18."""

    @classmethod
    def setUpClass(cls):
        cls.lot = parse_lot(load("lot_public.html"), "232477-1", now=NOW)

    def test_prices(self):
        self.assertEqual(self.lot["start_price"], 4_692_600)
        self.assertEqual(self.lot["price_now"], 3_519_450)
        self.assertEqual(self.lot["price_min"], 703_890)

    def test_difference(self):
        self.assertEqual(self.lot["difference"], 1_173_150)
        self.assertEqual(self.lot["discount_now"], 0.25)
        self.assertEqual(self.lot["discount_max"], 0.85)

    def test_schedule(self):
        self.assertEqual(len(self.lot["schedule"]), 18)
        self.assertEqual(self.lot["period"], "6 из 18")
        self.assertEqual(self.lot["next_price"], 3_284_820)
        self.assertEqual(self.lot["next_date"], datetime(2026, 9, 29, 9, 0))

    def test_dates(self):
        self.assertEqual(self.lot["trade_start"], datetime(2026, 9, 11, 9, 0))
        self.assertEqual(self.lot["apps_end"], datetime(2026, 11, 4, 9, 0))
        self.assertEqual(self.lot["deadline"], datetime(2026, 9, 29, 9, 0))

    def test_details(self):
        self.assertIn("Дом 96", self.lot["title"])
        self.assertEqual(self.lot["region"], "Свердловская область")
        self.assertEqual(self.lot["area"], 96)
        self.assertEqual(self.lot["price_per_m2"], 36_661)
        self.assertEqual(self.lot["cadastral"], "66:59:0216001:5654")
        self.assertIn("публичного предложения", self.lot["trade_form"])

    def test_price_in_period(self):
        lot = apply_window(dict(self.lot), datetime(2026, 10, 1), datetime(2026, 10, 15, 23, 59), now=NOW)
        self.assertEqual(lot["win_price_start"], 3_284_820)  # 1 октября действует 7-й период
        self.assertEqual(lot["win_price_min"], 2_111_670)    # 12-й период начинается 14 октября
        self.assertEqual(lot["win_min_date"], datetime(2026, 10, 14, 9, 0))
        self.assertAlmostEqual(lot["win_discount"], 0.55, places=2)

    def test_period_outside_trading(self):
        lot = apply_window(dict(self.lot), datetime(2027, 1, 1), datetime(2027, 1, 31), now=NOW)
        self.assertIsNone(lot["win_price_min"])


class MultiLotPage(unittest.TestCase):
    """На странице торгов 236596 восемь лотов — у каждого должны быть свои данные."""

    @classmethod
    def setUpClass(cls):
        cls.html = load("lot_multi.html")

    def test_lot_15(self):
        lot = parse_lot(self.html, "236596-15", now=NOW)
        self.assertTrue(lot["title"].startswith("Седельный тягач ЗИЛ"))
        self.assertEqual(lot["start_price"], 105_750)
        self.assertEqual(len(lot["schedule"]), 11)

    def test_lot_4(self):
        lot = parse_lot(self.html, "236596-4", now=NOW)
        self.assertTrue(lot["title"].startswith("КАМАЗ 65115"))
        self.assertEqual(lot["start_price"], 926_250.3)
        self.assertEqual(lot["price_min"], 463_125.15)


class AuctionLot(unittest.TestCase):
    """236617-1: аукцион — графика нет, цена не снижается."""

    @classmethod
    def setUpClass(cls):
        cls.lot = parse_lot(load("lot_auction.html"), "236617-1", now=NOW)

    def test_no_discount(self):
        self.assertEqual(self.lot["schedule"], [])
        self.assertEqual(self.lot["start_price"], 6_165_000)
        self.assertEqual(self.lot["discount_now"], 0)
        self.assertEqual(self.lot["discount_max"], 0)

    def test_details(self):
        self.assertEqual(self.lot["area"], 344.5)
        self.assertEqual(self.lot["cadastral"], "23:07:0203000:1435, 23:07:0203000:918")
        self.assertEqual(self.lot["trade_start"], datetime(2026, 9, 28, 0, 0))

    def test_price_in_period(self):
        lot = apply_window(dict(self.lot), datetime(2026, 10, 1), datetime(2026, 10, 15), now=NOW)
        self.assertEqual(lot["win_price_min"], 6_165_000)
        lot = apply_window(dict(self.lot), datetime(2026, 12, 1), datetime(2026, 12, 15), now=NOW)
        self.assertIsNone(lot["win_price_min"])  # приём заявок закончится 02.11


if __name__ == "__main__":
    unittest.main()
