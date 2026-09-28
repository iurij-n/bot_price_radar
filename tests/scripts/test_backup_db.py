"""Тесты скрипта бэкапа scripts/backup_db.py (тикет 11)."""

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backup_db  # noqa: E402


def make_db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE trackings (nm_id INTEGER PRIMARY KEY, price INTEGER)")
        conn.executemany(
            "INSERT INTO trackings (nm_id, price) VALUES (?, ?)",
            [(100 + i, 1000 * i) for i in range(rows)],
        )
    conn.close()


def run_backup(db: Path, dest_dir: Path, keep: int = 7) -> None:
    backup_db.main(["--db", str(db), "--dest-dir", str(dest_dir), "--keep", str(keep)])


def test_backup_creates_readable_snapshot(tmp_path, capsys):
    db = tmp_path / "data" / "price_radar.db"
    make_db(db)
    dest = tmp_path / "backups"

    run_backup(db, dest)

    snapshots = sorted(dest.glob("price_radar-*.db"))
    assert len(snapshots) == 1
    out = capsys.readouterr().out
    assert "снимок создан" in out

    conn = sqlite3.connect(f"{snapshots[0].as_uri()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM trackings").fetchone()[0] == 3
    finally:
        conn.close()


def test_rotation_keeps_newest_and_removes_old(tmp_path):
    db = tmp_path / "data" / "price_radar.db"
    make_db(db)
    dest = tmp_path / "backups"
    dest.mkdir()
    old_names = [f"price_radar-202609{i:02d}-000000.db" for i in range(1, 10)]
    for name in old_names:
        (dest / name).write_bytes(b"old snapshot")

    run_backup(db, dest, keep=7)

    remaining = sorted(p.name for p in dest.glob("price_radar-*.db"))
    assert len(remaining) == 7
    # новые (сегодняшний снимок) остались, 3 самых старых удалены
    for name in old_names[:3]:
        assert name not in remaining
    for name in old_names[3:]:
        assert name in remaining
    assert remaining == sorted(remaining)
    live = [p for p in dest.glob("price_radar-*.db") if p.stat().st_size > 0]
    assert any(p.name not in old_names for p in live)


def test_missing_db_exits_nonzero(tmp_path, capsys):
    missing = tmp_path / "data" / "nope.db"
    dest = tmp_path / "backups"

    with pytest.raises(SystemExit) as excinfo:
        run_backup(missing, dest)

    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    assert "БД не найдена" in err
    assert not dest.exists() or list(dest.iterdir()) == []
