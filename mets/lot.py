"""Разбор страницы лота m-ets.ru и расчёт «разницы» (снижения цены)."""

import re
from datetime import datetime

from bs4 import BeautifulSoup

from .client import BASE_URL
from .utils import clean, parse_date, parse_money


def _info_items(blocks):
    """Все пары «заголовок: значение» из блоков сведений о лоте.

    Ключ — «Подраздел / Заголовок», чтобы различать, например, ФИО должника и ФИО управляющего.
    """
    items = {}
    for block in blocks:
        section = ""
        for item in block.select(".lot-info-item"):
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
    for tr in soup.select("tr.price__tables"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        longdates = [clean(s.get_text()) for s in tr.select("span.longdate")]
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
    out = {"dates": {}}
    price_meta = soup.select_one(".lot-cost-item.price meta[itemprop=price]")
    if price_meta and price_meta.get("content"):
        out["price"] = float(price_meta["content"])
    label = soup.select_one(".lot-cost-item.price .title")
    out["price_label"] = clean(label.get_text()) if label else ""
    deposit = soup.select_one(".lot-cost-item.zadat .value")
    out["deposit"] = parse_money(deposit.get_text()) if deposit else None
    status = soup.select_one(".lot-status-name")
    out["status"] = clean(status.get_text()) if status else ""
    for item in soup.select(".lot-cost-item.date"):
        title, value = item.find(class_="title"), item.find(class_="value")
        if title and value:
            out["dates"][clean(title.get_text()).rstrip(":")] = parse_date(value.get_text())
    bids = soup.select_one(".lot-bid-couner")
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


def parse_lot(html, lot_id, now=None):
    soup = BeautifulSoup(html, "lxml")
    # На странице торгов с несколькими лотами лежат все лоты сразу, каждый в своём
    # generalview-container; общие сведения (торги, должник, контакты) — вне их.
    lot_no = lot_id.rsplit("-", 1)[-1]
    scope = soup.select_one(f'div.generalview-container[data-lotnumber="{lot_no}"]') or soup
    shared = [b for b in soup.select(".lot-info-block") if not b.find_parent(class_="generalview-container")]
    items = _info_items(scope.select(".lot-info-block") + (shared if scope is not soup else []))
    cost = _cost_block(scope)
    schedule = _schedule(scope)

    h2 = scope.select_one("h2.lot-title")
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

    # Площадь: сначала здание/помещение, иначе участок
    area = parse_money(items.get("Площадь", "")) or parse_money(items.get("Площадь участка", ""))

    cadastral = sorted(set(re.findall(r"kadNumReportDialog\('([^']+)'\)", str(scope))))

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
