# Terminal Review UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only, navigable terminal reviewer (`weftmark tui`) that
shows Change Set readiness, evidence, and blockers, reusing the existing
`StatusService` read model with zero new backend logic.

**Architecture:** A new `src/weftmark/tui/` package with four small,
independently-testable modules — `data.py` (loads `WorkspaceStatus` the same
way the CLI `status` command does), `formatting.py` (pure functions turning a
`ChangeSetStatus` into sorted rows / detail text, no Textual dependency),
`screens.py` (the two Textual `Screen`s: list and detail), and `app.py` (the
`App` subclass plus the `run_tui`/`main` entry points). `weftmark/cli/main.py`
gets one new `tui` subcommand that locally imports `weftmark.tui.app` so the
base CLI never imports Textual.

**Tech Stack:** Python 3.11+, Textual 8.2.8 (pinned `>=8,<9`) as an optional
`tui` extra, pytest (existing headless-async pattern: `asyncio.run(coro)`
wrapped in a sync test, no `pytest-asyncio` dependency).

**Spec:** `docs/superpowers/specs/2026-09-04-terminal-review-ui-design.md`

## Global Constraints

- Textual is an optional extra (`weftmark[tui]`), never a base dependency —
  `pyproject.toml`'s `dependencies` stays `[]`.
- Pin `textual>=8,<9` (verified current latest is 8.2.8; matches this repo's
  existing pin style, e.g. `pytest>=8,<9`).
- No new domain or application code. Every module in `tui/` only reads from
  `weftmark.application.status` (`StatusService`, `WorkspaceStatus`,
  `ChangeSetStatus`, `ScopeCollision`) and the same service-construction
  sequence `cli/main.py`'s `status` command already uses.
- Strictly read-only: no write/mutating action anywhere in the TUI.
- No auto-refresh / timer / file-watching.
- Importing the base `weftmark` CLI (`weftmark.cli.main`) must never import
  Textual — the `tui` subcommand's `from weftmark.tui.app import run_tui`
  import is local to its dispatch branch, guarded by `try/except ImportError`.
- Follow this repo's existing test doubles pattern: build a real tmp git repo
  via a local `git(path, *args)` subprocess helper (see
  `tests/application/test_task_claims.py`, `tests/cli/test_cli_status.py`),
  not a hand-rolled fake `GitPort`.

---

### Task 1: `tui/data.py` — load the read model

**Files:**
- Create: `src/weftmark/tui/__init__.py`
- Create: `src/weftmark/tui/data.py`
- Test: `tests/tui/test_data.py`

**Interfaces:**
- Produces: `TuiError(Exception)`; `load_workspace_status(repo: str, ledger_override: str | None, *, observed_at: datetime | None = None) -> WorkspaceStatus`.

- [ ] **Step 1: Create the package and write the failing tests**

```python
# src/weftmark/tui/__init__.py
"""Read-only terminal reviewer for Change Set/evidence/blocker state."""
```

```python
# tests/tui/test_data.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from weftmark.cli.main import main as cli_main
from weftmark.tui.data import TuiError, load_workspace_status


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    return tmp_path


def test_load_workspace_status_reads_change_sets(tmp_path: Path) -> None:
    repo = setup(tmp_path)
    assert (
        cli_main(
            [
                "--repo",
                str(repo),
                "changeset",
                "create",
                "chg-1",
                "--goal",
                "Ship it",
                "--scope",
                "file:**",
            ]
        )
        == 0
    )

    status = load_workspace_status(str(repo), None)

    assert len(status.change_sets) == 1
    assert status.change_sets[0].id == "chg-1"
    assert status.change_sets[0].goal == "Ship it"
    assert status.change_sets[0].lifecycle_state == "active"


def test_load_workspace_status_wraps_git_errors(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    with pytest.raises(TuiError, match="not a git repository"):
        load_workspace_status(str(not_a_repo), None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tui/test_data.py -v`
Expected: both FAIL with `ModuleNotFoundError: No module named 'weftmark.tui'` (or `weftmark.tui.data`) — the module doesn't exist yet.

- [ ] **Step 3: Write the implementation**

