"""Stand-in for a social platform API.

Reads canned responses from `seed/platform_posts.json` and fails on purpose.

The failures are the reason this file exists. An automation whose error path
has never executed is an automation whose error path does not work, so the
demo has to hit it. Two kinds are injected, matching the two the retry policy
distinguishes:

* a transient 503, which should be retried and usually succeed
* a permanent 404 for an unknown tracking code, which should not be retried
  at all and should land in the dead letter immediately

Failures are seeded rather than random so that a run is reproducible: the same
seed produces the same failures, which is what makes a test able to assert on
them and a demo able to show the same thing twice.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from ..config import config
from ..retry import PermanentError, TransientError

SEED_FILE = Path(__file__).resolve().parent.parent.parent / "seed" / "platform_posts.json"

_rng = random.Random(config.platform_seed)


def _load() -> dict[str, list[dict]]:
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


def fetch_posts(tracking_code: str) -> list[dict]:
    """Posts carrying this tracking code.

    Returning an empty list is a valid answer, not a failure: a creator who
    has been onboarded and has not published yet is the normal state for the
    first week. Only an unknown code is an error, because that means the
    pipeline is asking about somebody the platform has never seen, which no
    amount of retrying will change.
    """
    if _rng.random() < config.platform_failure_rate:
        raise TransientError("platform API returned 503")

    posts = _load()
    if tracking_code not in posts:
        raise PermanentError(f"unknown tracking code {tracking_code!r}")
    return posts[tracking_code]
