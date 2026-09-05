from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from textual.app import App
from textual.widgets import DataTable, Static

from weftmark.application.status import ChangeSetStatus, WorkspaceStatus
from weftmark.tui.data import TuiError
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


async def _list_screen_shows_a_message_when_there_are_no_change_sets():
    app = _HarnessApp(())
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 0
        message = app.screen.query_one("#empty-message", Static)
        assert message.display is True


def test_list_screen_shows_a_message_when_there_are_no_change_sets() -> None:
    run(_list_screen_shows_a_message_when_there_are_no_change_sets())


async def _list_screen_hides_the_empty_message_once_statuses_exist():
    app = _HarnessApp((make_status("cs-1"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        message = app.screen.query_one("#empty-message", Static)
        assert message.display is False


def test_list_screen_hides_the_empty_message_once_statuses_exist() -> None:
    run(_list_screen_hides_the_empty_message_once_statuses_exist())


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


class _RefreshHarnessApp(App):
    """A minimal App whose reload_status() is test-controlled, so
    ChangeSetListScreen.action_refresh's two branches (success and TuiError)
    can be exercised without a real workspace/ledger."""

    def __init__(self, initial: tuple[ChangeSetStatus, ...], *, on_refresh) -> None:
        super().__init__()
        self._initial = initial
        self._on_refresh = on_refresh

    def on_mount(self) -> None:
        self.push_screen(ChangeSetListScreen(self._initial))

    def reload_status(self) -> WorkspaceStatus:
        return self._on_refresh()


async def _refresh_action_updates_the_table_on_success():
    def on_refresh() -> WorkspaceStatus:
        return WorkspaceStatus(
            generated_at=NOW,
            change_sets=(make_status("cs-1"), make_status("cs-2")),
            active_claim_count=0,
            expired_claim_count=0,
            released_claim_count=0,
        )

    app = _RefreshHarnessApp((make_status("cs-1"),), on_refresh=on_refresh)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1

        await pilot.press("r")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2


def test_refresh_action_updates_the_table_on_success() -> None:
    run(_refresh_action_updates_the_table_on_success())


async def _refresh_action_does_not_crash_on_tui_error():
    def on_refresh() -> WorkspaceStatus:
        raise TuiError("ledger went missing")

    app = _RefreshHarnessApp((make_status("cs-1"),), on_refresh=on_refresh)
    async with app.run_test() as pilot:
        await pilot.pause()
        list_screen = app.screen
        assert isinstance(list_screen, ChangeSetListScreen)

        await pilot.press("r")
        await pilot.pause()

        assert app.screen is list_screen
        assert isinstance(app.screen, ChangeSetListScreen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1


def test_refresh_action_does_not_crash_on_tui_error() -> None:
    run(_refresh_action_does_not_crash_on_tui_error())
