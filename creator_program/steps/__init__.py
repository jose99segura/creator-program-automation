"""The steps, and the only place that maps a task kind to the code that runs it.

A step is a plain function `handle(conn, payload)`. It does one thing, it
raises rather than catching, and it never imports another step: the only way
it can cause more work to happen is to enqueue a kind, which is what keeps the
boundaries between them real rather than aspirational.
"""

from . import ingest, onboard, payout, screen, track

HANDLERS = {
    "ingest": ingest.handle,
    "screen": screen.handle,
    "onboard": onboard.handle,
    "track": track.handle,
    "payout": payout.handle,
}
