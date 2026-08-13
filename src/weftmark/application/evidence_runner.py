"""Privacy-preserving local command evidence bound to a Change Set head."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from weftmark.application.change_binding import ChangeBinding
from weftmark.domain.evidence import (
    ArtifactReference,
    Command,
    Environment,
    Evidence,
    EvidenceKind,
    EvidenceProducer,
    EvidenceState,
    EvidenceSubject,
    SubjectKind,
)


class EvidenceRunnerError(ValueError):
    """Raised when a command evidence request is unsafe or malformed."""


_SENSITIVE_KEY_MARKERS = (
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE",
    "SECRET",
    "TOKEN",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class CommandEvidenceRequest:
    id: str
    kind: EvidenceKind
    argv: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...] = ()
    redact_argv_indexes: frozenset[int] = frozenset()
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise EvidenceRunnerError("evidence id must not be empty")
        if not self.argv or any(not argument or "\x00" in argument for argument in self.argv):
            raise EvidenceRunnerError("argv must contain non-empty, NUL-free arguments")
        if not self.cwd or not self.cwd.strip():
            raise EvidenceRunnerError("cwd must not be empty")
        if self.timeout_seconds <= 0:
            raise EvidenceRunnerError("timeout_seconds must be positive")
        if any(index < 0 or index >= len(self.argv) for index in self.redact_argv_indexes):
            raise EvidenceRunnerError("redacted argv index is out of range")
        keys = tuple(key for key, _ in self.environment)
        if len(set(keys)) != len(keys):
            raise EvidenceRunnerError("environment contains duplicate keys")
        if any(
            not key or "=" in key or "\x00" in key or "\x00" in value
            for key, value in self.environment
        ):
            raise EvidenceRunnerError("environment keys and values must be process-safe")


@dataclass(frozen=True, slots=True)
class CommandEvidenceResult:
    evidence: Evidence
    exit_code: int | None
    duration_seconds: float
    stdout_digest: str
    stderr_digest: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.evidence.state is EvidenceState.PASSED


class LocalEvidenceRunner:
    def __init__(self, producer: EvidenceProducer) -> None:
        self._producer = producer

    def run(
        self,
        binding: ChangeBinding,
        request: CommandEvidenceRequest,
    ) -> CommandEvidenceResult:
        worktree = Path(binding.latest.worktree).resolve()
        cwd = Path(request.cwd).resolve()
        if not cwd.is_relative_to(worktree):
            raise EvidenceRunnerError("command cwd must be inside the bound worktree")
        if not cwd.is_dir():
            raise EvidenceRunnerError("command cwd must be an existing directory")

        process_environment = os.environ.copy()
        process_environment.update(dict(request.environment))
        recorded_argv = _redacted_argv(
            request.argv,
            process_environment,
            request.redact_argv_indexes,
        )
        environment = _fingerprint_environment(
            executable=recorded_argv[0],
            cwd=cwd,
            environment_keys=tuple(process_environment),
        )
        started_at = _now()
        exit_code: int | None = None
        timed_out = False
        detail: str | None = None

        try:
            completed = subprocess.run(
                request.argv,
                cwd=cwd,
                env=process_environment,
                shell=False,
                check=False,
                capture_output=True,
                timeout=request.timeout_seconds,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as error:
            stdout = _as_bytes(error.stdout)
            stderr = _as_bytes(error.stderr)
            timed_out = True
            detail = "command timed out before producing required evidence"
        except OSError as error:
            stdout = b""
            stderr = b""
            detail = f"command could not start ({type(error).__name__})"

        completed_at = _now()
        stdout_digest = _digest(stdout)
        stderr_digest = _digest(stderr)
        artifacts = (
            _output_artifact("stdout", stdout_digest),
            _output_artifact("stderr", stderr_digest),
        )
        evidence = Evidence.declare(
            id=request.id,
            kind=request.kind,
            producer=self._producer,
            subject=EvidenceSubject(SubjectKind.CHANGE_SET, binding.change_set.id),
            bound_commit_sha=binding.latest.head_sha,
            environment=environment,
            command=Command(recorded_argv, str(cwd)),
            artifacts=artifacts,
            at=started_at,
        ).start(at=started_at)
        if exit_code == 0:
            evidence = evidence.pass_(at=completed_at)
        elif exit_code is not None:
            evidence = evidence.fail(
                detail=f"command exited with status {exit_code}",
                at=completed_at,
            )
        else:
            evidence = evidence.unavailable(
                reason=detail or "command evidence unavailable",
                at=completed_at,
            )

        return CommandEvidenceResult(
            evidence=evidence,
            exit_code=exit_code,
            duration_seconds=(completed_at - started_at).total_seconds(),
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            timed_out=timed_out,
        )


def _redacted_argv(
    argv: tuple[str, ...],
    environment: dict[str, str],
    explicit_indexes: frozenset[int],
) -> tuple[str, ...]:
    secret_values = tuple(
        value
        for key, value in environment.items()
        if len(value) >= 4
        and any(marker in key.upper() for marker in _SENSITIVE_KEY_MARKERS)
    )
    return tuple(
        "<redacted>"
        if index in explicit_indexes
        or any(secret_value in argument for secret_value in secret_values)
        else argument
        for index, argument in enumerate(argv)
    )


def _fingerprint_environment(
    *,
    executable: str,
    cwd: Path,
    environment_keys: tuple[str, ...],
) -> Environment:
    facts = {
        "cwd": str(cwd),
        "environment_keys": sorted(environment_keys),
        "executable": executable,
        "machine": platform.machine(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "system": platform.system(),
        "system_release": platform.release(),
    }
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    description = (
        f"{facts['system']} {facts['system_release']} {facts['machine']}; "
        f"Python {facts['python']}"
    )
    return Environment(fingerprint, description)


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _output_artifact(stream: str, digest: str) -> ArtifactReference:
    return ArtifactReference(
        uri=f"urn:weftmark:command-output:{stream}:{digest}",
        digest=f"sha256:{digest}",
    )
