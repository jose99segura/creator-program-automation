"""The money. These are the tests worth having if there were only three.

An error anywhere else in this pipeline delays a creator. An error here pays
them the wrong amount, which is the one failure mode that costs trust rather
than time.
"""

from __future__ import annotations

from creator_program.obs import now
from creator_program.steps.payout import amount_cents, handle

RATE = 250  # cents per 1,000 views


def test_exact_thousands():
    assert amount_cents(1_000, RATE) == 250
    assert amount_cents(10_000, RATE) == 2_500


def test_rounds_half_up_toward_the_creator():
    # 1,002 views is 250.5 cents. Rounding down would quietly shave half a
    # cent off every payout, which over a programme is real money and an
    # argument nobody wants to have.
    assert amount_cents(1_002, RATE) == 251
    assert amount_cents(1_001, RATE) == 250


def test_no_floating_point_drift():
    """The reason cents are integers.

    In float arithmetic, summing a per-post amount and computing the total in
    one go can disagree in the last cent. Integer arithmetic cannot: these two
    are the same expression evaluated two ways.
    """
    views = [412_000, 96_500, 88_300, 21_400, 1_900]
    one_go = amount_cents(sum(views), RATE)
    assert isinstance(one_go, int)
    assert one_go == (sum(views) * RATE + 500) // 1000


def test_zero_views_is_zero():
    assert amount_cents(0, RATE) == 0


def _creator_with_posts(conn, views: list[int]) -> int:
    conn.execute(
        """INSERT INTO submissions (external_id, email, handle, platform,
                                    followers, status, raw, created_at)
           VALUES ('A1','a@creatorhub.io','a','youtube',50000,'accepted','{}',?)""",
        (now(),))
    conn.execute(
        """INSERT INTO creators (submission_id, email, handle, platform,
                                 tracking_code, rate_cents_per_1k, onboarded_at)
           VALUES (1,'a@creatorhub.io','a','youtube','CP-TEST',?,?)""",
        (RATE, now()))
    for i, v in enumerate(views):
        conn.execute(
            """INSERT INTO posts (creator_id, platform, external_id, url,
                                  published_at, views, first_seen_at, last_seen_at)
               VALUES (1,'youtube',?,?,'2026-05-01',?,?,?)""",
            (f"p{i}", f"https://x/{i}", v, now(), now()))
    conn.commit()
    return 1


def test_running_payout_twice_does_not_pay_twice(conn):
    """The constraint that stands between a retry and a double payment."""
    _creator_with_posts(conn, [100_000])
    period = {"period_start": "2026-01-01", "period_end": "2027-01-01"}

    handle(conn, period)
    handle(conn, period)

    rows = conn.execute("SELECT * FROM payouts").fetchall()
    assert len(rows) == 1
    assert rows[0]["amount_cents"] == amount_cents(100_000, RATE)


def test_recompute_follows_the_posts(conn):
    """Amounts are derived, never accumulated.

    A new post appearing between two payout runs changes the amount to the
    new correct total, not to the old total plus something. A running counter
    would drift the first time a task ran twice.
    """
    _creator_with_posts(conn, [100_000])
    period = {"period_start": "2026-01-01", "period_end": "2027-01-01"}
    handle(conn, period)

    conn.execute(
        """INSERT INTO posts (creator_id, platform, external_id, url,
                              published_at, views, first_seen_at, last_seen_at)
           VALUES (1,'youtube','later','https://x/later','2026-06-01',50000,?,?)""",
        (now(), now()))
    conn.commit()
    handle(conn, period)

    row = conn.execute("SELECT * FROM payouts").fetchone()
    assert row["post_count"] == 2
    assert row["amount_cents"] == amount_cents(150_000, RATE)


def test_posts_outside_the_period_are_not_paid(conn):
    _creator_with_posts(conn, [100_000])  # published 2026-05-01
    handle(conn, {"period_start": "2026-01-01", "period_end": "2026-04-01"})
    assert conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0] == 0


def test_below_the_minimum_nothing_is_written(conn):
    """Under the floor the transfer fee exceeds the transfer."""
    _creator_with_posts(conn, [100])  # 25 cents, under the 100 cent minimum
    handle(conn, {"period_start": "2026-01-01", "period_end": "2027-01-01"})
    assert conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0] == 0
