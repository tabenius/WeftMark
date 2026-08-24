"""Strict named runtime configuration, independent of concrete adapters."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class RuntimeRegistryError(ValueError):
    """Raised when runtime provider configuration is missing or ambiguous."""


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_MAX_CONFIG_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class RuntimeProviderConfig:
    name: str
    argv: tuple[str, ...]
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not _NAME.fullmatch(name):
            raise RuntimeRegistryError(f"invalid runtime provider name: {self.name!r}")
        if not self.argv or any(
            not isinstance(value, str) or not value or "\x00" in value
            for value in self.argv
        ):
            raise RuntimeRegistryError(f"provider {name!r} has an invalid argv")
        normalized = frozenset(str(value).strip().casefold() for value in self.capabilities)
        if any(not _CAPABILITY.fullmatch(value) for value in normalized):
            raise RuntimeRegistryError(f"provider {name!r} has invalid capabilities")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "capabilities", normalized)

    @property
    def fingerprint(self) -> str:
        """Stable identity for the exact executable configuration, without exposing argv."""

        canonical = json.dumps(
            {
                "name": self.name,
                "argv": self.argv,
                "capabilities": sorted(self.capabilities),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


class RuntimeProviderRegistry:
    def __init__(self, providers: Mapping[str, RuntimeProviderConfig]) -> None:
        normalized: dict[str, RuntimeProviderConfig] = {}
        for key, value in providers.items():
            if key != value.name:
                raise RuntimeRegistryError("provider mapping key must equal provider name")
            if key in normalized:
                raise RuntimeRegistryError(f"duplicate runtime provider: {key}")
            normalized[key] = value
        self._providers = normalized

    def get(self, name: str) -> RuntimeProviderConfig:
        try:
            return self._providers[name]
        except KeyError:
            raise RuntimeRegistryError(f"unknown runtime provider: {name}") from None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


def parse_runtime_provider_flag(value: str) -> RuntimeProviderConfig:
    """Accept legacy colon argv or a JSON argv array after ``name=``."""

    name, separator, body = value.partition("=")
    if not separator or not name.strip() or not body.strip():
        raise RuntimeRegistryError(
            "--runtime-provider must look like name=argv0:argv1[:cap=a,b] "
            "or name=[\"argv0\",\"argv1\"]"
        )
    capabilities: frozenset[str] = frozenset()
    argv: tuple[str, ...]
    body = body.strip()
    if body.startswith("["):
        parsed = _decode_json(body, source="--runtime-provider")
        argv = _string_tuple(parsed, name="runtime provider argv")
    else:
        parts = body.split(":")
        capability_parts = [part for part in parts if part.startswith("cap=")]
        if len(capability_parts) > 1:
            raise RuntimeRegistryError("--runtime-provider repeats cap=")
        if capability_parts and parts[-1] != capability_parts[0]:
            raise RuntimeRegistryError("--runtime-provider cap= must be the final field")
        if capability_parts:
            parts.pop()
            capabilities = _capabilities(capability_parts[0].removeprefix("cap=").split(","))
        argv = _string_tuple(parts, name="runtime provider argv")
    return RuntimeProviderConfig(name.strip(), argv, capabilities)


def load_runtime_registry(
    *, config_path: str | None = None, cli_flags: Sequence[str] = ()
) -> RuntimeProviderRegistry:
    providers: dict[str, RuntimeProviderConfig] = {}
    if config_path is not None:
        path = Path(config_path)
        try:
            if not path.is_file():
                raise RuntimeRegistryError(f"runtime config file not found: {config_path}")
            if path.stat().st_size > _MAX_CONFIG_BYTES:
                raise RuntimeRegistryError("runtime config exceeds 1 MiB")
            payload = _decode_json(path.read_text(encoding="utf-8"), source="runtime config")
        except OSError as error:
            raise RuntimeRegistryError(f"cannot read runtime config: {type(error).__name__}") from error
        if not isinstance(payload, Mapping) or not isinstance(payload.get("providers"), Mapping):
            raise RuntimeRegistryError("runtime config requires a providers object")
        for raw_name, raw_entry in payload["providers"].items():
            if not isinstance(raw_name, str) or not isinstance(raw_entry, Mapping):
                raise RuntimeRegistryError("runtime config provider entries must be objects")
            argv = _string_tuple(raw_entry.get("argv"), name=f"provider {raw_name!r} argv")
            raw_capabilities = raw_entry.get("capabilities", ())
            if not isinstance(raw_capabilities, (list, tuple, set, frozenset)):
                raise RuntimeRegistryError(f"provider {raw_name!r} capabilities must be a list")
            providers[raw_name] = RuntimeProviderConfig(
                raw_name, argv, _capabilities(raw_capabilities)
            )
    for flag in cli_flags:
        config = parse_runtime_provider_flag(flag)
        providers[config.name] = config
    return RuntimeProviderRegistry(providers)


def _decode_json(value: str, *, source: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise RuntimeRegistryError(f"{source} repeats key: {key}")
            result[key] = item
        return result

    try:
        return json.loads(value, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise RuntimeRegistryError(f"{source} is not valid JSON") from error


def _string_tuple(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise RuntimeRegistryError(f"{name} must be a non-empty string list")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise RuntimeRegistryError(f"{name} must contain non-empty strings without NUL")
    return tuple(value)


def _capabilities(values: object) -> frozenset[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise RuntimeRegistryError("capabilities must be a string collection")
    normalized = frozenset(str(value).strip().casefold() for value in values)
    if "" in normalized:
        raise RuntimeRegistryError("capabilities must not be empty")
    return normalized
