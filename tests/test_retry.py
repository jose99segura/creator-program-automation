"""The failure path. The half of the system that only runs when things break,
and therefore the half most likely to be broken without anyone noticing.
"""

from __future__ import annotations

import json

from creator_program import queue
from creator_program.retry import backoff_seconds


def test_backoff_grows_and_is_jittered():
    base = 2.0
    first = [backoff_seconds(1, base) for _ in range(50)]
    second = [backoff_seconds(2, base) for _ in range(50)]

    # Exponential: every attempt-2 wait exceeds the longest attempt-1 wait.
    assert min(second) > max(first)
    # Jittered: identical inputs do not produce identical waits, which is what
    # stops every task that failed against one outage from retrying in the
    # same instant.
    assert len(set(first)) > 1
    # Never below the nominal delay, never more than 25 percent above it.
    assert all(base <= d <= base * 1.25 for d in first)


def test_backoff_is_capped():
    """Past half an hour it is an outage, not a retry."""
    assert backoff_seconds(50, 2.0) <= 1800 * 1.25


def test_transient_failure_is_rescheduled(conn):
    task_id = queue.enqueue(conn, "track", {"creator_id": 1})
    task = queue.claim(conn)

    assert queue.fail(conn, task, "503 from the platform") == "retry"

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0] == 0
    # It is pending but not due: `claim` must not hand it straight back.
    assert queue.claim(conn) is None


def test_permanent_failure_skips_the_remaining_attempts(conn):
    """A wrong request does not become right by being repeated."""
    queue.enqueue(conn, "onboard", {"external_id": "APP-404"})
    task = queue.claim(conn)

    assert queue.fail(conn, task, "no such submission", permanent=True) == "dead"

    row = conn.execute("SELECT * FROM dead_letters").fetchone()
    assert row["reason"] == "permanent"
    assert row["attempts"] == 1  # not 4


def test_task_dies_after_max_attempts(conn):
    queue.enqueue(conn, "track", {"creator_id": 1})

    outcomes = []
    for _ in range(10):
        task = conn.execute(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
        if task["status"] == "done":
            break
        outcomes.append(queue.fail(conn, task, "503"))

    assert outcomes[-1] == "dead"
    assert outcomes.count("retry") == 3  # max_attempts is 4
    assert conn.execute(
        "SELECT reason FROM dead_letters").fetchone()["reason"] == "exhausted"


def test_replay_puts_the_work_back(conn):
    """Why the dead letter is a table and not a log line."""
    queue.enqueue(conn, "onboard", {"external_id": "APP-1"})
    task = queue.claim(conn)
    queue.fail(conn, task, "transient outage", permanent=True)

    dl_id = conn.execute("SELECT id FROM dead_letters").fetchone()["id"]
    new_task_id = queue.replay(conn, dl_id)

    new_task = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (new_task_id,)).fetchone()
    assert new_task["status"] == "pending"
    assert new_task["attempts"] == 0  # a clean slate, not a resumed count
    assert json.loads(new_task["payload"]) == {"external_id": "APP-1"}


def test_replaying_twice_is_refused(conn):
    """Otherwise a nervous operator pays somebody three times."""
    import pytest

    queue.enqueue(conn, "onboard", {"external_id": "APP-1"})
    queue.fail(conn, queue.claim(conn), "boom", permanent=True)
    dl_id = conn.execute("SELECT id FROM dead_letters").fetchone()["id"]

    queue.replay(conn, dl_id)
    with pytest.raises(ValueError):
        queue.replay(conn, dl_id)


def test_a_claimed_task_is_not_handed_out_again(conn):
    queue.enqueue(conn, "ingest", {"source": "seed"})
    assert queue.claim(conn) is not None
    assert queue.claim(conn) is None
