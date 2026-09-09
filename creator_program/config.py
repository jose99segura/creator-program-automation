"""Everything tunable, in one place.

Business rules live here rather than inside the steps that apply them. A
reviewer asking "what does it take to be accepted" should find the answer in
one file, not by reading `screen.py` and hoping there is not a second
condition somewhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    database: str = os.environ.get("DATABASE_PATH", "creator_program.db")

    # --- screening -------------------------------------------------------
    # Below this, decline. Between the two, send to a human. Above, accept.
    # Two thresholds rather than one because "not obviously yes" and
    # "obviously no" deserve different outcomes, and collapsing them either
    # rejects good applicants or floods the review queue.
    min_followers: int = _int("MIN_FOLLOWERS", 5_000)
    review_followers: int = _int("REVIEW_FOLLOWERS", 20_000)
    allowed_platforms: tuple[str, ...] = ("youtube", "tiktok", "instagram")

    # --- payouts ---------------------------------------------------------
    # Cents per 1,000 views. Integer on purpose; see the note in schema.sql.
    rate_cents_per_1k: int = _int("RATE_CENTS_PER_1K", 250)
    # Nobody is paid for a post nobody saw, and paying out sub-cent amounts
    # costs more in transaction fees than it transfers.
    min_payout_cents: int = _int("MIN_PAYOUT_CENTS", 100)

    # --- queue -----------------------------------------------------------
    max_attempts: int = _int("MAX_ATTEMPTS", 4)
    backoff_base_seconds: float = _float("BACKOFF_BASE_SECONDS", 2.0)

    # --- the fake platform API ------------------------------------------
    # The provider fails this often, deterministically seeded, so that the
    # retry and dead letter paths are exercised by `demo` rather than only by
    # the tests. An automation whose failure path has never run is an
    # automation whose failure path does not work.
    platform_failure_rate: float = _float("PLATFORM_FAILURE_RATE", 0.25)
    platform_seed: int = _int("PLATFORM_SEED", 7)

    # --- alerting --------------------------------------------------------
    # Any endpoint accepting a JSON body. Unset means failures still reach
    # stderr and the `dlq` command: quieter, never silent.
    alert_webhook_url: str | None = os.environ.get("ALERT_WEBHOOK_URL") or None


config = Config()
