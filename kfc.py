"""KFC — поиск выгодных лотов на МЭТС (m-ets.ru), консольный запуск.

Обычно удобнее приложение (ярлык «KFC Поиск лотов» или KFC.bat). Консоль — для запуска по расписанию.

    python kfc.py              поиск по настройкам из config.toml
    python kfc.py --lots 50    просмотреть только 50 лотов
    python kfc.py --find 10    остановиться, когда найдено 10 подходящих
    python kfc.py --list       все регионы, категории и статусы для config.toml
    python kfc.py --open       открыть Excel после выгрузки
"""

import argparse
import os
import sys
import tomllib
from pathlib import Path

from mets.dicts import CATEGORIES, REGIONS, STATUSES
from mets.runner import run_search, save_excel
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
    p.add_argument("--lots", type=int, help="просмотреть не больше N лотов (перекрывает max_lots)")
    p.add_argument("--find", type=int, help="остановиться, когда найдено N подходящих")
    p.add_argument("--fresh", action="store_true", help="игнорировать память и заново открыть все лоты")
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
    run = cfg.setdefault("run", {})
    if args.lots is not None:
        run["max_lots"] = args.lots
    if args.find is not None:
        run["stop_after_found"] = args.find

    def progress(info):
        print(f"\r  {info['text']}    ", end="", flush=True)

    try:
        result = run_search(cfg, ROOT, progress, fresh=args.fresh)
    except ConfigError as e:
        print(f"Ошибка в {args.config}: {e}")
        return 2

    s = result["stats"]
    path = save_excel(result, cfg, ROOT, "Консоль")
    print(f"\n\nГотово за {s['seconds']} сек ({s['reason_text']}). Просмотрено {s['viewed']}, исключено по словам"
          f" {s['excluded']}, подошло {s['found']} (новых {s['new']}, подешевели {s['cheaper']}, ошибок {s['errors']})")
    print(f"Excel: {path}")
    for lot in result["lots"][:5]:
        disc = f"-{lot['discount_now'] * 100:.0f}%" if lot.get("discount_now") else ""
        print(f"  {lot['number']:<18} {fmt_money(lot.get('price_now')):>13} ₽ {disc:>5}  {lot['title'][:60]}")

    if args.open:
        os.startfile(path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nОстановлено. Уже открытые лоты сохранены — следующий запуск возьмёт их из памяти.")
        sys.exit(130)
