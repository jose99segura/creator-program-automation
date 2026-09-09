"""Structured logs, run records, counters and one alert per drain.

The pipeline is meant to run unattended on a schedule. Unattended means the
only things that exist are what it wrote down, so every task execution leaves
a JSON log line and a row in `runs`, and anything that ended badly is
collected and reported once at the end.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone

# Counters for the current process. Deliberately not persisted: the durable
# record is the `runs` table, and these exist so a drain can print a summary
# without querying back what it just wrote.
counters: Counter[str] = Counter()

# Anything that did not end 'ok', so the alert at the end is one message.
_problems: list[dict] = []


def now() -> str:
    """UTC, ISO 8601, seconds. Used for every timestamp column in the schema.

    One function rather than `datetime.now()` scattered around, because
    timestamps that come from two different places eventually disagree about
    the time zone and then ordering by them stops meaning anything.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, with `extra=` fields promoted to top level.

    Prose is not queryable. `task_id`, `kind` and `duration_ms` as values are:
    `... | jq 'select(.status != "ok")'` answers "what is broken" in one line.
    """

    RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message", "asctime", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.RESERVED:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


log = logging.getLogger("creator_program")


def configure(level: str | None = None) -> None:
    """Send JSON logs to stdout. Safe to call more than once."""
    if log.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    log.addHandler(handler)
    log.setLevel(level or os.environ.get("LOG_LEVEL", "INFO").upper())
    log.propagate = False


def record(conn, task_id: int | None, kind: str, status: str,
           duration_ms: int, error: str | None = None) -> None:
    """Append one row to `runs` and emit the matching log line.

    Both, not either. The log line is what you read while something is going
    wrong; the row is what you query a week later to find out when it started.
    """
    conn.execute(
        """INSERT INTO runs (task_id, kind, status, duration_ms, error, started_at)
           VALUES (?,?,?,?,?,?)""",
        (task_id, kind, status, duration_ms, error, now()),
    )
    conn.commit()

    counters[f"task.{status}"] += 1
    fields = {"task_id": task_id, "kind": kind, "status": status,
              "duration_ms": duration_ms}
    if status == "ok":
        log.info("task ok", extra=fields)
    else:
        fields["error"] = error
        _problems.append(dict(fields))
        log.error("task failed", extra=fields)


def problems() -> list[dict]:
    return list(_problems)


def reset() -> None:
    """Clear per-process state. Only the tests need this."""
    counters.clear()
    _problems.clear()


def notify(summary: dict) -> None:
    """One message at the end of a drain, if anything went wrong.

    One per drain rather than one per failure on purpose: an outage at the
    platform API fails every creator being tracked, and thirty creators must
    not become thirty alerts. Alert fatigue is how real failures get ignored.

    Data quality counts as going wrong. A payload that failed validation never
    reached the database, which is the correct outcome and still something a
    human needs to know about, so `invalid` is in the alert alongside crashes.
    """
    from .config import config

    if not _problems:
        log.info("drain finished clean", extra=summary)
        return

    kinds = Counter(p["kind"] for p in _problems)
    text = (f"creator-program: {len(_problems)} problem(s) in this run -- "
            + ", ".join(f"{k} x{n}" for k, n in kinds.items()))
    print(text, file=sys.stderr)

    if not config.alert_webhook_url:
        return
    try:
        import urllib.request

        body = json.dumps({"text": text, "summary": summary,
                           "problems": _problems}).encode()
        req = urllib.request.Request(
            config.alert_webhook_url, data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as exc:  # noqa: BLE001 - a broken alert channel is not a run failure
        log.error("could not deliver alert", extra={"error": str(exc)})
