"""Step 2: validate one application, store it, and decide what happens to it.

This is the boundary. The raw record is untrusted until `SubmissionIn`
accepts it, and nothing downstream ever re-checks, because everything
downstream reads the stored row rather than the payload.

The record is stored before the decision, always, including when it is
rejected. Keeping only accepted applicants would make "how many people applied
and how many did we turn away" unanswerable, and that is the first question
anyone running a creator program asks.
"""

from __future__ import annotations

import json
import sqlite3

from .. import db, queue
from ..config import config
from ..models import SubmissionIn
from ..obs import log, now


def decide(followers: int) -> tuple[str, str]:
    """Accept, reject, or send to a human. Returns (status, reason).

    Three outcomes rather than two. Collapsing 'review' into 'accepted'
    onboards people nobody looked at; collapsing it into 'rejected' throws
    away the applicants most worth a conversation. The thresholds live in
    config.py so that changing the programme's bar is a config change and not
    a code review.
    """
    if followers < config.min_followers:
        return "rejected", f"{followers} followers, below the {config.min_followers} minimum"
    if followers < config.review_followers:
        return "review", f"{followers} followers, between the automatic thresholds"
    return "accepted", f"{followers} followers, above {config.review_followers}"


def handle(conn: sqlite3.Connection, payload: dict) -> None:
    # Raises pydantic.ValidationError on a bad payload. The runner turns that
    # into a dead letter with reason 'permanent': the payload will be exactly
    # as malformed on a retry, so there is nothing to wait for.
    submission = SubmissionIn.model_validate(payload)
    status, reason = decide(submission.followers)

    # INSERT OR IGNORE plus a UNIQUE external_id is the whole of idempotency
    # here. Replaying the same batch stores the applicant once.
    #
    # The follow-up is only enqueued when this run stored the row. Otherwise
    # re-reading a feed would onboard every accepted applicant again, and a
    # welcome email is not idempotent. The insert and the enqueue share a
    # transaction, so a crash cannot store the applicant and lose the task.
    with db.transaction(conn):
        inserted = conn.execute(
            """INSERT OR IGNORE INTO submissions
               (external_id, email, handle, platform, followers, status, reason, raw, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (submission.external_id, str(submission.email), submission.handle,
             submission.platform, submission.followers, status, reason,
             json.dumps(payload), now()),
        ).rowcount == 1

        # 'review' deliberately enqueues nothing. The applicant waits for a
        # human, who moves them on with `approve`. A step that cannot proceed
        # without a decision it is not allowed to make should stop, not guess.
        if inserted and status == "accepted":
            queue.enqueue(conn, "onboard", {"external_id": submission.external_id})

    log.info("applicant screened", extra={
        "external_id": submission.external_id, "handle": submission.handle,
        "platform": submission.platform, "followers": submission.followers,
        "decision": status, "duplicate": not inserted})
