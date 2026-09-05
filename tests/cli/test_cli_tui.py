from __future__ import annotations

import json
from pathlib import Path

from weftmark.cli.main import main


def test_tui_command_reports_clear_error_for_invalid_repo(
    tmp_path: Path, capsys
) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    exit_code = main(["--repo", str(not_a_repo), "tui"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not a git repository" in err


def test_tui_command_reports_json_error_for_invalid_repo_with_json_flag(
    tmp_path: Path, capsys
) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    exit_code = main(["--repo", str(not_a_repo), "--json", "tui"])

    assert exit_code == 2
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "not a git repository" in payload["error"]
