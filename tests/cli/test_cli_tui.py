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
