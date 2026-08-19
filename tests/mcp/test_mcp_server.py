from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mcp import Client

from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskIntent, TaskPriority, TaskState
from weftmark.mcp.server import build_server
from weftmark.mcp.service import McpToolService, McpWriteCapability


NOW = datetime(2026, 8, 19, 17, 15, tzinfo=timezone.utc)


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
    value = McpToolService.from_local(
        str(tmp_path),
        write_capabilities=capabilities,
        clock=lambda: NOW,
    )
    value.tasks.create(
        TaskIntent.create(
            id="task-a",
            title="MCP task",
            why="Verify the protocol surface.",
            what="List and optionally claim this task.",
            roi_note=None,
            priority=TaskPriority.P0,
            state=TaskState.TODO,
            scopes=(Scope.file("src/**"),),
            created_at=NOW,
        )
    )
    return value


def run(coro):
    return asyncio.run(coro)


async def listed_tools(value: McpToolService):
    server = build_server(value)
    async with Client(server) as client:
        return await client.list_tools()


def test_read_only_server_registers_only_read_tools(tmp_path: Path) -> None:
    result = run(listed_tools(service(tmp_path)))
    names = {tool.name for tool in result.tools}

    assert {
        "weft_workspace_status",
        "weft_task_list",
        "weft_task_next",
        "weft_task_eligibility",
        "weft_change_show",
        "weft_evidence_list",
        "weft_review_list",
        "weft_handoff_list",
    }.issubset(names)
    assert not any(name in names for name in {
        "weft_task_claim",
        "weft_claim_release",
        "weft_handoff_create",
        "weft_scope_audit",
        "weft_evidence_run",
    })
    for tool in result.tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


def test_only_explicit_write_capabilities_register_tools(tmp_path: Path) -> None:
    result = run(
        listed_tools(
            service(
                tmp_path,
                McpWriteCapability.CLAIM,
                McpWriteCapability.HANDOFF,
            )
        )
    )
    by_name = {tool.name: tool for tool in result.tools}

    assert "weft_task_claim" in by_name
    assert "weft_handoff_create" in by_name
    assert "weft_claim_release" not in by_name
    assert "weft_scope_audit" not in by_name
    assert "weft_evidence_run" not in by_name
    assert by_name["weft_task_claim"].annotations.read_only_hint is False
    assert by_name["weft_task_claim"].annotations.idempotent_hint is True


def test_in_memory_client_receives_structured_read_result(tmp_path: Path) -> None:
    value = service(tmp_path)
    server = build_server(value)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool("weft_task_list", {})
            return result

    result = run(scenario())
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["count"] == 1
    assert result.structured_content["tasks"][0]["id"] == "task-a"


def test_in_memory_client_can_dry_run_granted_claim_without_mutation(
    tmp_path: Path,
) -> None:
    value = service(tmp_path, McpWriteCapability.CLAIM)
    server = build_server(value)

    async def scenario():
        async with Client(server) as client:
            return await client.call_tool(
                "weft_task_claim",
                {
                    "task_id": "task-a",
                    "idempotency_key": "mcp-claim-0001",
                    "change_set_id": "chg-a",
                    "claim_id": "claim-a",
                    "base_revision": "HEAD",
                    "agent_id": "worker-a",
                    "session_id": "session-a",
                    "dry_run": True,
                },
            )

    result = run(scenario())
    assert result.is_error is False
    assert result.structured_content["dry_run"] is True
    assert result.structured_content["eligible"] is True
    assert value.tasks.require("task-a").state is TaskState.TODO
