"""Step 4: find what a creator has published, and keep its view count fresh.

Runs repeatedly for the same creator, which makes idempotency the whole
problem rather than a detail. The tracker seeing the same post on twenty
consecutive polls must produce one row with a current view count, not twenty
rows: `payout` sums this table, so a duplicate here is money.

The UNIQUE constraint on (platform, external_id) is what enforces that, and
the ON CONFLICT clause is what makes a repeat poll an update instead of an
error.
"""

from __future__ import annotations

import sqlite3

from ..models import PostIn
from ..obs import log, now
from ..providers import platform
from ..retry import PermanentError


def handle(conn: sqlite3.Connection, payload: dict) -> None:
    creator = conn.execute(
        "SELECT * FROM creators WHERE id = ?", (payload["creator_id"],)
    ).fetchone()
    if creator is None:
        raise PermanentError(f"no creator {payload['creator_id']}")
    if creator["status"] != "active":
        # Not an error. A paused creator is a normal state, and the task ends
        # having correctly done nothing.
        log.info("creator not active, skipping",
                 extra={"creator_id": creator["id"], "status": creator["status"]})
        return

    # Raises TransientError on a 503 and PermanentError on an unknown code.
    # Neither is caught here: the runner owns that decision, so every step
    # treats failure the same way and none of them can get it subtly wrong.
    raw_posts = platform.fetch_posts(creator["tracking_code"])

    stored = 0
    for raw in raw_posts:
        # Validated one at a time. One malformed post in a response of forty
        # must not discard the other thirty nine, so this failure is counted
        # and logged rather than raised.
        try:
            post = PostIn.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - bad data, not a broken run
            log.error("post failed validation", extra={
                "creator_id": creator["id"], "raw": raw, "error": str(exc)})
            continue

        conn.execute(
            """INSERT INTO posts (creator_id, platform, external_id, url,
                                  published_at, views, first_seen_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT (platform, external_id) DO UPDATE SET
                   views = excluded.views,
                   last_seen_at = excluded.last_seen_at""",
            (creator["id"], creator["platform"], post.external_id, post.url,
             post.published_at, post.views, now(), now()),
        )
        stored += 1
    conn.commit()

    log.info("posts tracked", extra={
        "creator_id": creator["id"], "tracking_code": creator["tracking_code"],
        "returned": len(raw_posts), "stored": stored})
