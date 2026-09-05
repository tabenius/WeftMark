"""Pure functions turning ChangeSetStatus into terminal-reviewer text. No
Textual dependency — kept testable and reusable independent of rendering."""

from __future__ import annotations

from weftmark.application.status import ChangeSetStatus

_TERMINAL_LIFECYCLE_STATES = frozenset({"merged", "closed", "abandoned"})
_NOT_UNREADY_READINESS = frozenset({"ready", "ready_with_follow_up"})


def attention_rank(status: ChangeSetStatus) -> int:
    """Lower sorts first: blocked, then not-ready, then ready, then terminal.

    A Change Set in a terminal lifecycle state (merged/closed/abandoned) has
    no further review to give it, so it always sorts after live work — even
    if it still carries scope collisions or a non-ready readiness, both of
    which are moot once the Change Set is done. ``ready_with_follow_up`` is
    treated the same as ``ready`` here, matching how
    ``kanban_projection._lane_for`` classifies that same readiness value.
    """

    if status.lifecycle_state in _TERMINAL_LIFECYCLE_STATES:
        return 3
    if status.scope_collisions:
        return 0
    if status.readiness not in _NOT_UNREADY_READINESS:
        return 1
    return 2


def sort_statuses(
    statuses: tuple[ChangeSetStatus, ...],
) -> tuple[ChangeSetStatus, ...]:
    return tuple(
        sorted(statuses, key=lambda status: (attention_rank(status), status.id))
    )


def evidence_summary(status: ChangeSetStatus) -> str:
    text = f"{status.current_evidence_count}/{status.evidence_count}"
    if status.failed_evidence_count:
        text += f" ({status.failed_evidence_count} failed)"
    return text


def blockers_text(status: ChangeSetStatus) -> tuple[str, ...]:
    return tuple(
        f"blocked by claim {collision.claim_id} "
        f"({collision.competing_change_set_id}) on {collision.owned_scope.canonical}"
        for collision in status.scope_collisions
    )


def detail_text(status: ChangeSetStatus) -> str:
    lines = [
        f"{status.id} — {status.goal}",
        f"state: {status.lifecycle_state}    readiness: {status.readiness}",
        f"branch: {status.branch}",
        f"head: {status.observed_head_sha} ({status.observed_at.isoformat()})",
        "",
        (
            f"evidence: {evidence_summary(status)}"
            f", obsolete {status.obsolete_evidence_count}"
            f", unavailable {status.unavailable_evidence_count}"
        ),
    ]
    if status.dirty_paths:
        lines.append(f"dirty paths: {len(status.dirty_paths)}")
    lines.extend(
        [
            (
                f"review: {status.latest_review_outcome} "
                f"({'current' if status.latest_review_is_current else 'stale'})"
                if status.latest_review_id
                else "review: none"
            ),
            (
                f"handoff: {status.latest_handoff_id} "
                f"({'current' if status.latest_handoff_is_current else 'stale'})"
                if status.latest_handoff_id
                else "handoff: none"
            ),
        ]
    )
    blockers = blockers_text(status)
    if blockers:
        lines.append("")
        lines.append("blockers:")
        lines.extend(f"  {line}" for line in blockers)
    return "\n".join(lines)
