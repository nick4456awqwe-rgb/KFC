"""KFC — поиск выгодных лотов на МЭТС (m-ets.ru) с выгрузкой в Excel.

    python kfc.py              поиск по настройкам из config.toml
    python kfc.py --pages 2    быстрая проверка: только 2 страницы выдачи (40 лотов)
    python kfc.py --list       все регионы, категории и статусы для config.toml
    python kfc.py --open       открыть Excel после выгрузки
"""

import argparse
import os
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from mets.client import MetsClient
from mets.dicts import CATEGORIES, REGIONS, STATUSES
from mets.excel import write_excel
from mets.lot import parse_lot, update_prices
from mets.search import ConfigError, build_query, fetch_cards, is_excluded, search_url
from mets.storage import LotCache

ROOT = Path(__file__).resolve().parent

SORTS = {
    "discount_now": (lambda l: l.get("discount_now") or 0, True),
    "difference": (lambda l: l.get("difference") or 0, True),
    "price_now": (lambda l: l.get("price_now") or 0, False),
    "price_per_m2": (lambda l: l.get("price_per_m2") or float("inf"), False),
    "deadline": (lambda l: l.get("deadline") or datetime.max, False),
}


def card_status(card):
    # "Торги по банкротству. Торги в стадии приема заявок" -> "Торги в стадии приема заявок"
    return card.trade_type.split(". ", 1)[-1]


def lot_from_card(card):
    """Лот только по данным из выдачи (без открытия страницы) — начальной цены и графика нет."""
    lot = {
        "lot_id": card.lot_id,
        "url": card.url,
        "number": card.lot_id.replace("-", "-МЭТС-", 1),
        "title": card.title,
        "region": card.region,
        "trade_kind": card.trade_type.split(". ", 1)[0],
        "price_min": card.price_min,
        "description": card.description,
        "schedule": [],
    }
    return update_prices(lot, card.price_now, card.period_end, card_status(card))


def passes(lot, flt):
    pct = flt.get("min_discount_now_pct", 0)
    if pct and (lot.get("discount_now") or 0) * 100 < pct:
        return False
    rub = flt.get("min_difference_rub", 0)
    if rub and (lot.get("difference") or 0) < rub:
        return False
    days = flt.get("min_days_left", 0)
    if days and lot.get("days_left") is not None and lot["days_left"] < days:
        return False
    per_m2 = flt.get("max_price_per_m2", 0)
    if per_m2 and lot.get("price_per_m2") and lot["price_per_m2"] > per_m2:
        return False
    return True


def fmt_money(value):
    return f"{value:,.0f}".replace(",", " ") if value else "—"


def print_dicts():
    for title, table in (("РЕГИОНЫ", REGIONS), ("КАТЕГОРИИ", CATEGORIES), ("СТАТУСЫ", STATUSES)):
        print(f"\n=== {title} ===")
        for name in sorted(table):
            print(f"  {name}")


