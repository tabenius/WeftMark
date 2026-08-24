"""Strict, read-only adapter for the reviewed ``weft-task-v0`` source plan."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from weftmark.domain.scope import Scope, ScopeError
from weftmark.domain.task import TaskPriority


class WeftPlanError(ValueError):
    """Raised when source-plan files cannot be represented without ambiguity."""


@dataclass(frozen=True, slots=True)
class WeftPlanFile:
    path: str
    digest: str
    size: int


@dataclass(frozen=True, slots=True)
class WeftPlanTask:
    slug: str
    title: str
    status: str
    priority: TaskPriority
    purpose: str
    deliverables: tuple[str, ...]
    scopes: tuple[str, ...]
    dependencies: tuple[str, ...]
    conflicts: tuple[str, ...]
    source_file: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class WeftPlanSnapshot:
    digest: str
    files: tuple[WeftPlanFile, ...]
    tasks: tuple[WeftPlanTask, ...]


_STATUSES = frozenset({"idea", "todo", "in_progress", "blocked", "review", "done"})
_EVIDENCE_KINDS = frozenset(
    {"test", "ci", "review", "benchmark", "deployment", "security", "docs"}
)
_TASK_FIELDS = frozenset(
    {
        "slug",
        "title",
        "status",
        "priority",
        "depends",
        "conflicts",
        "purpose",
        "scope",
        "deliverables",
        "accept",
        "negative",
        "evidence",
        "notes",
    }
)
_TOP_FIELDS = frozenset({"format", "phase", "summary", "tasks"})
_NATIVE_SCOPE_KINDS = frozenset({"contract", "boundary", "schema", "surface"})
_MAX_FILE_BYTES = 1_048_576
_MAX_FILES = 256
_MAX_TASKS = 10_000


class WeftPlanAdapter:
    """Read one repository's plan files without opening its runtime ledger."""

    def __init__(self, repository: str | Path) -> None:
        self._root = Path(repository).resolve()
        if not self._root.is_dir():
            raise WeftPlanError("source-plan repository must be an existing directory")

    def load(self, paths: Sequence[str | Path] | None = None) -> WeftPlanSnapshot:
        requested = (
            tuple(Path(path) for path in paths)
            if paths is not None
            else tuple(sorted((self._root / "tasks").glob("*.weft.yml")))
        )
        if not requested:
            raise WeftPlanError("no source-plan files were selected")
        if len(requested) > _MAX_FILES:
            raise WeftPlanError(f"source plan exceeds {_MAX_FILES} files")

        selected: list[tuple[str, Path]] = []
        for requested_path in requested:
            path = (
                requested_path
                if requested_path.is_absolute()
                else self._root / requested_path
            )
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(self._root).as_posix()
            except ValueError as error:
                raise WeftPlanError("source-plan file must remain inside the repository") from error
            selected.append((relative, resolved))
        selected.sort(key=lambda value: value[0])

        files: list[WeftPlanFile] = []
        tasks: list[WeftPlanTask] = []
        digest = hashlib.sha256()
        seen_paths: set[str] = set()
        seen_slugs: dict[str, str] = {}
        for relative, resolved in selected:
            if relative in seen_paths:
                raise WeftPlanError(f"duplicate source-plan path: {relative}")
            seen_paths.add(relative)
            if not resolved.is_file():
                raise WeftPlanError(f"source-plan path is not a regular file: {relative}")
            try:
                raw = resolved.read_bytes()
            except OSError as error:
                raise WeftPlanError(
                    f"source-plan file could not be read: {relative}"
                ) from error
            if len(raw) > _MAX_FILE_BYTES:
                raise WeftPlanError(f"source-plan file exceeds {_MAX_FILE_BYTES} bytes: {relative}")
            file_digest = hashlib.sha256(raw).hexdigest()
            files.append(WeftPlanFile(relative, f"sha256:{file_digest}", len(raw)))
            encoded_path = relative.encode("utf-8")
            digest.update(len(encoded_path).to_bytes(4, "big"))
            digest.update(encoded_path)
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)

            document = _load_document(raw, relative)
            for index, value in enumerate(document["tasks"]):
                task = _parse_task(value, source_file=relative, index=index)
                previous = seen_slugs.get(task.slug)
                if previous is not None:
                    raise WeftPlanError(
                        f"duplicate task slug {task.slug!r} in {previous} and {relative}"
                    )
                seen_slugs[task.slug] = relative
                tasks.append(task)
                if len(tasks) > _MAX_TASKS:
                    raise WeftPlanError(f"source plan exceeds {_MAX_TASKS} tasks")

        _validate_graph(tasks)
        return WeftPlanSnapshot(
            digest=f"sha256:{digest.hexdigest()}",
            files=tuple(files),
            tasks=tuple(sorted(tasks, key=lambda task: task.slug)),
        )


