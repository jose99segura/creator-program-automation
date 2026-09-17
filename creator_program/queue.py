"""The durable queue: four functions, and the seam the whole design turns on.

Steps never call each other. A step finishes by enqueuing a `kind`, and has no
idea what handles it or when. That is what makes the steps independent in a
way that survives contact with reality: reordering the pipeline, running a
step twice, or running two steps on different machines are all configuration
rather than a rewrite.

It is a table rather than Airflow, Temporal or SQS because the mechanics are
the thing being demonstrated, and a workflow engine hides exactly them. The
README says what to swap in when the table stops being enough; because the
rest of the code only ever calls the four functions below, that swap is this
file and nothing else.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db, obs
from .config import config
from .retry import backoff_seconds


def enqueue(conn: sqlite3.Connection, kind: str, payload: dict,
            delay_seconds: float = 0.0) -> int:
    """Add work. Returns the task id."""
    due = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    cur = conn.execute(
        """INSERT INTO tasks (kind, payload, next_attempt_at, created_at, updated_at,
                              max_attempts)
           VALUES (?,?,?,?,?,?)""",
        (kind, json.dumps(payload), due.isoformat(timespec="seconds"),
         obs.now(), obs.now(), config.max_attempts),
    )
    # No commit: inside `db.transaction` this lands with the work that caused
    # it, so a step cannot store its result and lose the follow-up task.
    return int(cur.lastrowid)


def claim(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Take the oldest task that is due, and mark it running.

    The UPDATE and the SELECT are one statement so that a task cannot be
    handed out twice. On Postgres this is where `FOR UPDATE SKIP LOCKED`
    would go and several workers could drain in parallel; SQLite serialises
    writers, so this is a single-worker queue by construction.

    `status = 'running'` is set before the handler runs, so a process that is
    killed mid-task leaves visible evidence rather than a task that looks
    pending and quietly ran twice. Evidence alone would strand it, though, so
    an expired lease is recovered first: see `recover_expired`.
    """
    recover_expired(conn)
    with db.transaction(conn):
        row = conn.execute(
            """SELECT * FROM tasks
               WHERE status = 'pending' AND next_attempt_at <= ?
               ORDER BY next_attempt_at, id LIMIT 1""",
            (obs.now(),),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ?",
                (obs.now(), row["id"]),
            )
    return row


def recover_expired(conn: sqlite3.Connection) -> int:
    """Fail tasks whose worker died mid-run. Returns how many.

    Without this a killed process leaves its task 'running' forever: never
    retried, never dead lettered, invisible to `dlq`. Recovering it through
    `fail` rather than resetting it to pending means it spends an attempt, so
    a task that crashes the worker every time ends in the dead letter instead
    of taking the worker down on every drain.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=config.lease_seconds)
              ).isoformat(timespec="seconds")
    stale = conn.execute(
        "SELECT * FROM tasks WHERE status = 'running' AND updated_at <= ?",
        (cutoff,),
    ).fetchall()
    for task in stale:
        error = f"lease expired after {config.lease_seconds}s: worker died mid-task"
        outcome = fail(conn, task, error)
        obs.record(conn, task["id"], task["kind"],
                   "transient" if outcome == "retry" else "permanent", 0, error)
    return len(stale)


def complete(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute(
        "UPDATE tasks SET status = 'done', updated_at = ? WHERE id = ?",
        (obs.now(), task_id),
    )


def fail(conn: sqlite3.Connection, task: sqlite3.Row, error: str,
         permanent: bool = False) -> str:
    """Reschedule the task, or retire it to the dead letter queue.

    Returns 'retry' or 'dead' so the caller can log which happened.

    A permanent failure skips the remaining attempts entirely. Retrying a
    request that is wrong does not make it right, and three more attempts only
    delay the moment a human finds out.
    """
    attempts = task["attempts"] + 1
    exhausted = attempts >= task["max_attempts"]

    if permanent or exhausted:
        # One transaction: a crash between these two would leave a dead
        # letter for a task that still looks like it is running.
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO dead_letters (task_id, kind, payload, attempts, error,
                                             reason, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (task["id"], task["kind"], task["payload"], attempts, error,
                 "permanent" if permanent else "exhausted", obs.now()),
            )
            conn.execute(
                """UPDATE tasks SET status = 'done', attempts = ?, last_error = ?,
                                    updated_at = ? WHERE id = ?""",
                (attempts, error, obs.now(), task["id"]),
            )
        obs.counters["dead_lettered"] += 1
        return "dead"

    due = datetime.now(timezone.utc) + timedelta(
        seconds=backoff_seconds(attempts, config.backoff_base_seconds))
    conn.execute(
        """UPDATE tasks SET status = 'pending', attempts = ?, last_error = ?,
                            next_attempt_at = ?, updated_at = ? WHERE id = ?""",
        (attempts, error, due.isoformat(timespec="seconds"), obs.now(), task["id"]),
    )
    obs.counters["retried"] += 1
    return "retry"


def replay(conn: sqlite3.Connection, dead_letter_id: int) -> int:
    """Put a dead lettered task back on the queue, attempts reset.

    This is why the dead letter is a table. Once the cause is fixed -- a
    credential rotated, a bug shipped, a creator created -- the work is
    recoverable without anyone writing SQL by hand.
    """
    # Mark and enqueue in one transaction, marking first: two operators
    # replaying the same row at once cannot both pass the check, and a crash
    # cannot leave a requeued task whose dead letter still offers a replay.
    with db.transaction(conn):
        row = conn.execute(
            "SELECT * FROM dead_letters WHERE id = ? AND replayed_at IS NULL",
            (dead_letter_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"dead letter {dead_letter_id} not found, or already replayed")
        conn.execute("UPDATE dead_letters SET replayed_at = ? WHERE id = ?",
                     (obs.now(), dead_letter_id))
        return enqueue(conn, row["kind"], json.loads(row["payload"]))


def pending_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'pending'").fetchone()[0])
