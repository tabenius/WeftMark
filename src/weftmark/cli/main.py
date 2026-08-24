"""First local WeftMark CLI surface."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from weftmark.adapters.acp import AcpProviderSpec, AcpRuntimeProxy
from weftmark.adapters.git_local import LocalGit, LocalGitError
from weftmark.adapters.frog import FrogImportError, read_frog_snapshot
from weftmark.adapters.bundle_file import (
    BundleFileError,
    read_bundle,
    write_bundle,
)
from weftmark.adapters.jsonl_ledger import JsonlLedger, JsonlLedgerError
from weftmark.adapters.weft_plan import WeftPlanAdapter, WeftPlanError
from weftmark.application.change_binding import ChangeBindingError
from weftmark.application.bundle import (
    BundleError,
    BundleService,
    verification_to_payload,
    verify_bundle,
)
from weftmark.application.bundle_import import (
    BundleImportError,
    BundleImportService,
    import_result_to_payload,
    imported_bundle_to_payload,
)
from weftmark.application.claims import (
    ClaimConflict,
    ClaimService,
    ClaimServiceError,
    claim_to_payload,
)
from weftmark.application.ledger import LedgerService, LedgerServiceError
from weftmark.application.identifiers import new_id
from weftmark.application.frog_receipts import (
    FrogReceiptError,
    FrogReceiptService,
    receipt_result_to_payload,
    receipt_summary_to_payload,
    receipt_to_payload,
)
from weftmark.application.frog_promotions import (
    FrogPromotionError,
    FrogPromotionService,
    promotion_result_to_payload,
)
from weftmark.application.frog_planning import (
    FrogPlanningError,
    FrogPlanningService,
    selection_to_payload,
)
from weftmark.application.frog_task_claims import (
    FrogTaskClaimError,
    FrogTaskClaimService,
    frog_task_claim_result_to_payload,
)
from weftmark.application.lifecycle import (
    LifecycleError,
    LifecyclePolicyError,
    LifecycleService,
)
from weftmark.application.evidence_runner import (
    CommandEvidenceRequest,
    EvidenceRunnerError,
)
from weftmark.application.local_workflow import (
    LocalWorkflowError,
    LocalWorkflowService,
    evidence_result_to_payload,
    review_summary_to_payload,
    scope_audit_to_payload,
)
from weftmark.application.plan_import import (
    PlanImportDriftError,
    PlanImportError,
    PlanImportService,
    plan_drift_to_payload,
    plan_import_result_to_payload,
    plan_inspection_to_payload,
)
from weftmark.application.ports.git import GitObjectId
from weftmark.application.status import StatusService, status_to_payload
from weftmark.application.runtime_registry import (
    RuntimeRegistryError,
    load_runtime_registry,
)
from weftmark.application.runtime_workers import (
    RuntimeWorkerError,
    RuntimeWorkerService,
    runtime_worker_record_to_payload,
)
from weftmark.application.task_planning import (
    TaskPlanningError,
    TaskPlanningService,
    task_selection_to_payload,
)
from weftmark.application.task_claims import (
    TaskClaimError,
    TaskClaimService,
    task_claim_result_to_payload,
)
from weftmark.application.task_completion import (
    TaskCompletionError,
    TaskCompletionService,
    task_completion_result_to_payload,
)
from weftmark.application.tasks import (
    TaskService,
    TaskServiceError,
    conflict_to_payload,
    dependency_to_payload,
    task_to_payload,
)
from weftmark.application.workspace import (
    WorkspaceError,
    WorkspaceService,
    binding_to_payload,
)
from weftmark.domain.changeset import ChangeSetError, ChangeSetState
from weftmark.domain.evidence import (
    EvidenceKind,
    EvidenceProducer,
    EvidenceState,
    ProducerKind,
)
from weftmark.domain.scope import Scope, ScopeError
from weftmark.domain.task import TaskError, TaskIntent, TaskPriority, TaskState


EXIT_INVALID = 2
EXIT_NOT_FOUND = 3
EXIT_LEDGER = 4
EXIT_POLICY = 5
EXIT_EVIDENCE_FAILED = 6
EXIT_EVIDENCE_UNAVAILABLE = 7
EXIT_CONFLICT = 8
EXIT_BUNDLE = 9


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weftmark",
        description="Local coordination, Git lineage, evidence, and review.",
    )
    parser.add_argument("--repo", default=".", help="path inside the Git repository")
    parser.add_argument("--ledger", help="override the local JSONL ledger path")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="summarize current local workspace records")

    bundle = commands.add_parser("bundle", help="export and verify portable records")
    bundle_commands = bundle.add_subparsers(dest="bundle_command", required=True)
    bundle_export = bundle_commands.add_parser(
        "export", help="export one Change Set and related records"
    )
    bundle_export.add_argument("changeset_id")
    bundle_export.add_argument("--output")
    bundle_verify = bundle_commands.add_parser(
        "verify", help="verify an exported bundle offline"
    )
    bundle_verify.add_argument("path")
    bundle_import = bundle_commands.add_parser(
        "import", help="record a verified external bundle idempotently"
    )
    bundle_import.add_argument("path")
    bundle_commands.add_parser("list", help="list imported external bundles")
    bundle_show = bundle_commands.add_parser("show", help="show an imported bundle")
    bundle_show.add_argument("digest")

    task = commands.add_parser("task", help="manage native local task intent")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_create = task_commands.add_parser("create", help="create native task intent")
    task_create.add_argument("id")
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--why", required=True)
    task_create.add_argument("--what", required=True, dest="what_text")
    task_create.add_argument("--roi-note")
    task_create.add_argument(
        "--priority",
        choices=tuple(value.value for value in TaskPriority),
        default=TaskPriority.P2.value,
    )
    task_create.add_argument(
        "--state",
        choices=(TaskState.IDEA.value, TaskState.TODO.value),
        default=TaskState.TODO.value,
    )
    task_create.add_argument("--scope", action="append", default=[])
    task_show = task_commands.add_parser("show", help="show native task intent")
    task_show.add_argument("id")
    task_list = task_commands.add_parser("list", help="list native task intent")
    task_list.add_argument(
        "--state", choices=tuple(value.value for value in TaskState)
    )
    task_next = task_commands.add_parser(
        "next", help="rank dependency-eligible native task intent"
    )
    task_next.add_argument("--limit", type=int, default=1)
    task_claim = task_commands.add_parser(
        "claim", help="create or recover local work authority for native intent"
    )
    task_claim.add_argument("id", help="native task ID")
    task_claim.add_argument("--changeset-id")
    task_claim.add_argument("--claim-id")
    task_claim.add_argument("--base", default="HEAD")
    task_claim.add_argument("--agent", default="weftmark-cli")
    task_claim.add_argument("--session", default="local-session")
    task_claim.add_argument("--lease-seconds", type=int, default=1800)
    task_complete = task_commands.add_parser(
        "complete", help="complete reviewed merged native work and release its claim"
    )
    task_complete.add_argument("id", help="native task ID")
    task_complete.add_argument("--actor", default="weftmark-cli")
    task_complete.add_argument("--reason", required=True)
    task_plan = task_commands.add_parser(
        "plan", help="inspect or import reviewed source-plan intent"
    )
    task_plan_commands = task_plan.add_subparsers(
        dest="task_plan_command", required=True
    )
    for task_plan_command, help_text in (
        ("inspect", "compare source plans with the recorded import receipt"),
        ("import", "import source plans as non-authoritative native intent"),
    ):
        task_plan_parser = task_plan_commands.add_parser(
            task_plan_command, help=help_text
        )
        task_plan_parser.add_argument("--source-label", required=True)
        task_plan_parser.add_argument(
            "--plan-root",
            help="repository root containing tasks/*.weft.yml (defaults to --repo)",
        )
        task_plan_parser.add_argument(
            "--file",
            action="append",
            default=[],
            help="source-plan file relative to --plan-root; repeatable",
        )
    task_transition = task_commands.add_parser(
        "transition", help="record a non-terminal native task transition"
    )
    task_transition.add_argument("id")
    task_transition.add_argument(
        "state",
        choices=tuple(
            value.value for value in TaskState if value is not TaskState.DONE
        ),
    )
    task_transition.add_argument("--actor", default="weftmark-cli")
    task_transition.add_argument("--reason", required=True)
    task_dependency = task_commands.add_parser(
        "dependency", help="manage directed native task prerequisites"
    )
    task_dependency_commands = task_dependency.add_subparsers(
        dest="task_dependency_command", required=True
    )
    task_dependency_add = task_dependency_commands.add_parser(
        "add", help="declare TASK depends on DEPENDENCY"
    )
    task_dependency_add.add_argument("task_id")
    task_dependency_add.add_argument("depends_on_task_id")
    task_dependency_list = task_dependency_commands.add_parser(
        "list", help="list native task dependencies"
    )
    task_dependency_list.add_argument("--task")
    task_conflict = task_commands.add_parser(
        "conflict", help="manage symmetric native task conflicts"
    )
    task_conflict_commands = task_conflict.add_subparsers(
        dest="task_conflict_command", required=True
    )
    task_conflict_add = task_conflict_commands.add_parser(
        "add", help="declare a symmetric task scheduling conflict"
    )
    task_conflict_add.add_argument("left_task_id")
    task_conflict_add.add_argument("right_task_id")
    task_conflict_add.add_argument("--reason", required=True)
    task_conflict_list = task_conflict_commands.add_parser(
        "list", help="list native task conflicts"
    )
    task_conflict_list.add_argument("--task")

    runtime = commands.add_parser(
        "runtime", help="drive claim-gated disposable coding-agent workers"
    )
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)

    def add_runtime_provider_args(command: argparse.ArgumentParser) -> None:
        command.add_argument("--runtime-config")
        command.add_argument(
            "--runtime-provider",
            action="append",
            default=[],
            metavar="NAME=ARGV0:ARGV1[:cap=a,b]",
        )

    runtime_start = runtime_commands.add_parser("start", help="start a worker")
    runtime_start.add_argument("id", help="native task ID")
    runtime_start.add_argument("--provider", required=True)
    runtime_start.add_argument("--prompt", required=True)
    add_runtime_provider_args(runtime_start)
    runtime_status = runtime_commands.add_parser("status", help="observe a worker")
    runtime_status.add_argument("id", help="native task ID")
    add_runtime_provider_args(runtime_status)
    runtime_input = runtime_commands.add_parser("send-input", help="send one ACP prompt")
    runtime_input.add_argument("id", help="native task ID")
    runtime_input.add_argument("--data", required=True)
    add_runtime_provider_args(runtime_input)
    runtime_stop = runtime_commands.add_parser("stop", help="stop a worker")
    runtime_stop.add_argument("id", help="native task ID")
    add_runtime_provider_args(runtime_stop)

    frog = commands.add_parser("frog", help="import and inspect Frog plan snapshots")
    frog_commands = frog.add_subparsers(dest="frog_command", required=True)
    frog_snapshot = frog_commands.add_parser(
        "snapshot", help="manage read-only Frog snapshot receipts"
    )
    frog_snapshot_commands = frog_snapshot.add_subparsers(
        dest="frog_snapshot_command", required=True
    )
    frog_import = frog_snapshot_commands.add_parser(
        "import", help="capture and record one Frog database snapshot"
    )
    frog_import.add_argument("path")
    frog_import.add_argument("--source-label", required=True)
    frog_snapshot_commands.add_parser("list", help="list Frog snapshot receipts")
    frog_show = frog_snapshot_commands.add_parser(
        "show", help="show one Frog snapshot receipt"
    )
    frog_show.add_argument("digest")
    frog_task = frog_commands.add_parser("task", help="inspect imported Frog tasks")
    frog_task_commands = frog_task.add_subparsers(
        dest="frog_task_command", required=True
    )
    frog_task_list = frog_task_commands.add_parser(
        "list", help="list tasks from one Frog snapshot"
    )
    frog_task_list.add_argument("digest")
    frog_task_list.add_argument("--repo-path")
    frog_task_list.add_argument("--workflow-status")
    frog_task_next = frog_task_commands.add_parser(
        "next", help="rank advisory task intent eligible in one snapshot"
    )
    frog_task_next.add_argument("digest")
    frog_task_next.add_argument("--repo-path")
    frog_task_next.add_argument("--limit", type=int, default=1)
    frog_task_claim = frog_task_commands.add_parser(
        "claim", help="promote eligible imported intent and acquire local scopes"
    )
    frog_task_claim.add_argument("digest")
    frog_task_claim.add_argument("task_slug")
    frog_task_claim.add_argument("--id", help="local Change Set ID")
    frog_task_claim.add_argument("--claim-id")
    frog_task_claim.add_argument("--base", default="HEAD")
    frog_task_claim.add_argument("--agent", default="weftmark-cli")
    frog_task_claim.add_argument("--session", default="local-session")
    frog_task_claim.add_argument("--lease-seconds", type=int, default=1800)
    frog_task_claim.add_argument(
        "--scope",
        action="append",
        required=True,
        help="operator-approved local scope; imported Frog scopes are not authoritative",
    )
    frog_task_promote = frog_task_commands.add_parser(
        "promote", help="create local Change Set authority from imported intent"
    )
    frog_task_promote.add_argument("digest")
    frog_task_promote.add_argument("task_slug")
    frog_task_promote.add_argument("--id")
    frog_task_promote.add_argument("--base", default="HEAD")
    frog_task_promote.add_argument(
        "--scope",
        action="append",
        required=True,
        help="operator-approved local scope; imported Frog scopes are not authoritative",
    )

    changeset = commands.add_parser("changeset", help="manage Change Sets")
    changeset_commands = changeset.add_subparsers(dest="changeset_command", required=True)

    create = changeset_commands.add_parser("create", help="create and activate a Change Set")
    create.add_argument("id", nargs="?")
    create.add_argument("--goal", required=True)
    create.add_argument("--base", default="HEAD", help="base Git revision")
    create.add_argument(
        "--scope",
        action="append",
        required=True,
        help="canonical scope such as file:src/** or contract:api-v1",
    )

    show = changeset_commands.add_parser("show", help="show the latest Change Set snapshot")
    show.add_argument("id")
    refresh = changeset_commands.add_parser(
        "refresh", help="record the latest Git head, diff, and dirty paths"
    )
    refresh.add_argument("id")
    refresh.add_argument("--base", help="replace the tracked base revision")
    changeset_commands.add_parser("list", help="list latest Change Set snapshots")
    transition = changeset_commands.add_parser(
        "transition", help="record a validated lifecycle transition"
    )
    transition.add_argument("id")
    transition.add_argument(
        "state",
        choices=tuple(
            state.value
            for state in ChangeSetState
            if state is not ChangeSetState.PLANNED
        ),
    )

    claim = commands.add_parser("claim", help="manage semantic Change Set leases")
    claim_commands = claim.add_subparsers(dest="claim_command", required=True)
    claim_acquire = claim_commands.add_parser(
        "acquire", help="atomically acquire every declared Change Set scope"
    )
    claim_acquire.add_argument("changeset_id")
    claim_acquire.add_argument("--id")
    claim_acquire.add_argument("--agent", default="weftmark-cli")
    claim_acquire.add_argument("--session", default="local-session")
    claim_acquire.add_argument("--lease-seconds", type=int, default=1800)
    claim_show = claim_commands.add_parser("show", help="show a stored claim")
    claim_show.add_argument("id")
    claim_list = claim_commands.add_parser("list", help="list latest claims")
    claim_list.add_argument("--changeset")
    claim_renew = claim_commands.add_parser("renew", help="extend an active claim")
    claim_renew.add_argument("id")
    claim_renew.add_argument("--agent", default="weftmark-cli")
    claim_renew.add_argument("--session", default="local-session")
    claim_renew.add_argument("--extend-seconds", type=int, default=1800)
    claim_release = claim_commands.add_parser("release", help="release an active claim")
    claim_release.add_argument("id")
    claim_release.add_argument("--agent", default="weftmark-cli")
    claim_release.add_argument("--session", default="local-session")
    claim_release.add_argument("--reason", required=True)

    scope = commands.add_parser("scope", help="audit declared file and semantic scope")
    scope_commands = scope.add_subparsers(dest="scope_command", required=True)
    scope_audit = scope_commands.add_parser("audit", help="refresh and audit a Change Set")
    scope_audit.add_argument("changeset_id")
    scope_audit.add_argument(
        "--semantic-change",
        action="append",
        default=[],
        help="observed non-file scope such as contract:api-v1",
    )

    evidence = commands.add_parser("evidence", help="capture and inspect proof")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_run = evidence_commands.add_parser("run", help="run commit-bound local evidence")
    evidence_run.add_argument("changeset_id")
    evidence_run.add_argument("--id")
    evidence_run.add_argument(
        "--kind",
        choices=tuple(kind.value for kind in EvidenceKind),
        default=EvidenceKind.TEST.value,
    )
    evidence_run.add_argument("--cwd", help="command directory inside the worktree")
    evidence_run.add_argument("--timeout", type=float, default=300.0)
    evidence_run.add_argument("--redact-index", type=int, action="append", default=[])
    evidence_run.add_argument(
        "--command",
        dest="argv",
        nargs=argparse.REMAINDER,
        required=True,
        help="command and arguments; must be the final CLI option",
    )
    evidence_show = evidence_commands.add_parser("show", help="show stored evidence")
    evidence_show.add_argument("id")
    evidence_list = evidence_commands.add_parser("list", help="list stored evidence")
    evidence_list.add_argument("--changeset")

    review = commands.add_parser("review", help="produce and inspect readiness decisions")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_create = review_commands.add_parser("create", help="evaluate current readiness")
    review_create.add_argument("changeset_id")
    review_create.add_argument("--id")
    review_create.add_argument("--author", default="weftmark-cli")
    review_create.add_argument(
        "--require",
        action="append",
        default=[],
        choices=tuple(kind.value for kind in EvidenceKind),
    )
    review_create.add_argument(
        "--optional",
        action="append",
        default=[],
        choices=tuple(kind.value for kind in EvidenceKind),
    )
    review_create.add_argument("--semantic-change", action="append", default=[])
    review_show = review_commands.add_parser("show", help="show a stored review")
    review_show.add_argument("id")
    review_list = review_commands.add_parser("list", help="list stored reviews")
    review_list.add_argument("--changeset")

    handoff = commands.add_parser("handoff", help="create and inspect continuation records")
    handoff_commands = handoff.add_subparsers(dest="handoff_command", required=True)
    handoff_create = handoff_commands.add_parser("create", help="create a clean-head handoff")
    handoff_create.add_argument("changeset_id")
    handoff_create.add_argument("--id")
    handoff_create.add_argument("--task", required=True)
    handoff_create.add_argument("--next", required=True, dest="next_action")
    handoff_create.add_argument("--created-by", default="weftmark-cli")
    handoff_create.add_argument("--receiver")
    handoff_create.add_argument("--known-failure", action="append", default=[])
    handoff_create.add_argument("--supersedes")
    handoff_show = handoff_commands.add_parser("show", help="show a stored handoff")
    handoff_show.add_argument("id")
    handoff_list = handoff_commands.add_parser("list", help="list stored handoffs")
    handoff_list.add_argument("--changeset")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "bundle" and args.bundle_command == "verify":
            verification = verification_to_payload(verify_bundle(read_bundle(args.path)))
            _emit_bundle_verification(verification, json_output=args.json)
            return 0
        git = LocalGit(args.repo)
        repository = git.repository()
        ledger_path = _ledger_path(args.ledger, repository.id)
        ledger = LedgerService(JsonlLedger(ledger_path))
        workspace = WorkspaceService(git, ledger)
        claims = ClaimService(workspace, ledger)
        workflow = LocalWorkflowService(
            workspace,
            ledger,
            EvidenceProducer(ProducerKind.WORKER, "weftmark-cli"),
        )
        lifecycle = LifecycleService(workspace, workflow)
        bundles = BundleService(workspace, claims, workflow)
        bundle_imports = BundleImportService(ledger)
        frog_receipts = FrogReceiptService(ledger)
        frog_promotions = FrogPromotionService(frog_receipts, workspace, ledger)
        frog_planning = FrogPlanningService(frog_receipts)
        frog_task_claims = FrogTaskClaimService(
            frog_planning, frog_promotions, claims
        )
        tasks = TaskService(ledger)
        task_planning = TaskPlanningService(tasks)
        task_claims = TaskClaimService(
            task_planning, tasks, workspace, claims, ledger
        )
        task_completions = TaskCompletionService(task_claims, claims, ledger)
        plan_imports = PlanImportService(tasks, ledger)

        def runtime_workers() -> RuntimeWorkerService:
            registry = load_runtime_registry(
                config_path=args.runtime_config,
                cli_flags=args.runtime_provider,
            )

            def adapter_factory(provider_name: str) -> AcpRuntimeProxy:
                provider = registry.get(provider_name)
                return AcpRuntimeProxy(AcpProviderSpec(provider.name, provider.argv))

            repo_path = repository.worktree or str(Path(args.repo).resolve())
            return RuntimeWorkerService(
                task_claims,
                claims,
                registry,
                adapter_factory,
                repo_path,
                ledger,
                lambda change_set_id: GitObjectId(
                    workspace.require_change_set(change_set_id).change_set.base_sha
                ),
            )

        if args.command == "status":
            payload = status_to_payload(
                StatusService(workspace, claims, workflow).summarize(
                    observed_at=_now()
                )
            )
            _emit_status(payload, json_output=args.json)
            return 0
        if args.command == "task" and args.task_command == "create":
            created_at = _now()
            value = tasks.create(
                TaskIntent.create(
                    id=args.id,
                    title=args.title,
                    why=args.why,
                    what=args.what_text,
                    roi_note=args.roi_note,
                    priority=TaskPriority(args.priority),
                    state=TaskState(args.state),
                    scopes=tuple(Scope.parse(value) for value in args.scope),
                    created_at=created_at,
                )
            )
            _emit_task(task_to_payload(value), json_output=args.json, action="created")
            return 0
        if args.command == "task" and args.task_command == "show":
            value = tasks.get(args.id)
            if value is None:
                _emit_error(f"Task not found: {args.id}", json_output=args.json)
                return EXIT_NOT_FOUND
            _emit_task(task_to_payload(value), json_output=args.json)
            return 0
        if args.command == "task" and args.task_command == "complete":
            result = task_completions.complete(
                args.id,
                actor_id=args.actor,
                reason=args.reason,
                completed_at=_now(),
            )
            _emit_native_task_completion(
                task_completion_result_to_payload(result), json_output=args.json
            )
            return 0
        if args.command == "task" and args.task_command == "list":
            state = None if args.state is None else TaskState(args.state)
            payloads = [
                task_to_payload(value)
                for value in tasks.list()
                if state is None or value.state is state
            ]
            _emit_task_list(payloads, json_output=args.json)
            return 0
        if args.command == "task" and args.task_command == "next":
            _emit_native_task_selection(
                task_selection_to_payload(task_planning.next(limit=args.limit)),
                json_output=args.json,
            )
            return 0
        if args.command == "task" and args.task_command == "claim":
            claimed_at = _now()
            result = task_claims.claim(
                args.id,
                change_set_id=args.changeset_id,
                claim_id=args.claim_id,
                base_revision=args.base,
                agent_id=args.agent,
                session_id=args.session,
                claimed_at=claimed_at,
                lease_seconds=args.lease_seconds,
            )
            _emit_native_task_claim(
                task_claim_result_to_payload(result, observed_at=claimed_at),
                json_output=args.json,
            )
            return 0
        if args.command == "task" and args.task_command == "plan":
            plan_root = args.plan_root or repository.worktree or args.repo
            snapshot = WeftPlanAdapter(plan_root).load(args.file or None)
            if args.task_plan_command == "inspect":
                _emit_source_plan_inspection(
                    plan_inspection_to_payload(
                        plan_imports.inspect_snapshot(
                            snapshot, source_label=args.source_label
                        )
                    ),
                    json_output=args.json,
                )
                return 0
            result = plan_imports.import_snapshot(
                snapshot,
                source_label=args.source_label,
                imported_at=_now(),
            )
            _emit_source_plan_import(
                plan_import_result_to_payload(result), json_output=args.json
            )
            return 0
        if args.command == "runtime" and args.runtime_command == "start":
            record = runtime_workers().start(
                args.id, provider=args.provider, prompt=args.prompt, started_at=_now()
            )
            _emit_runtime_worker(runtime_worker_record_to_payload(record), json_output=args.json)
            return 0
        if args.command == "runtime" and args.runtime_command == "status":
            record = runtime_workers().status(args.id, observed_at=_now())
            _emit_runtime_worker(runtime_worker_record_to_payload(record), json_output=args.json)
            return 0
        if args.command == "runtime" and args.runtime_command == "send-input":
            record = runtime_workers().send_input(args.id, args.data, observed_at=_now())
            _emit_runtime_worker(runtime_worker_record_to_payload(record), json_output=args.json)
            return 0
        if args.command == "runtime" and args.runtime_command == "stop":
            record = runtime_workers().stop(args.id, observed_at=_now())
            _emit_runtime_worker(runtime_worker_record_to_payload(record), json_output=args.json)
            return 0
        if args.command == "task" and args.task_command == "transition":
            value = tasks.transition(
                args.id,
                state=TaskState(args.state),
                actor_id=args.actor,
                rationale=args.reason,
                occurred_at=_now(),
            )
            _emit_task(
                task_to_payload(value), json_output=args.json, action="transitioned"
            )
            return 0
        if (
            args.command == "task"
            and args.task_command == "dependency"
            and args.task_dependency_command == "add"
        ):
            result = tasks.add_dependency(
                args.task_id, args.depends_on_task_id, created_at=_now()
            )
            _emit_task_relation(
                dependency_to_payload(result.relation),
                relation="dependency",
                created=result.created,
                json_output=args.json,
            )
            return 0
        if (
            args.command == "task"
            and args.task_command == "dependency"
            and args.task_dependency_command == "list"
        ):
            payloads = [
                dependency_to_payload(value)
                for value in tasks.dependencies()
                if args.task is None or value.task_id == args.task
            ]
            _emit_task_relation_list(
                payloads, relation="dependencies", json_output=args.json
            )
            return 0
        if (
            args.command == "task"
            and args.task_command == "conflict"
            and args.task_conflict_command == "add"
        ):
            result = tasks.add_conflict(
                args.left_task_id,
                args.right_task_id,
                reason=args.reason,
                created_at=_now(),
            )
            _emit_task_relation(
                conflict_to_payload(result.relation),
                relation="conflict",
                created=result.created,
                json_output=args.json,
            )
            return 0
        if (
            args.command == "task"
            and args.task_command == "conflict"
            and args.task_conflict_command == "list"
        ):
            payloads = [
                conflict_to_payload(value)
                for value in tasks.conflicts()
                if args.task is None or value.includes(args.task)
            ]
            _emit_task_relation_list(
                payloads, relation="conflicts", json_output=args.json
            )
            return 0
        if args.command == "bundle" and args.bundle_command == "export":
            payload = bundles.export(args.changeset_id, exported_at=_now())
            if args.output:
                output = write_bundle(args.output, payload)
                _emit_bundle_file(
                    output,
                    digest=payload["digest"],
                    change_set_id=args.changeset_id,
                    json_output=args.json,
                )
            else:
                print(json.dumps(payload, sort_keys=True, indent=None if args.json else 2))
            return 0
        if args.command == "bundle" and args.bundle_command == "import":
            result = bundle_imports.import_bundle(
                read_bundle(args.path), imported_at=_now()
            )
            _emit_bundle_import(
                import_result_to_payload(result), json_output=args.json
            )
            return 0
        if args.command == "bundle" and args.bundle_command == "list":
            payloads = [
                {
                    "digest": value.digest,
                    "change_set_id": value.change_set_id,
                    "imported_at": value.imported_at.isoformat(),
                }
                for value in bundle_imports.list()
            ]
            _emit_bundle_import_list(payloads, json_output=args.json)
            return 0
        if args.command == "bundle" and args.bundle_command == "show":
            value = bundle_imports.get(args.digest)
            if value is None:
                _emit_error(
                    f"Imported bundle not found: {args.digest}",
                    json_output=args.json,
                )
                return EXIT_NOT_FOUND
            payload = imported_bundle_to_payload(value)
            if args.json:
                print(json.dumps({"ok": True, "imported_bundle": payload}, sort_keys=True))
            else:
                print(json.dumps(payload, sort_keys=True, indent=2))
            return 0
        if (
            args.command == "frog"
            and args.frog_command == "snapshot"
            and args.frog_snapshot_command == "import"
        ):
            captured_at = _now()
            snapshot = read_frog_snapshot(
                args.path,
                source_label=args.source_label,
                captured_at=captured_at,
            )
            result = frog_receipts.record(
                snapshot.to_payload(), imported_at=_now()
            )
            _emit_frog_receipt_result(
                receipt_result_to_payload(result), json_output=args.json
            )
            return 0
        if (
            args.command == "frog"
            and args.frog_command == "snapshot"
            and args.frog_snapshot_command == "list"
        ):
            payloads = [
                receipt_summary_to_payload(receipt)
                for receipt in frog_receipts.list()
            ]
            _emit_frog_receipt_list(payloads, json_output=args.json)
            return 0
        if (
            args.command == "frog"
            and args.frog_command == "snapshot"
            and args.frog_snapshot_command == "show"
        ):
            receipt = frog_receipts.get(args.digest)
            if receipt is None:
                _emit_error(
                    f"Frog snapshot not found: {args.digest}",
                    json_output=args.json,
                )
                return EXIT_NOT_FOUND
            payload = receipt_to_payload(receipt)
            if args.json:
                print(json.dumps({"ok": True, "frog_snapshot": payload}, sort_keys=True))
            else:
                _emit_frog_receipt_summary(payload)
            return 0
        if (
            args.command == "frog"
            and args.frog_command == "task"
            and args.frog_task_command == "list"
        ):
            tasks = frog_receipts.tasks(
                args.digest,
                repo_path=args.repo_path,
                workflow_status=args.workflow_status,
            )
            if tasks is None:
                _emit_error(
                    f"Frog snapshot not found: {args.digest}",
                    json_output=args.json,
                )
                return EXIT_NOT_FOUND
            _emit_frog_task_list(list(tasks), json_output=args.json)
            return 0
        if (
            args.command == "frog"
            and args.frog_command == "task"
            and args.frog_task_command == "promote"
        ):
            result = frog_promotions.promote(
                args.digest,
                args.task_slug,
                change_set_id=args.id,
                base_revision=args.base,
                scopes=tuple(Scope.parse(value) for value in args.scope),
                promoted_at=_now(),
            )
            _emit_frog_promotion(
                promotion_result_to_payload(result), json_output=args.json
            )
            return 0
        if (
            args.command == "frog"
            and args.frog_command == "task"
            and args.frog_task_command == "next"
        ):
            selection = frog_planning.next(
                args.digest,
                repo_path=args.repo_path,
                limit=args.limit,
            )
            _emit_frog_task_selection(
                selection_to_payload(selection), json_output=args.json
            )
            return 0
        if (
            args.command == "frog"
            and args.frog_command == "task"
            and args.frog_task_command == "claim"
        ):
            claimed_at = _now()
            result = frog_task_claims.claim(
                args.digest,
                args.task_slug,
                change_set_id=args.id,
                claim_id=args.claim_id,
                base_revision=args.base,
                scopes=tuple(Scope.parse(value) for value in args.scope),
                agent_id=args.agent,
                session_id=args.session,
                claimed_at=claimed_at,
                lease_seconds=args.lease_seconds,
            )
            _emit_frog_task_claim(
                frog_task_claim_result_to_payload(result, observed_at=claimed_at),
                json_output=args.json,
            )
            return 0

        if args.command == "changeset" and args.changeset_command == "create":
            result = _create_changeset(args, workspace)
            _emit(result, json_output=args.json, action="created")
            return 0
        if args.command == "changeset" and args.changeset_command == "show":
            binding = workspace.get_change_set(args.id)
            if binding is None:
                _emit_error(f"Change Set not found: {args.id}", json_output=args.json)
                return EXIT_NOT_FOUND
            _emit(binding_to_payload(binding), json_output=args.json)
            return 0
        if args.command == "changeset" and args.changeset_command == "refresh":
            binding = workspace.refresh_change_set(
                args.id,
                observed_at=_now(),
                base_revision=args.base,
            )
            _emit(
                binding_to_payload(binding),
                json_output=args.json,
                action="refreshed",
            )
            return 0
        if args.command == "changeset" and args.changeset_command == "list":
            result = [
                binding_to_payload(binding)
                for binding in workspace.list_change_sets()
            ]
            _emit_list(result, json_output=args.json)
            return 0
        if args.command == "changeset" and args.changeset_command == "transition":
            binding = lifecycle.transition(
                args.id,
                state=ChangeSetState(args.state),
                transitioned_at=_now(),
            )
            _emit(
                binding_to_payload(binding),
                json_output=args.json,
                action="transitioned",
            )
            return 0
        if args.command == "claim" and args.claim_command == "acquire":
            observed_at = _now()
            value = claims.acquire(
                args.changeset_id,
                id=args.id or new_id("claim", at=observed_at),
                agent_id=args.agent,
                session_id=args.session,
                acquired_at=observed_at,
                lease_seconds=args.lease_seconds,
            )
            _emit_claim(
                claim_to_payload(value, observed_at=observed_at),
                json_output=args.json,
            )
            return 0
        if args.command == "claim" and args.claim_command == "show":
            value = claims.get(args.id)
            if value is None:
                _emit_error(f"Claim not found: {args.id}", json_output=args.json)
                return EXIT_NOT_FOUND
            _emit_claim(
                claim_to_payload(value, observed_at=_now()),
                json_output=args.json,
            )
            return 0
        if args.command == "claim" and args.claim_command == "list":
            observed_at = _now()
            payloads = [
                claim_to_payload(value, observed_at=observed_at)
                for value in claims.list(change_set_id=args.changeset)
            ]
            _emit_claim_list(payloads, json_output=args.json)
            return 0
        if args.command == "claim" and args.claim_command == "renew":
            observed_at = _now()
            value = claims.renew(
                args.id,
                agent_id=args.agent,
                session_id=args.session,
                renewed_at=observed_at,
                extend_seconds=args.extend_seconds,
            )
            _emit_claim(
                claim_to_payload(value, observed_at=observed_at),
                json_output=args.json,
            )
            return 0
        if args.command == "claim" and args.claim_command == "release":
            observed_at = _now()
            value = claims.release(
                args.id,
                agent_id=args.agent,
                session_id=args.session,
                released_at=observed_at,
                reason=args.reason,
            )
            _emit_claim(
                claim_to_payload(value, observed_at=observed_at),
                json_output=args.json,
            )
            return 0
        if args.command == "scope" and args.scope_command == "audit":
            result = workflow.audit_scope(
                args.changeset_id,
                semantic_changes=tuple(
                    Scope.parse(value) for value in args.semantic_change
                ),
                audited_at=_now(),
            )
            payload = scope_audit_to_payload(
                result, change_set_id=args.changeset_id
            )
            _emit_scope(payload, json_output=args.json)
            return 0 if result.is_within_scope else EXIT_POLICY
        if args.command == "evidence" and args.evidence_command == "run":
            argv = tuple(args.argv)
            binding = workspace.require_change_set(args.changeset_id)
            observed_at = _now()
            result = workflow.run_evidence(
                args.changeset_id,
                CommandEvidenceRequest(
                    id=args.id or new_id("evidence", at=observed_at),
                    kind=EvidenceKind(args.kind),
                    argv=argv,
                    cwd=args.cwd or binding.latest.worktree,
                    redact_argv_indexes=frozenset(args.redact_index),
                    timeout_seconds=args.timeout,
                ),
                observed_at=observed_at,
            )
            _emit_evidence(
                evidence_result_to_payload(result), json_output=args.json
            )
            if result.evidence.state is EvidenceState.PASSED:
                return 0
            if result.evidence.state is EvidenceState.FAILED:
                return EXIT_EVIDENCE_FAILED
            return EXIT_EVIDENCE_UNAVAILABLE
        if args.command == "evidence" and args.evidence_command == "show":
            result = workflow.get_evidence(args.id)
            if result is None:
                _emit_error(f"Evidence not found: {args.id}", json_output=args.json)
                return EXIT_NOT_FOUND
            _emit_evidence(
                evidence_result_to_payload(result), json_output=args.json
            )
            return 0
        if args.command == "evidence" and args.evidence_command == "list":
            results = workflow.list_evidence(change_set_id=args.changeset)
            payloads = [evidence_result_to_payload(result) for result in results]
            _emit_evidence_list(payloads, json_output=args.json)
            return 0
        if args.command == "review" and args.review_command == "create":
            required = args.require or [EvidenceKind.TEST.value]
            decided_at = _now()
            summary = workflow.review(
                args.changeset_id,
                decision_id=args.id or new_id("review", at=decided_at),
                author_id=args.author,
                required_kinds=tuple(EvidenceKind(value) for value in required),
                optional_kinds=tuple(EvidenceKind(value) for value in args.optional),
                semantic_changes=tuple(
                    Scope.parse(value) for value in args.semantic_change
                ),
                decided_at=decided_at,
            )
            payload = review_summary_to_payload(summary)
            _emit_review(payload, json_output=args.json)
            return 0 if summary.is_releasable else EXIT_POLICY
        if args.command == "review" and args.review_command == "show":
            payload = workflow.get_review(args.id)
            if payload is None:
                _emit_error(f"Review not found: {args.id}", json_output=args.json)
                return EXIT_NOT_FOUND
            _emit_review(payload, json_output=args.json)
            return 0
        if args.command == "review" and args.review_command == "list":
            payloads = list(workflow.list_reviews(change_set_id=args.changeset))
            _emit_review_list(payloads, json_output=args.json)
            return 0
        if args.command == "handoff" and args.handoff_command == "create":
            created_at = _now()
            handoff = workflow.create_handoff(
                args.changeset_id,
                id=args.id or new_id("handoff", at=created_at),
                task_id=args.task,
                next_action=args.next_action,
                created_by=args.created_by,
                created_at=created_at,
                intended_receiver_id=args.receiver,
                known_failures=tuple(args.known_failure),
                supersedes_id=args.supersedes,
            )
            _emit_handoff(handoff.to_dict(), json_output=args.json)
            return 0
        if args.command == "handoff" and args.handoff_command == "show":
            handoff = workflow.get_handoff(args.id)
            if handoff is None:
                _emit_error(f"Handoff not found: {args.id}", json_output=args.json)
                return EXIT_NOT_FOUND
            _emit_handoff(handoff.to_dict(), json_output=args.json)
            return 0
        if args.command == "handoff" and args.handoff_command == "list":
            payloads = [
                handoff.to_dict()
                for handoff in workflow.list_handoffs(
                    change_set_id=args.changeset
                )
            ]
            _emit_handoff_list(payloads, json_output=args.json)
            return 0
    except LifecyclePolicyError as error:
        _emit_error(str(error), json_output=args.json)
        return EXIT_POLICY
    except ClaimConflict as error:
        _emit_error(str(error), json_output=args.json)
        return EXIT_CONFLICT
    except (BundleError, BundleFileError, BundleImportError) as error:
        _emit_error(str(error), json_output=args.json)
        return EXIT_BUNDLE
    except (
        FrogImportError,
        FrogPlanningError,
        FrogPromotionError,
        FrogReceiptError,
        FrogTaskClaimError,
    ) as error:
        _emit_error(str(error), json_output=args.json)
        return EXIT_INVALID
    except PlanImportDriftError as error:
        _emit_source_plan_drift(
            str(error), plan_drift_to_payload(error.drift), json_output=args.json
        )
        return EXIT_INVALID
    except (JsonlLedgerError, LedgerServiceError) as error:
        _emit_error(str(error), json_output=args.json)
        return EXIT_LEDGER
    except (
        ChangeBindingError,
        ChangeSetError,
        ClaimServiceError,
        LifecycleError,
        LocalGitError,
        ScopeError,
        TaskError,
        TaskClaimError,
        TaskCompletionError,
        TaskPlanningError,
        TaskServiceError,
        RuntimeRegistryError,
        RuntimeWorkerError,
        WorkspaceError,
        EvidenceRunnerError,
        LocalWorkflowError,
        PlanImportError,
        ValueError,
        WeftPlanError,
    ) as error:
        _emit_error(str(error), json_output=args.json)
        return EXIT_INVALID
    parser.error("unsupported command")
    return EXIT_INVALID


def _create_changeset(
    args: argparse.Namespace,
    workspace: WorkspaceService,
) -> dict[str, Any]:
    scopes = tuple(Scope.parse(value) for value in args.scope)
    timestamp = _now()
    binding = workspace.create_change_set(
        id=args.id or new_id("chg", at=timestamp),
        goal=args.goal,
        base_revision=args.base,
        scopes=scopes,
        created_at=timestamp,
    )
    return binding_to_payload(binding)


def _ledger_path(override: str | None, repository_id: str) -> Path:
    if override:
        return Path(override).resolve()
    if not repository_id.startswith("git:"):
        raise ChangeBindingError("local repository identity cannot select a ledger")
    return Path(repository_id.removeprefix("git:")) / "weftmark" / "ledger.jsonl"


def _emit(payload: dict[str, Any], *, json_output: bool, action: str | None = None) -> None:
    if json_output:
        print(json.dumps({"ok": True, "changeset": payload}, sort_keys=True))
        return
    prefix = f"{action} " if action else ""
    print(f"{prefix}{payload['id']}  {payload['state']}")
    print(f"  goal: {payload['goal']}")
    print(f"  branch: {payload['branch']}")
    print(f"  base: {payload['base_sha']}")
    print(f"  head: {payload['head_sha']}")
    print("  scopes: " + ", ".join(f"{scope['kind']}:{scope['key']}" for scope in payload["scopes"]))


def _emit_task(
    payload: dict[str, Any], *, json_output: bool, action: str | None = None
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "task": payload}, sort_keys=True))
        return
    prefix = f"{action} " if action else ""
    print(f"{prefix}{payload['id']}  {payload['state']}  {payload['priority']}")
    print(f"  title: {payload['title']}")
    print(
        "  scopes: "
        + (
            ", ".join(
                f"{scope['kind']}:{scope['key']}" for scope in payload["scopes"]
            )
            or "(none)"
        )
    )


def _emit_task_list(payloads: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "tasks": payloads}, sort_keys=True))
        return
    if not payloads:
        print("no native tasks")
        return
    for payload in payloads:
        print(
            f"{payload['id']}  {payload['state']}  "
            f"{payload['priority']}  {payload['title']}"
        )


def _emit_native_task_selection(
    payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "task_selection": payload}, sort_keys=True))
        return
    print(f"{payload['eligible']} eligible of {payload['considered']} considered")
    if not payload["tasks"]:
        print("no dependency-eligible native tasks")
    for value in payload["tasks"]:
        task = value["task"]
        print(
            f"{task['id']}  {task['priority']}  {task['state']}  {task['title']}"
        )
    if payload["skipped"]:
        print("  skipped:")
        shown = payload["skipped"][:5]
        for value in shown:
            print(f"    {value['id']}: " + "; ".join(value["reasons"]))
        remaining = payload["skipped_count"] - len(shown)
        if remaining:
            print(f"    ... and {remaining} more")
    print("  advisory only; acquire a Change Set claim before editing")


def _emit_native_task_claim(
    payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "task_claim": payload}, sort_keys=True))
        return
    action = "claimed" if payload["claimed"] else "already claimed"
    print(
        f"{action} {payload['task_id']}  {payload['change_set']['id']}  "
        f"{payload['claim']['id']}"
    )
    print(f"  expires: {payload['claim']['locks'][0]['expires_at']}")
    print(
        "  scopes: "
        + ", ".join(
            f"{lock['scope']['kind']}:{lock['scope']['key']}"
            for lock in payload["claim"]["locks"]
        )
    )


def _emit_native_task_completion(
    payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "task_completion": payload}, sort_keys=True))
        return
    action = "completed" if payload["completed"] else "already completed"
    print(f"{action} {payload['task_id']}  {payload['change_set_id']}")
    print(f"  review: {payload['review_id']}  head: {payload['head_sha']}")
    print(f"  claim: {payload['claim_id']}  released:{payload['claim_released']}")


def _emit_source_plan_inspection(
    payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "source_plan_inspection": payload}, sort_keys=True))
        return
    print(
        f"source plan {payload['source_label']}  {payload['status']}  "
        f"{payload['source_digest']}"
    )
    print(f"  authority: {payload['authority']}")
    if payload["drift"] is not None:
        _print_source_plan_drift(payload["drift"])


def _emit_source_plan_import(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "source_plan_import": payload}, sort_keys=True))
        return
    action = "imported" if payload["imported"] else "already imported"
    print(f"{action} {payload['source_label']}  {payload['source_digest']}")
    print(
        f"  tasks created:{len(payload['created_tasks'])} "
        f"existing:{len(payload['existing_tasks'])} "
        f"terminal source-only:{len(payload['skipped_terminal_tasks'])}"
    )
    print(f"  authority: {payload['authority']}")


def _emit_source_plan_drift(
    message: str, payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(
            json.dumps(
                {"ok": False, "error": message, "source_plan_drift": payload},
                sort_keys=True,
            )
        )
        return
    print(f"error: {message}", file=sys.stderr)
    _print_source_plan_drift(payload, stream=sys.stderr)


def _print_source_plan_drift(
    payload: Mapping[str, Any], *, stream: Any = sys.stdout
) -> None:
    print(
        f"  digest: {payload['previous_digest']} -> {payload['current_digest']}",
        file=stream,
    )
    for key in (
        "added_tasks",
        "removed_tasks",
        "changed_tasks",
        "added_files",
        "removed_files",
        "changed_files",
    ):
        values = payload[key]
        if values:
            print(f"  {key.replace('_', ' ')}: {', '.join(values)}", file=stream)


def _emit_runtime_worker(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "runtime_worker": payload}, sort_keys=True))
        return
    print(f"{payload['task_id']}  {payload['provider']}  {payload['state']}")
    print(f"  change set: {payload['change_set_id']}")


def _emit_task_relation(
    payload: dict[str, Any],
    *,
    relation: str,
    created: bool,
    json_output: bool,
) -> None:
    if json_output:
        print(
            json.dumps(
                {"ok": True, relation: payload, "created": created}, sort_keys=True
            )
        )
        return
    action = "created" if created else "already exists"
    print(f"{action} {relation}")
    if relation == "dependency":
        print(f"  {payload['task_id']} depends on {payload['depends_on_task_id']}")
    else:
        print(
            f"  {payload['first_task_id']} conflicts with "
            f"{payload['second_task_id']}: {payload['reason']}"
        )


def _emit_task_relation_list(
    payloads: list[dict[str, Any]], *, relation: str, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, relation: payloads}, sort_keys=True))
        return
    if not payloads:
        print(f"no task {relation}")
        return
    for payload in payloads:
        if relation == "dependencies":
            print(f"{payload['task_id']} -> {payload['depends_on_task_id']}")
        else:
            print(
                f"{payload['first_task_id']} x {payload['second_task_id']}  "
                f"{payload['reason']}"
            )


def _emit_list(payloads: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "changesets": payloads}, sort_keys=True))
        return
    if not payloads:
        print("no Change Sets")
        return
    for payload in payloads:
        print(f"{payload['id']}  {payload['state']}  {payload['branch']}  {payload['goal']}")


def _emit_status(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "status": payload}, sort_keys=True))
        return
    counts = payload["counts"]
    print(
        f"{counts['change_sets']} Change Sets  "
        f"{counts['active_claims']} active claims  "
        f"{counts['expired_claims']} expired  "
        f"{counts['released_claims']} released"
    )
    if not payload["change_sets"]:
        print("no Change Sets")
        return
    for value in payload["change_sets"]:
        dirty = f"  dirty:{len(value['dirty_paths'])}" if value["dirty_paths"] else ""
        claims = ",".join(value["active_claim_ids"]) or "unclaimed"
        evidence = value["evidence"]
        print(
            f"{value['id']}  {value['lifecycle_state']}  {value['readiness']}  "
            f"claim:{claims}  evidence:{evidence['current']}/{evidence['total']}"
            f"{dirty}"
        )
        print(f"  observed head: {value['observed_head_sha']}  {value['observed_at']}")


def _emit_bundle_file(
    path: Path,
    *,
    digest: str,
    change_set_id: str,
    json_output: bool,
) -> None:
    payload = {
        "path": str(path),
        "digest": digest,
        "change_set_id": change_set_id,
    }
    if json_output:
        print(json.dumps({"ok": True, "bundle": payload}, sort_keys=True))
        return
    print(f"exported {change_set_id}  {digest}")
    print(f"  path: {path}")


def _emit_bundle_verification(
    payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "verification": payload}, sort_keys=True))
        return
    counts = payload["counts"]
    print(f"verified {payload['change_set_id']}  {payload['digest']}")
    print(
        f"  claims:{counts['claims']} evidence:{counts['evidence']} "
        f"reviews:{counts['reviews']} handoffs:{counts['handoffs']}"
    )


def _emit_bundle_import(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "import": payload}, sort_keys=True))
        return
    action = "imported" if payload["imported"] else "already imported"
    print(f"{action} {payload['change_set_id']}  {payload['digest']}")
    print(f"  ledger sequence: {payload['sequence']}")


def _emit_bundle_import_list(
    payloads: list[dict[str, Any]], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "imports": payloads}, sort_keys=True))
        return
    if not payloads:
        print("no imported bundles")
        return
    for payload in payloads:
        print(
            f"{payload['change_set_id']}  {payload['digest']}  "
            f"{payload['imported_at']}"
        )


def _emit_frog_receipt_result(
    payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "frog_import": payload}, sort_keys=True))
        return
    action = "imported" if payload["imported"] else "already imported"
    print(f"{action} {payload['source_label']}  {payload['digest']}")
    print(f"  tasks:{payload['counts'].get('tasks', 0)}  sequence:{payload['sequence']}")


def _emit_frog_receipt_list(
    payloads: list[dict[str, Any]], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "frog_snapshots": payloads}, sort_keys=True))
        return
    if not payloads:
        print("no Frog snapshots")
        return
    for payload in payloads:
        _emit_frog_receipt_summary(payload)


def _emit_frog_receipt_summary(payload: dict[str, Any]) -> None:
    print(f"{payload['source_label']}  {payload['digest']}")
    print(
        f"  captured:{payload['captured_at']}  "
        f"tasks:{payload['counts'].get('tasks', 0)}  "
        f"locks:{payload['counts'].get('locks', 0)}"
    )


def _emit_frog_task_list(
    payloads: list[Mapping[str, Any]], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "frog_tasks": payloads}, sort_keys=True))
        return
    if not payloads:
        print("no matching Frog tasks")
        return
    for payload in payloads:
        print(
            f"{payload['slug']}  {payload['workflow_status']}  "
            f"{payload['priority']}  {payload['title']}"
        )


def _emit_frog_promotion(
    payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "frog_promotion": payload}, sort_keys=True))
        return
    action = "promoted" if payload["promoted"] else "already promoted"
    change_set = payload["change_set"]
    print(f"{action} {payload['source_task_slug']}  {change_set['id']}")
    print(f"  source snapshot: {payload['source_snapshot_digest']}")
    print(f"  source repo: {payload['source_repo_path']}")
    print(
        "  local scopes: "
        + ", ".join(
            f"{scope['kind']}:{scope['key']}" for scope in change_set["scopes"]
        )
    )


def _emit_frog_task_selection(
    payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "frog_task_selection": payload}, sort_keys=True))
        return
    print(
        f"{payload['eligible']} eligible of {payload['considered']} considered  "
        f"source:{payload['source_label']}"
    )
    if not payload["tasks"]:
        print("no dependency-eligible imported tasks")
    for value in payload["tasks"]:
        task = value["task"]
        print(
            f"{task['slug']}  {task.get('priority') or '-'}  "
            f"{task.get('workflow_status') or '-'}  {task['title']}"
        )
    ignored = payload["ignored_observations"]
    print(
        f"  advisory only; ignored imported authority: "
        f"{ignored['locks']} locks, {ignored['assignments']} assignments"
    )
    if payload["skipped"]:
        print("  skipped:")
        shown = payload["skipped"][:5]
        for value in shown:
            print(
                f"    {value['slug']}: " + "; ".join(value["reasons"])
            )
        remaining = payload["skipped_count"] - len(shown)
        if remaining:
            print(f"    ... and {remaining} more")


def _emit_frog_task_claim(
    payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps({"ok": True, "frog_task_claim": payload}, sort_keys=True))
        return
    action = "claimed" if payload["claimed"] else "already claimed"
    promotion = payload["promotion"]
    claim = payload["claim"]
    print(
        f"{action} {promotion['source_task_slug']}  "
        f"{promotion['change_set']['id']}  {claim['id']}"
    )
    print(f"  expires: {claim['locks'][0]['expires_at']}")
    print(
        "  local scopes: "
        + ", ".join(
            f"{lock['scope']['kind']}:{lock['scope']['key']}"
            for lock in claim["locks"]
        )
    )


def _emit_claim(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "claim": payload}, sort_keys=True))
        return
    print(
        f"{payload['id']}  {payload['effective_state']}  "
        f"{payload['change_set_id']}  {payload['agent_id']}/{payload['session_id']}"
    )
    print(f"  expires: {payload['locks'][0]['expires_at']}")
    print(
        "  scopes: "
        + ", ".join(
            f"{lock['scope']['kind']}:{lock['scope']['key']}"
            for lock in payload["locks"]
        )
    )


def _emit_claim_list(payloads: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "claims": payloads}, sort_keys=True))
        return
    if not payloads:
        print("no claims")
        return
    for payload in payloads:
        print(
            f"{payload['id']}  {payload['effective_state']}  "
            f"{payload['change_set_id']}  {payload['agent_id']}"
        )


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": False, "error": message}, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)


def _emit_scope(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": payload["is_within_scope"], "scope_audit": payload}, sort_keys=True))
        return
    status = "within scope" if payload["is_within_scope"] else "scope drift"
    print(f"{payload['change_set_id']}  {status}")
    print("  actual paths: " + (", ".join(payload["actual_paths"]) or "(none)"))
    for finding in payload["findings"]:
        print(f"  BLOCKING {finding['scope']['kind']}:{finding['scope']['key']}  {finding['rationale']}")


def _emit_evidence(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": payload["state"] == "passed", "evidence": payload}, sort_keys=True))
        return
    print(f"{payload['id']}  {payload['kind']}  {payload['state']}")
    print(f"  change set: {payload['subject']['id']}")
    print(f"  commit: {payload['bound_commit_sha']}")
    print(f"  duration: {payload['duration_seconds']:.3f}s")
    if payload["detail"]:
        print(f"  detail: {payload['detail']}")


def _emit_evidence_list(payloads: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "evidence": payloads}, sort_keys=True))
        return
    if not payloads:
        print("no evidence")
        return
    for payload in payloads:
        print(f"{payload['id']}  {payload['kind']}  {payload['state']}  {payload['subject']['id']}")


def _emit_review(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": payload["is_releasable"], "review": payload}, sort_keys=True))
        return
    decision = payload["decision"]
    print(f"{decision['id']}  {decision['outcome']}  {decision['head_sha']}")
    for explanation in payload["explanations"]:
        print(f"  {explanation}")


def _emit_review_list(payloads: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "reviews": payloads}, sort_keys=True))
        return
    if not payloads:
        print("no reviews")
        return
    for payload in payloads:
        decision = payload["decision"]
        print(f"{decision['id']}  {decision['outcome']}  {decision['change_set_id']}")


def _emit_handoff(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "handoff": payload}, sort_keys=True))
        return
    print(f"{payload['id']}  generation {payload['generation']}  {payload['head_sha']}")
    print(f"  task: {payload['task_id']}")
    print(f"  next: {payload['next_action']}")
    if payload["intended_receiver_id"]:
        print(f"  receiver: {payload['intended_receiver_id']}")


def _emit_handoff_list(payloads: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "handoffs": payloads}, sort_keys=True))
        return
    if not payloads:
        print("no handoffs")
        return
    for payload in payloads:
        print(f"{payload['id']}  gen {payload['generation']}  {payload['change_set_id']}  {payload['next_action']}")


if __name__ == "__main__":
    raise SystemExit(main())
