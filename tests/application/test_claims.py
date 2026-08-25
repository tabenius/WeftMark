from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.claims import (
    Claim,
    ClaimConflict,
    ClaimService,
    claim_to_payload,
)
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import LedgerDraft
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.lock import LockEventKind, LockState, SemanticLock
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 14, 10, 30, tzinfo=timezone.utc)


class InterleavingLedger:
    def __init__(self, inner: JsonlLedger) -> None:
        self.inner = inner
        self.pending: LedgerDraft | None = None

    def append(self, draft: LedgerDraft):
        return self.inner.append(draft)

    def append_if_head(self, draft: LedgerDraft, *, expected_digest: str):
        if self.pending is not None:
            self.inner.append(self.pending)
            self.pending = None
        return self.inner.append_if_head(draft, expected_digest=expected_digest)

    def entries(self):
        return self.inner.entries()


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(tmp_path: Path) -> tuple[WorkspaceService, ClaimService]:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / ".git" / "weftmark" / "test.jsonl"))
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    workspace.create_change_set(
        id="chg-1",
        goal="First claimant",
        base_revision="HEAD",
        scopes=(Scope.file("src/**"), Scope.contract("shared-api")),
        created_at=NOW,
    )
    workspace.create_change_set(
        id="chg-2",
        goal="Competing claimant",
        base_revision="HEAD",
        scopes=(Scope.file("docs/**"), Scope.contract("shared-api")),
        created_at=NOW,
    )
    return workspace, ClaimService(workspace, ledger)


def test_claim_acquires_all_change_set_scopes_and_round_trips(tmp_path: Path) -> None:
    _, claims = setup(tmp_path)
    value = claims.acquire(
        "chg-1",
        id="claim-1",
        agent_id="worker-1",
        session_id="session-1",
        acquired_at=NOW,
        lease_seconds=60,
    )

    assert [lock.scope.canonical for lock in value.locks] == [
        "file:src/**",
        "contract:shared-api",
    ]
    assert value.state_at(NOW + timedelta(seconds=1)) is LockState.ACTIVE
    assert claims.get("claim-1") == value
    assert claims.list(change_set_id="chg-1") == (value,)


def test_overlapping_semantic_scope_refuses_atomically(tmp_path: Path) -> None:
    _, claims = setup(tmp_path)
    claims.acquire(
        "chg-1",
        id="claim-1",
        agent_id="worker-1",
        session_id="session-1",
        acquired_at=NOW,
        lease_seconds=60,
    )

    with pytest.raises(ClaimConflict, match="contract:shared-api.*claim-1"):
        claims.acquire(
            "chg-2",
            id="claim-2",
            agent_id="worker-2",
            session_id="session-2",
            acquired_at=NOW + timedelta(seconds=1),
            lease_seconds=60,
        )
    assert claims.get("claim-2") is None


def test_interleaved_competing_append_is_rechecked_before_acquisition(
    tmp_path: Path,
) -> None:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "base")
    port = InterleavingLedger(JsonlLedger(tmp_path / "claims.jsonl"))
    ledger = LedgerService(port)
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    for id in ("chg-1", "chg-2"):
        workspace.create_change_set(
            id=id,
            goal=id,
            base_revision="HEAD",
            scopes=(Scope.contract("shared-api"),),
            created_at=NOW,
        )
    claims = ClaimService(workspace, ledger)
    competing = Claim(
        id="claim-racer",
        change_set_id="chg-1",
        agent_id="racer",
        session_id="racing-session",
        locks=(
            SemanticLock.acquire(
                id="claim-racer:1",
                scope=Scope.contract("shared-api"),
                agent_id="racer",
                session_id="racing-session",
                change_set_id="chg-1",
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            ),
        ),
        updated_at=NOW,
    )
    port.pending = LedgerDraft(
        "claim",
        competing.id,
        json.dumps(claim_to_payload(competing), sort_keys=True, separators=(",", ":")),
        NOW,
    )

    with pytest.raises(ClaimConflict, match="claim-racer"):
        claims.acquire(
            "chg-2",
            id="claim-loser",
            agent_id="worker",
            session_id="session",
            acquired_at=NOW + timedelta(seconds=1),
            lease_seconds=60,
        )
    assert claims.get("claim-loser") is None


def test_expired_or_released_claim_does_not_block_new_ownership(tmp_path: Path) -> None:
    _, claims = setup(tmp_path)
    first = claims.acquire(
        "chg-1",
        id="claim-expiring",
        agent_id="worker-1",
        session_id="session-1",
        acquired_at=NOW,
        lease_seconds=10,
    )
    assert first.state_at(NOW + timedelta(seconds=10)) is LockState.EXPIRED
    after_expiry = claims.acquire(
        "chg-2",
        id="claim-after-expiry",
        agent_id="worker-2",
        session_id="session-2",
        acquired_at=NOW + timedelta(seconds=11),
        lease_seconds=60,
    )
    released = claims.release(
        "claim-after-expiry",
        agent_id="worker-2",
        session_id="session-2",
        released_at=NOW + timedelta(seconds=12),
        reason="slice completed",
    )
    assert released.state_at(NOW + timedelta(seconds=12)) is LockState.RELEASED
    assert released.locks[0].events[-1].kind is LockEventKind.RELEASED
    replacement = claims.acquire(
        "chg-1",
        id="claim-after-release",
        agent_id="worker-3",
        session_id="session-3",
        acquired_at=NOW + timedelta(seconds=13),
        lease_seconds=60,
    )
    assert replacement.state_at(NOW + timedelta(seconds=13)) is LockState.ACTIVE


