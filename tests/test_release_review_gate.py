import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_release_review", ROOT / "scripts/check_release_review.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
HEAD = "a" * 40


def bundle():
    return {"schema": "weftmark.release-evidence.v0", "source_sha": HEAD,
            "review": {"review_id": "review-1", "change_set_id": "alpha-release-evidence-cs",
                       "head_sha": HEAD, "outcome": "ready", "is_releasable": True},
            "test_evidence": {"pytest": {"returncode": 0}, "make_smoke": {"returncode": 0}}}


def test_exact_head_ready_review_and_passing_checks_are_required():
    gate.validate(bundle(), HEAD)


@pytest.mark.parametrize("field,value", [("review_id", ""), ("change_set_id", "other"),
                                        ("head_sha", "b" * 40), ("outcome", "ready_with_follow_up"),
                                        ("outcome", "blocked"), ("is_releasable", 1), ("is_releasable", False)])
def test_invalid_review_fails_closed(field, value):
    data = bundle()
    data["review"][field] = value
    with pytest.raises(ValueError):
        gate.validate(data, HEAD)


@pytest.mark.parametrize("change", [{"review": None}, {"review": {}}, {"source_sha": "b" * 40},
                                    {"schema": "future"}, {"test_evidence": None},
                                    {"test_evidence": {"pytest": {"returncode": False}}}])
def test_missing_malformed_or_mismatched_evidence_fails_closed(change):
    data = bundle()
    data.update(copy.deepcopy(change))
    with pytest.raises(ValueError):
        gate.validate(data, HEAD)


def test_workflow_checks_review_before_signing_or_publishing():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert workflow.index("scripts/check_release_review.py") < workflow.index("actions/attest-build-provenance")
