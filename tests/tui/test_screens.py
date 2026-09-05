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
