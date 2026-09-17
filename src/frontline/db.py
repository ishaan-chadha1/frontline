"""Database connection and schema management.

SQLite for the pilot. The schema ports to Postgres without a redesign; the
migration trigger is concurrency, not row count.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from .config import settings

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_FILE.read_text())
    conn.commit()


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] != "init":
        print("usage: python -m frontline.db init", file=sys.stderr)
        return 2
    conn = connect()
    init_schema(conn)
    names = table_names(conn)
    print(f"initialised {settings.db_path}")
    print(f"{len(names)} tables: {', '.join(names)}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