def test_renew_extends_each_lock_in_one_claim_snapshot(tmp_path: Path) -> None:
    _, claims = setup(tmp_path)
    original = claims.acquire(
        "chg-1",
        id="claim-1",
        agent_id="worker-1",
        session_id="session-1",
        acquired_at=NOW,
        lease_seconds=60,
    )
    renewed = claims.renew(
        "claim-1",
        agent_id="worker-1",
        session_id="session-1",
        renewed_at=NOW + timedelta(seconds=30),
        extend_seconds=120,
    )
    assert renewed.locks[0].expires_at == original.locks[0].expires_at + timedelta(
        seconds=120
    )
    assert all(lock.events[-1].kind is LockEventKind.RENEWED for lock in renewed.locks)


def test_expired_claim_can_be_reacquired_only_by_owner_without_collision(
    tmp_path: Path,
) -> None:
    _, claims = setup(tmp_path)
    original = claims.acquire(
        "chg-1",
        id="claim-1",
        agent_id="worker-1",
        session_id="session-1",
        acquired_at=NOW,
        lease_seconds=10,
    )
    reacquired_at = NOW + timedelta(seconds=11)
    reacquired = claims.reacquire(
        "claim-1",
        agent_id="worker-1",
        session_id="session-1",
        reacquired_at=reacquired_at,
        lease_seconds=60,
    )

    assert reacquired.state_at(reacquired_at) is LockState.ACTIVE
    assert reacquired.locks[0].events[:-1] == original.locks[0].events
    assert reacquired.locks[0].events[-1].kind is LockEventKind.REACQUIRED

    with pytest.raises(ValueError, match="only an expired"):
        claims.reacquire(
            "claim-1",
            agent_id="worker-1",
            session_id="session-1",
            reacquired_at=reacquired_at + timedelta(seconds=1),
            lease_seconds=60,
        )
    with pytest.raises(ValueError, match="owning agent and session"):
        claims.reacquire(
            "claim-1",
            agent_id="worker-2",
            session_id="session-2",
            reacquired_at=reacquired_at + timedelta(seconds=61),
            lease_seconds=60,
        )


def test_expired_claim_reacquisition_rechecks_current_scope_owners(
    tmp_path: Path,
) -> None:
    _, claims = setup(tmp_path)
    claims.acquire(
        "chg-1",
        id="claim-expired",
        agent_id="worker-1",
        session_id="session-1",
        acquired_at=NOW,
        lease_seconds=10,
    )
    claims.acquire(
        "chg-2",
        id="claim-current",
        agent_id="worker-2",
        session_id="session-2",
        acquired_at=NOW + timedelta(seconds=11),
        lease_seconds=60,
    )

    with pytest.raises(ClaimConflict, match="claim-current"):
        claims.reacquire(
            "claim-expired",
            agent_id="worker-1",
            session_id="session-1",
            reacquired_at=NOW + timedelta(seconds=12),
            lease_seconds=60,
        )
    assert claims.get("claim-expired").state_at(
        NOW + timedelta(seconds=12)
    ) is LockState.EXPIRED


def test_only_owning_agent_session_can_renew_or_release(tmp_path: Path) -> None:
    _, claims = setup(tmp_path)
    original = claims.acquire(
        "chg-1",
        id="claim-1",
        agent_id="worker-1",
        session_id="session-1",
        acquired_at=NOW,
        lease_seconds=60,
    )
    with pytest.raises(ValueError, match="owning agent and session"):
        claims.renew(
            "claim-1",
            agent_id="worker-2",
            session_id="session-2",
            renewed_at=NOW + timedelta(seconds=1),
            extend_seconds=60,
        )
    with pytest.raises(ValueError, match="owning agent and session"):
        claims.release(
            "claim-1",
            agent_id="worker-1",
            session_id="wrong-session",
            released_at=NOW + timedelta(seconds=1),
            reason="not mine",
        )
    assert claims.get("claim-1") == original


@pytest.mark.parametrize("seconds", (0, -1, 604_801))
def test_claim_lease_duration_is_bounded(tmp_path: Path, seconds: int) -> None:
    _, claims = setup(tmp_path)
    with pytest.raises(ValueError, match="lease duration"):
        claims.acquire(
            "chg-1",
            id="claim-invalid",
            agent_id="worker",
            session_id="session",
            acquired_at=NOW,
            lease_seconds=seconds,
        )
