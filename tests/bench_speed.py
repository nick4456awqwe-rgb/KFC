"""Замер скорости на живом сайте: сколько лотов в секунду проверяет поиск.

    python tests/bench_speed.py          по 100 лотов в двух вариантах выдачи
    python tests/bench_speed.py 50

Лоты открываются заново (временная память), рабочую память приложения замер не трогает.
«Дешёвые» — самый тяжёлый случай: там много торгов по 100–200 лотов на одной странице.
"""

import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mets.client import MetsClient  # noqa: E402
from mets.runner import run_search  # noqa: E402
from mets.storage import LotCache  # noqa: E402

ORDERS = [("new", "новые на сайте"), ("cheap", "дешёвые (большие торги)")]


def bench(n, site_sort, workers, delay):
    settings = {
        "search": {"statuses": ["Объявленные торги", "Прием заявок"], "site_sort": site_sort},
        "filter": {},
        "run": {"max_lots": n, "workers": workers, "delay_sec": delay},
    }
    with tempfile.TemporaryDirectory() as tmp:
        started = time.monotonic()
        result = run_search(settings, tmp, client=MetsClient(delay=delay), cache=LotCache(Path(tmp) / "c.json"), fresh=True)
        seconds = time.monotonic() - started
    return result["stats"]["checked"], seconds, result["stats"]["errors"]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    with open(ROOT / "config.toml", "rb") as fh:
        run = tomllib.load(fh).get("run", {})
    workers, delay = run.get("workers", 8), run.get("delay_sec", 0.1)
    print(f"Проверяю по {n} лотов с сайта МЭТС ({workers} потоков, пауза {delay} с)…")
    for site_sort, title in ORDERS:
        checked, seconds, errors = bench(n, site_sort, workers, delay)
        print(f"  {title}: {checked} лотов за {seconds:.1f} с = {checked / seconds:.1f} лота/с"
              f" (~{checked / seconds * 60:.0f} в минуту), ошибок: {errors}")


if __name__ == "__main__":
    main()