```python
# src/weftmark/tui/data.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tui/test_data.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/weftmark/tui/__init__.py src/weftmark/tui/data.py tests/tui/test_data.py
git commit -m "feat: load the terminal reviewer's read model from StatusService"
```

---

### Task 2: `tui/formatting.py` — pure presentation helpers

**Files:**
- Create: `src/weftmark/tui/formatting.py`
- Test: `tests/tui/test_formatting.py`

**Interfaces:**
- Consumes: `weftmark.application.status.ChangeSetStatus`, `ScopeCollision` (Task 1's dependency, already exists in the codebase).
- Produces: `attention_rank(status: ChangeSetStatus) -> int`; `sort_statuses(statuses: tuple[ChangeSetStatus, ...]) -> tuple[ChangeSetStatus, ...]`; `evidence_summary(status: ChangeSetStatus) -> str`; `blockers_text(status: ChangeSetStatus) -> tuple[str, ...]`; `detail_text(status: ChangeSetStatus) -> str`. Task 4 (`screens.py`) calls all five.

- [ ] **Step 1: Write the failing tests**

```python
# tests/tui/test_formatting.py
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from weftmark.application.status import ChangeSetStatus, ScopeCollision
from weftmark.domain.scope import Scope
from weftmark.tui.formatting import (
    attention_rank,
    blockers_text,
    detail_text,
    evidence_summary,
    sort_statuses,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def make_status(
    id: str,
    *,
    reviewed: bool = False,
    scope_collisions: tuple[ScopeCollision, ...] = (),
) -> ChangeSetStatus:
    return ChangeSetStatus(
        id=id,
        goal=f"Goal for {id}",
        lifecycle_state="active",
        branch="main",
        observed_head_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        observed_at=NOW,
        dirty_paths=(),
        active_claim_ids=(),
        evidence_count=2,
        current_evidence_count=2,
        obsolete_evidence_count=0,
        failed_evidence_count=0,
        unavailable_evidence_count=0,
        latest_review_id="rev-1" if reviewed else None,
        latest_review_outcome="ready" if reviewed else None,
        latest_review_head_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        if reviewed
        else None,
        latest_review_is_current=reviewed,
        latest_handoff_id=None,
        latest_handoff_head_sha=None,
        latest_handoff_is_current=False,
        scope_collisions=scope_collisions,
    )


def make_collision(claim_id: str = "other-claim") -> ScopeCollision:
    return ScopeCollision(
        claim_id=claim_id,
        competing_change_set_id="other-cs",
        requested_scope=Scope.file("a.py"),
        owned_scope=Scope.contract("api-v1"),
    )


def test_attention_rank_prioritizes_blockers_over_unready_over_ready() -> None:
    blocked = make_status("blocked-cs", reviewed=True, scope_collisions=(make_collision(),))
    unready = make_status("unready-cs")
    ready = make_status("ready-cs", reviewed=True)

    assert attention_rank(blocked) == 0
    assert attention_rank(unready) == 1
    assert attention_rank(ready) == 2


def test_sort_statuses_orders_blockers_then_unready_then_ready_by_id() -> None:
    ready_b = make_status("ready-b", reviewed=True)
    ready_a = make_status("ready-a", reviewed=True)
    unready = make_status("unready")
    blocked = make_status("blocked", reviewed=True, scope_collisions=(make_collision(),))

    ordered = sort_statuses((ready_b, ready_a, unready, blocked))

    assert [item.id for item in ordered] == ["blocked", "unready", "ready-a", "ready-b"]


def test_evidence_summary_reports_current_over_total_and_failures() -> None:
    healthy = make_status("cs-1")
    assert evidence_summary(healthy) == "2/2"

    failing = replace(healthy, failed_evidence_count=1)
    assert evidence_summary(failing) == "2/2 (1 failed)"


def test_blockers_text_describes_each_collision() -> None:
    blocked = make_status(
        "blocked-cs", reviewed=True, scope_collisions=(make_collision(),)
    )

    assert blockers_text(blocked) == (
        "blocked by claim other-claim (other-cs) on contract:api-v1",
    )


def test_detail_text_includes_goal_state_evidence_and_blockers() -> None:
    blocked = make_status(
        "blocked-cs", reviewed=True, scope_collisions=(make_collision(),)
    )

    text = detail_text(blocked)

    assert "blocked-cs" in text
    assert "Goal for blocked-cs" in text
    assert "evidence: 2/2" in text
    assert "blocked by claim other-claim" in text


def test_detail_text_reports_no_review_and_no_handoff_when_absent() -> None:
    unready = make_status("unready-cs")

    text = detail_text(unready)

    assert "review: none" in text
    assert "handoff: none" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tui/test_formatting.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'weftmark.tui.formatting'`

- [ ] **Step 3: Write the implementation**

```python
# src/weftmark/tui/formatting.py
"""Pure functions turning ChangeSetStatus into terminal-reviewer text. No
Textual dependency — kept testable and reusable independent of rendering."""

from __future__ import annotations

from weftmark.application.status import ChangeSetStatus


def attention_rank(status: ChangeSetStatus) -> int:
    """Lower sorts first: blocked, then not-ready, then ready."""

    if status.scope_collisions:
        return 0
    if status.readiness != "ready":
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
    blockers = blockers_text(status)
    if blockers:
        lines.append("")
        lines.append("blockers:")
        lines.extend(f"  {line}" for line in blockers)
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tui/test_formatting.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/weftmark/tui/formatting.py tests/tui/test_formatting.py
git commit -m "feat: add pure formatting helpers for the terminal reviewer"
```

---

### Task 3: Package scaffolding — `tui` extra and console script

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `textual` installed via the `tui` and `test` extras; console script name `weftmark-tui` registered (target module doesn't exist until Task 5 — this step only reserves the entry in `pyproject.toml`).

- [ ] **Step 1: Update `pyproject.toml`**

Change:
```toml
[project.optional-dependencies]
mcp = [
  "mcp>=2,<3",
]
test = [
  "pytest>=8,<9",
  "mcp>=2,<3",
]
```
to:
```toml
[project.optional-dependencies]
mcp = [
  "mcp>=2,<3",
]
tui = [
  "textual>=8,<9",
]
test = [
  "pytest>=8,<9",
  "mcp>=2,<3",
  "textual>=8,<9",
]
```

Change:
```toml
[project.scripts]
weftmark = "weftmark.cli.main:main"
weftmark-mcp = "weftmark.mcp.server:main"
```
to:
```toml
[project.scripts]
weftmark = "weftmark.cli.main:main"
weftmark-mcp = "weftmark.mcp.server:main"
weftmark-tui = "weftmark.tui.app:main"
```

- [ ] **Step 2: Install the extras and verify the package imports**

Run: `pip install -e '.[test,tui]'`
Expected: installs `textual` alongside the existing `test` extras, no errors.

Run: `python -c "import weftmark.tui; import textual; print(textual.__version__)"`
Expected: prints `8.2.8` (or the newest `8.x` resolved at install time) with no traceback. (`weftmark.tui` already exists from Task 1.)

- [ ] **Step 3: Run the full test suite to confirm nothing broke**

Run: `python -m pytest -q`
Expected: all existing tests still pass (the console script target doesn't exist yet, but nothing imports it until Task 5, so this doesn't fail).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add the tui optional extra (textual) and weftmark-tui script"
```

---

### Task 4: `tui/screens.py` — list and detail screens

**Files:**
- Create: `src/weftmark/tui/screens.py`
- Test: `tests/tui/test_screens.py`

**Interfaces:**
- Consumes: `weftmark.application.status.ChangeSetStatus`; `weftmark.tui.formatting.{sort_statuses, evidence_summary, detail_text}` (Task 2).
- Produces: `ChangeSetListScreen(Screen)` — constructor `__init__(self, statuses: tuple[ChangeSetStatus, ...])`, public method `set_statuses(self, statuses: tuple[ChangeSetStatus, ...]) -> None`; `ChangeSetDetailScreen(Screen)` — constructor `__init__(self, status: ChangeSetStatus)`. Task 5 (`app.py`) pushes `ChangeSetListScreen` on mount and calls its `set_statuses` after a refresh.

- [ ] **Step 1: Write the failing test**

```python
# tests/tui/test_screens.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from textual.app import App
from textual.widgets import DataTable, Static

from weftmark.application.status import ChangeSetStatus
from weftmark.tui.screens import ChangeSetDetailScreen, ChangeSetListScreen

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def make_status(id: str) -> ChangeSetStatus:
    return ChangeSetStatus(
        id=id,
        goal=f"Goal for {id}",
        lifecycle_state="active",
        branch="main",
        observed_head_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        observed_at=NOW,
        dirty_paths=(),
        active_claim_ids=(),
        evidence_count=1,
        current_evidence_count=1,
        obsolete_evidence_count=0,
        failed_evidence_count=0,
        unavailable_evidence_count=0,
        latest_review_id=None,
        latest_review_outcome=None,
        latest_review_head_sha=None,
        latest_review_is_current=False,
        latest_handoff_id=None,
        latest_handoff_head_sha=None,
        latest_handoff_is_current=False,
        scope_collisions=(),
    )


class _HarnessApp(App):
    def __init__(self, statuses: tuple[ChangeSetStatus, ...]) -> None:
        super().__init__()
        self._statuses = statuses

    def on_mount(self) -> None:
        self.push_screen(ChangeSetListScreen(self._statuses))


def run(coro):
    return asyncio.run(coro)


async def _list_screen_renders_one_row_per_status():
    app = _HarnessApp((make_status("cs-1"), make_status("cs-2")))
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2


def test_list_screen_renders_one_row_per_status() -> None:
    run(_list_screen_renders_one_row_per_status())


async def _selecting_a_row_opens_its_detail_and_h_goes_back():
    app = _HarnessApp((make_status("cs-1"), make_status("cs-2")))
    async with app.run_test() as pilot:
        await pilot.pause()
        list_screen = app.screen

        await pilot.press("enter")
        await pilot.pause()
        detail = app.screen
        assert isinstance(detail, ChangeSetDetailScreen)
        static = detail.query_one("#detail", Static)
        assert "cs-1" in static.content

        await pilot.press("h")
        await pilot.pause()
        assert app.screen is list_screen


def test_selecting_a_row_opens_its_detail_and_h_goes_back() -> None:
    run(_selecting_a_row_opens_its_detail_and_h_goes_back())


async def _refreshed_statuses_replace_the_table_rows():
    app = _HarnessApp((make_status("cs-1"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        list_screen = app.screen
        assert isinstance(list_screen, ChangeSetListScreen)

        list_screen.set_statuses((make_status("cs-1"), make_status("cs-2")))
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2


def test_refreshed_statuses_replace_the_table_rows() -> None:
    run(_refreshed_statuses_replace_the_table_rows())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/tui/test_screens.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'weftmark.tui.screens'`

- [ ] **Step 3: Write the implementation**

```python
# src/weftmark/tui/screens.py
"""The two read-only screens: a Change Set list and its detail view."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from weftmark.application.status import ChangeSetStatus
from weftmark.tui.data import TuiError
from weftmark.tui.formatting import detail_text, evidence_summary, sort_statuses


class ChangeSetListScreen(Screen):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "select", "Open", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, statuses: tuple[ChangeSetStatus, ...]) -> None:
        super().__init__()
        self._initial_statuses = statuses
        self._by_id: dict[str, ChangeSetStatus] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="change-sets")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("Change Set", "State", "Readiness", "Evidence", "Claim", "Blockers")
        self.set_statuses(self._initial_statuses)

    def set_statuses(self, statuses: tuple[ChangeSetStatus, ...]) -> None:
        ordered = sort_statuses(statuses)
        self._by_id = {status.id: status for status in ordered}
        table = self.query_one(DataTable)
        table.clear()
        for status in ordered:
            table.add_row(
                status.id,
                status.lifecycle_state,
                status.readiness,
                evidence_summary(status),
                ", ".join(status.active_claim_ids) or "unclaimed",
                str(len(status.scope_collisions)),
                key=status.id,
            )

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_select(self) -> None:
        self.query_one(DataTable).action_select_cursor()

    def action_refresh(self) -> None:
        try:
            status = self.app.reload_status()  # type: ignore[attr-defined]
        except TuiError as error:
            self.notify(str(error), severity="error")
            return
        self.set_statuses(status.change_sets)

    def action_quit(self) -> None:
        self.app.exit()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        status = self._by_id[str(event.row_key.value)]
        self.app.push_screen(ChangeSetDetailScreen(status))


class ChangeSetDetailScreen(Screen):
    BINDINGS = [
        Binding("h", "back", "Back", show=True),
        Binding("escape", "back", "Back", show=False),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, status: ChangeSetStatus) -> None:
        super().__init__()
        self._status = status

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static(detail_text(self._status), id="detail"))
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()
```

Note on the `# type: ignore[attr-defined]` in `action_refresh`: `Screen.app`
is typed generically (`App[object]`), so a type checker can't see
`ReviewApp.reload_status()` through it without a cast — the ignore comment is
the standard Textual pattern for this, not a placeholder. `reload_status`
itself doesn't exist until Task 5, but `screens.py` only needs to know its
call signature and that it raises `TuiError`, both already fixed by Task 1's
`data.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/tui/test_screens.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/weftmark/tui/screens.py tests/tui/test_screens.py
git commit -m "feat: add the Change Set list and detail screens"
```

---

### Task 5: `tui/app.py` — the App, `run_tui`, and `main`

**Files:**
- Create: `src/weftmark/tui/app.py`
- Create: `src/weftmark/tui/__main__.py`
- Test: `tests/tui/test_app.py`

**Interfaces:**
- Consumes: `weftmark.tui.data.{TuiError, load_workspace_status}` (Task 1); `weftmark.tui.screens.ChangeSetListScreen` (Task 4); `weftmark.application.status.WorkspaceStatus`.
- Produces: `ReviewApp(App)` — constructor `__init__(self, *, repo: str, ledger_override: str | None, initial_status: WorkspaceStatus)`, method `reload_status(self) -> WorkspaceStatus`; `run_tui(repo: str, ledger_override: str | None) -> int`; `main(argv: list[str] | None = None) -> int`. Task 6 (CLI integration) calls `run_tui`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/tui/test_app.py
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from weftmark.cli.main import main as cli_main
from weftmark.tui.app import ReviewApp, run_tui
from weftmark.tui.data import load_workspace_status
from weftmark.tui.screens import ChangeSetListScreen


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    assert (
        cli_main(
            [
                "--repo",
                str(tmp_path),
                "changeset",
                "create",
                "chg-1",
                "--goal",
                "Ship it",
                "--scope",
                "file:**",
            ]
        )
        == 0
    )
    return tmp_path


def run(coro):
    return asyncio.run(coro)


async def _app_mounts_list_screen_with_loaded_status(repo: Path):
    initial = load_workspace_status(str(repo), None)
    app = ReviewApp(repo=str(repo), ledger_override=None, initial_status=initial)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ChangeSetListScreen)


def test_app_mounts_list_screen_with_loaded_status(tmp_path: Path) -> None:
    repo = setup(tmp_path)
    run(_app_mounts_list_screen_with_loaded_status(repo))


async def _reload_status_returns_fresh_workspace_status(repo: Path):
    initial = load_workspace_status(str(repo), None)
    app = ReviewApp(repo=str(repo), ledger_override=None, initial_status=initial)
    async with app.run_test():
        assert (
            cli_main(
                [
                    "--repo",
                    str(repo),
                    "changeset",
                    "create",
                    "chg-2",
                    "--goal",
                    "Second",
                    "--scope",
                    "file:other/**",
                ]
            )
            == 0
        )
        refreshed = app.reload_status()
        assert len(refreshed.change_sets) == 2


def test_reload_status_returns_fresh_workspace_status(tmp_path: Path) -> None:
    repo = setup(tmp_path)
    run(_reload_status_returns_fresh_workspace_status(repo))


def test_run_tui_reports_clear_error_for_invalid_repo(tmp_path, capsys) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    exit_code = run_tui(str(not_a_repo), None)

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "not a git repository" in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tui/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'weftmark.tui.app'`

- [ ] **Step 3: Write the implementation**

```python
# src/weftmark/tui/app.py
"""The terminal reviewer's Textual App and CLI/console-script entry points."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from textual.app import App

from weftmark.application.status import WorkspaceStatus
from weftmark.tui.data import TuiError, load_workspace_status
from weftmark.tui.screens import ChangeSetListScreen


class ReviewApp(App):
    TITLE = "WeftMark Review"

    def __init__(
        self,
        *,
        repo: str,
        ledger_override: str | None,
        initial_status: WorkspaceStatus,
    ) -> None:
        super().__init__()
        self._repo = repo
        self._ledger_override = ledger_override
        self._workspace_status = initial_status

    def on_mount(self) -> None:
        self.push_screen(ChangeSetListScreen(self._workspace_status.change_sets))

    def reload_status(self) -> WorkspaceStatus:
        status = load_workspace_status(self._repo, self._ledger_override)
        self._workspace_status = status
        return status


def run_tui(repo: str, ledger_override: str | None) -> int:
    try:
        initial_status = load_workspace_status(repo, ledger_override)
    except TuiError as error:
        print(f"weftmark tui: {error}", file=sys.stderr)
        return 1
    app = ReviewApp(
        repo=repo, ledger_override=ledger_override, initial_status=initial_status
    )
    app.run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weftmark-tui")
    parser.add_argument("--repo", default=".", help="path inside the Git repository")
    parser.add_argument("--ledger", help="override the local JSONL ledger path")
    args = parser.parse_args(argv)
    return run_tui(args.repo, args.ledger)


if __name__ == "__main__":
    sys.exit(main())
```

```python
# src/weftmark/tui/__main__.py
"""Allow `python -m weftmark.tui` as an alternative to the weftmark-tui script."""

from __future__ import annotations

import sys

from weftmark.tui.app import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tui/test_app.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/weftmark/tui/app.py src/weftmark/tui/__main__.py tests/tui/test_app.py
git commit -m "feat: add the terminal reviewer App, run_tui, and CLI entry point"
```

---

### Task 6: `weftmark tui` CLI subcommand

**Files:**
- Modify: `src/weftmark/cli/main.py:172` (subparser registration, next to the existing `status` parser)
- Modify: `src/weftmark/cli/main.py:596-598` (dispatch, right after the existing `bundle verify` short-circuit and before the generic `git = LocalGit(args.repo)` service-construction block)
- Test: `tests/cli/test_cli_tui.py`

**Interfaces:**
- Consumes: `weftmark.tui.app.run_tui` (Task 5), imported locally inside the dispatch branch only.

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_cli_tui.py
from __future__ import annotations

from pathlib import Path

from weftmark.cli.main import main


def test_tui_command_reports_clear_error_for_invalid_repo(
    tmp_path: Path, capsys
) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    exit_code = main(["--repo", str(not_a_repo), "tui"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "not a git repository" in err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cli/test_cli_tui.py -v`
Expected: FAIL with `argparse` error (`invalid choice: 'tui'`) since the `tui` subcommand doesn't exist yet.

- [ ] **Step 3: Wire the subcommand**

In `src/weftmark/cli/main.py`, immediately after:
```python
    commands.add_parser("status", help="summarize current local workspace records")
```
add:
```python
    commands.add_parser("tui", help="open the interactive terminal reviewer")
```

Then, in `main()`, immediately after:
```python
        if args.command == "bundle" and args.bundle_command == "verify":
            verification = verification_to_payload(verify_bundle(read_bundle(args.path)))
            _emit_bundle_verification(verification, json_output=args.json)
            return 0
```
add:
```python
        if args.command == "tui":
            try:
                from weftmark.tui.app import run_tui
            except ImportError:
                print(
                    "weftmark tui: the terminal reviewer needs the 'tui' extra: "
                    "pip install weftmark[tui]",
                    file=sys.stderr,
                )
                return 1
            return run_tui(args.repo, args.ledger)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/cli/test_cli_tui.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests pass, no regressions in existing CLI tests (the new `tui`
subparser is additive; the dispatch branch only triggers on `args.command ==
"tui"`).

- [ ] **Step 6: Commit**

```bash
git add src/weftmark/cli/main.py tests/cli/test_cli_tui.py
git commit -m "feat: wire the weftmark tui CLI subcommand"
```

---

### Task 7: Benchmark evidence + close out the source-plan task

**Files:**
- Create: `tests/tui/test_benchmark.py`
- Modify: `tasks/50-surfaces.weft.yml` (`terminal-review-ui` status: `todo` → `review`, in the same commit as the benchmark test, per this repo's task-plan convention)

**Interfaces:**
- Consumes: `weftmark.tui.data.load_workspace_status` (Task 1), `weftmark.tui.app.ReviewApp` (Task 5), `weftmark.cli.main.main` (for fixture setup).

This is `terminal-review-ui`'s required evidence entry:
```yaml
    evidence:
      - kind: benchmark
        required: true
        criterion: Warm startup remains interactive on a workspace with at least 50 repositories.
```
As decided in the design doc (decision 4), "50 repositories" is modeled as 50
Change Set ledger entries sharing one ledger — `StatusService.summarize()`
does not filter, group, or branch on `repository_id` anywhere in
`src/weftmark/application/status.py`, so a single real repo with 50 Change
Sets exercises the exact same O(ledger size) code path as 50 real
repositories sharing one ledger would, without the cost/fragility of
constructing 50 real Git worktrees in a test.

- [ ] **Step 1: Write the failing test**

```python
# tests/tui/test_benchmark.py
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from weftmark.cli.main import main as cli_main
from weftmark.tui.app import ReviewApp
from weftmark.tui.data import load_workspace_status

CHANGE_SET_COUNT = 50
BUDGET_SECONDS = 1.0


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def build_workspace_with_many_change_sets(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    for index in range(CHANGE_SET_COUNT):
        assert (
            cli_main(
                [
                    "--repo",
                    str(tmp_path),
                    "changeset",
                    "create",
                    f"bench-cs-{index}",
                    "--goal",
                    f"Simulated repository {index}",
                    "--scope",
                    f"contract:bench-{index}",
                ]
            )
            == 0
        )
    return tmp_path


def run(coro):
    return asyncio.run(coro)


async def _mount_after_loading(repo: Path) -> float:
    started = time.monotonic()
    initial = load_workspace_status(str(repo), None)
    app = ReviewApp(repo=str(repo), ledger_override=None, initial_status=initial)
    async with app.run_test() as pilot:
        await pilot.pause()
    return time.monotonic() - started


def test_startup_stays_interactive_with_fifty_change_sets(tmp_path: Path) -> None:
    repo = build_workspace_with_many_change_sets(tmp_path)

    elapsed = run(_mount_after_loading(repo))

    assert elapsed < BUDGET_SECONDS, (
        f"startup (load + first render) took {elapsed:.3f}s, "
        f"budget is {BUDGET_SECONDS}s"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/tui/test_benchmark.py -v`
Expected: FAILs only if Task 1/5 aren't already implemented (they are, by
this point in the plan) — if run in order, this test should already PASS on
first run since it only exercises existing code with a larger fixture. If it
fails, it must fail on the timing assertion (`elapsed < BUDGET_SECONDS`), not
an import/attribute error — if it's an import error, an earlier task's
module is missing or misnamed.

- [ ] **Step 3: Run test to verify it passes**

Run: `python -m pytest tests/tui/test_benchmark.py -v`
Expected: PASS (1 passed). If it fails on timing, profile
`StatusService.summarize()` before touching this test — the fix belongs in
`load_workspace_status` or `StatusService`, never in loosening the budget
without first understanding why it regressed.

- [ ] **Step 4: Flip the source-plan task status**

In `tasks/50-surfaces.weft.yml`, change the `terminal-review-ui` entry's
```yaml
    status: todo
```
to:
```yaml
    status: review
```

- [ ] **Step 5: Validate the task graph**

Run: `python scripts/validate_tasks.py`
Expected: validates cleanly (no schema/dependency errors).

- [ ] **Step 6: Run the full suite one last time**

Run: `python -m pytest -q`
Expected: all tests pass, including every test added in Tasks 1-7.

- [ ] **Step 7: Commit**

```bash
git add tests/tui/test_benchmark.py tasks/50-surfaces.weft.yml
git commit -m "test: add terminal reviewer startup benchmark, mark terminal-review-ui review"
```

---

## After this plan

This plan produces working, merge-ready code but does **not** cover the
native WeftMark/Frog claim-and-evidence bookkeeping this repo's `AGENTS.md`
requires around it (claiming the task on both systems, running
`weftmark evidence run`, `weftmark review create`, `weftmark handoff create`,
pushing the branch, opening a PR). That's session-level process, not an
implementation task, and should follow the same sequence already used for
`task-claim-scope-amendment-recovery` (see git history on `main`).
