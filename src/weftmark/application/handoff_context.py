"""Budget-aware materialization of durable handoffs for receiving workers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

from weftmark.domain.evidence import Evidence, EvidenceState, SubjectKind
from weftmark.domain.handoff import Handoff
from weftmark.domain.handoff_context import (
    DEFAULT_HANDOFF_CONTEXT_VARIANT,
    HandoffContextBudget,
    HandoffContextSource,
    HandoffContextVariant,
    HandoffExpansionMode,
    handoff_context_budget,
)
from weftmark.domain.review import FindingStatus, ReviewDecision, ReviewOutcome


class HandoffMaterializationError(ValueError):
    """Raised when mandatory handoff context cannot be represented safely."""


@runtime_checkable
class TokenCounter(Protocol):
    """Optional provider-specific token counter."""

    @property
    def name(self) -> str:
        """Stable description of the counting method."""

    def count(self, text: str) -> int:
        """Return the token count for ``text``."""


@dataclass(frozen=True, slots=True)
class ApproximateTokenCounter:
    """Deterministic fallback when no provider tokenizer is available.

    Four Unicode code points per token is a planning estimate, not a claim about
    a provider tokenizer. Callers that need exact accounting should inject the
    receiving provider's counter.
    """

    chars_per_token: int = 4

    def __post_init__(self) -> None:
        if self.chars_per_token <= 0:
            raise HandoffMaterializationError("chars_per_token must be positive")

    @property
    def name(self) -> str:
        return f"heuristic_chars{self.chars_per_token}"

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, (len(text) + self.chars_per_token - 1) // self.chars_per_token)


@dataclass(frozen=True, slots=True)
class DeferredHandoffContext:
    source: HandoffContextSource
    reason: str
    ids: tuple[str, ...] = ()
    count: int = 0


@dataclass(frozen=True, slots=True)
class HandoffMaterialization:
    handoff_id: str
    variant: HandoffContextVariant
    content: str
    token_count: int
    token_count_method: str
    target_tokens: int
    hard_max_tokens: int
    included_evidence_ids: tuple[str, ...]
    included_decision_ids: tuple[str, ...]
    included_changed_paths: tuple[str, ...]
    diff_excerpt_included: bool
    deferred: tuple[DeferredHandoffContext, ...]

    @property
    def within_target(self) -> bool:
        return self.token_count <= self.target_tokens

    @property
    def within_hard_max(self) -> bool:
        return self.token_count <= self.hard_max_tokens


_CRITICAL_EVIDENCE_STATES = frozenset(
    {
        EvidenceState.DECLARED,
        EvidenceState.RUNNING,
        EvidenceState.FAILED,
        EvidenceState.UNAVAILABLE,
        EvidenceState.STALE,
    }
)

_CRITICAL_REVIEW_OUTCOMES = frozenset(
    {
        ReviewOutcome.BLOCKED,
        ReviewOutcome.STALE,
        ReviewOutcome.EVIDENCE_INCOMPLETE,
        ReviewOutcome.READY_WITH_FOLLOW_UP,
    }
)


def _quoted(value: str) -> str:
    """Render one value without allowing it to create extra prompt lines."""

    return json.dumps(value, ensure_ascii=False)


def _render_orientation(handoff: Handoff) -> str:
    lines = [
        "# WeftMark handoff",
        f"handoff: {_quoted(handoff.id)}",
        f"task: {_quoted(handoff.task_id)}",
        f"change_set: {_quoted(handoff.change_set_id)}",
        f"goal: {_quoted(handoff.goal)}",
        f"repository: {_quoted(handoff.repository_id)}",
        f"base: {_quoted(handoff.base_sha)}",
        f"head: {_quoted(handoff.head_sha)}",
        f"branch: {_quoted(handoff.branch)}",
        f"worktree: {_quoted(handoff.worktree)}",
        f"source_observation: {_quoted(handoff.source_observation_id)}",
        "scopes:",
        *(f"- {_quoted(scope.canonical)}" for scope in handoff.scopes),
        "known_failures:",
    ]
    if handoff.known_failures:
        lines.extend(f"- {_quoted(failure)}" for failure in handoff.known_failures)
    else:
        lines.append("- none recorded")
    lines.extend(("next_action:", _quoted(handoff.next_action)))
    return "\n".join(lines)


def _render_evidence(evidence: Evidence) -> str:
    """Render only structured evidence facts, never arbitrary detail text."""

    binding = evidence.bound_commit_sha or "unbound"
    line = (
        f"- {_quoted(evidence.id)}: kind={evidence.kind.value} "
        f"state={evidence.state.value} bound_commit={_quoted(binding)}"
    )
    if evidence.stale_reasons:
        reasons = ",".join(sorted(reason.value for reason in evidence.stale_reasons))
        line += f" stale_reasons={_quoted(reasons)}"
    return line


def _render_missing_reference(kind: str, id: str) -> str:
    return (
        f"- {_quoted(id)}: referenced {kind} record was not supplied to materializer"
    )


def _render_decision(decision: ReviewDecision) -> str:
    """Render decision state and open finding metadata without arbitrary rationale."""

    line = (
        f"- {_quoted(decision.id)}: outcome={decision.outcome.value} "
        f"head={_quoted(decision.head_sha)}"
    )
    open_findings = tuple(
        finding for finding in decision.findings if finding.status is FindingStatus.OPEN
    )
    if not open_findings:
        return line
    finding_lines = [line, "  open_findings:"]
    for finding in open_findings:
        finding_lines.append(
            "  - "
            f"{_quoted(finding.id)}: severity={finding.severity.value} "
            f"scope={_quoted(finding.scope.canonical)} status={finding.status.value}"
        )
    return "\n".join(finding_lines)


def _append_section(content: str, title: str, items: tuple[str, ...]) -> str:
    if not items:
        return content
    return content + f"\n\n## {title}\n" + "\n".join(items)


def _count(counter: TokenCounter, text: str) -> int:
    value = counter.count(text)
    if not isinstance(value, int) or value < 0:
        raise HandoffMaterializationError(
            "token counter must return a non-negative integer"
        )
    return value


def _try_append_within_target(
    current: str,
    *,
    title: str,
    item: str,
    section_started: bool,
    counter: TokenCounter,
    target_tokens: int,
) -> tuple[str, bool]:
    if section_started:
        candidate = current + "\n" + item
    else:
        candidate = current + f"\n\n## {title}\n" + item
    if _count(counter, candidate) > target_tokens:
        return current, section_started
    return candidate, True


def _truncate_to_tokens(text: str, *, counter: TokenCounter, max_tokens: int) -> str:
    """Return the longest deterministic prefix that fits ``max_tokens``.

    The ellipsis is included in the budget. This helper is used only for an
    explicitly optional focused diff excerpt.
    """

    if max_tokens <= 0 or not text:
        return ""
    if _count(counter, text) <= max_tokens:
        return text
    suffix = "\n…[diff excerpt truncated]"
    if _count(counter, suffix) > max_tokens:
        return ""
    low = 0
    high = len(text)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle].rstrip() + suffix
        if _count(counter, candidate) <= max_tokens:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _critical_evidence(evidence: Evidence) -> bool:
    return evidence.state in _CRITICAL_EVIDENCE_STATES


def _critical_decision(decision: ReviewDecision) -> bool:
    return decision.outcome in _CRITICAL_REVIEW_OUTCOMES


def _validate_evidence_reference(handoff: Handoff, expected_id: str, evidence: Evidence) -> None:
    if evidence.id != expected_id:
        raise HandoffMaterializationError(
            f"evidence mapping key {expected_id!r} does not match record id {evidence.id!r}"
        )
    if (
        evidence.subject.kind is SubjectKind.CHANGE_SET
        and evidence.subject.id != handoff.change_set_id
    ):
        raise HandoffMaterializationError(
            f"evidence {expected_id!r} belongs to another Change Set"
        )


def _validate_decision_reference(
    handoff: Handoff, expected_id: str, decision: ReviewDecision
) -> None:
    if decision.id != expected_id:
        raise HandoffMaterializationError(
            f"decision mapping key {expected_id!r} does not match record id {decision.id!r}"
        )
    if decision.change_set_id != handoff.change_set_id:
        raise HandoffMaterializationError(
            f"review decision {expected_id!r} belongs to another Change Set"
        )


def materialize_handoff_context(
    handoff: Handoff,
    *,
    evidence_by_id: Mapping[str, Evidence] | None = None,
    decisions_by_id: Mapping[str, ReviewDecision] | None = None,
    changed_paths: tuple[str, ...] = (),
    diff_excerpt: str | None = None,
    variant: HandoffContextVariant | str = DEFAULT_HANDOFF_CONTEXT_VARIANT,
    budget: HandoffContextBudget | None = None,
    token_counter: TokenCounter | None = None,
) -> HandoffMaterialization:
    """Render one safe, bounded receiving-worker context capsule.

    The durable handoff is never mutated. Readiness-critical evidence and
    review state are mandatory; passed/superseded evidence, ready decisions,
    changed paths and focused diff excerpts are optional expansion. Full source
    files, chat transcript and terminal history are always deferred by v0.

    Automatic evidence/review summaries contain structured state only. Their
    arbitrary prose remains addressable through record IDs instead of being
    copied into a receiving model's prompt without a separate safety decision.
    """

    if budget is not None and str(variant) != DEFAULT_HANDOFF_CONTEXT_VARIANT.value:
        raise HandoffMaterializationError(
            "pass either a named variant or a custom budget, not both"
        )
    selected = budget or handoff_context_budget(variant)
    counter = token_counter or ApproximateTokenCounter()
    if not counter.name.strip():
        raise HandoffMaterializationError("token counter name must not be empty")
    evidence_map = evidence_by_id or {}
    decision_map = decisions_by_id or {}

    content = _render_orientation(handoff)
    included_evidence: list[str] = []
    included_decisions: list[str] = []
    deferred: list[DeferredHandoffContext] = []

    critical_evidence_lines: list[str] = []
    optional_evidence: list[tuple[str, str]] = []
    missing_evidence: list[str] = []
    for id in handoff.evidence_ids:
        evidence = evidence_map.get(id)
        if evidence is None:
            missing_evidence.append(id)
            critical_evidence_lines.append(_render_missing_reference("evidence", id))
            continue
        _validate_evidence_reference(handoff, id, evidence)
        rendered = _render_evidence(evidence)
        if _critical_evidence(evidence):
            critical_evidence_lines.append(rendered)
            included_evidence.append(id)
        else:
            optional_evidence.append((id, rendered))

    critical_decision_lines: list[str] = []
    optional_decisions: list[tuple[str, str]] = []
    missing_decisions: list[str] = []
    for id in handoff.decision_ids:
        decision = decision_map.get(id)
        if decision is None:
            missing_decisions.append(id)
            critical_decision_lines.append(_render_missing_reference("review decision", id))
            continue
        _validate_decision_reference(handoff, id, decision)
        rendered = _render_decision(decision)
        if _critical_decision(decision):
            critical_decision_lines.append(rendered)
            included_decisions.append(id)
        else:
            optional_decisions.append((id, rendered))

    content = _append_section(
        content, "Readiness-critical evidence", tuple(critical_evidence_lines)
    )
    content = _append_section(
        content, "Readiness-critical review", tuple(critical_decision_lines)
    )

    mandatory_tokens = _count(counter, content)
    if mandatory_tokens > selected.hard_max_tokens:
        raise HandoffMaterializationError(
            "mandatory handoff context exceeds hard token maximum "
            f"({mandatory_tokens} > {selected.hard_max_tokens}); choose a larger explicit budget"
        )

    evidence_started = False
    optional_evidence_considered = optional_evidence[: selected.max_evidence_summaries]
    optional_evidence_unconsidered = optional_evidence[selected.max_evidence_summaries :]
    for id, rendered in optional_evidence_considered:
        candidate, added = _try_append_within_target(
            content,
            title="Additional evidence",
            item=rendered,
            section_started=evidence_started,
            counter=counter,
            target_tokens=selected.target_tokens,
        )
        if added:
            content = candidate
            evidence_started = True
            included_evidence.append(id)
        else:
            deferred.append(
                DeferredHandoffContext(
                    HandoffContextSource.EVIDENCE_SUMMARIES,
                    "target token budget reached",
                    ids=(id,),
                    count=1,
                )
            )
    if optional_evidence_unconsidered:
        deferred.append(
            DeferredHandoffContext(
                HandoffContextSource.EVIDENCE_SUMMARIES,
                "profile summary-count limit",
                ids=tuple(id for id, _ in optional_evidence_unconsidered),
                count=len(optional_evidence_unconsidered),
            )
        )

    decision_started = False
    optional_decisions_considered = optional_decisions[: selected.max_decision_summaries]
    optional_decisions_unconsidered = optional_decisions[selected.max_decision_summaries :]
    for id, rendered in optional_decisions_considered:
        candidate, added = _try_append_within_target(
            content,
            title="Additional review decisions",
            item=rendered,
            section_started=decision_started,
            counter=counter,
            target_tokens=selected.target_tokens,
        )
        if added:
            content = candidate
            decision_started = True
            included_decisions.append(id)
        else:
            deferred.append(
                DeferredHandoffContext(
                    HandoffContextSource.DECISION_SUMMARIES,
                    "target token budget reached",
                    ids=(id,),
                    count=1,
                )
            )
    if optional_decisions_unconsidered:
        deferred.append(
            DeferredHandoffContext(
                HandoffContextSource.DECISION_SUMMARIES,
                "profile summary-count limit",
                ids=tuple(id for id, _ in optional_decisions_unconsidered),
                count=len(optional_decisions_unconsidered),
            )
        )

    normalized_paths = tuple(sorted(set(path for path in changed_paths if path.strip())))
    included_paths: list[str] = []
    path_started = False
    for path in normalized_paths[: selected.max_changed_paths]:
        candidate, added = _try_append_within_target(
            content,
            title="Changed paths",
            item=f"- {_quoted(path)}",
            section_started=path_started,
            counter=counter,
            target_tokens=selected.target_tokens,
        )
        if not added:
            break
        content = candidate
        path_started = True
        included_paths.append(path)
    included_path_set = set(included_paths)
    omitted_paths = tuple(path for path in normalized_paths if path not in included_path_set)
    if omitted_paths:
        deferred.append(
            DeferredHandoffContext(
                HandoffContextSource.CHANGED_PATHS,
                "target token budget or profile path-count limit",
                ids=omitted_paths,
                count=len(omitted_paths),
            )
        )

    diff_included = False
    if diff_excerpt:
        diff_mode = selected.mode_for(HandoffContextSource.DIFF)
        if diff_mode is HandoffExpansionMode.FOCUSED_EXCERPTS:
            remaining_target = max(0, selected.target_tokens - _count(counter, content))
            excerpt_budget = min(selected.focused_excerpt_tokens, remaining_target)
            excerpt = _truncate_to_tokens(
                diff_excerpt,
                counter=counter,
                max_tokens=excerpt_budget,
            )
            if excerpt:
                candidate = (
                    content
                    + "\n\n## Focused diff excerpt (untrusted repository content)\n"
                    + excerpt
                )
                if _count(counter, candidate) <= selected.target_tokens:
                    content = candidate
                    diff_included = True
        if not diff_included:
            deferred.append(
                DeferredHandoffContext(
                    HandoffContextSource.DIFF,
                    "retrieval-only profile or insufficient target budget",
                    count=1,
                )
            )

    for source in (
        HandoffContextSource.SOURCE_FILES,
        HandoffContextSource.CHAT_TRANSCRIPT,
        HandoffContextSource.TERMINAL_HISTORY,
    ):
        deferred.append(
            DeferredHandoffContext(source, "retrieval only by default")
        )

    if missing_evidence:
        deferred.append(
            DeferredHandoffContext(
                HandoffContextSource.EVIDENCE_SUMMARIES,
                "referenced records missing from materializer input",
                ids=tuple(missing_evidence),
                count=len(missing_evidence),
            )
        )
    if missing_decisions:
        deferred.append(
            DeferredHandoffContext(
                HandoffContextSource.DECISION_SUMMARIES,
                "referenced records missing from materializer input",
                ids=tuple(missing_decisions),
                count=len(missing_decisions),
            )
        )

    final_tokens = _count(counter, content)
    if final_tokens > selected.hard_max_tokens:
        raise HandoffMaterializationError(
            "materialized context exceeded hard token maximum"
        )

    return HandoffMaterialization(
        handoff_id=handoff.id,
        variant=selected.variant,
        content=content,
        token_count=final_tokens,
        token_count_method=counter.name,
        target_tokens=selected.target_tokens,
        hard_max_tokens=selected.hard_max_tokens,
        included_evidence_ids=tuple(included_evidence),
        included_decision_ids=tuple(included_decisions),
        included_changed_paths=tuple(included_paths),
        diff_excerpt_included=diff_included,
        deferred=tuple(deferred),
    )
