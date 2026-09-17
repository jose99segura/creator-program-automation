"""Step 3: turn an accepted application into a creator who can be tracked.

Three things happen, in an order chosen so that a crash between any two of
them is recoverable: derive the tracking code, create the creator, send the
welcome email.
"""

from __future__ import annotations

import hashlib
import sqlite3

from .. import db, queue
from ..config import config
from ..obs import log, now
from ..providers import mailer
from ..retry import PermanentError


def tracking_code(external_id: str) -> str:
    """A creator's code, derived from their application id.

    Derived rather than random, and that is the entire reason this is a
    function. A random code would be a different code on every retry, so a
    task that crashed after creating the creator but before sending the email
    would, on replay, either create a second creator or send a code that does
    not match the stored one. A pure function of the input has neither
    problem: run it a hundred times, get the same code.
    """
    digest = hashlib.sha1(external_id.encode()).hexdigest()[:8].upper()
    return f"CP-{digest}"


def handle(conn: sqlite3.Connection, payload: dict) -> None:
    external_id = payload["external_id"]
    row = conn.execute(
        "SELECT * FROM submissions WHERE external_id = ?", (external_id,)
    ).fetchone()

    if row is None:
        # The application is gone. Retrying cannot bring it back, so this
        # goes to the dead letter, where a replay after fixing the cause is
        # one command.
        raise PermanentError(f"no submission {external_id!r}")
    if row["status"] != "accepted":
        raise PermanentError(
            f"submission {external_id!r} is {row['status']}, not accepted")

    code = tracking_code(external_id)
    with db.transaction(conn):
        # Identity here is the tracking code, a pure function of external_id
        # -- not the email. The email is exactly the field a human might
        # correct between a failed send and a replay, so looking this up by
        # email would miss the row that attempt already created and collide
        # with it on the UNIQUE tracking_code instead.
        creator = conn.execute(
            "SELECT * FROM creators WHERE tracking_code = ?", (code,)
        ).fetchone()

        if creator is None:
            # A *different* application already claims this email. Carrying
            # on would email this application's code, which is stored
            # nowhere, and every post tagged with it would go unpaid. A human
            # decides whether it is a duplicate or a typo.
            clash = conn.execute(
                "SELECT id FROM creators WHERE email = ?", (row["email"],)
            ).fetchone()
            if clash is not None:
                raise PermanentError(
                    f"{row['email']!r} already belongs to creator "
                    f"{clash['id']} from another application")

            conn.execute(
                """INSERT INTO creators
                   (submission_id, email, handle, platform, tracking_code,
                    rate_cents_per_1k, onboarded_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (row["id"], row["email"], row["handle"], row["platform"], code,
                 config.rate_cents_per_1k, now()),
            )
            creator = conn.execute(
                "SELECT * FROM creators WHERE tracking_code = ?", (code,)
            ).fetchone()
        elif creator["email"] != row["email"]:
            # The submission was corrected since this creator was first
            # created (e.g. after a rejected-address replay). The submission
            # is the source of truth, so sync it -- otherwise a corrected
            # address would replay forever against the one that failed.
            clash = conn.execute(
                "SELECT id FROM creators WHERE email = ? AND id != ?",
                (row["email"], creator["id"]),
            ).fetchone()
            if clash is not None:
                raise PermanentError(
                    f"{row['email']!r} already belongs to creator "
                    f"{clash['id']} from another application")
            conn.execute("UPDATE creators SET email = ? WHERE id = ?",
                        (row["email"], creator["id"]))
            creator = conn.execute(
                "SELECT * FROM creators WHERE id = ?", (creator["id"],)
            ).fetchone()

    # The email goes last, after the creator exists. If it fails, a replay
    # re-runs the whole step: the lookup above is then a no-op and only the
    # email is retried. Sending first would risk welcoming somebody the
    # database does not have.
    mailer.send_welcome(creator["email"], creator["handle"], code)

    log.info("creator onboarded", extra={
        "creator_id": creator["id"], "handle": creator["handle"],
        "tracking_code": code})

    queue.enqueue(conn, "track", {"creator_id": creator["id"]})
