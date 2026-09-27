"""KFC — поиск выгодных лотов на МЭТС (m-ets.ru), консольный запуск.

Обычно удобнее приложение: KFC.bat (app.py). Консоль — для запуска по расписанию.

    python kfc.py              поиск по настройкам из config.toml
    python kfc.py --pages 2    быстрая проверка: только 2 страницы выдачи (40 лотов)
    python kfc.py --list       все регионы, категории и статусы для config.toml
    python kfc.py --open       открыть Excel после выгрузки
"""

import argparse
import os
import sys
import tomllib
from pathlib import Path

from mets.dicts import CATEGORIES, REGIONS, STATUSES
from mets.runner import run_search
from mets.search import ConfigError

ROOT = Path(__file__).resolve().parent


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

    last_stage = [None]

    def progress(stage, done, total, text):
        if stage != last_stage[0] and last_stage[0] is not None:
            print()
        last_stage[0] = stage
        print(f"\r  {text}    ", end="", flush=True)

    try:
        result = run_search(cfg, ROOT, progress, fresh=args.fresh, max_pages=args.pages,
                            details=False if args.no_details else None)
    except ConfigError as e:
        print(f"Ошибка в {args.config}: {e}")
        return 2

    s = result["stats"]
    print(f"\n\nГотово за {s['minutes']} мин. В выдаче {s['in_results']}, исключено по словам {s['excluded']},"
          f" прошли фильтр {s['found']} (новых {s['new']}, подешевели {s['cheaper']}, ошибок {s['errors']})")
    print(f"Excel: {result['file']}")
    for lot in result["lots"][:5]:
        disc = f"-{lot['discount_now'] * 100:.0f}%" if lot.get("discount_now") else ""
        print(f"  {lot['number']:<18} {fmt_money(lot.get('price_now')):>13} ₽ {disc:>5}  {lot['title'][:60]}")

    if args.open:
        os.startfile(result["file"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nОстановлено. Уже открытые лоты сохранены в кэш — следующий запуск продолжит с них.")
        sys.exit(130)
