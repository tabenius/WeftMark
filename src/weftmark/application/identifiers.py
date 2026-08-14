"""Dependency-free, time-sortable local record identifiers."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone


class IdentifierError(ValueError):
    """Raised when an identifier kind or timestamp is unsafe."""


_KIND = re.compile(r"^[a-z][a-z0-9-]{0,20}$")


def new_id(kind: str, *, at: datetime | None = None) -> str:
    """Return a readable UTC-sortable ID with 48 bits of random collision space."""

    if not _KIND.fullmatch(kind):
        raise IdentifierError("identifier kind must be lowercase letters, digits, or dashes")
    timestamp = at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise IdentifierError("identifier timestamp must include a timezone")
    utc = timestamp.astimezone(timezone.utc)
    sortable = utc.strftime("%Y%m%dT%H%M%S") + f"{utc.microsecond:06d}Z"
    return f"{kind}-{sortable}-{secrets.token_hex(6)}"
