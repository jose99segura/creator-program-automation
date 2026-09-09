"""The boundary. What gets in, what gets turned away, and what gets tidied up
on the way through.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from creator_program.models import PostIn, SubmissionIn
from creator_program.runner import run_one
from creator_program import queue

GOOD = {"external_id": "APP-1", "email": "a@creatorhub.io", "handle": "@a",
        "platform": "youtube", "followers": 50_000}


def test_accepts_a_good_application():
    s = SubmissionIn.model_validate(GOOD)
    assert s.followers == 50_000


def test_handle_is_normalised():
    """"@creator" and "creator" are one person.

    Normalising here means every later comparison is a plain equality, rather
    than every call site remembering to strip the @ and one of them forgetting.
    """
    assert SubmissionIn.model_validate({**GOOD, "handle": "@a"}).handle == "a"
    assert SubmissionIn.model_validate({**GOOD, "handle": " a "}).handle == "a"


def test_platform_is_case_insensitive():
    assert SubmissionIn.model_validate({**GOOD, "platform": "YouTube"}).platform == "youtube"


def test_unsupported_platform_is_rejected():
    """Accepting one would create a creator the tracker can never find, and
    therefore a creator who can never be paid."""
    with pytest.raises(ValidationError, match="unsupported platform"):
        SubmissionIn.model_validate({**GOOD, "platform": "twitch"})


def test_bad_email_is_rejected():
    with pytest.raises(ValidationError):
        SubmissionIn.model_validate({**GOOD, "email": "not-an-email"})


def test_negative_followers_is_rejected():
    with pytest.raises(ValidationError):
        SubmissionIn.model_validate({**GOOD, "followers": -5})


def test_unknown_fields_do_not_break_ingestion():
    """An upstream form adding a question must not take the pipeline down."""
    s = SubmissionIn.model_validate({**GOOD, "how_did_you_hear": "a friend"})
    assert s.external_id == "APP-1"


def test_implausible_view_count_is_rejected():
    """A data quality gate, not a business rule.

    Views feed the payout calculation directly, so a provider returning a
    nonsense number would turn into a nonsense transfer.
    """
    post = {"external_id": "p1", "url": "https://x/1",
            "published_at": "2026-01-01", "tracking_code": "CP-1"}
    assert PostIn.model_validate({**post, "views": 900_000}).views == 900_000
    with pytest.raises(ValidationError, match="implausible"):
        PostIn.model_validate({**post, "views": 5_000_000_000})


def test_invalid_payload_dead_letters_without_retrying(conn):
    """End to end: bad data is recorded as `invalid`, retired immediately, and
    never written to the domain tables."""
    queue.enqueue(conn, "screen", {**GOOD, "platform": "twitch"})
    run_one(conn)

    run = conn.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "invalid"

    dead = conn.execute("SELECT * FROM dead_letters").fetchone()
    assert dead["reason"] == "permanent"
    assert dead["attempts"] == 1

    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