def _load_document(raw: bytes, source_file: str) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WeftPlanError(f"source-plan file is not UTF-8: {source_file}") from error
    if "\x00" in text:
        raise WeftPlanError(f"source-plan file contains NUL bytes: {source_file}")
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - exercised by package smoke later
        raise WeftPlanError(
            "source-plan import requires the PyYAML package"
        ) from error

    class StrictLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
            if self.check_event(yaml.AliasEvent):
                raise WeftPlanError(
                    f"YAML aliases are not supported in source plans: {source_file}"
                )
            return super().compose_node(parent, index)

    def construct_mapping(loader, node, deep=False):  # type: ignore[no-untyped-def]
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as error:
                raise WeftPlanError(
                    f"source-plan mapping key is not scalar: {source_file}"
                ) from error
            if duplicate:
                raise WeftPlanError(
                    f"duplicate YAML mapping key {key!r}: {source_file}"
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping,
    )
    try:
        document = yaml.load(text, Loader=StrictLoader)
    except WeftPlanError:
        raise
    except yaml.YAMLError as error:
        raise WeftPlanError(f"invalid source-plan YAML: {source_file}") from error
    if not isinstance(document, Mapping):
        raise WeftPlanError(f"source-plan document must be a mapping: {source_file}")
    unexpected = _unexpected_fields(document, _TOP_FIELDS, source_file)
    if unexpected:
        raise WeftPlanError(
            f"unsupported source-plan fields in {source_file}: {sorted(unexpected)}"
        )
    if document.get("format") != "weft-task-v0":
        raise WeftPlanError(f"unsupported source-plan format: {source_file}")
    if not isinstance(document.get("tasks"), list):
        raise WeftPlanError(f"source-plan tasks must be a list: {source_file}")
    return document


def _parse_task(value: object, *, source_file: str, index: int) -> WeftPlanTask:
    location = f"{source_file} task {index + 1}"
    if not isinstance(value, Mapping):
        raise WeftPlanError(f"{location} must be a mapping")
    unexpected = _unexpected_fields(value, _TASK_FIELDS, location)
    if unexpected:
        raise WeftPlanError(f"{location} has unsupported fields: {sorted(unexpected)}")
    slug = _text(value, "slug", location)
    title = _text(value, "title", location)
    status = _text(value, "status", location).casefold()
    if status not in _STATUSES:
        raise WeftPlanError(f"{location} has unsupported status: {status}")
    try:
        priority = TaskPriority(_text(value, "priority", location).casefold())
    except ValueError as error:
        raise WeftPlanError(f"{location} has unsupported priority") from error
    purpose = _text(value, "purpose", location)
    deliverables = _text_list(value, "deliverables", location, required=True)
    _text_list(value, "accept", location, required=True)
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise WeftPlanError(f"{location} evidence must be a non-empty list")
    for record in evidence:
        if not isinstance(record, Mapping) or record.get("kind") not in _EVIDENCE_KINDS:
            raise WeftPlanError(f"{location} has an invalid evidence record")
    dependencies = _text_list(value, "depends", location)
    conflicts = _text_list(value, "conflicts", location)
    scope_value = value.get("scope") or {}
    if not isinstance(scope_value, Mapping):
        raise WeftPlanError(f"{location} scope must be a mapping")
    unexpected_scope = _unexpected_fields(
        scope_value,
        frozenset({"files", "contracts"}),
        f"{location} scope",
    )
    if unexpected_scope:
        raise WeftPlanError(
            f"{location} scope has unsupported fields: {sorted(unexpected_scope)}"
        )
    scopes: list[str] = []
    for path in _text_list(scope_value, "files", f"{location} scope"):
        try:
            scopes.append(Scope.file(path).canonical)
        except ScopeError as error:
            raise WeftPlanError(f"{location} has invalid file scope") from error
    for contract in _text_list(scope_value, "contracts", f"{location} scope"):
        scopes.append(_native_contract_scope(contract, location))
    if not scopes:
        raise WeftPlanError(f"{location} must declare at least one scope")
    canonical_scopes = tuple(sorted(set(scopes)))
    normalized = {
        "slug": slug,
        "title": title,
        "status": status,
        "priority": priority.value,
        "purpose": purpose,
        "deliverables": deliverables,
        "scopes": canonical_scopes,
        "dependencies": tuple(sorted(set(dependencies))),
        "conflicts": tuple(sorted(set(conflicts))),
    }
    fingerprint = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return WeftPlanTask(
        slug=slug,
        title=title,
        status=status,
        priority=priority,
        purpose=purpose,
        deliverables=deliverables,
        scopes=canonical_scopes,
        dependencies=normalized["dependencies"],
        conflicts=normalized["conflicts"],
        source_file=source_file,
        fingerprint=f"sha256:{fingerprint}",
    )


