"""Предпросмотр без записи в базу и без перемещения файлов."""

import hashlib
import sqlite3

from .engine import matches, safe_open, signature


def preview(cfg):
    cfg.validate_dirs()
    dbpath = cfg.data / "state.sqlite3"
    rows = []
    if dbpath.exists():
        with sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM files WHERE state='active' AND category='images'"
            ).fetchall()
    for path in sorted(cfg.root.iterdir()):
        category = cfg.category(path)
        if not category:
            continue
        try:
            sig = signature(path)
        except (OSError, ValueError):
            continue
        print(f"{path.name!r} → {cfg.rules[category]['dir']} (после ожидания стабильности)")
        if category == "images":
            with safe_open(path) as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            if not matches(path, sig):
                print("  Файл меняется: сравнение отложено")
                continue
            for row in rows:
                old = cfg.root / row["path"]
                if (
                    old.parent == cfg.root / cfg.rules["images"]["dir"]
                    and row["digest"] == digest
                    and matches(old, row["sig"])
                ):
                    print(
                        f"  Кандидат на замену: {row['path']!r}; перед заменой будет побайтовая проверка"
                    )
    print("Предпросмотр завершён. Файлы не изменены.")
