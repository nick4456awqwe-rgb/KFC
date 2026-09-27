"""Разбор страницы лота m-ets.ru и расчёт «разницы» (снижения цены)."""

import re
from datetime import datetime

from bs4 import BeautifulSoup

from .client import BASE_URL
from .utils import clean, parse_date, parse_money

_KAD_RE = re.compile("kadNumReportDialog")

# Скрипты, стили, иконки и выпадающие списки не нужны для разбора, а страницы торгов
# со многими лотами весят до 5 МБ (одних <option> там десятки тысяч)
_STRIP_RE = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<svg\b.*?</svg>|<select\b.*?</select>|<!--.*?-->", re.S | re.I)


def _info_items(blocks):
    """Все пары «заголовок: значение» из блоков сведений о лоте.

    Ключ — «Подраздел / Заголовок», чтобы различать, например, ФИО должника и ФИО управляющего.
    """
    items = {}
    for block in blocks:
        section = ""
        for item in block.find_all("div", class_="lot-info-item"):
            title_el = item.find(class_="title")
            if not title_el:
                continue
            title = clean(title_el.get_text(" ")).rstrip(" ?")
            if "title" in item.get("class", []):  # подзаголовок раздела
                section = title
                continue
            value_el = item.find(class_="value")
            if value_el is None:
                continue
            value = clean(value_el.get_text(" "))
            items.setdefault(f"{section} / {title}" if section else title, value)
            items.setdefault(title, value)
    return items


def _find(items, *needles):
    """Первое значение, у которого заголовок содержит все подстроки."""
    for key, value in items.items():
        low = key.lower()
        if all(n.lower() in low for n in needles):
            return value
    return ""


