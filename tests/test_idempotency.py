"""The steps run more than once. These are the ways that must not cost money
or send a second email.
"""

from __future__ import annotations

import pytest

from creator_program.providers import mailer
from creator_program.retry import PermanentError
from creator_program.steps import onboard, screen


def _applicant(external_id: str, email: str) -> dict:
    return {"external_id": external_id, "email": email, "handle": external_id,
            "platform": "youtube", "followers": 50_000}


@pytest.fixture()
def emails(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(mailer, "send_welcome",
                        lambda email, handle, code: sent.append(code))
    return sent


def _onboard_tasks(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE kind = 'onboard'").fetchone()[0]


def test_screening_the_same_applicant_twice_onboards_them_once(conn):
    """Re-reading a feed must not queue a second welcome email."""
    screen.handle(conn, _applicant("APP-1", "a@creatorhub.io"))
    screen.handle(conn, _applicant("APP-1", "a@creatorhub.io"))
    assert _onboard_tasks(conn) == 1


def test_a_second_application_with_the_same_email_is_not_sent_a_dead_code(conn, emails):
    """The email would carry a tracking code stored nowhere, so every post
    tagged with it would go unpaid."""
    screen.handle(conn, _applicant("APP-1", "same@creatorhub.io"))
    screen.handle(conn, _applicant("APP-2", "same@creatorhub.io"))

    onboard.handle(conn, {"external_id": "APP-1"})
    with pytest.raises(PermanentError, match="already belongs"):
        onboard.handle(conn, {"external_id": "APP-2"})

    stored = [r[0] for r in conn.execute("SELECT tracking_code FROM creators")]
    assert emails == stored == [onboard.tracking_code("APP-1")]


def test_onboarding_twice_keeps_one_creator_with_the_same_code(conn, emails):
    screen.handle(conn, _applicant("APP-1", "a@creatorhub.io"))
    onboard.handle(conn, {"external_id": "APP-1"})
    onboard.handle(conn, {"external_id": "APP-1"})

    assert conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0] == 1
    assert set(emails) == {onboard.tracking_code("APP-1")}


def test_a_rolled_back_step_leaves_no_follow_up_task(conn, monkeypatch):
    """Store and enqueue are one transaction: all of it, or none."""
    from creator_program import queue

    def broken_enqueue(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(queue, "enqueue", broken_enqueue)
    with pytest.raises(RuntimeError):
        screen.handle(conn, _applicant("APP-1", "a@creatorhub.io"))

    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0


def test_correcting_the_email_after_a_failed_send_replays_clean(conn, emails):
    """The exact bug a live walkthrough of this repo found: a creator is
    created, the welcome email bounces, a human fixes the address in
    `submissions`, and the task is replayed.

    The first attempt must not be re-run against the identity it looked the
    creator up by (email, which just changed) and collide with the row it
    already made.
    """
    monkeypatch_email = "bad@nosuchmail.io"
    screen.handle(conn, _applicant("APP-1", monkeypatch_email))

    original_send = mailer.send_welcome
    def bounce(email, handle, code):
        if email == monkeypatch_email:
            raise PermanentError("provider rejected it")
        return original_send(email, handle, code)
    mailer.send_welcome = bounce
    try:
        with pytest.raises(PermanentError):
            onboard.handle(conn, {"external_id": "APP-1"})
    finally:
        mailer.send_welcome = original_send

    assert conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0] == 1

    conn.execute("UPDATE submissions SET email = 'good@creatorhub.io' WHERE external_id = 'APP-1'")
    conn.commit()

    onboard.handle(conn, {"external_id": "APP-1"})

    assert conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0] == 1
    row = conn.execute("SELECT email FROM creators").fetchone()
    assert row["email"] == "good@creatorhub.io"
    assert emails == [onboard.tracking_code("APP-1")]
