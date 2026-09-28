"""Консистентный бэкап SQLite-БД через backup API sqlite3.

Снимок единственного файла БД делается без остановки бота: источник открывается
в read-only, копирование выполняет `sqlite3.Connection.backup()` (не raw-копия).
Ротация: в каталоге назначения остаются последние N (по умолчанию 7) снимков
вида `price_radar-YYYYmmdd-HHMMSS.db`, старые удаляются.

Запуск: `python scripts/backup_db.py [--db data/price_radar.db]
[--dest-dir backups] [--keep 7]`.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn

TIME_FORMAT = "%Y%m%d-%H%M%S"


def fail(message: str) -> NoReturn:
    print(f"backup_db: {message}", file=sys.stderr)
    sys.exit(1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Бэкап SQLite-БД с ротацией")
    parser.add_argument("--db", default="data/price_radar.db", help="файл живой БД")
    parser.add_argument("--dest-dir", default="backups", help="каталог снимков")
    parser.add_argument("--keep", type=int, default=7, help="сколько снимков оставлять")
    args = parser.parse_args(argv)
    if args.keep < 1:
        fail("--keep должен быть >= 1")
    return args


def backup(db: Path, dest_dir: Path) -> Path:
    if not db.is_file():
        fail(f"БД не найдена: {db}")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"не удалось создать каталог {dest_dir}: {exc}")

    target = dest_dir / f"{db.stem}-{datetime.now().strftime(TIME_FORMAT)}.db"
    try:
        source = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        fail(f"не удалось открыть БД {db}: {exc}")
    try:
        with source, sqlite3.connect(target) as dst:
            source.backup(dst)
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        fail(f"ошибка при копировании БД в {target}: {exc}")
    return target


def rotate(dest_dir: Path, pattern: str, keep: int) -> list[Path]:
    """Удаляет старые снимки, оставляя самые новые `keep` (имена сортируются по времени)."""
    snapshots = sorted(dest_dir.glob(pattern))
    stale = snapshots[:-keep] if len(snapshots) > keep else []
    for path in stale:
        try:
            path.unlink()
        except OSError as exc:
            fail(f"не удалось удалить старый снимок {path}: {exc}")
    return stale


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    db = Path(args.db)
    dest_dir = Path(args.dest_dir)
    target = backup(db, dest_dir)
    removed = rotate(dest_dir, f"{db.stem}-*.db", args.keep)
    print(f"backup_db: снимок создан: {target}")
    for path in removed:
        print(f"backup_db: удалён старый снимок: {path}")


if __name__ == "__main__":
    main()
