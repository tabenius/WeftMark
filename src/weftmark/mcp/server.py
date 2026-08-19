"""Optional stdio MCP server over WeftMark application services."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from weftmark.mcp.service import McpToolService, McpWriteCapability


class McpDependencyError(RuntimeError):
    """Raised when the optional official MCP SDK is not installed."""


def build_server(service: McpToolService):
    """Build an MCPServer while keeping the SDK optional for core WeftMark."""
    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ModuleNotFoundError as error:
        raise McpDependencyError(
            "MCP support is optional; install WeftMark with the 'mcp' extra"
        ) from error

    server = MCPServer(
        "WeftMark",
        instructions=(
            "WeftMark is the authority for coordination, evidence, review and handoff. "
            "Read tools are advisory views over durable state. Write tools are only "
            "registered when explicitly enabled by the operator."
        ),
    )

    read_annotations = ToolAnnotations(
        read_only_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )

    @server.tool(
        name="weft_workspace_status",
        title="WeftMark workspace status",
        annotations=read_annotations,
    )
    def workspace_status() -> dict[str, Any]:
        """Read Change Sets, claims, evidence counts, blockers and readiness."""
        return service.workspace_status()

    @server.tool(
        name="weft_task_list",
        title="List WeftMark tasks",
        annotations=read_annotations,
    )
    def task_list(state: str | None = None) -> dict[str, Any]:
        """List durable native task intent, optionally filtered by task state."""
        return service.task_list(state=state)

    @server.tool(
        name="weft_task_next",
        title="Select next WeftMark task",
        annotations=read_annotations,
    )
    def task_next(limit: int = 1) -> dict[str, Any]:
        """Advisory next-task selection; this tool never creates a claim."""
        return service.task_next(limit=limit)

    @server.tool(
        name="weft_task_eligibility",
        title="Explain task eligibility",
        annotations=read_annotations,
    )
    def task_eligibility(task_id: str) -> dict[str, Any]:
        """Explain why one native task is or is not currently claimable."""
        return service.task_eligibility(task_id)

    @server.tool(
        name="weft_change_show",
        title="Show WeftMark Change Set",
        annotations=read_annotations,
    )
    def change_show(change_set_id: str) -> dict[str, Any]:
        """Read one Change Set through the shared status model."""
        return service.change_show(change_set_id)

    @server.tool(
        name="weft_evidence_list",
        title="List WeftMark evidence",
        annotations=read_annotations,
    )
    def evidence_list(change_set_id: str | None = None) -> dict[str, Any]:
        """List durable evidence, optionally for one Change Set."""
        return service.evidence_list(change_set_id=change_set_id)

    @server.tool(
        name="weft_review_list",
        title="List WeftMark reviews",
        annotations=read_annotations,
    )
    def review_list(change_set_id: str | None = None) -> dict[str, Any]:
        """List durable review decisions and findings."""
        return service.review_list(change_set_id=change_set_id)

    @server.tool(
        name="weft_handoff_list",
        title="List WeftMark handoffs",
        annotations=read_annotations,
    )
    def handoff_list(change_set_id: str | None = None) -> dict[str, Any]:
        """List durable handoff records without replaying chat history."""
        return service.handoff_list(change_set_id=change_set_id)

    if McpWriteCapability.CLAIM in service.write_capabilities:

        @server.tool(
            name="weft_task_claim",
            title="Claim WeftMark task",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        def task_claim(
            task_id: str,
            idempotency_key: str,
            change_set_id: str,
            claim_id: str,
            base_revision: str,
            agent_id: str,
            session_id: str,
            lease_seconds: int = 900,
            dry_run: bool = False,
        ) -> dict[str, Any]:
            """Claim task intent through WeftMark ownership rules; supports dry-run."""
            return service.claim_task(
                task_id,
                idempotency_key=idempotency_key,
                change_set_id=change_set_id,
                claim_id=claim_id,
                base_revision=base_revision,
                agent_id=agent_id,
                session_id=session_id,
                lease_seconds=lease_seconds,
                dry_run=dry_run,
            )

    if McpWriteCapability.RELEASE in service.write_capabilities:

        @server.tool(
            name="weft_claim_release",
            title="Release WeftMark claim",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        def claim_release(
            claim_id: str,
            idempotency_key: str,
            agent_id: str,
            session_id: str,
            reason: str,
            dry_run: bool = False,
        ) -> dict[str, Any]:
            """Release an owned semantic claim; supports a non-mutating preflight."""
            return service.release_claim(
                claim_id,
                idempotency_key=idempotency_key,
                agent_id=agent_id,
                session_id=session_id,
                reason=reason,
                dry_run=dry_run,
            )

    if McpWriteCapability.HANDOFF in service.write_capabilities:

        @server.tool(
            name="weft_handoff_create",
            title="Create WeftMark handoff",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        def handoff_create(
            change_set_id: str,
            idempotency_key: str,
            handoff_id: str,
            task_id: str,
            next_action: str,
            created_by: str,
            intended_receiver_id: str | None = None,
            known_failures: list[str] | None = None,
            supersedes_id: str | None = None,
        ) -> dict[str, Any]:
            """Create a clean-head, transcript-free handoff through WeftMark."""
            return service.create_handoff(
                change_set_id,
                idempotency_key=idempotency_key,
                handoff_id=handoff_id,
                task_id=task_id,
                next_action=next_action,
                created_by=created_by,
                intended_receiver_id=intended_receiver_id,
                known_failures=tuple(known_failures or ()),
                supersedes_id=supersedes_id,
            )

    if McpWriteCapability.SCOPE_AUDIT in service.write_capabilities:

        @server.tool(
            name="weft_scope_audit",
            title="Audit WeftMark scope",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        )
        def scope_audit(
            change_set_id: str,
            semantic_changes: list[str] | None = None,
            dry_run: bool = False,
        ) -> dict[str, Any]:
            """Record scope drift/semantic changes; dry-run writes nothing."""
            return service.audit_scope(
                change_set_id,
                semantic_changes=tuple(semantic_changes or ()),
                dry_run=dry_run,
            )

    if McpWriteCapability.EVIDENCE_EXEC in service.write_capabilities:

        @server.tool(
            name="weft_evidence_run",
            title="Run WeftMark command evidence",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        )
        def evidence_run(
            change_set_id: str,
            evidence_id: str,
            kind: str,
            argv: list[str],
            cwd: str,
            timeout_seconds: float = 300.0,
            redact_argv_indexes: list[int] | None = None,
            dry_run: bool = True,
        ) -> dict[str, Any]:
            """Execute local command evidence; defaults to dry-run for safety."""
            return service.run_evidence(
                change_set_id,
                evidence_id=evidence_id,
                kind=kind,
                argv=tuple(argv),
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                redact_argv_indexes=tuple(redact_argv_indexes or ()),
                dry_run=dry_run,
            )

    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weftmark-mcp",
        description=(
            "Serve WeftMark over MCP stdio. Read tools are always available; "
            "write tools require explicit process capabilities."
        ),
    )
    parser.add_argument("--repo", default=".", help="path inside the Git repository")
    parser.add_argument("--ledger", help="override the local JSONL ledger path")
    parser.add_argument(
        "--write-capability",
        action="append",
        choices=tuple(value.value for value in McpWriteCapability),
        default=[],
        help="register one write capability; repeat for multiple capabilities",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    capabilities = frozenset(
        McpWriteCapability(value) for value in args.write_capability
    )
    try:
        service = McpToolService.from_local(
            args.repo,
            ledger_path=(None if args.ledger is None else Path(args.ledger).resolve()),
            write_capabilities=capabilities,
        )
        server = build_server(service)
    except (McpDependencyError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    # stdio is deliberate for v0: process launch/configuration is the transport
    # trust boundary. Remote Streamable HTTP requires a separate auth design.
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
