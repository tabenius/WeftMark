from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weftmark.application.change_binding import ChangeBinding, GitLineageObservation
from weftmark.application.evidence_runner import (
    CommandEvidenceRequest,
    CommandEvidenceResult,
    EvidenceRunnerError,
    LocalEvidenceRunner,
)
from weftmark.domain.changeset import ChangeSet
from weftmark.domain.evidence import (
    EvidenceKind,
    EvidenceProducer,
    EvidenceState,
    ProducerKind,
)
from weftmark.domain.policy import EvidencePolicy, EvidenceProblem, EvidenceRequirement


NOW = datetime(2026, 8, 14, 0, 10, tzinfo=timezone.utc)
HEAD = "b" * 40
PRODUCER = EvidenceProducer(ProducerKind.WORKER, "local-runner")


def binding(worktree: Path) -> ChangeBinding:
    change_set = ChangeSet.plan(
        id="chg-1",
        goal="Capture local proof",
        repository_id="repo-1",
        base_sha="a" * 40,
        branch="feature",
        worktree=str(worktree),
        at=NOW,
    ).activate(head_sha=HEAD, at=NOW)
    observation = GitLineageObservation(
        id="chg-1:git:1",
        repository_id="repo-1",
        base_revision="main",
        base_sha="a" * 40,
        head_sha=HEAD,
        branch="feature",
        worktree=str(worktree),
        changed_paths=(),
        dirty_paths=(),
        observed_at=NOW,
    )
    return ChangeBinding(change_set, "main", (observation,))


def request(tmp_path: Path, *argv: str, **kwargs: object) -> CommandEvidenceRequest:
    return CommandEvidenceRequest(
        id="ev-1",
        kind=EvidenceKind.TEST,
        argv=tuple(argv),
        cwd=str(tmp_path),
        **kwargs,
    )


def test_success_is_commit_bound_passed_evidence_with_digest_only_output(
    tmp_path: Path,
) -> None:
    result = LocalEvidenceRunner(PRODUCER).run(
        binding(tmp_path),
        request(tmp_path, sys.executable, "-c", "print('verified')"),
    )

    assert result.passed
    assert result.exit_code == 0
    assert result.evidence.bound_commit_sha == HEAD
    assert result.evidence.subject.id == "chg-1"
    assert result.stdout_digest == hashlib.sha256(b"verified\n").hexdigest()
    assert result.evidence.artifacts[0].digest == f"sha256:{result.stdout_digest}"
    assert not hasattr(result, "stdout")
    assert not hasattr(result, "stderr")
    assert result.duration_seconds >= 0


def test_nonzero_exit_is_failed_not_unavailable(tmp_path: Path) -> None:
    result = LocalEvidenceRunner(PRODUCER).run(
        binding(tmp_path),
        request(tmp_path, sys.executable, "-c", "raise SystemExit(7)"),
    )

    assert result.evidence.state is EvidenceState.FAILED
    assert result.exit_code == 7
    assert result.evidence.detail == "command exited with status 7"


def test_command_evidence_becomes_stale_after_head_moves(tmp_path: Path) -> None:
    result = LocalEvidenceRunner(PRODUCER).run(
        binding(tmp_path),
        request(tmp_path, sys.executable, "-c", "pass"),
    )
    policy = EvidencePolicy(
        "tests-current",
        result.evidence.subject,
        (EvidenceRequirement("tests", EvidenceKind.TEST),),
    )

    evaluation = policy.evaluate(
        (result.evidence,),
        current_commit_sha="c" * 40,
    )
    assert not evaluation.is_satisfied
    assert evaluation.issues[0].problem is EvidenceProblem.STALE


def test_timeout_is_unavailable_not_failed(tmp_path: Path) -> None:
    result = LocalEvidenceRunner(PRODUCER).run(
        binding(tmp_path),
        request(
            tmp_path,
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
            timeout_seconds=0.01,
        ),
    )

    assert result.evidence.state is EvidenceState.UNAVAILABLE
    assert result.exit_code is None
    assert result.timed_out
    assert "timed out" in (result.evidence.detail or "")


def test_missing_executable_is_unavailable_without_internal_path_detail(
    tmp_path: Path,
) -> None:
    missing = str(tmp_path / "does-not-exist")
    result = LocalEvidenceRunner(PRODUCER).run(
        binding(tmp_path), request(tmp_path, missing)
    )

    assert result.evidence.state is EvidenceState.UNAVAILABLE
    assert result.exit_code is None
    assert result.evidence.detail == "command could not start (FileNotFoundError)"


