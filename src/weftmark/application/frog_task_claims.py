"""Recoverable composition of imported eligibility, promotion, and local claims."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from weftmark.application.claims import Claim, ClaimService, claim_to_payload
from weftmark.application.frog_planning import FrogPlanningService
from weftmark.application.frog_promotions import (
    FrogPromotionResult,
    FrogPromotionService,
    promotion_result_to_payload,
)
from weftmark.application.identifiers import new_id
from weftmark.domain.lock import LockState
from weftmark.domain.scope import Scope


class FrogTaskClaimError(ValueError):
    """Raised when external intent is not eligible for a local native claim."""


@dataclass(frozen=True, slots=True)
class FrogTaskClaimResult:
    promotion: FrogPromotionResult
    claim: Claim
    claimed: bool


class FrogTaskClaimService:
    def __init__(
        self,
        planning: FrogPlanningService,
        promotions: FrogPromotionService,
        claims: ClaimService,
    ) -> None:
        self._planning = planning
        self._promotions = promotions
        self._claims = claims

    def claim(
        self,
        snapshot_digest: str,
        task_slug: str,
        *,
        change_set_id: str | None,
        claim_id: str | None,
        base_revision: str,
        scopes: tuple[Scope, ...],
        agent_id: str,
        session_id: str,
        claimed_at: datetime,
        lease_seconds: int,
    ) -> FrogTaskClaimResult:
        _validate_request(
            claim_id=claim_id,
            agent_id=agent_id,
            session_id=session_id,
            claimed_at=claimed_at,
            lease_seconds=lease_seconds,
        )
        eligibility = self._planning.eligibility(snapshot_digest, task_slug)
        if not eligibility.eligible:
            raise FrogTaskClaimError(
                f"Frog task is not eligible: {task_slug}: "
                + "; ".join(eligibility.reasons)
            )
        promotion = self._promotions.promote(
            snapshot_digest,
            task_slug,
            change_set_id=change_set_id,
            base_revision=base_revision,
            scopes=scopes,
            promoted_at=claimed_at,
        )
        selected_change_set = promotion.promotion.change_set_id

        if claim_id is not None:
            existing = self._claims.get(claim_id)
            if existing is not None:
                return FrogTaskClaimResult(
                    promotion,
                    _require_matching_active_claim(
                        existing,
                        change_set_id=selected_change_set,
                        agent_id=agent_id,
                        session_id=session_id,
                        observed_at=claimed_at,
                    ),
                    False,
                )
        else:
            matching = tuple(
                claim
                for claim in self._claims.list(change_set_id=selected_change_set)
                if claim.agent_id == agent_id
                and claim.session_id == session_id
                and claim.state_at(claimed_at) is LockState.ACTIVE
            )
            if len(matching) > 1:
                raise FrogTaskClaimError("multiple matching active local claims")
            if matching:
                return FrogTaskClaimResult(promotion, matching[0], False)

        claim = self._claims.acquire(
            selected_change_set,
            id=claim_id or new_id("claim", at=claimed_at),
            agent_id=agent_id,
            session_id=session_id,
            acquired_at=claimed_at,
            lease_seconds=lease_seconds,
        )
        return FrogTaskClaimResult(promotion, claim, True)


def frog_task_claim_result_to_payload(
    result: FrogTaskClaimResult, *, observed_at: datetime
) -> dict[str, Any]:
    return {
        "claimed": result.claimed,
        "promotion": promotion_result_to_payload(result.promotion),
        "claim": claim_to_payload(result.claim, observed_at=observed_at),
        "authority": "local Change Set and semantic claim",
    }


def _require_matching_active_claim(
    claim: Claim,
    *,
    change_set_id: str,
    agent_id: str,
    session_id: str,
    observed_at: datetime,
) -> Claim:
    if (
        claim.change_set_id != change_set_id
        or claim.agent_id != agent_id
        or claim.session_id != session_id
    ):
        raise FrogTaskClaimError(f"Claim already exists with different intent: {claim.id}")
    if claim.state_at(observed_at) is not LockState.ACTIVE:
        raise FrogTaskClaimError(f"Claim is no longer active: {claim.id}")
    return claim


def _validate_request(
    *,
    claim_id: str | None,
    agent_id: str,
    session_id: str,
    claimed_at: datetime,
    lease_seconds: int,
) -> None:
    if claimed_at.tzinfo is None or claimed_at.utcoffset() is None:
        raise FrogTaskClaimError("claimed_at must include a timezone")
    if not agent_id.strip() or not session_id.strip():
        raise FrogTaskClaimError("agent and session must not be empty")
    if claim_id is not None and not claim_id.strip():
        raise FrogTaskClaimError("claim id must not be empty")
    if lease_seconds < 1 or lease_seconds > 604_800:
        raise FrogTaskClaimError(
            "lease duration must be between 1 and 604800 seconds"
        )
