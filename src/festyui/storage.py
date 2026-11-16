"""SQLite хранит срок и намерение до каждой файловой операции."""

import json
import sqlite3


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS files (
          id INTEGER PRIMARY KEY, original TEXT, source TEXT, path TEXT, category TEXT,
          sig TEXT, first REAL, deadline REAL, state TEXT, digest TEXT,
          error TEXT DEFAULT '', replacement INTEGER);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Повреждена база учёта")

    def rows(self, where="1", args=()):
        return list(
            self.db.execute("SELECT * FROM files WHERE " + where + " ORDER BY first,id", args)
        )

    def get(self, ident):
        return self.db.execute("SELECT * FROM files WHERE id=?", (ident,)).fetchone()

    def update(self, ident, **values):
        self.db.execute(
            "UPDATE files SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
            (*values.values(), ident),
        )
        self.db.commit()

    def add(self, **values):
        c = self.db.execute(
            "INSERT INTO files ("
            + ",".join(values)
            + ") VALUES ("
            + ",".join("?" for _ in values)
            + ")",
            tuple(values.values()),
        )
        self.db.commit()
        return c.lastrowid

    def meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()
