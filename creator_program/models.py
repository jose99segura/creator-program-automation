"""The validation gate.

Nothing reaches a table without passing through one of these. That boundary is
deliberate and it is where the cheapest possible rejection happens: a bad
applicant record caught here costs a log line, and the same record caught in
the payouts table costs a transfer to the wrong person and a conversation
about it.

Validation failures are not crashes. A malformed payload is data, not an
outage, so it is recorded as `invalid`, sent to the dead letter with reason
`permanent`, and included in the alert. Retrying it would be pointless: the
payload will be exactly as malformed in four seconds.
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator


class SubmissionIn(BaseModel):
    """An application, as it arrives from the form or the webhook.

    `extra` fields are ignored rather than rejected: an upstream form that
    adds a question should not take the pipeline down. Missing or wrong
    fields are still fatal for that record, because those are the ones the
    rest of the pipeline reads.
    """

    external_id: str = Field(min_length=1, max_length=64)
    email: EmailStr
    handle: str = Field(min_length=1, max_length=64)
    platform: str
    followers: int = Field(ge=0)

    @field_validator("platform")
    @classmethod
    def known_platform(cls, value: str) -> str:
        """Lowercased and checked against the allowed list.

        An unknown platform is rejected here rather than later because the
        tracker has no way to look for posts on it, so accepting the
        applicant would create a creator who can never be paid.
        """
        from .config import config

        value = value.strip().lower()
        if value not in config.allowed_platforms:
            raise ValueError(
                f"unsupported platform {value!r}, expected one of "
                f"{', '.join(config.allowed_platforms)}")
        return value

    @field_validator("handle")
    @classmethod
    def strip_at(cls, value: str) -> str:
        """"@creator" and "creator" are the same person.

        Normalising on the way in means every later comparison is a plain
        string equality, instead of every call site remembering to strip.
        """
        return value.strip().lstrip("@")


class PostIn(BaseModel):
    """A post as the platform API returns it."""

    external_id: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1)
    published_at: str
    views: int = Field(ge=0)
    tracking_code: str = Field(min_length=1)

    @field_validator("views")
    @classmethod
    def sane_views(cls, value: int) -> int:
        """A hard ceiling on a single post's view count.

        This is a data quality gate, not a business rule. Views feed the
        payout calculation directly, so a provider returning a nonsense
        number -- a parsing bug, a units change, a corrupted response -- would
        turn straight into a nonsense transfer. The number below is far above
        anything plausible, which is the point: it only ever fires on data
        that is wrong rather than data that is surprising.
        """
        if value > 1_000_000_000:
            raise ValueError(f"implausible view count: {value}")
        return value
