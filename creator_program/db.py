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
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from .config import config

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.database, isolation_level=None)
    # Rows by column name. Positional access makes a query and its consumer
    # silently disagree the moment a column is added in the middle.
    conn.row_factory = sqlite3.Row
    # SQLite ignores REFERENCES clauses unless this is on, per connection.
    # Without it a post can point at a creator that does not exist, and the
    # payout join silently drops it.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets a reader run while a writer holds the database, which is what
    # makes `stats` usable from another terminal while `work` is draining.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Several statements that must land together, or not at all.

    The connection is in autocommit mode (`isolation_level=None`), so every
    statement outside this block is its own transaction and `conn.commit()`
    does nothing. That is the right default for single writes, and exactly
    wrong for "insert the dead letter, then retire the task": a crash between
    the two would leave both. Helpers such as `queue.enqueue` therefore never
    commit themselves, so they compose inside this block.

    It is also the cheap way to write many rows: one fsync for a batch of
    posts instead of one per post.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init(conn: sqlite3.Connection) -> None:
    """Apply the schema. Every statement is `IF NOT EXISTS`, so running it
    against an existing database is a no-op rather than an error."""
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