def _schedule(soup):
    rows = []
    for tr in soup.find_all("tr", class_="price__tables"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        longdates = [clean(s.get_text()) for s in tr.find_all("span", class_="longdate")]
        rows.append({
            "n": clean(cells[0].get_text()),
            "start": parse_date(longdates[0]) if longdates else parse_date(cells[1].get_text()),
            "end": parse_date(longdates[1]) if len(longdates) > 1 else parse_date(cells[2].get_text()),
            "price": parse_money(cells[3].get_text()),
            "deposit": parse_money(cells[4].get_text()) if len(cells) > 4 else None,
            "status": tr.get("data-status", ""),
        })
    return rows


def _cost_block(soup):
    """Блок справа от фото: цена, задаток, статус, даты."""
    out = {"dates": {}, "price": None, "deposit": None}
    for item in soup.find_all("div", class_="lot-cost-item"):
        kinds = item.get("class", [])
        if "price" in kinds and out["price"] is None:
            meta = item.find("meta", attrs={"itemprop": "price"})
            if meta and meta.get("content"):
                out["price"] = float(meta["content"])
        elif "zadat" in kinds and out["deposit"] is None:
            value = item.find(class_="value")
            out["deposit"] = parse_money(value.get_text()) if value else None
        elif "date" in kinds:
            title, value = item.find(class_="title"), item.find(class_="value")
            if title and value:
                out["dates"][clean(title.get_text()).rstrip(":")] = parse_date(value.get_text())
    status = soup.find(class_="lot-status-name")
    out["status"] = clean(status.get_text()) if status else ""
    bids = soup.find(class_="lot-bid-couner")
    m = re.search(r"\d+", bids.get_text()) if bids else None
    out["bids"] = int(m.group(0)) if m else None
    return out


def _pct(part, whole):
    if not part or not whole:
        return None
    return round(1 - part / whole, 4)


def _current_row(schedule, price_now, now):
    """Текущий период графика: сначала по цене с сайта, затем по датам."""
    if not schedule:
        return None
    if price_now:
        by_price = [r for r in schedule if r["price"] and abs(r["price"] - price_now) < 1]
        if len(by_price) == 1:
            return by_price[0]
    for r in schedule:
        if r["start"] and r["end"] and r["start"] <= now < r["end"]:
            return r
    if schedule[0]["start"] and now < schedule[0]["start"]:
        return schedule[0]
    return schedule[-1] if schedule[-1]["end"] and now >= schedule[-1]["end"] else None


def update_prices(lot, price_now=None, deadline=None, status=None, now=None):
    """Пересчитывает всё, что меняется со временем: текущую цену, разницу, период, срок.

    Статичные данные (начальная цена, график) берутся из лота, свежие — из выдачи поиска,
    поэтому уже открытый лот не нужно скачивать заново.
    """
    now = now or datetime.now()
    schedule = lot.get("schedule") or []
    row = _current_row(schedule, price_now, now)
    if price_now is None:
        price_now = row["price"] if row else lot.get("price_now") or lot.get("start_price")
    if deadline is None:
        deadline = row["end"] if row else lot.get("deadline")
    if status:
        lot["status"] = status

    next_row = None
    if row is not None:
        idx = schedule.index(row)
        next_row = schedule[idx + 1] if idx + 1 < len(schedule) else None

    if not lot.get("trade_start") and schedule:
        lot["trade_start"] = schedule[0]["start"]

    start, area = lot.get("start_price"), lot.get("area")
    lot.update({
        "price_now": price_now,
        "difference": round(start - price_now, 2) if start and price_now else None,
        "discount_now": _pct(price_now, start),
        "discount_max": _pct(lot.get("price_min"), start),
        "period": f"{schedule.index(row) + 1} из {len(schedule)}" if row else "",
        "next_date": next_row["start"] if next_row else None,
        "next_price": next_row["price"] if next_row else None,
        "deadline": deadline,
        "days_left": round((deadline - now).total_seconds() / 86400, 1) if deadline else None,
        "deposit": row["deposit"] if row and row.get("deposit") else lot.get("deposit"),
        "price_per_m2": round(price_now / area) if price_now and area else None,
    })
    return lot


def apply_window(lot, date_from=None, date_to=None, now=None):
    """Цены лота в выбранном периоде (по графику снижения).

    win_price_start — цена в начале периода, win_price_min — самая низкая цена внутри периода,
    win_min_date — с какого числа она действует, win_discount — её снижение от начальной цены.
    Если лот в этот период не торгуется — поля пустые.
    """
    keys = ("win_price_start", "win_price_min", "win_min_date", "win_discount")
    if not date_from and not date_to:
        for k in keys:
            lot.pop(k, None)
        return lot
    now = now or datetime.now()
    start = date_from or now
    end = date_to or datetime.max
    for k in keys:
        lot[k] = None

    schedule = lot.get("schedule") or []
    if schedule:
        rows = [r for r in schedule if r["price"] and r["start"] and r["end"] and r["start"] <= end and r["end"] > start]
        if rows:
            best = min(rows, key=lambda r: r["price"])
            lot["win_price_start"] = rows[0]["price"]
            lot["win_price_min"] = best["price"]
            lot["win_min_date"] = max(best["start"], start)
    else:
        # Аукцион: цена не снижается, важно лишь, что заявки принимаются в этот период
        opens, closes = lot.get("trade_start"), lot.get("apps_end") or lot.get("deadline")
        if (not opens or opens <= end) and (not closes or closes > start):
            lot["win_price_start"] = lot["win_price_min"] = lot.get("price_now")
            lot["win_min_date"] = max(opens, start) if opens else start
    lot["win_discount"] = _pct(lot["win_price_min"], lot.get("start_price"))
    return lot


def _soup(html):
    return BeautifulSoup(_STRIP_RE.sub("", html), "lxml")


def _lots_from_soup(soup, trade_no, now):
    # На странице торгов с несколькими лотами лежат все лоты сразу, каждый в своём
    # generalview-container; общие сведения (торги, должник, контакты) — вне их.
    shared = [b for b in soup.find_all("div", class_="lot-info-block") if not b.find_parent(class_="generalview-container")]
    shared_items = _info_items(shared)
    lots = {}
    for scope in soup.find_all("div", class_="generalview-container", attrs={"data-lotnumber": True}):
        lot_id = f"{trade_no}-{scope['data-lotnumber'].strip()}"
        lots[lot_id] = _parse_scope(scope, shared_items, lot_id, now)
    return lots


def parse_trade(html, trade_no, now=None):
    """Все лоты со страницы торгов: {lot_id: лот}. Страница разбирается один раз на все лоты."""
    return _lots_from_soup(_soup(html), trade_no, now)


def parse_lot(html, lot_id, now=None):
    """Один лот со страницы. Если нужны несколько лотов одних торгов — parse_trade."""
    soup = _soup(html)
    lots = _lots_from_soup(soup, lot_id.rsplit("-", 1)[0], now)
    if lot_id in lots:
        return lots[lot_id]
    if lots:
        raise ValueError(f"Лота {lot_id} нет на странице торгов")
    return _parse_scope(soup, {}, lot_id, now)  # страница без блоков по лотам — берём целиком


def _parse_scope(scope, shared_items, lot_id, now):
    items = _info_items(scope.find_all("div", class_="lot-info-block"))
    for key, value in shared_items.items():
        items.setdefault(key, value)
    cost = _cost_block(scope)
    schedule = _schedule(scope)

    h2 = scope.find("h2", class_="lot-title")
    title = clean(h2.get_text(" ")) if h2 else ""
    title = re.sub(r"\s*еще$", "", title)

    form = _find(items, "Форма проведения торгов")

    start_price = parse_money(_find(items, "Начальная цена продажи"))
    if not start_price and schedule:
        start_price = schedule[0]["price"]
    start_price = start_price or cost.get("price")
    price_min = min((r["price"] for r in schedule if r["price"]), default=None) or cost.get("price")

    # Срок, до которого можно подать заявку по текущей цене
    deadline = None
    for label, dt in cost["dates"].items():
        if "окончание" in label.lower():
            deadline = dt

    # Когда начинаются торги (первый период) и когда заканчивается приём заявок
    trade_start = parse_date(_find(items, "Начало предоставления заявок"))
    if schedule and schedule[0]["start"]:
        trade_start = schedule[0]["start"]
    apps_end = parse_date(_find(items, "Окончание предоставления заявок"))
    if not apps_end and schedule:
        apps_end = schedule[-1]["end"]

    # Площадь: сначала здание/помещение, иначе участок
    area = parse_money(items.get("Площадь", "")) or parse_money(items.get("Площадь участка", ""))

    cadastral = sorted({m for el in scope.find_all(onclick=_KAD_RE)
                        for m in re.findall(r"kadNumReportDialog\('([^']+)'\)", el["onclick"])})

    description = _find(items, "об имуществе")
    description = re.sub(r"^Проверено модератором\s*", "", description)
    description = re.sub(r"\s*\.\.\.\s*Показать полностью\s*", " ", description)

    debtor = _find(items, "Сведения о должнике /", "ФИО") or _find(items, "Сведения о должнике /", "наименование")
    manager = next((v for k, v in items.items() if "управляющий / ФИО" in k), "")

    lot = {
        "lot_id": lot_id,
        "url": f"{BASE_URL}/{lot_id}",
        "number": lot_id.replace("-", "-МЭТС-", 1),
        "title": title,
        "categories": _find(items, "Категории поиска").replace(" ,", ","),
        "region": _find(items, "Регион местонахождения"),
        "trade_kind": _find(items, "Вид торгов"),
        "trade_form": form,
        "status": cost["status"],
        "trade_start": trade_start,
        "apps_end": apps_end,
        "start_price": start_price,
        "price_min": price_min,
        "deposit": cost.get("deposit"),
        "bids": cost.get("bids"),
        "area": area,
        "cadastral": ", ".join(cadastral),
        "year": _find(items, "Год выпуска"),
        "encumbrance": _find(items, "обременений"),
        "debtor": debtor,
        "debtor_inn": _find(items, "Сведения о должнике /", "ИНН"),
        "manager": manager,
        "case_number": _find(items, "Номер дела"),
        "organizer_phone": _find(items, "Контактная информация", "телефон") or _find(items, "Телефон"),
        "organizer_email": _find(items, "Контактная информация", "почты") or _find(items, "почты"),
        "description": description,
        "schedule": schedule,
    }
    return update_prices(lot, price_now=cost.get("price"), deadline=deadline, now=now)
