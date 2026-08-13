"""Typed identities for file and semantic coordination scopes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
import re
import unicodedata
from typing import Any, Mapping


class ScopeError(ValueError):
    """Raised when a scope cannot be represented canonically."""


class ScopeKind(StrEnum):
    FILE = "file"
    CONTRACT = "contract"
    BOUNDARY = "boundary"
    SCHEMA = "schema"
    SURFACE = "surface"


_SEMANTIC_KEY = re.compile(r"^[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?$")


def _require_text(name: str, value: str) -> str:
    if not value or not value.strip():
        raise ScopeError(f"{name} must not be empty")
    return value.strip()


def _normalize_file(value: str) -> str:
    value = _require_text("file scope", value).replace("\\", "/")
    if value.startswith("/"):
        raise ScopeError("file scope must be repository-relative")
    raw_parts = value.split("/")
    if ".." in raw_parts:
        raise ScopeError("file scope must not escape the repository")
    if any("\x00" in part for part in raw_parts):
        raise ScopeError("file scope must not contain NUL bytes")

    normalized = str(PurePosixPath(value))
    if normalized in {"", "."}:
        raise ScopeError("file scope must identify a path or pattern")
    if ":" in normalized:
        raise ScopeError("file scope must not contain a drive or scheme prefix")
    return normalized


def _normalize_semantic(value: str) -> str:
    value = unicodedata.normalize("NFKC", _require_text("semantic scope", value))
    value = re.sub(r"\s+", "-", value.casefold())
    value = re.sub(r"-+", "-", value)
    if not _SEMANTIC_KEY.fullmatch(value):
        raise ScopeError(
            "semantic scope must use letters, digits, dots, dashes, underscores, or slashes"
        )
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ScopeError("semantic scope contains an invalid path segment")
    return value


@dataclass(frozen=True, slots=True)
class Scope:
    """Canonical scope identity with optional non-identity display metadata."""

    kind: ScopeKind
    key: str
    display_label: str | None = field(default=None, compare=False, hash=False)

    def __post_init__(self) -> None:
        normalized = (
            _normalize_file(self.key)
            if self.kind is ScopeKind.FILE
            else _normalize_semantic(self.key)
        )
        object.__setattr__(self, "key", normalized)
        if self.display_label is not None:
            object.__setattr__(
                self, "display_label", _require_text("display label", self.display_label)
            )

    @property
    def canonical(self) -> str:
        return f"{self.kind.value}:{self.key}"

    @property
    def identity(self) -> tuple[ScopeKind, str]:
        return (self.kind, self.key)

    @classmethod
    def parse(cls, value: str, *, display_label: str | None = None) -> Scope:
        raw = _require_text("scope", value)
        prefix, separator, key = raw.partition(":")
        if not separator:
            raise ScopeError("scope must use kind:key form")
        try:
            kind = ScopeKind(prefix.casefold())
        except ValueError as exc:
            raise ScopeError(f"unknown scope kind: {prefix}") from exc
        return cls(kind=kind, key=key, display_label=display_label)

    @classmethod
    def file(cls, key: str, *, display_label: str | None = None) -> Scope:
        return cls(ScopeKind.FILE, key, display_label)

    @classmethod
    def contract(cls, key: str, *, display_label: str | None = None) -> Scope:
        return cls(ScopeKind.CONTRACT, key, display_label)

    @classmethod
    def boundary(cls, key: str, *, display_label: str | None = None) -> Scope:
        return cls(ScopeKind.BOUNDARY, key, display_label)

    @classmethod
    def schema(cls, key: str, *, display_label: str | None = None) -> Scope:
        return cls(ScopeKind.SCHEMA, key, display_label)

    @classmethod
    def surface(cls, key: str, *, display_label: str | None = None) -> Scope:
        return cls(ScopeKind.SURFACE, key, display_label)

    def to_dict(self) -> dict[str, str]:
        data = {"kind": self.kind.value, "key": self.key}
        if self.display_label is not None:
            data["display_label"] = self.display_label
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Scope:
        allowed = {"kind", "key", "display_label"}
        unexpected = set(data) - allowed
        if unexpected:
            raise ScopeError(f"unexpected scope fields: {sorted(unexpected)}")
        try:
            kind = ScopeKind(data["kind"])
            key = data["key"]
            display_label = data.get("display_label")
        except (KeyError, ValueError) as exc:
            raise ScopeError("scope record has an invalid kind or missing field") from exc
        if not isinstance(key, str) or (
            display_label is not None and not isinstance(display_label, str)
        ):
            raise ScopeError("scope key and display label must be strings")
        return cls(kind, key, display_label)

    def __str__(self) -> str:
        return self.canonical

