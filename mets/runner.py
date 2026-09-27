"""Весь поиск целиком: выдача → открытие лотов → отбор по разнице → Excel.

Используется и приложением (app.py), и консольным запуском (kfc.py).
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from .client import Cancelled, MetsClient
from .excel import write_excel
from .lot import parse_lot, update_prices
from .search import build_query, fetch_cards, is_excluded, search_url
from .storage import LotCache

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


def _is_cheaper(lot):
    return bool(lot.get("prev_price") and lot.get("price_now") and lot["price_now"] < lot["prev_price"])


def run_search(cfg, root, progress=None, cancel=None, fresh=False, max_pages=None, details=None):
    """Выполняет поиск по настройкам cfg = {"search": ..., "filter": ..., "run": ...}.

    progress(stage, done, total, text) вызывается по ходу работы; stage: search | lots | excel.
    cancel — threading.Event: если выставить, поиск остановится (исключение Cancelled).
    Возвращает словарь с путём к Excel, найденными лотами и статистикой.
    """
    progress = progress or (lambda *a: None)
    search, flt, run = cfg.get("search", {}), cfg.get("filter", {}), cfg.get("run", {})
    max_pages = run.get("max_pages", 0) if max_pages is None else max_pages
    details = run.get("fetch_details", True) if details is None else details
    root = Path(root)

    query = build_query(search)  # ConfigError при неверном регионе/категории
    client = MetsClient(delay=run.get("delay_sec", 0.3), cancel=cancel)
    cache = LotCache(root / "data" / "lots_cache.json", max_age_days=0 if fresh else run.get("cache_days", 7))
    workers = run.get("workers", 3)
    started = time.monotonic()

    progress("search", 0, 0, "Ищу лоты на МЭТС…")
    stats = {"total_on_site": 0}

    def on_page(done, pages, total):
        stats["total_on_site"] = total
        progress("search", done, pages, f"Загружаю выдачу: страница {done} из {pages} (на сайте найдено {total})")

    cards = fetch_cards(client, query, max_pages, workers, on_page=on_page)
    exclude = search.get("exclude_words", [])
    skipped = [c for c in cards if is_excluded(f"{c.title} {c.description}", exclude)]
    cards = [c for c in cards if c not in skipped]

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
            from_cache = len(cards) - len(to_fetch)
            progress("lots", 0, len(to_fetch), f"Открываю лоты: 0 из {len(to_fetch)} (ещё {from_cache} из кэша)")
            fetch_started = time.monotonic()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(lambda c: parse_lot(client.get(c.url), c.lot_id), c): c for c in to_fetch}
                for i, future in enumerate(as_completed(futures), 1):
                    card = futures[future]
                    try:
                        lot = future.result()
                        cache.put(lot)
                    except Cancelled:
                        pool.shutdown(wait=False, cancel_futures=True)
                        raise
                    except Exception as e:  # лот мог быть снят или страница не открылась
                        lot = lot_from_card(card)
                        lot["description"] = f"[не удалось открыть: {e}] {lot['description']}"
                        errors += 1
                    lots[card.lot_id] = lot
                    if i % 5 == 0 or i == len(to_fetch):
                        eta = (time.monotonic() - fetch_started) / i * (len(to_fetch) - i)
                        left = f"осталось ~{eta / 60:.0f} мин" if eta >= 60 else f"осталось ~{eta:.0f} сек"
                        progress("lots", i, len(to_fetch), f"Открываю лоты: {i} из {len(to_fetch)}, {left}")
                    if i % 200 == 0:
                        cache.save()  # чтобы при обрыве не начинать заново
    finally:
        cache.save()

    progress("excel", 0, 0, "Отбираю лоты и сохраняю Excel…")
    ordered = [lots[c.lot_id] for c in cards if c.lot_id in lots]
    # В карточке выдачи у многолотовых торгов название первого лота — проверяем ещё раз по самому лоту
    ordered = [l for l in ordered if not is_excluded(f"{l['title']} {l.get('description', '')}", exclude)]
    found = [lot for lot in ordered if passes(lot, flt)] if details else ordered
    for lot in found:
        cache.mark_seen(lot)
    if run.get("only_new"):
        found = [l for l in found if l["is_new"] or _is_cheaper(l)]
    key, reverse = SORTS.get(run.get("sort_by", "discount_now"), SORTS["discount_now"])
    found.sort(key=key, reverse=reverse)
    cache.save()

    stats.update({
        "in_results": len(cards) + len(skipped),
        "excluded": len(skipped),
        "found": len(found),
        "new": sum(1 for l in found if l.get("is_new")),
        "cheaper": sum(1 for l in found if _is_cheaper(l)),
        "errors": errors,
        "minutes": round((time.monotonic() - started) / 60, 1),
    })

    out = root / run.get("output_dir", "output") / f"lots_{datetime.now():%Y-%m-%d_%H-%M-%S}.xlsx"
    write_excel(out, found, {
        "Ссылка на поиск": search_url(query),
        "Лотов в выдаче": stats["in_results"],
        "Исключено по словам": stats["excluded"],
        "Прошли фильтр": stats["found"],
        "Новых": stats["new"],
        "Ошибок загрузки": errors,
        "Фильтр [search]": search,
        "Фильтр [filter]": flt,
    })
    return {"file": out, "lots": found, "stats": stats, "search_url": search_url(query)}
