from datetime import datetime, timedelta, timezone
import json
import asyncio

import pytest

from weftmark.adapters.runtime_status import load_runtime_status
from weftmark.cli.main import main

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


@pytest.mark.parametrize("seconds,state", [(0, "current"), (121, "stale"), (-1, "clock-skew")])
def test_snapshot_freshness_and_terminal_controls(tmp_path, seconds, state):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema": "ragbaz.runtime-status.v1", "components": {},
                                "observed_at": (NOW - timedelta(seconds=seconds)).isoformat(),
                                "summary": ["minotaur: running\u001b]8;;https://bad\u0007"]}))
    report = load_runtime_status(str(path), now=NOW)
    assert report["status"] == state
    assert "\x1b" not in "\n".join(report["summary"])
    assert "\x07" not in "\n".join(report["summary"])


def test_missing_snapshot_does_not_require_repository_or_ephor(monkeypatch, capsys):
    monkeypatch.delenv("RAGBAZ_RUNTIME_STATUS", raising=False)
    assert main(["--repo", "/no/repository", "--json", "system"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not-configured"


@pytest.mark.parametrize("content", ["{}", "[]", "not JSON", "x" * (1024 * 1024 + 1)])
def test_invalid_snapshot_never_looks_current(tmp_path, content):
    path = tmp_path / "snapshot"
    path.write_text(content)
    assert load_runtime_status(str(path))["status"] == "unavailable"


def test_runtime_screen_opens_and_returns_without_a_runtime(monkeypatch):
    from textual.app import App
    from textual.widgets import Static
    from weftmark.tui.screens import ChangeSetListScreen, RuntimeScreen
    monkeypatch.delenv("RAGBAZ_RUNTIME_STATUS", raising=False)
    class Harness(App):
        def on_mount(self):
            self.push_screen(ChangeSetListScreen(()))
    async def exercise():
        app = Harness()
        async with app.run_test() as pilot:
            await pilot.press("s")
            assert isinstance(app.screen, RuntimeScreen)
            assert "not configured" in str(app.screen.query_one("#runtime", Static).content)
            await pilot.press("r", "h")
            assert isinstance(app.screen, ChangeSetListScreen)
    asyncio.run(exercise())
