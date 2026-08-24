from __future__ import annotations

import json
from pathlib import Path

import pytest

from weftmark.application.runtime_registry import (
    RuntimeProviderConfig,
    RuntimeProviderRegistry,
    RuntimeRegistryError,
    load_runtime_registry,
    parse_runtime_provider_flag,
)


def test_registry_is_deterministic_and_unknown_fails_closed() -> None:
    registry = RuntimeProviderRegistry({
        "z": RuntimeProviderConfig("z", ("z",)),
        "a": RuntimeProviderConfig("a", ("a",)),
    })
    assert registry.names() == ("a", "z")
    assert registry.get("a").argv == ("a",)
    with pytest.raises(RuntimeRegistryError, match="unknown runtime provider"):
        registry.get("missing")


def test_provider_fingerprint_binds_exact_argv_and_capabilities() -> None:
    first = RuntimeProviderConfig("acp", ("agent", "acp"), frozenset({"edit", "read"}))
    reordered = RuntimeProviderConfig("acp", ("agent", "acp"), frozenset({"read", "edit"}))
    changed = RuntimeProviderConfig("acp", ("other-agent", "acp"), frozenset({"read", "edit"}))

    assert first.fingerprint == reordered.fingerprint
    assert first.fingerprint.startswith("sha256:")
    assert first.fingerprint != changed.fingerprint


def test_flag_accepts_legacy_and_json_argv_forms() -> None:
    legacy = parse_runtime_provider_flag("codex-acp=codex:acp:cap=Read,edit")
    assert legacy.argv == ("codex", "acp")
    assert legacy.capabilities == frozenset({"read", "edit"})
    json_form = parse_runtime_provider_flag('portable=["python","-m","agent:acp"]')
    assert json_form.argv[-1] == "agent:acp"


@pytest.mark.parametrize("value", ["missing", "=x", "a=", "a=:x", "a=x:cap=a:tail", "a=x:cap=a:cap=b"])
def test_flag_refuses_ambiguous_or_empty_values(value: str) -> None:
    with pytest.raises(RuntimeRegistryError):
        parse_runtime_provider_flag(value)


def test_file_merge_accepts_extra_metadata_but_flags_win(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({
        "schema": 1,
        "providers": {
            "acp": {"argv": ["old"], "capabilities": ["read"], "comment": "accepted"}
        },
    }), encoding="utf-8")
    registry = load_runtime_registry(
        config_path=str(path), cli_flags=["acp=new:cap=edit"]
    )
    assert registry.get("acp") == RuntimeProviderConfig("acp", ("new",), frozenset({"edit"}))


def test_file_refuses_duplicate_keys_and_malformed_shapes(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"providers":{"a":{"argv":["x"]},"a":{"argv":["y"]}}}', encoding="utf-8")
    with pytest.raises(RuntimeRegistryError, match="repeats key"):
        load_runtime_registry(config_path=str(duplicate))
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"providers":[]}', encoding="utf-8")
    with pytest.raises(RuntimeRegistryError, match="providers object"):
        load_runtime_registry(config_path=str(malformed))