def test_environment_values_and_secret_arguments_are_never_serialized(
    tmp_path: Path,
) -> None:
    secret = "highly-sensitive-test-token"
    result = LocalEvidenceRunner(PRODUCER).run(
        binding(tmp_path),
        request(
            tmp_path,
            sys.executable,
            "-c",
            "import os; print(os.environ['API_TOKEN'])",
            f"--token={secret}",
            environment=(("API_TOKEN", secret),),
        ),
    )

    assert result.evidence.state is EvidenceState.PASSED
    assert result.evidence.command is not None
    assert result.evidence.command.argv[-1] == "<redacted>"
    assert secret not in repr(result)
    assert secret not in (result.evidence.environment.description or "")


def test_explicit_argument_redaction_covers_non_environment_secrets(
    tmp_path: Path,
) -> None:
    result = LocalEvidenceRunner(PRODUCER).run(
        binding(tmp_path),
        request(
            tmp_path,
            sys.executable,
            "-c",
            "pass",
            "private-argument",
            redact_argv_indexes=frozenset({3}),
        ),
    )
    assert result.evidence.command is not None
    assert result.evidence.command.argv[-1] == "<redacted>"


def test_environment_fingerprint_excludes_values(tmp_path: Path) -> None:
    runner = LocalEvidenceRunner(PRODUCER)
    first = runner.run(
        binding(tmp_path),
        request(
            tmp_path,
            sys.executable,
            "-c",
            "pass",
            environment=(("API_TOKEN", "first-secret"),),
        ),
    )
    second = runner.run(
        binding(tmp_path),
        CommandEvidenceRequest(
            id="ev-2",
            kind=EvidenceKind.TEST,
            argv=(sys.executable, "-c", "pass"),
            cwd=str(tmp_path),
            environment=(("API_TOKEN", "second-secret"),),
        ),
    )
    assert first.evidence.environment == second.evidence.environment


def test_command_cwd_cannot_escape_bound_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    outside = tmp_path / "outside"
    worktree.mkdir()
    outside.mkdir()

    with pytest.raises(EvidenceRunnerError, match="bound worktree"):
        LocalEvidenceRunner(PRODUCER).run(
            binding(worktree), request(outside, sys.executable, "-c", "pass")
        )


def test_command_evidence_refuses_dirty_source_binding(tmp_path: Path) -> None:
    current = binding(tmp_path)
    dirty_observation = GitLineageObservation(
        id="chg-1:git:2",
        repository_id="repo-1",
        base_revision="main",
        base_sha="a" * 40,
        head_sha=HEAD,
        branch="feature",
        worktree=str(tmp_path),
        changed_paths=(),
        dirty_paths=("src/uncommitted.py",),
        observed_at=NOW,
    )
    dirty = ChangeBinding(
        current.change_set,
        "main",
        (*current.observations, dirty_observation),
    )

    with pytest.raises(EvidenceRunnerError, match="clean worktree"):
        LocalEvidenceRunner(PRODUCER).run(
            dirty, request(tmp_path, sys.executable, "-c", "pass")
        )


def test_request_validation_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(EvidenceRunnerError, match="argv"):
        request(tmp_path)
    with pytest.raises(EvidenceRunnerError, match="positive"):
        request(tmp_path, sys.executable, timeout_seconds=0)
    with pytest.raises(EvidenceRunnerError, match="duplicate"):
        request(
            tmp_path,
            sys.executable,
            environment=(("A", "1"), ("A", "2")),
        )


def test_result_metadata_must_agree_with_terminal_evidence(tmp_path: Path) -> None:
    passed_result = LocalEvidenceRunner(PRODUCER).run(
        binding(tmp_path), request(tmp_path, sys.executable, "-c", "pass")
    )
    with pytest.raises(EvidenceRunnerError, match="exit status zero"):
        CommandEvidenceResult(
            evidence=passed_result.evidence,
            exit_code=1,
            duration_seconds=0,
            stdout_digest="a" * 64,
            stderr_digest="b" * 64,
        )
    with pytest.raises(EvidenceRunnerError, match="SHA-256"):
        CommandEvidenceResult(
            evidence=passed_result.evidence,
            exit_code=0,
            duration_seconds=0,
            stdout_digest="bad",
            stderr_digest="b" * 64,
        )
