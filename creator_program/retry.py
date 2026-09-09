"""The retry policy: what is worth trying again, and how long to wait.

Two exception types, and the difference between them is the whole point.

`TransientError` is the world being briefly unavailable: a timeout, a 503, a
rate limit. Trying again later is likely to work.

`PermanentError` is the request being wrong: a creator that does not exist, a
malformed address, a post id the platform has never heard of. Trying again
produces the identical failure, so it goes straight to the dead letter queue
without burning three attempts and nine seconds to arrive at the same place.

Everything else -- an unexpected `KeyError`, a bug -- is treated as transient.
That is the safer default: a genuine bug will exhaust its attempts and land in
the dead letter anyway, while treating an unknown failure as permanent would
throw away work that a fix and a replay could recover.
"""

from __future__ import annotations

import random


class TransientError(Exception):
    """Worth retrying: the operation might succeed later, unchanged."""


class PermanentError(Exception):
    """Not worth retrying: the same input will fail the same way."""


def backoff_seconds(attempt: int, base: float) -> float:
    """Delay before attempt N, exponential with jitter.

    Jitter matters more than it looks. When the platform API comes back after
    an outage, every task that failed against it is due at once, and they all
    retry in the same instant -- a small thundering herd that can knock the
    provider straight back over. Spreading them out costs nothing.

    Growth is capped at 30 minutes: past that, the wait stops being a retry
    and starts being an outage somebody should be told about, which is what
    the dead letter and the alert are for.
    """
    delay = min(base * (2 ** max(0, attempt - 1)), 1800.0)
    return delay * (1 + random.random() * 0.25)
