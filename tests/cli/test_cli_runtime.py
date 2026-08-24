from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from weftmark.cli.main import main


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repo), *args), check=True, capture_output=True, text=True
    ).stdout.strip()


def _command(repo: Path, *args: str) -> list[str]:
    return ["--repo", str(repo), "--json", *args]


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_runtime_cli_reconnects_and_refuses_unclaimed_work(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "WeftMark Tests")
    _git(repo, "config", "user.email", "weftmark@example.invalid")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    agent = tmp_path / "agent.py"
    agent.write_text(
        textwrap.dedent(
            """
            import json, sys
            def send(value):
                sys.stdout.write(json.dumps(value) + "\\n"); sys.stdout.flush()
            for line in sys.stdin:
                value = json.loads(line)
                if "id" not in value: continue
                method = value["method"]
                if method == "initialize": result = {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}
                elif method == "session/new": result = {"sessionId": "cli-session"}
                elif method == "session/prompt": result = {"stopReason": "end_turn"}
                else: result = {}
                send({"jsonrpc": "2.0", "id": value["id"], "result": result})
            """
        ),
        encoding="utf-8",
    )
    provider = "echo=" + json.dumps(
        [sys.executable, str(agent)], separators=(",", ":")
    )

    assert main(
        _command(
            repo,
            "task", "create", "owned",
            "--title", "Owned", "--why", "exercise runtime",
            "--what", "drive ACP", "--scope", "file:README.md",
        )
    ) == 0
    capsys.readouterr()
    assert main(
        _command(
            repo,
            "task", "claim", "owned",
            "--changeset-id", "owned-cs", "--claim-id", "owned-claim",
            "--lease-seconds", "300",
        )
    ) == 0
    capsys.readouterr()
    claimed_base = _git(repo, "rev-parse", "HEAD")
    (repo / "later.txt").write_text("after claim\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "advance after claim")
    assert main(
        _command(
            repo,
            "runtime", "start", "owned", "--provider", "echo",
            "--prompt", "work", "--runtime-provider", provider,
        )
    ) == 0
    started = _payload(capsys)["runtime_worker"]
    assert started["provider"] == "echo"
    assert started["authority"] == "operational_observation_only"
    runtime_worktree = repo.parent / ".weftmark-runtime-repo-owned-cs"
    assert _git(runtime_worktree, "rev-parse", "HEAD") == claimed_base

    repeated_pid = started["pid"]
    assert main(
        _command(
            repo,
            "runtime", "start", "owned", "--provider", "echo",
            "--prompt", "must not start twice", "--runtime-provider", provider,
        )
    ) == 0
    assert _payload(capsys)["runtime_worker"]["pid"] == repeated_pid

    changed_provider = "echo=" + json.dumps(
        [sys.executable, "-c", "pass"], separators=(",", ":")
    )
    assert main(
        _command(
            repo,
            "runtime",
            "status",
            "owned",
            "--runtime-provider",
            changed_provider,
        )
    ) == 2
    assert "configuration differs" in _payload(capsys)["error"]

    deadline = time.monotonic() + 2
    while True:
        assert main(
            _command(repo, "runtime", "status", "owned", "--runtime-provider", provider)
        ) == 0
        state = _payload(capsys)["runtime_worker"]["state"]
        if state == "awaiting_input" or time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    assert state == "awaiting_input"
    assert main(
        _command(
            repo, "runtime", "send-input", "owned", "--data", "continue",
            "--runtime-provider", provider,
        )
    ) == 0
    capsys.readouterr()
    assert main(
        _command(repo, "runtime", "stop", "owned", "--runtime-provider", provider)
    ) == 0
    assert _payload(capsys)["runtime_worker"]["state"] == "exited"

    assert main(
        _command(
            repo,
            "task", "create", "unclaimed", "--title", "No",
            "--why", "refusal", "--what", "do not run", "--scope", "file:NO",
        )
    ) == 0
    capsys.readouterr()
    assert main(
        _command(
            repo,
            "runtime", "start", "unclaimed", "--provider", "echo",
            "--prompt", "no", "--runtime-provider", provider,
        )
    ) == 2
    assert "completed native claim" in _payload(capsys)["error"]