def parse_args():
    p = argparse.ArgumentParser(description="Поиск выгодных лотов на МЭТС с выгрузкой в Excel")
    p.add_argument("--config", default=str(ROOT / "config.toml"), help="файл настроек (по умолчанию config.toml)")
    p.add_argument("--pages", type=int, help="сколько страниц выдачи обойти (перекрывает max_pages)")
    p.add_argument("--no-details", action="store_true", help="не открывать лоты — только данные из выдачи")
    p.add_argument("--fresh", action="store_true", help="игнорировать кэш и заново открыть все лоты")
    p.add_argument("--list", action="store_true", help="показать регионы, категории и статусы")
    p.add_argument("--open", action="store_true", help="открыть Excel после выгрузки")
    return p.parse_args()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    if args.list:
        print_dicts()
        return 0

    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)
    search, flt, run = cfg.get("search", {}), cfg.get("filter", {}), cfg.get("run", {})
    max_pages = args.pages if args.pages is not None else run.get("max_pages", 0)
    details = run.get("fetch_details", True) and not args.no_details

    try:
        query = build_query(search)
    except ConfigError as e:
        print(f"Ошибка в {args.config}: {e}")
        return 2

    client = MetsClient(delay=run.get("delay_sec", 0.3))
    cache = LotCache(ROOT / "data" / "lots_cache.json", max_age_days=0 if args.fresh else run.get("cache_days", 7))
    started = time.monotonic()

    print(f"Поиск на МЭТС: {search_url(query)}")

    def on_page(done, pages, total):
        print(f"\r  страниц выдачи загружено {done}/{pages}  (всего на сайте найдено: {total})", end="", flush=True)

    workers = run.get("workers", 3)
    cards = fetch_cards(client, query, max_pages, workers, on_page=on_page)
    exclude = search.get("exclude_words", [])
    skipped = [c for c in cards if is_excluded(f"{c.title} {c.description}", exclude)]
    cards = [c for c in cards if c not in skipped]
    print(f"\nВ выдаче лотов: {len(cards) + len(skipped)}, исключено по словам: {len(skipped)}")

    lots, to_fetch = {}, []
    for card in cards:
        cached = cache.get(card.lot_id) if details else None
        if cached:
            lots[card.lot_id] = update_prices(cached, card.price_now, card.period_end, card_status(card))
        elif details:
            to_fetch.append(card)
        else:
            lots[card.lot_id] = lot_from_card(card)

    errors = 0
    try:
        if to_fetch:
            print(f"Открываю лоты: {len(to_fetch)} новых, {len(cards) - len(to_fetch)} из кэша")
            fetch_started = time.monotonic()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(lambda c: parse_lot(client.get(c.url), c.lot_id), c): c for c in to_fetch}
                for i, future in enumerate(as_completed(futures), 1):
                    card = futures[future]
                    try:
                        lot = future.result()
                        cache.put(lot)
                    except Exception as e:  # лот мог быть снят или страница не открылась
                        lot = lot_from_card(card)
                        lot["description"] = f"[не удалось открыть: {e}] {lot['description']}"
                        errors += 1
                    lots[card.lot_id] = lot
                    if i % 10 == 0 or i == len(to_fetch):
                        eta = (time.monotonic() - fetch_started) / i * (len(to_fetch) - i)
                        print(f"\r  открыто {i}/{len(to_fetch)}, осталось ~{eta / 60:.0f} мин ", end="", flush=True)
                    if i % 200 == 0:
                        cache.save()  # чтобы при обрыве не начинать заново
            print()
    finally:
        cache.save()

    ordered = [lots[c.lot_id] for c in cards if c.lot_id in lots]
    # В карточке выдачи у многолотовых торгов название первого лота — проверяем ещё раз по самому лоту
    ordered = [l for l in ordered if not is_excluded(f"{l['title']} {l.get('description', '')}", exclude)]
    found =[lot for lot in ordered if passes(lot, flt)] if details else ordered
    for lot in found:
        cache.mark_seen(lot)
    if run.get("only_new"):
        found = [l for l in found if l["is_new"] or (l["prev_price"] and l["price_now"] and l["price_now"] < l["prev_price"])]
    key, reverse = SORTS.get(run.get("sort_by", "discount_now"), SORTS["discount_now"])
    found.sort(key=key, reverse=reverse)
    cache.save()

    out = ROOT / run.get("output_dir", "output") / f"lots_{datetime.now():%Y-%m-%d_%H-%M-%S}.xlsx"
    write_excel(out, found, {
        "Ссылка на поиск": search_url(query),
        "Лотов в выдаче": len(cards) + len(skipped),
        "Исключено по словам": len(skipped),
        "Прошли фильтр": len(found),
        "Новых": sum(1 for l in found if l.get("is_new")),
        "Ошибок загрузки": errors,
        "Фильтр [search]": search,
        "Фильтр [filter]": flt,
    })

    minutes = (time.monotonic() - started) / 60
    print(f"\nГотово за {minutes:.1f} мин. Прошли фильтр: {len(found)} из {len(cards)}"
          f" (новых: {sum(1 for l in found if l.get('is_new'))}, ошибок: {errors})")
    print(f"Excel: {out}")
    for lot in found[:5]:
        disc = f"-{lot['discount_now'] * 100:.0f}%" if lot.get("discount_now") else ""
        print(f"  {lot['number']:<18} {fmt_money(lot.get('price_now')):>13} ₽ {disc:>5}  {lot['title'][:60]}")

    if args.open:
        os.startfile(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nОстановлено. Уже открытые лоты сохранены в кэш — следующий запуск продолжит с них.")
        sys.exit(130)
