from __future__ import annotations

from pathlib import Path

import pytest

from weftmark.adapters.weft_plan import WeftPlanAdapter, WeftPlanError


def _task(
    slug: str,
    *,
    status: str = "todo",
    priority: str = "P1",
    depends: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
    contract: str = "adapter:local-v0",
) -> str:
    depends_yaml = "".join(f"\n      - {value}" for value in depends) or " []"
    conflicts_yaml = "".join(f"\n      - {value}" for value in conflicts) or " []"
    return f"""  - slug: {slug}
    title: {slug} title
    status: {status}
    priority: {priority}
    depends:{depends_yaml}
    conflicts:{conflicts_yaml}
    purpose: Preserve {slug} intent.
    scope:
      files:
        - src/{slug}.py
      contracts:
        - {contract}
    deliverables:
      - Deliver {slug}.
    accept:
      - {slug} is inspectable.
    negative:
      - Authority is not inferred.
    evidence:
      - kind: test
        command: python -m pytest
"""


def _write_plan(path: Path, *tasks: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "format: weft-task-v0\nphase: test\nsummary: Test plan.\ntasks:\n"
        + "".join(tasks),
        encoding="utf-8",
    )


def test_adapter_loads_complete_graph_and_maps_source_contract_kinds(
    tmp_path: Path,
) -> None:
    _write_plan(
        tmp_path / "tasks" / "20-b.weft.yml",
        _task("worker", depends=("base",), conflicts=("review",)),
        _task("review", contract="surface:review"),
    )
    _write_plan(
        tmp_path / "tasks" / "10-a.weft.yml",
        _task("base", status="done", priority="P0", contract="boundary:git"),
    )

    snapshot = WeftPlanAdapter(tmp_path).load()

    assert snapshot.digest.startswith("sha256:")
    assert [value.path for value in snapshot.files] == [
        "tasks/10-a.weft.yml",
        "tasks/20-b.weft.yml",
    ]
    assert [task.slug for task in snapshot.tasks] == ["base", "review", "worker"]
    worker = next(task for task in snapshot.tasks if task.slug == "worker")
    assert worker.dependencies == ("base",)
    assert worker.conflicts == ("review",)
    assert worker.scopes == (
        "contract:adapter/local-v0",
        "file:src/worker.py",
    )
    assert WeftPlanAdapter(tmp_path).load() == snapshot


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "format: weft-task-v0\nformat: weft-task-v0\ntasks: []\n",
            "duplicate YAML mapping key",
        ),
        (
            "format: weft-task-v0\nphase: &phase test\nsummary: *phase\ntasks: []\n",
            "aliases are not supported",
        ),
        (
            "format: future-format\ntasks: []\n",
            "unsupported source-plan format",
        ),
        (
            "format: weft-task-v0\n1: unsupported\ntasks: []\n",
            "mapping keys must be text",
        ),
    ],
)
def test_adapter_rejects_ambiguous_or_unsupported_yaml(
    tmp_path: Path,
    body: str,
    message: str,
) -> None:
    path = tmp_path / "tasks" / "plan.weft.yml"
    path.parent.mkdir()
    path.write_text(body, encoding="utf-8")

    with pytest.raises(WeftPlanError, match=message):
        WeftPlanAdapter(tmp_path).load()


def test_adapter_rejects_missing_relations_and_cycles(tmp_path: Path) -> None:
    path = tmp_path / "tasks" / "plan.weft.yml"
    _write_plan(path, _task("one", depends=("missing",)))
    with pytest.raises(WeftPlanError, match="missing dependency"):
        WeftPlanAdapter(tmp_path).load()

    _write_plan(
        path,
        _task("one", depends=("two",)),
        _task("two", depends=("one",)),
    )
    with pytest.raises(WeftPlanError, match="dependency cycle"):
        WeftPlanAdapter(tmp_path).load()


def test_adapter_refuses_files_outside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside.weft.yml"
    _write_plan(outside, _task("outside"))

    with pytest.raises(WeftPlanError, match="inside the repository"):
        WeftPlanAdapter(repository).load([outside])


def test_repository_source_plan_is_importable() -> None:
    repository = Path(__file__).resolve().parents[2]

    snapshot = WeftPlanAdapter(repository).load()

    assert len(snapshot.files) == 12
    assert len(snapshot.tasks) >= 73
    assert {"source-plan-native-import-core", "source-plan-native-import"} <= {
        task.slug for task in snapshot.tasks
    }


def test_explicit_file_order_does_not_change_snapshot_identity(tmp_path: Path) -> None:
    first = tmp_path / "tasks" / "a.weft.yml"
    second = tmp_path / "tasks" / "b.weft.yml"
    _write_plan(first, _task("first"))
    _write_plan(second, _task("second"))
    adapter = WeftPlanAdapter(tmp_path)

    assert adapter.load([first, second]) == adapter.load([second, first])
