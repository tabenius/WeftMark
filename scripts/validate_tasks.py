#!/usr/bin/env python3
"""Validate the initial WeftMark task graph.

This validates the source-plan mini-format documented in AGENTS.md. It does not
claim to be the future runtime schema; keeping the format small is intentional.
"""
from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
TASK_DIR = ROOT / "tasks"
STATUSES = {"idea", "todo", "in_progress", "blocked", "review", "done"}
PRIORITIES = {"P0", "P1", "P2", "P3"}
EVIDENCE_KINDS = {"test", "ci", "review", "benchmark", "deployment", "security", "docs"}


def fail(message: str) -> None:
    print(f"task validation: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    files = sorted(TASK_DIR.glob("*.weft.yml"))
    if not files:
        fail("no tasks/*.weft.yml files found")

    tasks: dict[str, dict] = {}
    origins: dict[str, str] = {}
    for path in files:
        data = yaml.safe_load(path.read_text()) or {}
        if data.get("format") != "weft-task-v0":
            fail(f"{path.name}: format must be weft-task-v0")
        items = data.get("tasks")
        if not isinstance(items, list):
            fail(f"{path.name}: tasks must be a list")
        for task in items:
            if not isinstance(task, dict):
                fail(f"{path.name}: every task must be a mapping")
            slug = task.get("slug")
            if not slug or not isinstance(slug, str):
                fail(f"{path.name}: task missing slug")
            if slug in tasks:
                fail(f"duplicate slug {slug} in {path.name} and {origins[slug]}")
            tasks[slug] = task
            origins[slug] = path.name

    required = {"slug", "title", "status", "priority", "purpose", "accept", "evidence"}
    graph: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)

    for slug, task in tasks.items():
        missing = sorted(required - task.keys())
        if missing:
            fail(f"{slug}: missing required fields: {', '.join(missing)}")
        if task["status"] not in STATUSES:
            fail(f"{slug}: invalid status {task['status']!r}")
        if task["priority"] not in PRIORITIES:
            fail(f"{slug}: invalid priority {task['priority']!r}")
        if not isinstance(task["accept"], list) or not task["accept"]:
            fail(f"{slug}: accept must be a non-empty list")
        if not isinstance(task["evidence"], list) or not task["evidence"]:
            fail(f"{slug}: evidence must be a non-empty list")
        for ev in task["evidence"]:
            if not isinstance(ev, dict) or ev.get("kind") not in EVIDENCE_KINDS:
                fail(f"{slug}: invalid evidence record {ev!r}")
        for dep in task.get("depends", []) or []:
            if dep not in tasks:
                fail(f"{slug}: unknown dependency {dep}")
            if dep == slug:
                fail(f"{slug}: task cannot depend on itself")
            graph[dep].add(slug)
            reverse[slug].add(dep)

    # Kahn topological sort.
    indegree = {slug: len(reverse[slug]) for slug in tasks}
    queue = deque(sorted(slug for slug, n in indegree.items() if n == 0))
    visited = []
    while queue:
        slug = queue.popleft()
        visited.append(slug)
        for nxt in sorted(graph[slug]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(visited) != len(tasks):
        cycle_nodes = sorted(slug for slug, n in indegree.items() if n > 0)
        fail("dependency cycle involving: " + ", ".join(cycle_nodes))

    print(f"validated {len(tasks)} tasks across {len(files)} files")
    print("topological roots: " + ", ".join(slug for slug in tasks if not reverse[slug]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
