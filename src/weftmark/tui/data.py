"""Load the same read-only workspace status the CLI `status` command uses."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from weftmark.adapters.git_local import LocalGit, LocalGitError
from weftmark.adapters.jsonl_ledger import JsonlLedger, JsonlLedgerError
from weftmark.application.change_binding import ChangeBindingError
from weftmark.application.claims import ClaimService
from weftmark.application.ledger import LedgerService, LedgerServiceError
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.status import StatusService, WorkspaceStatus
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceProducer, ProducerKind


class TuiError(Exception):
    """Raised when the terminal reviewer cannot load workspace state."""


def _ledger_path(override: str | None, repository_id: str) -> Path:
    if override:
        return Path(override).resolve()
    if not repository_id.startswith("git:"):
        raise ChangeBindingError("local repository identity cannot select a ledger")
    return Path(repository_id.removeprefix("git:")) / "weftmark" / "ledger.jsonl"


def load_workspace_status(
    repo: str,
    ledger_override: str | None,
    *,
    observed_at: datetime | None = None,
) -> WorkspaceStatus:
    try:
        git = LocalGit(repo)
        repository = git.repository()
        ledger_path = _ledger_path(ledger_override, repository.id)
        ledger = LedgerService(JsonlLedger(ledger_path))
        workspace = WorkspaceService(git, ledger)
        claims = ClaimService(workspace, ledger)
        workflow = LocalWorkflowService(
            workspace,
            ledger,
            EvidenceProducer(ProducerKind.WORKER, "weftmark-tui"),
        )
        status_service = StatusService(workspace, claims, workflow)
        return status_service.summarize(
            observed_at=observed_at or datetime.now(timezone.utc)
        )
    except (
        LocalGitError,
        JsonlLedgerError,
        LedgerServiceError,
        ChangeBindingError,
    ) as error:
        raise TuiError(str(error)) from error
