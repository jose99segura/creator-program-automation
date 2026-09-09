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

from .. import queue
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
    # here. Replaying the same batch, or retrying this task after a crash
    # between the insert and the enqueue, stores the applicant once.
    conn.execute(
        """INSERT OR IGNORE INTO submissions
           (external_id, email, handle, platform, followers, status, reason, raw, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (submission.external_id, str(submission.email), submission.handle,
         submission.platform, submission.followers, status, reason,
         json.dumps(payload), now()),
    )
    conn.commit()

    log.info("applicant screened", extra={
        "external_id": submission.external_id, "handle": submission.handle,
        "platform": submission.platform, "followers": submission.followers,
        "decision": status})

    if status == "accepted":
        queue.enqueue(conn, "onboard", {"external_id": submission.external_id})

    # 'review' deliberately enqueues nothing. The applicant sits in the table
    # waiting for a human, and the pipeline is done with them. A step that
    # cannot proceed without a decision it is not allowed to make should stop,
    # not guess.
