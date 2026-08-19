from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from weftmark.application.handoff_context import (
    HandoffMaterializationError,
    materialize_handoff_context,
)
from weftmark.domain.evidence import (
    Evidence,
    EvidenceKind,
    EvidenceProducer,
    EvidenceState,
    EvidenceSubject,
    ProducerKind,
    SubjectKind,
)
from weftmark.domain.handoff import Handoff
from weftmark.domain.handoff_context import (
    HandoffContextBudget,
    HandoffContextSource,
    HandoffContextVariant,
    handoff_context_budget,
)
from weftmark.domain.review import ReviewDecision, ReviewOutcome
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def handoff(**changes: object) -> Handoff:
    values = {
        "id": "handoff-1",
        "task_id": "runtime-provider-swap",
        "change_set_id": "chg-1",
        "goal": "Continue the provider-neutral runtime integration",
        "repository_id": "git:/work/repo/.git",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "branch": "feature/runtime-provider",
        "worktree": "/work/repo",
        "source_observation_id": "chg-1:git:4",
        "scopes": (
            Scope.file("src/weftmark/application/**"),
            Scope.contract("runtime-provider-v0"),
        ),
        "evidence_ids": ("ev-failed", "ev-passed"),
        "decision_ids": ("review-incomplete", "review-ready"),
        "known_failures": ("Provider reconnect path is not verified",),
        "next_action": "Verify provider reconnect with a fresh worker",
        "created_by": "worker-1",
        "created_at": NOW,
        "intended_receiver_id": "worker-2",
    }
    values.update(changes)
    return Handoff(**values)  # type: ignore[arg-type]


def evidence(
    id: str,
    *,
    state: EvidenceState,
    detail: str | None = None,
    change_set_id: str = "chg-1",
) -> Evidence:
    record = Evidence.declare(
        id=id,
        kind=EvidenceKind.TEST,
        producer=EvidenceProducer(ProducerKind.CI, "ci"),
        subject=EvidenceSubject(SubjectKind.CHANGE_SET, change_set_id),
        bound_commit_sha="b" * 40,
        at=NOW,
    )
    if state is EvidenceState.DECLARED:
        return record
    if state is EvidenceState.RUNNING:
        return record.start(at=NOW + timedelta(seconds=1))
    running = record.start(at=NOW + timedelta(seconds=1))
    if state is EvidenceState.PASSED:
        return running.pass_(detail=detail, at=NOW + timedelta(seconds=2))
    if state is EvidenceState.FAILED:
        return running.fail(
            detail=detail or "failure",
            at=NOW + timedelta(seconds=2),
        )
    if state is EvidenceState.UNAVAILABLE:
        return running.unavailable(
            reason=detail or "unavailable",
            at=NOW + timedelta(seconds=2),
        )
    if state is EvidenceState.STALE:
        passed = running.pass_(detail=detail, at=NOW + timedelta(seconds=2))
        return passed.mark_stale(
            current_commit_sha="c" * 40,
            current_environment_fingerprint=None,
            at=NOW + timedelta(seconds=3),
        )
    if state is EvidenceState.SUPERSEDED:
        passed = running.pass_(detail=detail, at=NOW + timedelta(seconds=2))
        return passed.supersede(at=NOW + timedelta(seconds=3))
    raise AssertionError(state)


def decision(
    id: str,
    *,
    outcome: ReviewOutcome,
    change_set_id: str = "chg-1",
) -> ReviewDecision:
    return ReviewDecision(
        id=id,
        change_set_id=change_set_id,
        author_id="reviewer-1",
        outcome=outcome,
        head_sha="b" * 40,
        evidence_ids=(),
        findings=(),
        rationale="review narrative should remain retrieval-only",
        created_at=NOW + timedelta(minutes=1),
    )


@dataclass(frozen=True)
class CharacterCounter:
    name: str = "test-characters"

    def count(self, text: str) -> int:
        return len(text)


def materialization_inputs() -> dict[str, object]:
    return {
        "evidence_by_id": {
            "ev-failed": evidence(
                "ev-failed",
                state=EvidenceState.FAILED,
                detail="password=hunter2 should never enter automatic context",
            ),
            "ev-passed": evidence(
                "ev-passed",
                state=EvidenceState.PASSED,
                detail="long successful execution narrative",
            ),
        },
        "decisions_by_id": {
            "review-incomplete": decision(
                "review-incomplete", outcome=ReviewOutcome.EVIDENCE_INCOMPLETE
            ),
            "review-ready": decision("review-ready", outcome=ReviewOutcome.READY),
        },
        "changed_paths": (
            "src/weftmark/application/handoff_context.py",
            "tests/application/test_handoff_materializer.py",
        ),
    }


def test_standard_materialization_is_default_and_budgeted() -> None:
    record = handoff()
    result = materialize_handoff_context(record, **materialization_inputs())

    assert result.variant is HandoffContextVariant.STANDARD
    assert result.target_tokens == 1600
    assert result.hard_max_tokens == 2500
    assert result.token_count_method == "heuristic_chars4"
    assert result.within_hard_max
    assert "Provider reconnect path is not verified" in result.content
    assert "ev-failed" in result.content
    assert "state=failed" in result.content
    assert "review-incomplete" in result.content
    assert "outcome=evidence_incomplete" in result.content
    assert result.handoff_id == record.id


