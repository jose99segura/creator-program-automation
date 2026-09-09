"""SQLite, opened the same way everywhere.

SQLite rather than Postgres so that `git clone && python -m creator_program
demo` works with nothing installed. The cost is one capability, called out in
the README: Postgres can hand a task to one worker out of many with
`SELECT ... FOR UPDATE SKIP LOCKED`, and SQLite cannot, which is why `claim`
is written the way it is. Every other line of this project is unchanged by
that swap, which is the point of keeping the queue behind four functions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import config

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.database, isolation_level=None)
    # Rows by column name. Positional access makes a query and its consumer
    # silently disagree the moment a column is added in the middle.
    conn.row_factory = sqlite3.Row
    # Without this SQLite does not enforce the UNIQUE-backed idempotency the
    # schema relies on across statements in a transaction.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets a reader run while a writer holds the database, which is what
    # makes `stats` usable from another terminal while `work` is draining.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Apply the schema. Every statement is `IF NOT EXISTS`, so running it
    against an existing database is a no-op rather than an error."""
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
