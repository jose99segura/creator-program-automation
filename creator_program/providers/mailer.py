"""Stand-in for an email provider.

Logs instead of sending. It exists to give the onboarding step a real external
call to make, with the same two failure classes as everything else, so that
`onboard` is not the one step in the pipeline that cannot go wrong.
"""

from __future__ import annotations

import uuid

from ..obs import log
from ..retry import PermanentError


def send_welcome(email: str, handle: str, tracking_code: str) -> str:
    """Send the welcome message. Returns a provider message id.

    An address the provider rejects is permanent: the same address will be
    rejected identically on every retry. It belongs in the dead letter, where
    somebody can correct it and replay, rather than in a retry loop.
    """
    if email.endswith("@nosuchmail.io"):
        raise PermanentError(
            f"provider rejected {email!r}: domain does not accept mail")

    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    log.info("welcome email sent", extra={
        "email": email, "handle": handle, "tracking_code": tracking_code,
        "message_id": message_id})
    return message_id
