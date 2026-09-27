"""Поиск лотов: сборка запроса из конфига, обход страниц выдачи, разбор карточек."""

import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import quote

from bs4 import BeautifulSoup

from .client import BASE_URL
from .dicts import CATEGORIES, REGIONS, STATUSES
from .utils import clean, parse_date, parse_money


class ConfigError(ValueError):
    pass


def _resolve(names, table, what):
    """Переводит названия (или коды) из конфига в коды сайта. Допускает часть названия."""
    codes = []
    for name in names:
        name = str(name).strip()
        if not name:
            continue
        if name in table.values():
            codes.append(name)
            continue
        low = name.lower()
        exact = [code for title, code in table.items() if title.lower() == low]
        partial = [(title, code) for title, code in table.items() if low in title.lower()]
        if exact:
            codes.append(exact[0])
        elif len(partial) == 1:
            codes.append(partial[0][1])
        elif partial:
            variants = ", ".join(t for t, _ in partial)
            raise ConfigError(f"{what} «{name}» неоднозначен, подходят: {variants}")
        else:
            raise ConfigError(f"{what} «{name}» не найден. Список: python kfc.py --list")
    return codes


def _num(value):
    return str(int(value)) if value else ""


def build_query(search):
    """Секция [search] конфига -> словарь параметров, который понимает m-ets.ru."""
    q = {"displayby": "2"}  # показывать лоты, а не торги

    if search.get("keywords"):
        q["lots"] = search["keywords"]
    # exclude_words сюда не передаём: с ними сайт ищет в ~20 раз дольше, фильтруем сами (is_excluded)

    regions = _resolve(search.get("regions", []), REGIONS, "Регион")
    if regions:
        q["xregion[]"] = regions
    categories = _resolve(search.get("categories", []), CATEGORIES, "Категория")
    if categories:
        q["search_category"] = ",".join(categories)
    statuses = _resolve(search.get("statuses", []), STATUSES, "Статус")
    if statuses:
        q["stat[]"] = statuses

    forms = [f.lower() for f in search.get("trade_forms", [])]
    if any("аукц" in f for f in forms):
        q["isauk"] = "on"
    if any("публ" in f for f in forms):
        q["ispub"] = "on"
    kinds = [k.lower() for k in search.get("trade_kinds", [])]
    if any("банкр" in k for k in kinds):
        q["isbankr"] = "on"
    if any("коммер" in k for k in kinds):
        q["iscom"] = "on"

    ranges = {
        "cena_tek": ("price_now_from", "price_now_to"),
        "cena_nach": ("start_price_from", "start_price_to"),
        "cena_min": ("min_price_from", "min_price_to"),
        "proc_snij": ("max_drop_pct_from", "max_drop_pct_to"),
    }
    for prefix, (key_from, key_to) in ranges.items():
        if _num(search.get(key_from)):
            q[f"{prefix}_ot"] = _num(search[key_from])
        if _num(search.get(key_to)):
            q[f"{prefix}_do"] = _num(search[key_to])
    return q


def encode_query(q):
    raw = json.dumps(q, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def search_url(q, page=1):
    url = f"{BASE_URL}/search?q={quote(encode_query(q), safe='')}"
    return url if page == 1 else f"{url}&page={page}"


@dataclass
class Card:
    lot_id: str          # "232477-1" — номер торгов и номер лота
    url: str
    title: str
    trade_type: str      # "Торги по банкротству. Торги в стадии приема заявок"
    region: str
    price_now: float | None
    price_min: float | None
    period_end: object   # datetime | None
    description: str


def parse_search_page(html):
    """Возвращает (карточки, всего найдено, номер последней страницы)."""
    soup = BeautifulSoup(html, "lxml")

    total = 0
    count_meta = soup.find("meta", attrs={"itemprop": "offerCount"})
    if count_meta and count_meta.get("content", "").isdigit():
        total = int(count_meta["content"])

    last_page = 1
    for a in soup.select("a[href*='/search?q=']"):
        m = re.search(r"[?&]page=(\d+)", a.get("href", ""))
        if m:
            last_page = max(last_page, int(m.group(1)))

    cards = []
    for div in soup.select("#search-result-list > div.card-so"):
        link = div.select_one("a.search-comp-item")
        if not link or not link.get("href"):
            continue
        lot_id = link["href"].strip("/")

        def text(selector):
            el = div.select_one(selector)
            return clean(el.get_text(" ")) if el else ""

        price_el = div.select_one(".price.current [itemprop=price]")
        price_now = float(price_el["content"]) if price_el and price_el.get("content") else None
        region_el = div.select_one(".comp-info-description .search-item-location") or div.select_one(".search-item-location")

        cards.append(Card(
            lot_id=lot_id,
            url=f"{BASE_URL}/{lot_id}",
            title=text(".comp-title") or text(".info .title"),
            trade_type=text(".comp-type"),
            region=clean(region_el.get_text(" ")) if region_el else "",
            price_now=price_now,
            price_min=parse_money(text(".price.min")),
            period_end=parse_date(text(".comp-dates .value")),
            description=text(".comp-body .description"),
        ))
    return cards, total, last_page


def is_excluded(text, words):
    low = (text or "").lower()
    return any(w.strip().lower() in low for w in words if w.strip())


def fetch_cards(client, q, max_pages=0, workers=3, on_page=None):
    """Все карточки лотов из выдачи. max_pages=0 — все страницы.

    Первая страница сообщает, сколько их всего, остальные грузятся параллельно.
    """
    first, total, last_page = parse_search_page(client.get(search_url(q, 1)))
    pages = min(last_page, max_pages) if max_pages else last_page
    if on_page:
        on_page(1, pages, total)

    results = {1: first}
    if pages > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(lambda p: parse_search_page(client.get(search_url(q, p)))[0], p): p
                       for p in range(2, pages + 1)}
            for done, future in enumerate(as_completed(futures), 2):
                results[futures[future]] = future.result()
                if on_page:
                    on_page(done, pages, total)

    cards, seen = [], set()
    for page in sorted(results):
        for card in results[page]:
            if card.lot_id not in seen:
                seen.add(card.lot_id)
                cards.append(card)
    return cards
