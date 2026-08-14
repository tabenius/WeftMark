"""Privacy-minimized, integrity-digested portable Change Set bundles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from weftmark.application.claims import ClaimService, claim_to_payload
from weftmark.application.local_workflow import (
    LocalWorkflowService,
    evidence_result_to_payload,
)
from weftmark.application.workspace import WorkspaceService, binding_to_payload


class BundleError(ValueError):
    """Raised when a portable bundle is unsafe, malformed, or corrupt."""


@dataclass(frozen=True, slots=True)
class BundleVerification:
    digest: str
    change_set_id: str
    claim_count: int
    evidence_count: int
    review_count: int
    handoff_count: int


class BundleService:
    def __init__(
        self,
        workspace: WorkspaceService,
        claims: ClaimService,
        workflow: LocalWorkflowService,
    ) -> None:
        self._workspace = workspace
        self._claims = claims
        self._workflow = workflow

    def export(self, change_set_id: str, *, exported_at: datetime) -> dict[str, Any]:
        binding = self._workspace.require_change_set(change_set_id)
        change_set = binding_to_payload(binding)
        repository_id = str(change_set.pop("repository_id"))
        change_set.pop("worktree", None)
        for observation in change_set["observations"]:
            observation.pop("repository_id", None)
            observation.pop("worktree", None)
        change_set["repository_fingerprint"] = _sha256(repository_id)

        evidence = []
        for result in self._workflow.list_evidence(change_set_id=change_set_id):
            payload = evidence_result_to_payload(result)
            command = payload.get("command")
            if command is not None:
                command["cwd"] = _relative_cwd(
                    str(command["cwd"]), binding.latest.worktree
                )
                command["argv"] = _portable_argv(command["argv"])
            environment = payload.get("environment")
            if environment is not None:
                environment.pop("description", None)
            evidence.append(payload)

        reviews = []
        for value in self._workflow.list_reviews(change_set_id=change_set_id):
            payload = _copy(value)
            payload.pop("repository_id", None)
            payload.pop("worktree", None)
            reviews.append(payload)

        handoffs = []
        for value in self._workflow.list_handoffs(change_set_id=change_set_id):
            payload = value.to_dict()
            payload.pop("repository_id", None)
            payload.pop("worktree", None)
            handoffs.append(payload)

        contents = {
            "format": "weftmark-portable-bundle-v1",
            "exported_at": exported_at.isoformat(),
            "change_set": change_set,
            "claims": [
                claim_to_payload(value, observed_at=exported_at)
                for value in self._claims.list(change_set_id=change_set_id)
            ],
            "evidence": evidence,
            "reviews": reviews,
            "handoffs": handoffs,
        }
        _validate_contents(contents)
        return {
            "schema_version": 1,
            "digest": f"sha256:{_sha256(_canonical(contents))}",
            "contents": contents,
        }


def verify_bundle(bundle: Mapping[str, Any]) -> BundleVerification:
    try:
        if set(bundle) != {"schema_version", "digest", "contents"}:
            raise BundleError("bundle envelope has unexpected fields")
        if bundle["schema_version"] != 1:
            raise BundleError("unsupported bundle schema version")
        contents = bundle["contents"]
        if not isinstance(contents, Mapping):
            raise BundleError("bundle contents must be an object")
        _validate_contents(contents)
        expected = f"sha256:{_sha256(_canonical(contents))}"
        if bundle["digest"] != expected:
            raise BundleError("bundle digest does not match its contents")
        change_set_id = contents["change_set"]["id"]
        return BundleVerification(
            digest=expected,
            change_set_id=str(change_set_id),
            claim_count=len(contents["claims"]),
            evidence_count=len(contents["evidence"]),
            review_count=len(contents["reviews"]),
            handoff_count=len(contents["handoffs"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, BundleError):
            raise
        raise BundleError("portable bundle is malformed") from error


def verification_to_payload(value: BundleVerification) -> dict[str, Any]:
    return {
        "digest": value.digest,
        "change_set_id": value.change_set_id,
        "counts": {
            "claims": value.claim_count,
            "evidence": value.evidence_count,
            "reviews": value.review_count,
            "handoffs": value.handoff_count,
        },
    }


def _validate_contents(contents: Mapping[str, Any]) -> None:
    expected = {
        "format",
        "exported_at",
        "change_set",
        "claims",
        "evidence",
        "reviews",
        "handoffs",
    }
    if set(contents) != expected:
        raise BundleError("bundle contents have unexpected fields")
    if contents["format"] != "weftmark-portable-bundle-v1":
        raise BundleError("unsupported portable bundle format")
    exported_at = datetime.fromisoformat(str(contents["exported_at"]))
    if exported_at.tzinfo is None or exported_at.utcoffset() is None:
        raise BundleError("bundle export timestamp must include a timezone")
    if not isinstance(contents["change_set"], Mapping):
        raise BundleError("bundle Change Set must be an object")
    for name in ("claims", "evidence", "reviews", "handoffs"):
        if not isinstance(contents[name], list):
            raise BundleError(f"bundle {name} must be a list")
    forbidden = _forbidden_paths(contents)
    if forbidden:
        raise BundleError(
            "bundle contains local or raw-output fields: " + ", ".join(forbidden)
        )


def _forbidden_paths(value: object, path: str = "$") -> tuple[str, ...]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if key_text in {"repository_id", "worktree", "stdout", "stderr"}:
                findings.append(child_path)
            if key_text == "cwd" and isinstance(child, str):
                cwd = Path(child)
                if cwd.is_absolute() or ".." in cwd.parts:
                    findings.append(child_path)
            findings.extend(_forbidden_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(_forbidden_paths(child, f"{path}[{index}]"))
    return tuple(findings)


def _relative_cwd(cwd: str, worktree: str) -> str:
    try:
        relative = Path(cwd).resolve().relative_to(Path(worktree).resolve())
    except ValueError as error:
        raise BundleError("evidence command cwd is outside the Change Set worktree") from error
    return "." if str(relative) == "." else relative.as_posix()


def _portable_argv(argv: list[str]) -> list[str]:
    values: list[str] = []
    for index, value in enumerate(argv):
        path = Path(value)
        if path.is_absolute():
            values.append(path.name if index == 0 else "<absolute-path>")
        else:
            values.append(value)
    return values


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))
