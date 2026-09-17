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

from .. import db
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

    # One aggregate query rather than one per creator, and the arithmetic
    # stays in `amount_cents` so there is a single definition of the money.
    rows = conn.execute(
        """SELECT c.id, c.rate_cents_per_1k,
                  COUNT(p.id) AS posts, COALESCE(SUM(p.views), 0) AS views
           FROM creators c
           LEFT JOIN posts p ON p.creator_id = c.id
                            AND p.published_at >= ? AND p.published_at < ?
           WHERE c.status = 'active'
           GROUP BY c.id, c.rate_cents_per_1k""",
        (period_start, period_end),
    ).fetchall()

    due = [(row, amount_cents(row["views"], row["rate_cents_per_1k"])) for row in rows]
    # Below the floor the transfer fee exceeds the transfer, so nothing is
    # written. The views are not carried into the next period: periods do
    # not overlap, so a creator who never clears the minimum is never paid.
    payable = [(row, cents) for row, cents in due if cents >= config.min_payout_cents]

    # ON CONFLICT makes a second run for the same period an update rather
    # than a second payout. This is the constraint that stands between a
    # retry and paying somebody twice. One transaction, so either every
    # creator's row reflects this run or none does.
    with db.transaction(conn):
        conn.executemany(
            """INSERT INTO payouts (creator_id, period_start, period_end,
                                    post_count, total_views, amount_cents, computed_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (creator_id, period_start, period_end) DO UPDATE SET
                   post_count = excluded.post_count,
                   total_views = excluded.total_views,
                   amount_cents = excluded.amount_cents,
                   computed_at = excluded.computed_at""",
            [(row["id"], period_start, period_end, row["posts"], row["views"],
              cents, now()) for row, cents in payable],
        )

    log.info("payouts computed", extra={
        "period_start": period_start, "period_end": period_end,
        "creators": len(rows), "computed": len(payable),
        "below_minimum": len(rows) - len(payable)})
