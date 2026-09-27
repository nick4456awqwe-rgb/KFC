"""Настройки поиска человеческим языком — для листа «Что искали» в Excel."""

from .search import to_date


def _money(v):
    return f"{int(v):,}".replace(",", " ") + " ₽"


def _range(frm, to, fmt):
    if frm and to:
        return f"от {fmt(frm)} до {fmt(to)}"
    if frm:
        return f"от {fmt(frm)}"
    if to:
        return f"до {fmt(to)}"
    return None


def _date(v):
    d = to_date(v)
    return d.strftime("%d.%m.%Y") if d else ""


def _dates(frm, to):
    if frm and to:
        return f"с {_date(frm)} по {_date(to)}"
    if frm:
        return f"с {_date(frm)}"
    if to:
        return f"по {_date(to)}"
    return None


def _list(values, empty=None):
    return ", ".join(values) if values else empty


SITE_SORT_NAMES = {"new": "сначала новые на сайте", "ending": "сначала те, где скоро конец приёма заявок",
                   "cheap": "сначала дешёвые", "expensive": "сначала дорогие"}
SORT_NAMES = {"discount_now": "сильнее всего подешевели", "difference": "больше всего разница в ₽",
              "price_now": "сначала дешёвые", "price_per_m2": "дешевле за м²", "deadline": "сначала срочные",
              "window_min": "дешевле в выбранный период", "trade_start": "раньше начинаются торги"}


def describe_settings(settings):
    """Список (что, значение) — только заполненные фильтры."""
    s, f, r = settings.get("search", {}), settings.get("filter", {}), settings.get("run", {})
    pct = lambda v: f"{v} %"
    days = lambda v: f"{v} дн."
    rows = [
        ("Что ищем (строка поиска)", s.get("keywords") or None),
        ("Категории", _list(s.get("categories"), "все")),
        ("Регионы", _list(s.get("regions"), "все")),
        ("Форма торгов", _list(s.get("trade_forms"), "любая")),
        ("Вид торгов", _list(s.get("trade_kinds"), "любой")),
        ("Состояние торгов", _list(s.get("statuses"), "любое")),
        ("Исключить слова", _list(s.get("exclude_words"))),
        ("Начальная цена", _range(s.get("start_price_from"), s.get("start_price_to"), _money)),
        ("Текущая цена", _range(s.get("price_now_from"), s.get("price_now_to"), _money)),
        ("Минимальная цена", _range(s.get("min_price_from"), s.get("min_price_to"), _money)),
        ("Процент снижения по графику", _range(s.get("max_drop_pct_from"), s.get("max_drop_pct_to"), pct)),
        ("Начало приёма заявок", _dates(s.get("apps_start_from"), s.get("apps_start_to"))),
        ("Конец приёма заявок", _dates(s.get("apps_end_from"), s.get("apps_end_to"))),
        ("Цена уже снизилась", _range(f.get("min_discount_now_pct"), None, pct)),
        ("Разница с начальной ценой", _range(f.get("min_difference_rub"), None, _money)),
        ("Цена за м²", _range(None, f.get("max_price_per_m2"), _money)),
        ("Приём заявок ещё", _range(f.get("min_days_left"), None, days)),
        ("Цена в период", _dates(f.get("window_from"), f.get("window_to"))),
        ("Снижение в этот период", _range(f.get("min_window_discount_pct"), None, pct)),
        ("Мин. цена в этот период", _range(None, f.get("max_window_price"), _money)),
        ("Какие лоты смотреть первыми", SITE_SORT_NAMES.get(s.get("site_sort"))),
        ("Просмотреть лотов", r.get("max_lots") or "все"),
        ("Остановиться, когда найдено", f"{r['stop_after_found']} подходящих" if r.get("stop_after_found") else None),
        ("Только новые и подешевевшие", "да" if r.get("only_new") else None),
        ("Сортировка в таблице", SORT_NAMES.get(r.get("sort_by"))),
    ]
    return [(k, v) for k, v in rows if v not in (None, "")]