def _native_contract_scope(value: str, location: str) -> str:
    prefix, separator, key = value.partition(":")
    if not separator or not prefix or not key:
        raise WeftPlanError(f"{location} has invalid contract scope: {value!r}")
    native = (
        value
        if prefix.casefold() in _NATIVE_SCOPE_KINDS
        else f"contract:{prefix.casefold()}/{key}"
    )
    try:
        return Scope.parse(native).canonical
    except ScopeError as error:
        raise WeftPlanError(f"{location} has invalid contract scope: {value!r}") from error


def _text(value: Mapping[str, Any], name: str, location: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise WeftPlanError(f"{location} {name} must be non-empty text")
    return item.strip()


def _unexpected_fields(
    value: Mapping[object, object],
    allowed: frozenset[str],
    location: str,
) -> list[str]:
    if any(not isinstance(key, str) for key in value):
        raise WeftPlanError(f"{location} mapping keys must be text")
    return sorted(str(key) for key in value if key not in allowed)


def _text_list(
    value: Mapping[str, Any],
    name: str,
    location: str,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    item = value.get(name)
    if item is None and not required:
        return ()
    if not isinstance(item, list) or (required and not item):
        qualifier = "a non-empty list" if required else "a list"
        raise WeftPlanError(f"{location} {name} must be {qualifier}")
    result: list[str] = []
    for child in item:
        if not isinstance(child, str) or not child.strip():
            raise WeftPlanError(f"{location} {name} entries must be non-empty text")
        result.append(child.strip())
    if len(set(result)) != len(result):
        raise WeftPlanError(f"{location} {name} must not contain duplicates")
    return tuple(result)


def _validate_graph(tasks: Sequence[WeftPlanTask]) -> None:
    by_slug = {task.slug: task for task in tasks}
    graph: dict[str, set[str]] = {}
    for task in tasks:
        for dependency in task.dependencies:
            if dependency not in by_slug:
                raise WeftPlanError(
                    f"task {task.slug!r} references missing dependency {dependency!r}"
                )
            if dependency == task.slug:
                raise WeftPlanError(f"task {task.slug!r} cannot depend on itself")
            graph.setdefault(task.slug, set()).add(dependency)
        for conflict in task.conflicts:
            if conflict not in by_slug:
                raise WeftPlanError(
                    f"task {task.slug!r} references missing conflict {conflict!r}"
                )
            if conflict == task.slug:
                raise WeftPlanError(f"task {task.slug!r} cannot conflict with itself")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(slug: str) -> None:
        if slug in visiting:
            raise WeftPlanError(f"source-plan dependency cycle includes {slug!r}")
        if slug in visited:
            return
        visiting.add(slug)
        for dependency in sorted(graph.get(slug, ())):
            visit(dependency)
        visiting.remove(slug)
        visited.add(slug)

    for slug in sorted(by_slug):
        visit(slug)
