from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from weftmark.application.frog_projection import _attention, _lane
from weftmark.application.frog_receipts import FrogReceiptError, _receipt_from_snapshot


def test_pinned_observation_profiles_preserve_legacy_lane_and_unknown_status_attention():
    directory = Path(__file__).with_name("fixtures")
    raw = (directory / "task-observations.v1.json").read_bytes()
    provenance = json.loads((directory / "task-observations.provenance.json").read_text())
    assert len(provenance["source_commit"]) == 40
    assert hashlib.sha256(raw).hexdigest() == provenance["sha256"]
    fixture = json.loads(raw)
    assert fixture["schema"] == "ragbaz.task-observation-fixtures/v1"
    for case in fixture["statuses"]:
        assert _lane(case["status"]).value == case["weftmark_lane"]
        assert list(_attention(case["status"], ())) == case["attention"]
    # A runtime card is not a verified immutable import receipt or a native claim.
    with pytest.raises(FrogReceiptError):
        _receipt_from_snapshot(fixture["private_projection"], imported_at=datetime.now(timezone.utc))
