"""Step 5: work out what each creator is owed for a period.

Two rules govern this file, and both exist because it deals with money.

**Integer cents, never floats.** `0.1 + 0.2` is not `0.3` in binary floating
point, and a rounding error in a payout is a support ticket and a loss of
trust. Every amount here is an `int` of cents from beginning to end.

**Recomputed from posts, never accumulated.** The amount is derived from the
posts table on every run. A running total kept in a column would drift the
first time a task ran twice, and drift is invisible until somebody complains.
Recomputing means running this step ten times produces the same number ten
times.
"""

from __future__ import annotations

import sqlite3

from ..config import config
from ..obs import log, now


def amount_cents(total_views: int, rate_cents_per_1k: int) -> int:
    """Cents owed for a number of views, rounded half up.

    Integer arithmetic throughout: `views * rate` is exact, and the division
    by 1000 rounds by adding half the divisor before the floor. Rounding up
    at the half is the convention that favours the creator, which is the side
    to err on for a relationship you want to keep.
    """
    return (total_views * rate_cents_per_1k + 500) // 1000


def handle(conn: sqlite3.Connection, payload: dict) -> None:
    period_start = payload["period_start"]
    period_end = payload["period_end"]

    creators = conn.execute(
        "SELECT * FROM creators WHERE status = 'active'").fetchall()

    computed = skipped = 0
    for creator in creators:
        row = conn.execute(
            """SELECT COUNT(*) AS posts, COALESCE(SUM(views), 0) AS views
               FROM posts
               WHERE creator_id = ? AND published_at >= ? AND published_at < ?""",
            (creator["id"], period_start, period_end),
        ).fetchone()

        cents = amount_cents(row["views"], creator["rate_cents_per_1k"])
        if cents < config.min_payout_cents:
            # Below the floor the transfer fee exceeds the transfer. The
            # views are not lost: they stay in the posts table, so the next
            # period recomputes over the same rows if the window includes
            # them.
            skipped += 1
            continue

        # ON CONFLICT makes a second run for the same period an update rather
        # than a second payout. This is the constraint that stands between a
        # retry and paying somebody twice.
        conn.execute(
            """INSERT INTO payouts (creator_id, period_start, period_end,
                                    post_count, total_views, amount_cents, computed_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (creator_id, period_start, period_end) DO UPDATE SET
                   post_count = excluded.post_count,
                   total_views = excluded.total_views,
                   amount_cents = excluded.amount_cents,
                   computed_at = excluded.computed_at""",
            (creator["id"], period_start, period_end, row["posts"],
             row["views"], cents, now()),
        )
        computed += 1
    conn.commit()

    log.info("payouts computed", extra={
        "period_start": period_start, "period_end": period_end,
        "creators": len(creators), "computed": computed,
        "below_minimum": skipped})
