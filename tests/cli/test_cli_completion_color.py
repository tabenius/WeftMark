from __future__ import annotations

import io

from weftmark.cli.color import color_enabled, paint, status_styles
from weftmark.cli.main import main


def test_completion_bash_emits_a_sourceable_script(capsys):
    assert main(["completion", "bash"]) == 0
    out = capsys.readouterr().out
    assert "complete -F _weftmark_complete weftmark" in out
    # Derived from the live parser, so real subcommands appear.
    assert "evidence" in out and "changeset" in out and "claim" in out


def test_completion_fish_emits_per_subcommand_lines(capsys):
    assert main(["completion", "fish"]) == 0
    out = capsys.readouterr().out
    assert "complete -c weftmark -f" in out
    assert "complete -c weftmark -n __fish_use_subcommand -a evidence" in out


def test_completion_needs_no_repo(capsys):
    # No --repo given; completion must not touch Git/ledger.
    assert main(["completion", "bash"]) == 0
    assert "complete" in capsys.readouterr().out


def test_color_disabled_for_non_tty_stream():
    assert color_enabled(io.StringIO()) is False


def test_no_color_env_wins_over_force_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert color_enabled(io.StringIO()) is False


def test_force_color_enables_without_a_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert color_enabled(io.StringIO()) is True


def test_paint_wraps_only_when_enabled():
    assert paint("passed", "green", enabled=True) == "\033[32mpassed\033[0m"
    assert paint("passed", "green", enabled=False) == "passed"
    assert paint("passed", enabled=True) == "passed"


def test_status_styles_cover_evidence_and_review_words():
    assert status_styles("passed") == ("green",)
    assert status_styles("failed") == ("red",)
    assert status_styles("ready") == ("green",)
    assert status_styles("changes_requested") == ("red",)
    assert status_styles("mystery") == ()
