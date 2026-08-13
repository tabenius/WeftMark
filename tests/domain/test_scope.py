from __future__ import annotations

import json

import pytest

from weftmark.domain.scope import Scope, ScopeError, ScopeKind


@pytest.mark.parametrize(
    ("scope", "canonical"),
    [
        (Scope.file("src/weftmark/**/*.py"), "file:src/weftmark/**/*.py"),
        (Scope.contract("Tenant Authentication"), "contract:tenant-authentication"),
        (Scope.boundary("Domain-Adapter"), "boundary:domain-adapter"),
        (Scope.schema("Evidence/V0"), "schema:evidence/v0"),
        (Scope.surface("CLI Review"), "surface:cli-review"),
    ],
)
def test_every_scope_kind_has_a_canonical_form(scope: Scope, canonical: str) -> None:
    assert scope.canonical == canonical
    assert str(scope) == canonical


def test_file_scopes_are_relative_posix_patterns_and_case_sensitive() -> None:
    assert Scope.file(r"Src\WeftMark\\*.py").key == "Src/WeftMark/*.py"
    assert Scope.file("Src/file.py") != Scope.file("src/file.py")

    with pytest.raises(ScopeError, match="repository-relative"):
        Scope.file("/etc/passwd")
    with pytest.raises(ScopeError, match="escape"):
        Scope.file("src/../secrets")
    with pytest.raises(ScopeError, match="drive or scheme"):
        Scope.file(r"C:\repo\file.py")


def test_parse_normalizes_kind_and_semantic_key() -> None:
    scope = Scope.parse("CONTRACT: Tenant   Authentication ")

    assert scope.kind is ScopeKind.CONTRACT
    assert scope.key == "tenant-authentication"


def test_scope_kind_is_part_of_identity() -> None:
    assert Scope.contract("evidence-v0") != Scope.schema("evidence-v0")


def test_display_label_is_not_part_of_identity_or_hash() -> None:
    first = Scope.contract("tenant-auth", display_label="Tenant authentication")
    second = Scope.contract("tenant-auth", display_label="Auth contract")

    assert first == second
    assert hash(first) == hash(second)
    assert first.identity == (ScopeKind.CONTRACT, "tenant-auth")


def test_serialization_is_stable_and_round_trips() -> None:
    scope = Scope.schema("events/v1", display_label="Event schema")
    serialized = json.dumps(scope.to_dict(), sort_keys=True, separators=(",", ":"))

    assert serialized == (
        '{"display_label":"Event schema","key":"events/v1","kind":"schema"}'
    )
    assert Scope.from_dict(json.loads(serialized)) == scope


@pytest.mark.parametrize(
    "value",
    ["", "contract", "unknown:key", "contract:", "contract:../auth", "schema:events//v1"],
)
def test_invalid_or_ambiguous_scopes_fail_closed(value: str) -> None:
    with pytest.raises(ScopeError):
        Scope.parse(value)


def test_deserialization_rejects_unknown_fields() -> None:
    with pytest.raises(ScopeError, match="unexpected"):
        Scope.from_dict({"kind": "contract", "key": "auth", "owner": "agent"})

