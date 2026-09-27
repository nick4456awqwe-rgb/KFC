"""Локальная память лотов между запусками.

Хранит разобранные страницы лотов (график цен не меняется — повторно лот не качаем),
дату, когда лот впервые попался, и цену на прошлом поиске — для пометок NEW и «подешевел».
Одну память могут использовать несколько поисков сразу; при сохранении записи других
процессов с диска не теряются.
"""

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

_DATE_KEYS = ("start", "end", "deadline", "next_date", "trade_start", "apps_end", "win_min_date")


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


def lots_to_json(lots):
    return json.dumps(lots, ensure_ascii=False, default=_to_json)


def lots_from_json(text):
    return [_restore_dates(lot) for lot in json.loads(text)]


class LotCache:
    def __init__(self, path, max_age_days=7):
        self.path = Path(path)
        self.max_age = timedelta(days=max_age_days)
        self.lock = threading.RLock()
        self.dirty = set()
        self.data = self._read()

    def _read(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    def get(self, lot_id, fresh=False):
        """Лот из памяти, если он есть и не старше max_age_days (fresh=True — всегда None)."""
        if fresh:
            return None
        with self.lock:
            entry = self.data.get(lot_id)
            if not entry or "lot" not in entry:
                return None
            if datetime.now() - datetime.fromisoformat(entry["fetched_at"]) > self.max_age:
                return None
            return _restore_dates(json.loads(json.dumps(entry["lot"])))

    def put(self, lot):
        stored = json.loads(json.dumps(lot, default=_to_json))
        with self.lock:
            entry = self.data.setdefault(lot["lot_id"], {})
            entry["fetched_at"] = datetime.now().isoformat(timespec="minutes")
            entry["lot"] = stored
            self.dirty.add(lot["lot_id"])

    def mark_seen(self, lot):
        """Ставит lot['is_new'] и lot['prev_price'] по прошлым поискам и запоминает текущую цену."""
        with self.lock:
            entry = self.data.setdefault(lot["lot_id"], {})
            lot["is_new"] = "first_seen" not in entry
            lot["prev_price"] = entry.get("last_price")
            entry.setdefault("first_seen", datetime.now().isoformat(timespec="minutes"))
            entry["last_price"] = lot.get("price_now")
            self.dirty.add(lot["lot_id"])

    def size(self):
        with self.lock:
            return sum(1 for v in self.data.values() if "lot" in v)

    def save(self):
        with self.lock:
            # Сначала подтягиваем то, что успели записать другие процессы, затем пишем своё
            on_disk = self._read()
            for key in self.dirty:
                on_disk[key] = self.data[key]
            self.data = on_disk
            self.dirty.clear()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, default=_to_json), encoding="utf-8")
            tmp.replace(self.path)
