from __future__ import annotations

import json
import subprocess
from pathlib import Path

from weftmark.cli.main import main


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.name", "WeftMark Tests")
    _git(path, "config", "user.email", "weftmark@example.invalid")
    _git(path, "commit", "--allow-empty", "-m", "base")
    return path


def _task(slug: str, *, status: str, depends: tuple[str, ...] = ()) -> str:
    dependencies = "".join(f"\n      - {value}" for value in depends) or " []"
    return f"""  - slug: {slug}
    title: {slug} title
    status: {status}
    priority: P1
    depends:{dependencies}
    purpose: Preserve {slug} intent.
    scope:
      files:
        - src/{slug}.py
      contracts:
        - contract:{slug}-v0
    deliverables:
      - Deliver {slug}.
    accept:
      - {slug} is inspectable.
    negative:
      - Runtime authority is not inferred.
    evidence:
      - kind: test
        command: python -m pytest
"""


def _write_plan(repo: Path, *, active_title: str = "active title") -> None:
    tasks = repo / "tasks"
    tasks.mkdir(exist_ok=True)
    value = (
        "format: weft-task-v0\nphase: test\nsummary: Test plan.\ntasks:\n"
        + _task("done-base", status="done")
        + _task("active", status="in_progress", depends=("done-base",))
    ).replace("active title", active_title)
    (tasks / "plan.weft.yml").write_text(value, encoding="utf-8")


def _command(repo: Path, action: str) -> list[str]:
    return [
        "--repo",
        str(repo),
        "--json",
        "task",
        "plan",
        action,
        "--source-label",
        "workspace/main",
    ]


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_cli_inspects_and_idempotently_imports_non_authoritative_plan_intent(
    tmp_path: Path, capsys
) -> None:
    repo = _repository(tmp_path / "repo")
    _write_plan(repo)

    assert main(_command(repo, "inspect")) == 0
    inspected = _payload(capsys)["source_plan_inspection"]
    assert inspected["status"] == "new"
    assert inspected["authority"].startswith("read-only source intent")

    assert main(_command(repo, "import")) == 0
    imported = _payload(capsys)["source_plan_import"]
    assert imported["imported"] is True
    assert imported["created_tasks"] == ["active"]
    assert imported["skipped_terminal_tasks"] == ["done-base"]
    assert imported["satisfied_source_dependencies"] == [["active", "done-base"]]
    assert "no Change Set, claim, evidence, review" in imported["authority"]

    assert main(_command(repo, "import")) == 0
    repeated = _payload(capsys)["source_plan_import"]
    assert repeated["imported"] is False
    assert repeated["existing_tasks"] == ["active"]

    assert main(
        [
            "--repo",
            str(repo),
            "task",
            "plan",
            "inspect",
            "--source-label",
            "workspace/main",
        ]
    ) == 0
    human = capsys.readouterr().out
    assert "source plan workspace/main  unchanged" in human
    assert "read-only source intent comparison" in human

    assert main(["--repo", str(repo), "--json", "task", "show", "active"]) == 0
    task = _payload(capsys)["task"]
    assert task["state"] == "todo"
    assert main(["--repo", str(repo), "--json", "changeset", "list"]) == 0
    assert _payload(capsys)["changesets"] == []
    assert main(["--repo", str(repo), "--json", "claim", "list"]) == 0
    assert _payload(capsys)["claims"] == []


def test_cli_reports_drift_without_mutating_native_intent(
    tmp_path: Path, capsys
) -> None:
    repo = _repository(tmp_path / "repo")
    _write_plan(repo)
    assert main(_command(repo, "import")) == 0
    _payload(capsys)

    _write_plan(repo, active_title="changed active title")
    assert main(_command(repo, "inspect")) == 0
    inspection = _payload(capsys)["source_plan_inspection"]
    assert inspection["status"] == "drift"
    assert inspection["drift"]["changed_tasks"] == ["active"]
    assert inspection["drift"]["changed_files"] == ["tasks/plan.weft.yml"]

    assert main(_command(repo, "import")) == 2
    refused = _payload(capsys)
    assert refused["ok"] is False
    assert refused["source_plan_drift"]["changed_tasks"] == ["active"]
    assert "explicit drift reconciliation is required" in refused["error"]

    assert main(["--repo", str(repo), "--json", "task", "show", "active"]) == 0
    task = _payload(capsys)["task"]
    assert task["title"] == "active title"
    assert task["state"] == "todo"


def test_cli_accepts_explicit_plan_root_and_files(tmp_path: Path, capsys) -> None:
    repo = _repository(tmp_path / "repo")
    source = tmp_path / "source"
    source.mkdir()
    _write_plan(source)

    command = _command(repo, "inspect") + [
        "--plan-root",
        str(source),
        "--file",
        "tasks/plan.weft.yml",
    ]
    assert main(command) == 0
    payload = _payload(capsys)["source_plan_inspection"]
    assert payload["status"] == "new"
