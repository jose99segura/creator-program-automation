"""Step 1: take a batch of applications and turn it into one task each.

Fanning out is the only thing this step does, and that is on purpose. If
ingestion validated and decided as well, a single malformed record in a batch
of five hundred would fail the whole batch, and the retry would reprocess the
four hundred and ninety nine that were already fine.

One task per applicant means the blast radius of a bad record is that record.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import queue
from ..obs import log
from ..retry import PermanentError

SEED_FILE = Path(__file__).resolve().parent.parent.parent / "seed" / "submissions.json"


def handle(conn: sqlite3.Connection, payload: dict) -> None:
    source = payload.get("source", "seed")
    if source != "seed":
        # A real deployment would read a webhook table or an ATS API here.
        # Failing loudly on an unknown source beats silently ingesting
        # nothing and reporting success.
        raise PermanentError(f"unknown ingestion source {source!r}")

    records = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    for raw in records:
        queue.enqueue(conn, "screen", raw)

    log.info("batch fanned out", extra={"source": source, "applicants": len(records)})
