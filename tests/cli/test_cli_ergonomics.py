"""Public CLI surface: banner, version, completions, JSON and environment."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from weftmark.cli.banner import help_banner, plain_logo, render_logo
from weftmark.cli.color import color_enabled, color_preference
from weftmark.cli.completion import bash_script, command_tree, fish_script
from weftmark.cli.main import build_parser, main
from weftmark.cli.output import editor_command, pager_command
from weftmark.cli.version import version_payload


def test_logo_resembles_a_woven_w_and_has_ascii_fallback() -> None:
    logo = plain_logo()
    assert "╲" in logo and "╱" in logo and "╳" in logo and "═" in logo
    assert "╲" not in plain_logo(ascii_only=True)
    assert "\\" in plain_logo(ascii_only=True)
    assert len(logo.splitlines()) == 10


def test_banner_uses_no_ansi_when_disabled() -> None:
    assert "\033" not in help_banner(enabled=False)
    assert "\033[" in render_logo(enabled=True)


def test_color_precedence() -> None:
    assert color_preference({"WEFTMARK_COLOR": "always"}) is True
    assert color_preference({"WEFTMARK_COLOR": "never"}) is False
    assert color_preference({}) is None
    assert color_enabled(env={"WEFTMARK_COLOR": "never", "FORCE_COLOR": "1"}) is False
    assert color_enabled(env={"WEFTMARK_COLOR": "always", "NO_COLOR": ""}) is False
    assert color_enabled(env={"CLICOLOR_FORCE": "1"}) is True
    assert color_enabled(env={"TERM": "dumb"}) is False


def test_version_needs_no_repository_and_lists_capabilities(capsys) -> None:
    assert main(["--repo", "/nonexistent/weftmark", "--version", "--no-pager"]) == 0
    out = capsys.readouterr().out
    assert "WeftMark 0.1.0" in out
    assert "completions" in out and "bash, fish" in out
    assert "forge" in out and "sqlite" in out and "tui" in out


def test_version_json_is_machine_readable_even_with_color_forced(monkeypatch, capsys) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert main(["--version", "--json", "--no-pager"]) == 0
    out = capsys.readouterr().out
    assert "\033" not in out
    payload = json.loads(out)
    assert payload["product"] == "weftmark"
    assert payload["capabilities"]["completions"] == ["bash", "fish"]
    assert version_payload()["capabilities"]["ledger_jsonl"] is True


def test_top_level_help_has_banner_but_subcommand_help_does_not(capsys) -> None:
    assert main(["--help", "--no-pager"]) == 0
    assert "╳" in capsys.readouterr().out
    assert main(["task", "--help", "--no-pager"]) == 0
    sub = capsys.readouterr().out
    assert "usage: weftmark task" in sub
    assert "╳" not in sub


def test_completions_are_derived_from_live_parser() -> None:
    parser = build_parser()
    tree = command_tree(parser)
    names = {child.path[-1] for child in tree.children}
    assert {"task", "evidence", "changeset"} <= names

    bash = bash_script(parser)
    assert "complete -F _weftmark_complete weftmark" in bash
    for command in ("task", "evidence", "changeset", "claim", "handoff"):
        assert command in bash
    assert "--json" in bash
    assert '--no-pager' in bash
    assert '"task create" )' in bash
    assert "task" in bash and "create" in bash

    fish = fish_script(parser)
    assert "complete -c weftmark -f" in fish
    assert "complete -c weftmark -n __fish_use_subcommand -a task" in fish
    assert "__fish_seen_subcommand_from task" in fish
    assert "-l json" in fish
    assert "-l no-pager" in fish


def test_generated_bash_completion_completes_option_values(tmp_path: Path) -> None:
    import shlex
    import shutil

    if shutil.which("bash") is None:
        pytest.skip("bash is not available")
    script = tmp_path / "completion.bash"
    script.write_text(bash_script(build_parser()))
    probe = (
        f"source {shlex.quote(str(script))}; "
        "COMP_WORDS=(weftmark task create --priority p); COMP_CWORD=4; "
        "_weftmark_complete; printf '%s' \"${COMPREPLY[*]}\""
    )
    result = subprocess.run(
        ("bash", "-c", probe), check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == "p0 p1 p2 p3"
    assert "grep: unrecognized option" not in result.stderr


def test_completion_output_is_plain_when_color_is_forced(monkeypatch, capsys) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    for shell in ("bash", "fish"):
        assert main(["completion", shell]) == 0
        assert "\033" not in capsys.readouterr().out


def test_pager_and_editor_environment_precedence() -> None:
    assert pager_command({"PAGER": "more -d"}) == ["more", "-d"]
    assert pager_command({"WEFTMARK_PAGER": "cat", "PAGER": "less"}) == ["cat"]
    assert pager_command({"WEFTMARK_PAGER": "", "PAGER": "less"}) is None
    assert pager_command({"PAGER": ""}) is None
    assert pager_command({}) == ["less", "-FRX"]
    # Broken argv falls back instead of being handed to a shell.
    assert pager_command({"PAGER": "less 'unterminated"}) == ["less", "-FRX"]

    assert editor_command({}) == ["vi"]
    assert editor_command({"EDITOR": "nano"}) == ["nano"]
    assert editor_command({"VISUAL": "code -w", "EDITOR": "nano"}) == ["code", "-w"]
    assert editor_command({"WEFTMARK_EDITOR": "hx", "VISUAL": "code"}) == ["hx"]


def test_task_create_editor_draft_and_actionable_missing_fields(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "-C", str(repo), "init", "--initial-branch=main"), check=True)
    subprocess.run(("git", "-C", str(repo), "config", "user.name", "Tests"), check=True)
    subprocess.run(
        ("git", "-C", str(repo), "config", "user.email", "tests@example.invalid"),
        check=True,
    )
    subprocess.run(("git", "-C", str(repo), "commit", "--allow-empty", "-m", "base"), check=True)

    editor = tmp_path / "editor.py"
    editor.write_text(
        "import pathlib, sys\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "p.write_text(\n"
        "    p.read_text()\n"
        "    .replace('WHY THIS TASK EXISTS', 'Because tests need it')\n"
        "    .replace('WHAT WILL BE DELIVERED', 'A verified CLI')\n"
        ")\n"
    )
    monkeypatch.setenv("WEFTMARK_EDITOR", f"{sys.executable} {editor}")

    assert main([
        "--repo", str(repo), "--json", "task", "create", "draft-task",
        "--title", "Drafted task", "--edit",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["why"] == "Because tests need it"
    assert payload["task"]["what"] == "A verified CLI"

    assert main([
        "--repo", str(repo), "--json", "task", "create", "empty-task",
        "--title", "Empty task",
    ]) == 2
    error = json.loads(capsys.readouterr().out)
    assert "--why" in error["error"] and "--what" in error["error"]
    assert "--edit" in error["error"]
