"""Весь поиск целиком: выдача → открытие лотов → отбор по разнице → результат.

Лоты проверяются по порядку выдачи, пока не случится одно из:
  • просмотрено столько лотов, сколько задано (run.max_lots);
  • найдено столько подходящих, сколько нужно (run.stop_after_found);
  • пользователь остановил поиск (cancel);
  • выдача закончилась.
В любом случае возвращается то, что успели найти.

Используется и приложением (app.py), и консольным запуском (kfc.py).
"""

import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path

from .client import Cancelled, MetsClient
from .describe import describe_settings
from .excel import write_excel
from .lot import apply_window, parse_lot, update_prices
from .search import build_query, is_excluded, iter_pages, search_url, to_date
from .storage import LotCache

SORTS = {
    "discount_now": (lambda l: l.get("discount_now") or 0, True),
    "difference": (lambda l: l.get("difference") or 0, True),
    "price_now": (lambda l: l.get("price_now") or 0, False),
    "price_per_m2": (lambda l: l.get("price_per_m2") or float("inf"), False),
    "deadline": (lambda l: l.get("deadline") or datetime.max, False),
    "window_min": (lambda l: l.get("win_price_min") or float("inf"), False),
    "trade_start": (lambda l: l.get("trade_start") or datetime.max, False),
}

STOP_REASONS = {
    None: "просмотрена вся выдача",
    "user": "остановлен вручную",
    "found": "найдено нужное количество",
    "limit": "просмотрено заданное количество лотов",
}


def card_status(card):
    # "Торги по банкротству. Торги в стадии приема заявок" -> "Торги в стадии приема заявок"
    return card.trade_type.split(". ", 1)[-1]


def _window(flt):
    date_from = to_date(flt.get("window_from"))
    date_to = to_date(flt.get("window_to"))
    if date_to:
        date_to += timedelta(days=1) - timedelta(minutes=1)  # включительно, до конца дня
    return date_from, date_to


def passes(lot, flt, window_on):
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
    if window_on:
        if lot.get("win_price_min") is None:  # в выбранный период лот не торгуется
            return False
        wpct = flt.get("min_window_discount_pct", 0)
        if wpct and (lot.get("win_discount") or 0) * 100 < wpct:
            return False
        wmax = flt.get("max_window_price", 0)
        if wmax and lot["win_price_min"] > wmax:
            return False
    return True


def _is_cheaper(lot):
    return bool(lot.get("prev_price") and lot.get("price_now") and lot["price_now"] < lot["prev_price"])