def test_automatic_context_does_not_copy_arbitrary_evidence_or_review_prose() -> None:
    result = materialize_handoff_context(handoff(), **materialization_inputs())

    assert "password=hunter2" not in result.content
    assert "long successful execution narrative" not in result.content
    assert "review narrative should remain retrieval-only" not in result.content


def test_chat_terminal_and_full_source_are_always_deferred() -> None:
    result = materialize_handoff_context(handoff(), **materialization_inputs())
    deferred_sources = {item.source for item in result.deferred}

    assert HandoffContextSource.CHAT_TRANSCRIPT in deferred_sources
    assert HandoffContextSource.TERMINAL_HISTORY in deferred_sources
    assert HandoffContextSource.SOURCE_FILES in deferred_sources


def test_standard_defers_diff_but_deep_can_include_bounded_untrusted_excerpt() -> None:
    diff = "+" + ("changed source line\n" * 1000)

    standard = materialize_handoff_context(
        handoff(),
        diff_excerpt=diff,
        **materialization_inputs(),
    )
    assert not standard.diff_excerpt_included
    assert "Focused diff excerpt" not in standard.content

    deep = materialize_handoff_context(
        handoff(),
        variant="deep",
        diff_excerpt=diff,
        **materialization_inputs(),
    )
    assert deep.diff_excerpt_included
    assert "Focused diff excerpt (untrusted repository content)" in deep.content
    assert "[diff excerpt truncated]" in deep.content
    assert deep.token_count <= deep.target_tokens


def test_missing_referenced_records_are_mandatory_visibility_not_silent_omissions() -> None:
    record = handoff(
        evidence_ids=("ev-missing",),
        decision_ids=("review-missing",),
    )
    result = materialize_handoff_context(record)

    assert "ev-missing" in result.content
    assert "was not supplied to materializer" in result.content
    assert "review-missing" in result.content
    deferred_ids = {id for item in result.deferred for id in item.ids}
    assert {"ev-missing", "review-missing"}.issubset(deferred_ids)


def test_readiness_critical_state_is_included_even_when_optional_summary_limits_are_zero() -> None:
    base = handoff_context_budget("compact")
    budget = HandoffContextBudget(
        variant=HandoffContextVariant.COMPACT,
        target_tokens=base.target_tokens,
        hard_max_tokens=base.hard_max_tokens,
        max_evidence_summaries=0,
        max_decision_summaries=0,
        max_changed_paths=0,
        focused_excerpt_tokens=0,
        source_modes=base.source_modes,
    )
    result = materialize_handoff_context(
        handoff(),
        budget=budget,
        **materialization_inputs(),
    )

    assert "ev-failed" in result.content
    assert "review-incomplete" in result.content
    assert "ev-passed" not in result.content
    assert "review-ready" not in result.content


def test_mandatory_context_over_hard_max_fails_closed() -> None:
    base = handoff_context_budget("compact")
    budget = HandoffContextBudget(
        variant=HandoffContextVariant.COMPACT,
        target_tokens=80,
        hard_max_tokens=120,
        max_evidence_summaries=0,
        max_decision_summaries=0,
        max_changed_paths=0,
        focused_excerpt_tokens=0,
        source_modes=base.source_modes,
    )

    with pytest.raises(HandoffMaterializationError, match="mandatory handoff context"):
        materialize_handoff_context(
            handoff(),
            budget=budget,
            token_counter=CharacterCounter(),
        )


def test_provider_token_counter_is_reported_without_changing_durable_handoff() -> None:
    record = handoff(evidence_ids=(), decision_ids=())
    before = record.to_dict()
    result = materialize_handoff_context(
        record,
        token_counter=CharacterCounter(),
        variant="deep",
    )

    assert result.token_count_method == "test-characters"
    assert result.token_count == len(result.content)
    assert record.to_dict() == before


def test_reference_identity_mismatch_fails_closed() -> None:
    with pytest.raises(HandoffMaterializationError, match="another Change Set"):
        materialize_handoff_context(
            handoff(evidence_ids=("ev-failed",), decision_ids=()),
            evidence_by_id={
                "ev-failed": evidence(
                    "ev-failed",
                    state=EvidenceState.FAILED,
                    change_set_id="chg-other",
                )
            },
        )

    with pytest.raises(HandoffMaterializationError, match="another Change Set"):
        materialize_handoff_context(
            handoff(evidence_ids=(), decision_ids=("review-incomplete",)),
            decisions_by_id={
                "review-incomplete": decision(
                    "review-incomplete",
                    outcome=ReviewOutcome.EVIDENCE_INCOMPLETE,
                    change_set_id="chg-other",
                )
            },
        )


def test_changed_paths_are_deduplicated_sorted_and_line_safe() -> None:
    result = materialize_handoff_context(
        handoff(evidence_ids=(), decision_ids=()),
        changed_paths=("z.py", "a.py", "a.py", "odd\npath.py"),
    )

    assert result.included_changed_paths == ("a.py", "odd\npath.py", "z.py")
    assert '- "odd\\npath.py"' in result.content
