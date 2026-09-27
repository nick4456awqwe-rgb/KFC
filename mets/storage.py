"""Локальный кэш лотов между запусками.

Хранит разобранные страницы лотов (график цен не меняется — повторно лот не качаем),
дату, когда лот впервые попался, и цену на прошлом запуске — для пометок NEW и «подешевел».
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

_DATE_KEYS = ("start", "end", "deadline", "next_date")


def _to_json(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    raise TypeError(type(value))


def _restore_dates(lot):
    for key in _DATE_KEYS:
        if isinstance(lot.get(key), str):
            lot[key] = datetime.fromisoformat(lot[key])
    for row in lot.get("schedule") or []:
        _restore_dates(row)
    return lot


class LotCache:
    def __init__(self, path, max_age_days=7):
        self.path = Path(path)
        self.max_age = timedelta(days=max_age_days)
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.data = {}

    def get(self, lot_id):
        """Лот из кэша, если он есть и не старше max_age_days."""
        entry = self.data.get(lot_id)
        if not entry or "lot" not in entry:
            return None
        if datetime.now() - datetime.fromisoformat(entry["fetched_at"]) > self.max_age:
            return None
        return _restore_dates(json.loads(json.dumps(entry["lot"])))

    def put(self, lot):
        entry = self.data.setdefault(lot["lot_id"], {})
        entry["fetched_at"] = datetime.now().isoformat(timespec="minutes")
        entry["lot"] = json.loads(json.dumps(lot, default=_to_json))

    def mark_seen(self, lot):
        """Ставит lot['is_new'] и lot['prev_price'] по данным прошлых запусков."""
        entry = self.data.setdefault(lot["lot_id"], {})
        lot["is_new"] = "first_seen" not in entry
        lot["prev_price"] = entry.get("last_price")
        entry.setdefault("first_seen", datetime.now().isoformat(timespec="minutes"))
        entry["last_price"] = lot.get("price_now")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, default=_to_json), encoding="utf-8")
        tmp.replace(self.path)
