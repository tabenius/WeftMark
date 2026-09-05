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
        status = self._by_id.get(str(event.row_key.value))
        if status is None:
            self.notify("Selected Change Set is no longer available.", severity="warning")
            return
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
