from __future__ import annotations

from datetime import datetime, timezone

import pytest

from weftmark.application.identifiers import IdentifierError, new_id


def test_identifier_is_kind_prefixed_utc_sortable_and_unique() -> None:
    first_at = datetime(2026, 8, 14, 12, 0, 0, 1, tzinfo=timezone.utc)
    second_at = datetime(2026, 8, 14, 12, 0, 0, 2, tzinfo=timezone.utc)
    first = new_id("evidence", at=first_at)
    second = new_id("evidence", at=second_at)

    assert first.startswith("evidence-20260814T120000000001Z-")
    assert len(first.rsplit("-", 1)[1]) == 12
    assert first < second
    assert new_id("evidence", at=first_at) != first


@pytest.mark.parametrize("kind", ("", "Review", "has_space", "a" * 22))
def test_identifier_kind_is_strict(kind: str) -> None:
    with pytest.raises(IdentifierError, match="identifier kind"):
        new_id(kind)


def test_identifier_timestamp_requires_timezone() -> None:
    with pytest.raises(IdentifierError, match="timezone"):
        new_id("claim", at=datetime(2026, 8, 14, 12, 0))
