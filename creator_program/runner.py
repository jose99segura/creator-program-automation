"""The worker loop: claim a task, run its step, decide what its failure meant.

Every step raises and none of them catch, so the classification of failures
lives here, in one place. That is the point: if each step decided for itself
what was worth retrying, they would drift, and the one that got it wrong would
be the one quietly hammering a dead endpoint at three in the morning.

Four outcomes, and they map onto four different human responses:

    ok         nothing to do
    transient  the world was briefly unavailable; try again with backoff
    permanent  the request was wrong; dead letter it now, do not retry
    invalid    the data was wrong; dead letter it now, and tell somebody

`invalid` is separated from `permanent` even though both go straight to the
dead letter, because they are different problems. Permanent means this
pipeline asked for something that does not exist. Invalid means something
upstream sent malformed data, and upstream is where the fix belongs.
"""

from __future__ import annotations

import json
import sqlite3
import time

from pydantic import ValidationError

from . import obs, queue
from .retry import PermanentError, TransientError
from .steps import HANDLERS


def run_one(conn: sqlite3.Connection) -> bool:
    """Process a single due task. Returns False when the queue is empty."""
    task = queue.claim(conn)
    if task is None:
        return False

    handler = HANDLERS.get(task["kind"])
    started = time.monotonic()

    if handler is None:
        # An unknown kind is a deployment problem: a task was enqueued by a
        # version of the code that knew a step this one does not. Retrying
        # cannot fix it, and losing it silently would be worse.
        elapsed = int((time.monotonic() - started) * 1000)
        error = f"no handler for kind {task['kind']!r}"
        queue.fail(conn, task, error, permanent=True)
        obs.record(conn, task["id"], task["kind"], "permanent", elapsed, error)
        return True

    try:
        handler(conn, json.loads(task["payload"]))
    except ValidationError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        error = f"validation failed: {exc.error_count()} error(s): {exc.errors()[0]['msg']}"
        queue.fail(conn, task, error, permanent=True)
        obs.record(conn, task["id"], task["kind"], "invalid", elapsed, error)
    except PermanentError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        queue.fail(conn, task, str(exc), permanent=True)
        obs.record(conn, task["id"], task["kind"], "permanent", elapsed, str(exc))
    except (TransientError, Exception) as exc:  # noqa: BLE001
        # An unexpected exception is treated as transient on purpose. A real
        # bug will exhaust its attempts and land in the dead letter anyway,
        # whereas calling every unknown failure permanent would throw away
        # work that a fix and a replay could have recovered.
        elapsed = int((time.monotonic() - started) * 1000)
        error = f"{type(exc).__name__}: {exc}"
        outcome = queue.fail(conn, task, error, permanent=False)
        status = "transient" if outcome == "retry" else "permanent"
        obs.record(conn, task["id"], task["kind"], status, elapsed, error)
    else:
        elapsed = int((time.monotonic() - started) * 1000)
        queue.complete(conn, task["id"])
        obs.record(conn, task["id"], task["kind"], "ok", elapsed)

    return True


def drain(conn: sqlite3.Connection, max_tasks: int = 1000) -> dict:
    """Work until nothing is due, then report once.

    Stops when the queue has nothing *due*, not when it is empty: tasks
    waiting out a backoff are still there. That is what makes a scheduled
    drain the natural unit of work -- it does what it can now, and the next
    run picks up whatever has become due since.

    `max_tasks` is a stop so that a step which enqueues itself by mistake
    cannot spin forever.
    """
    # Counters are per process, so a second drain would otherwise report
    # the first one's numbers again. The summary is this drain's delta.
    before = dict(obs.counters)
    processed = 0
    while processed < max_tasks and run_one(conn):
        processed += 1

    def delta(key: str) -> int:
        return obs.counters[key] - before.get(key, 0)

    summary = {
        "processed": processed,
        "ok": delta("task.ok"),
        "retried": delta("retried"),
        "dead_lettered": delta("dead_lettered"),
        "still_pending": queue.pending_count(conn),
    }
    obs.notify(summary)
    return summary
