from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weftmark.mcp.service import (
    McpPermissionError,
    McpToolService,
    McpWriteCapability,
)
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskIntent, TaskPriority, TaskState


NOW = datetime(2026, 8, 19, 17, 0, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def service(
    tmp_path: Path,
    *capabilities: McpWriteCapability,
) -> McpToolService:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark MCP Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    return McpToolService.from_local(
        str(tmp_path),
        write_capabilities=capabilities,
        clock=lambda: NOW,
    )


def create_task(value: McpToolService, id: str = "task-a") -> None:
    value.tasks.create(
        TaskIntent.create(
            id=id,
            title=f"Task {id}",
            why="Exercise the MCP surface.",
            what="Coordinate through existing application services.",
            roi_note=None,
            priority=TaskPriority.P0,
            state=TaskState.TODO,
            scopes=(Scope.file("src/**"), Scope.contract("mcp-v0")),
            created_at=NOW,
        )
    )


def test_read_tools_work_without_any_write_capability(tmp_path: Path) -> None:
    value = service(tmp_path)
    create_task(value)

    listed = value.task_list()
    selected = value.task_next(limit=1)
    eligibility = value.task_eligibility("task-a")
    status = value.workspace_status()

    assert listed["count"] == 1
    assert listed["tasks"][0]["id"] == "task-a"
    assert selected["eligible"] == 1
    assert eligibility["eligible"] is True
    assert status["counts"]["change_sets"] == 0


def test_write_capability_is_enforced_inside_service_not_only_by_mcp_metadata(
    tmp_path: Path,
) -> None:
    value = service(tmp_path)
    create_task(value)

    with pytest.raises(McpPermissionError, match="claim"):
        value.claim_task(
            "task-a",
            idempotency_key="mcp-claim-0001",
            change_set_id="chg-a",
            claim_id="claim-a",
            base_revision="HEAD",
            agent_id="worker-a",
            session_id="session-a",
        )

    assert value.tasks.require("task-a").state is TaskState.TODO


def test_claim_dry_run_is_advisory_and_actual_claim_reuses_control_idempotency(
    tmp_path: Path,
) -> None:
    value = service(tmp_path, McpWriteCapability.CLAIM)
    create_task(value)

    dry = value.claim_task(
        "task-a",
        idempotency_key="mcp-claim-0001",
        change_set_id="chg-a",
        claim_id="claim-a",
        base_revision="HEAD",
        agent_id="worker-a",
        session_id="session-a",
        dry_run=True,
    )
    assert dry["dry_run"] is True
    assert dry["eligible"] is True
    assert value.tasks.require("task-a").state is TaskState.TODO

    first = value.claim_task(
        "task-a",
        idempotency_key="mcp-claim-0001",
        change_set_id="chg-a",
        claim_id="claim-a",
        base_revision="HEAD",
        agent_id="worker-a",
        session_id="session-a",
    )
    replay = value.claim_task(
        "task-a",
        idempotency_key="mcp-claim-0001",
        change_set_id="chg-a",
        claim_id="claim-a",
        base_revision="HEAD",
        agent_id="worker-a",
        session_id="session-a",
    )

    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert value.tasks.require("task-a").state is TaskState.IN_PROGRESS
    assert value.change_show("chg-a")["change_set"]["active_claim_ids"] == ["claim-a"]


def test_release_and_handoff_use_same_control_contract(tmp_path: Path) -> None:
    value = service(
        tmp_path,
        McpWriteCapability.CLAIM,
        McpWriteCapability.RELEASE,
        McpWriteCapability.HANDOFF,
    )
    create_task(value)
    value.claim_task(
        "task-a",
        idempotency_key="mcp-claim-0001",
        change_set_id="chg-a",
        claim_id="claim-a",
        base_revision="HEAD",
        agent_id="worker-a",
        session_id="session-a",
    )

    handoff = value.create_handoff(
        "chg-a",
        idempotency_key="mcp-handoff-0001",
        handoff_id="handoff-a",
        task_id="task-a",
        next_action="Continue in another worker",
        created_by="worker-a",
        intended_receiver_id="worker-b",
    )
    released = value.release_claim(
        "claim-a",
        idempotency_key="mcp-release-0001",
        agent_id="worker-a",
        session_id="session-a",
        reason="handoff",
    )

    assert handoff["result"]["id"] == "handoff-a"
    assert released["result"]["effective_state"] == "released"
    assert value.handoff_list(change_set_id="chg-a")["count"] == 1


def test_scope_audit_and_command_evidence_are_separate_high_risk_capabilities(
    tmp_path: Path,
) -> None:
    value = service(
        tmp_path,
        McpWriteCapability.CLAIM,
        McpWriteCapability.SCOPE_AUDIT,
        McpWriteCapability.EVIDENCE_EXEC,
    )
    create_task(value)
    value.claim_task(
        "task-a",
        idempotency_key="mcp-claim-0001",
        change_set_id="chg-a",
        claim_id="claim-a",
        base_revision="HEAD",
        agent_id="worker-a",
        session_id="session-a",
    )

    audit_dry = value.audit_scope(
        "chg-a",
        semantic_changes=("contract:mcp-v0",),
        dry_run=True,
    )
    assert audit_dry["dry_run"] is True

    evidence_dry = value.run_evidence(
        "chg-a",
        evidence_id="ev-status",
        kind="test",
        argv=("git", "status", "--porcelain"),
        cwd=str(tmp_path),
    )
    assert evidence_dry["dry_run"] is True
    assert value.evidence_list(change_set_id="chg-a")["count"] == 0

    evidence = value.run_evidence(
        "chg-a",
        evidence_id="ev-status",
        kind="test",
        argv=("git", "status", "--porcelain"),
        cwd=str(tmp_path),
        dry_run=False,
    )
    assert evidence["state"] == "passed"
    assert value.evidence_list(change_set_id="chg-a")["count"] == 1


def test_evidence_execution_is_not_implied_by_other_write_capabilities(
    tmp_path: Path,
) -> None:
    value = service(tmp_path, McpWriteCapability.CLAIM)
    create_task(value)

    with pytest.raises(McpPermissionError, match="evidence-exec"):
        value.run_evidence(
            "missing-change",
            evidence_id="ev-a",
            kind="test",
            argv=("true",),
            cwd=str(tmp_path),
            dry_run=True,
        )
