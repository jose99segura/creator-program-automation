"""Command line interface.

Every command is something you would actually want at three in the morning:
what is queued, what failed, what did it pay, put that failed thing back.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from . import db, obs, queue, runner
from .retry import PermanentError


def cmd_init(conn, args) -> None:
    db.init(conn)
    print("Schema applied.")


def cmd_ingest(conn, args) -> None:
    task_id = queue.enqueue(conn, "ingest", {"source": args.source})
    print(f"Queued ingest task {task_id} from {args.source!r}.")


def cmd_work(conn, args) -> None:
    summary = runner.drain(conn, max_tasks=args.max)
    print(f"Processed {summary['processed']}: {summary['ok']} ok, "
          f"{summary['retried']} retried, {summary['dead_lettered']} dead lettered. "
          f"{summary['still_pending']} still queued.")


def cmd_track(conn, args) -> None:
    """Poll every active creator once.

    Onboarding tracks a creator once, but views keep growing for weeks, so
    this is what a scheduler runs to keep the posts table, and therefore the
    payouts computed from it, current.
    """
    with db.transaction(conn):
        ids = [row["id"] for row in conn.execute(
            "SELECT id FROM creators WHERE status = 'active' ORDER BY id")]
        for creator_id in ids:
            queue.enqueue(conn, "track", {"creator_id": creator_id})
    print(f"Queued {len(ids)} track task(s).")


def cmd_approve(conn, args) -> None:
    """Accept an applicant who was sent to review, and onboard them.

    `screen` stops at 'review' because it is not allowed to make that call.
    Without this command the only way on was an UPDATE by hand.
    """
    with db.transaction(conn):
        updated = conn.execute(
            """UPDATE submissions SET status = 'accepted', reason = ?
               WHERE external_id = ? AND status = 'review'""",
            (f"approved by hand: {args.note}", args.external_id),
        ).rowcount
        if updated != 1:
            raise PermanentError(
                f"no applicant {args.external_id!r} waiting for review")
        task_id = queue.enqueue(conn, "onboard", {"external_id": args.external_id})
    print(f"Approved {args.external_id}; onboard task {task_id} queued.")


def cmd_payout(conn, args) -> None:
    task_id = queue.enqueue(conn, "payout", {
        "period_start": args.start, "period_end": args.end})
    print(f"Queued payout task {task_id} for {args.start} to {args.end}.")


def cmd_stats(conn, args) -> None:
    print("Queue")
    for row in conn.execute(
            "SELECT status, COUNT(*) n FROM tasks GROUP BY status ORDER BY status"):
        print(f"  {row['status']:<10} {row['n']}")

    print("\nLast 24h by outcome")
    rows = conn.execute(
        """SELECT kind, status, COUNT(*) n, CAST(AVG(duration_ms) AS INT) avg_ms
           FROM runs WHERE started_at >= ?
           GROUP BY kind, status ORDER BY kind, status""",
        # Same format as the stored timestamps. SQLite's datetime('now') puts
        # a space where they have a 'T', and string comparison then counts
        # the whole of yesterday.
        ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"),),
    ).fetchall()
    for row in rows or []:
        print(f"  {row['kind']:<10} {row['status']:<10} {row['n']:>4}  "
              f"avg {row['avg_ms']}ms")
    if not rows:
        print("  (nothing yet)")

    dead = conn.execute(
        "SELECT COUNT(*) FROM dead_letters WHERE replayed_at IS NULL").fetchone()[0]
    print(f"\nDead letters awaiting a decision: {dead}")

    print("\nProgramme")
    for table, label in [("submissions", "applicants"), ("creators", "creators"),
                         ("posts", "posts"), ("payouts", "payouts")]:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {label:<12} {n}")


def cmd_dlq(conn, args) -> None:
    rows = conn.execute(
        """SELECT id, kind, attempts, reason, error, created_at
           FROM dead_letters WHERE replayed_at IS NULL ORDER BY id""").fetchall()
    if not rows:
        print("Dead letter queue is empty.")
        return
    for row in rows:
        print(f"  [{row['id']}] {row['kind']:<10} {row['reason']:<10} "
              f"attempts={row['attempts']}  {row['error'][:70]}")
    print(f"\nReplay one with: python -m creator_program replay <id>")


def cmd_replay(conn, args) -> None:
    task_id = queue.replay(conn, args.id)
    print(f"Dead letter {args.id} requeued as task {task_id}.")


def cmd_payouts(conn, args) -> None:
    rows = conn.execute(
        """SELECT c.handle, c.platform, p.period_start, p.period_end,
                  p.post_count, p.total_views, p.amount_cents
           FROM payouts p JOIN creators c ON c.id = p.creator_id
           ORDER BY p.amount_cents DESC""").fetchall()
    if not rows:
        print("No payouts computed yet.")
        return
    total = 0
    for row in rows:
        total += row["amount_cents"]
        print(f"  {row['handle']:<18} {row['platform']:<10} "
              f"{row['post_count']:>2} posts  {row['total_views']:>8} views  "
              f"{row['amount_cents'] / 100:>8.2f} EUR")
    print(f"  {'TOTAL':<18} {'':<10} {'':>2}        {'':>8}        "
          f"{total / 100:>8.2f} EUR")


def cmd_demo(conn, args) -> None:
    """Everything, end to end, on a fresh database.

    Drains twice on purpose. The platform provider fails a quarter of its
    calls, so the first drain leaves tasks waiting out a backoff; the second
    picks them up once they are due. Watching that happen is the point of the
    demo.
    """
    import time

    db.init(conn)
    queue.enqueue(conn, "ingest", {"source": "seed"})

    print("\n--- drain 1 -------------------------------------------------")
    cmd_work(conn, argparse.Namespace(max=1000))

    pending = queue.pending_count(conn)
    if pending:
        print(f"\n{pending} task(s) waiting out a backoff. Sleeping 5s.")
        time.sleep(5)
        print("\n--- drain 2 (retries) ---------------------------------------")
        cmd_work(conn, argparse.Namespace(max=1000))

    print("\n--- payouts -------------------------------------------------")
    cmd_payout(conn, argparse.Namespace(start="2026-01-01", end="2027-01-01"))
    cmd_work(conn, argparse.Namespace(max=1000))
    print()
    cmd_payouts(conn, args)

    print("\n--- stats ---------------------------------------------------")
    cmd_stats(conn, args)
    print("\n--- dead letters --------------------------------------------")
    cmd_dlq(conn, args)


def main() -> None:
    obs.configure()
    parser = argparse.ArgumentParser(prog="creator_program")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the tables").set_defaults(func=cmd_init)

    p = sub.add_parser("ingest", help="queue a batch of applications")
    p.add_argument("--source", default="seed")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("work", help="process every task that is due")
    p.add_argument("--max", type=int, default=1000)
    p.set_defaults(func=cmd_work)

    sub.add_parser("track", help="queue a poll of every active creator").set_defaults(func=cmd_track)

    p = sub.add_parser("approve", help="accept an applicant waiting for review")
    p.add_argument("external_id")
    p.add_argument("--note", default="no note")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("payout", help="queue a payout run for a period")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.set_defaults(func=cmd_payout)

    sub.add_parser("stats", help="queue depth, outcomes, programme totals").set_defaults(func=cmd_stats)
    sub.add_parser("payouts", help="what has been computed").set_defaults(func=cmd_payouts)
    sub.add_parser("dlq", help="tasks that gave up").set_defaults(func=cmd_dlq)

    p = sub.add_parser("replay", help="put a dead lettered task back on the queue")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_replay)

    sub.add_parser("demo", help="run the whole thing end to end").set_defaults(func=cmd_demo)

    args = parser.parse_args()
    conn = db.connect()
    try:
        args.func(conn, args)
    except Exception as exc:  # noqa: BLE001
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