def run_search(settings, root, progress=None, cancel=None, client=None, cache=None, fresh=False):
    """Выполняет поиск по настройкам {"search": ..., "filter": ..., "run": ...}.

    progress(info) получает словарь: text, fraction (0..1 или None), viewed, found, total_on_site.
    cancel — threading.Event: выставили — поиск заканчивается и возвращает найденное.
    client/cache можно передать общие (приложение делит их между поисками).
    """
    progress = progress or (lambda info: None)
    search, flt, run = settings.get("search", {}), settings.get("filter", {}), settings.get("run", {})
    root = Path(root)
    query = build_query(search)  # ConfigError при неверном регионе/категории/дате
    date_from, date_to = _window(flt)
    window_on = bool(date_from or date_to)

    client = client or MetsClient(delay=run.get("delay_sec", 0.15))
    cache = cache or LotCache(root / "data" / "lots_cache.json", run.get("cache_days", 7))
    workers = run.get("workers", 5)
    max_lots = int(run.get("max_lots") or 0) or int(run.get("max_pages") or 0) * 20
    need = int(run.get("stop_after_found") or 0)
    only_new = bool(run.get("only_new"))
    exclude = search.get("exclude_words", [])
    started = time.monotonic()

    # viewed — лоты из выдачи, взятые в работу; checked — проверенные по фильтрам
    st = {"viewed": 0, "checked": 0, "excluded": 0, "excluded_cards": 0, "errors": 0, "total_on_site": 0}
    matched = []

    def consider(lot):
        """Лот со всеми данными: исключения, цена в период, фильтры по разнице."""
        st["checked"] += 1
        if st["checked"] % 300 == 0:
            cache.save()  # чтобы при сбое не открывать лоты заново
        if is_excluded(f"{lot.get('title', '')} {lot.get('description', '')}", exclude):
            st["excluded"] += 1
            return
        apply_window(lot, date_from, date_to)
        if not passes(lot, flt, window_on):
            return
        cache.mark_seen(lot)
        if only_new and not (lot["is_new"] or _is_cheaper(lot)):
            return
        matched.append(lot)

    def stop_reason():
        if cancel is not None and cancel.is_set():
            return "user"
        if need and len(matched) >= need:
            return "found"
        return None

    def report(extra=""):
        viewed, found, total = st["viewed"], len(matched), st["total_on_site"]
        if need:
            fraction = min(1.0, found / need)
        elif max_lots:
            fraction = min(1.0, st["checked"] / max_lots)
        else:
            fraction = min(1.0, (st["checked"] + st["excluded_cards"]) / total) if total else None
        text = f"Проверено {st['checked']}"
        text += f" из {max_lots}" if max_lots else (f" из {total}" if total else "")
        text += f" · подходят {found}" + (f" из {need}" if need else "")
        if fraction and fraction > 0.03 and not need:
            eta = (time.monotonic() - started) / fraction * (1 - fraction)
            text += f" · осталось ~{eta / 60:.0f} мин" if eta >= 90 else f" · осталось ~{eta:.0f} сек"
        progress({"text": text + extra, "fraction": fraction, "viewed": viewed, "checked": st["checked"],
                  "found": found, "total_on_site": total})

    progress({"text": "Ищу лоты на МЭТС…", "fraction": None, "viewed": 0, "checked": 0, "found": 0, "total_on_site": 0})

    pending = {}
    pool = ThreadPoolExecutor(max_workers=workers)

    def fetch(card):
        lot = parse_lot(client.get(card.url, cancel=cancel), card.lot_id)
        cache.put(lot)
        return lot

    def handle(done):
        for future in done:
            card = pending.pop(future)
            try:
                lot = future.result()
            except Cancelled:
                continue
            except Exception:  # лот сняли с торгов или страница не открылась
                st["errors"] += 1
                continue
            consider(lot)

    seen = set()
    try:
        pages = iter_pages(client, query, cancel=cancel)
        try:
            for page, last_page, total, cards in pages:
                st["total_on_site"] = total
                for card in cards:
                    if max_lots and st["viewed"] >= max_lots:
                        break
                    if card.lot_id in seen:
                        continue
                    seen.add(card.lot_id)
                    if is_excluded(f"{card.title} {card.description}", exclude):
                        st["excluded"] += 1
                        st["excluded_cards"] += 1
                        continue
                    st["viewed"] += 1
                    cached = cache.get(card.lot_id, fresh=fresh)
                    if cached:
                        consider(update_prices(cached, card.price_now, card.period_end, card_status(card)))
                    else:
                        pending[pool.submit(fetch, card)] = card
                    if stop_reason():
                        break
                handle([f for f in list(pending) if f.done()])
                # Не убегаем со страницами далеко вперёд, пока лоты открываются
                while len(pending) > workers * 4 and not stop_reason():
                    done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                    handle(done)
                report(f" · открываю лоты: {len(pending)}" if pending else "")
                if stop_reason() or (max_lots and st["viewed"] >= max_lots):
                    break
        finally:
            pages.close()

        while pending and not stop_reason():
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED, timeout=1)
            handle(done)
            report(f" · открываю лоты: {len(pending)}" if pending else "")
    except Cancelled:
        pass  # остановили во время загрузки выдачи — отдаём то, что есть
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        cache.save()

    reason = stop_reason()
    if reason is None and max_lots and st["viewed"] >= max_lots:
        reason = "limit"

    key, reverse = SORTS.get(run.get("sort_by", "discount_now"), SORTS["discount_now"])
    matched.sort(key=key, reverse=reverse)
    if need:
        matched = matched[:need]

    stats = {
        "total_on_site": st["total_on_site"],
        "viewed": st["viewed"],
        "checked": st["checked"],
        "excluded": st["excluded"],
        "found": len(matched),
        "new": sum(1 for l in matched if l.get("is_new")),
        "cheaper": sum(1 for l in matched if _is_cheaper(l)),
        "errors": st["errors"],
        "seconds": round(time.monotonic() - started),
        "reason": reason,
        "reason_text": STOP_REASONS.get(reason, ""),
    }
    progress({"text": f"Готово: подходят {len(matched)}", "fraction": 1.0, "viewed": st["viewed"],
              "checked": st["checked"], "found": len(matched), "total_on_site": st["total_on_site"]})
    return {"lots": matched, "stats": stats, "search_url": search_url(query), "window": window_on}


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]+', " ", name or "").strip()
    return re.sub(r"\s+", " ", name)[:60] or "lots"


def save_excel(result, settings, root, name="lots", log=None):
    """Сохраняет результат поиска в Excel. Возвращает путь к файлу."""
    run = settings.get("run", {})
    out = Path(root) / run.get("output_dir", "output") / f"{safe_name(name)}_{datetime.now():%Y-%m-%d_%H-%M-%S}.xlsx"
    s = result["stats"]
    summary = [
        ("Поиск", name),
        ("Сохранено", datetime.now().strftime("%d.%m.%Y %H:%M")),
        ("Чем закончился", s.get("reason_text", "")),
        ("Найдено на сайте по фильтрам", s.get("total_on_site")),
        ("Просмотрено лотов", s.get("viewed")),
        ("Исключено по словам", s.get("excluded")),
        ("Подошло", s.get("found")),
        ("Из них новых / подешевели", f"{s.get('new', 0)} / {s.get('cheaper', 0)}"),
        ("Не открылись", s.get("errors")),
        ("Ссылка на этот поиск на сайте", result.get("search_url")),
    ]
    date_from, date_to = _window(settings.get("filter", {}))
    write_excel(out, result["lots"], summary, describe_settings(settings), log or [],
                window=bool(date_from or date_to), window_range=(date_from, date_to))
    return out
