"""Evidence-gated Change Set lifecycle operations."""

from __future__ import annotations

from datetime import datetime

from weftmark.application.change_binding import ChangeBinding
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.changeset import ChangeSetState


class LifecycleError(ValueError):
    """Raised when a lifecycle request is malformed."""


class LifecyclePolicyError(LifecycleError):
    """Raised when durable proof does not permit a lifecycle transition."""


class LifecycleService:
    def __init__(
        self,
        workspace: WorkspaceService,
        workflow: LocalWorkflowService,
    ) -> None:
        self._workspace = workspace
        self._workflow = workflow

    def transition(
        self,
        change_set_id: str,
        *,
        state: ChangeSetState,
        transitioned_at: datetime,
    ) -> ChangeBinding:
        binding = self._workspace.require_change_set(change_set_id)
        binding.change_set.transition(state, at=transitioned_at)
        if state is ChangeSetState.REVIEW:
            reviews = self._workflow.list_reviews(change_set_id=change_set_id)
            if (
                not reviews
                or reviews[-1]["decision"]["head_sha"] != binding.latest.head_sha
            ):
                raise LifecyclePolicyError(
                    "review transition requires a current review decision"
                )
            if binding.latest.dirty_paths:
                raise LifecyclePolicyError(
                    "review transition requires the latest recorded observation to be clean"
                )
        if state is ChangeSetState.MERGED:
            reviews = self._workflow.list_reviews(change_set_id=change_set_id)
            if not reviews:
                raise LifecyclePolicyError("merged transition requires a review")
            latest = reviews[-1]
            if (
                not latest["is_releasable"]
                or latest["decision"]["head_sha"] != binding.latest.head_sha
            ):
                raise LifecyclePolicyError(
                    "merged transition requires a current releasable review"
                )
        if state is ChangeSetState.CLOSED:
            handoffs = self._workflow.list_handoffs(change_set_id=change_set_id)
            if not handoffs or handoffs[-1].head_sha != binding.latest.head_sha:
                raise LifecyclePolicyError(
                    "closed transition requires a current clean-head handoff"
                )
        return self._workspace.transition_change_set(
            change_set_id,
            state=state,
            transitioned_at=transitioned_at,
        )
