"""Read-only GitHub implementation of the provider-neutral ForgePort."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from weftmark.application.ports.forge import (
    ForgeActor,
    ForgeAvailability,
    ForgeChangedFile,
    ForgeChangeRequest,
    ForgeChangeState,
    ForgeCheck,
    ForgeComment,
    ForgeCommentKind,
    ForgeConclusion,
    ForgePort,
    ForgeRepository,
    ForgeResult,
    ForgeReview,
    ForgeReviewState,
    ForgeRunStatus,
    ForgeWorkflowRun,
)
from weftmark.application.ports.git import GitChangeKind, GitDiffEntry, GitObjectId


class GithubAdapterError(ValueError):
    """Raised for invalid local GitHub adapter configuration."""


class GithubTransportError(RuntimeError):
    """Raised when the configured HTTP transport cannot make an observation."""


@dataclass(frozen=True, slots=True)
class GithubHttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class GithubTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str]) -> GithubHttpResponse:
        """Perform one GET without interpreting GitHub semantics."""


class UrlLibGithubTransport:
    """Small stdlib transport so the adapter does not require a GitHub SDK."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise GithubAdapterError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def get(self, url: str, *, headers: Mapping[str, str]) -> GithubHttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return GithubHttpResponse(
                    response.status,
                    response.read(),
                    dict(response.headers.items()),
                )
        except HTTPError as error:
            return GithubHttpResponse(
                error.code,
                error.read(),
                dict(error.headers.items()) if error.headers is not None else {},
            )
        except (URLError, TimeoutError, socket.timeout, OSError) as error:
            raise GithubTransportError("GitHub transport unavailable") from error


class GithubForgeAdapter(ForgePort):
    """Observe one GitHub repository without mutating forge state."""

    def __init__(
        self,
        repository: str,
        *,
        token: str | None = None,
        api_base: str = "https://api.github.com",
        web_base: str = "https://github.com",
        transport: GithubTransport | None = None,
    ) -> None:
        parts = repository.strip().split("/")
        if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
            raise GithubAdapterError("repository must be owner/name")
        if any("\x00" in part for part in parts):
            raise GithubAdapterError("repository must be NUL-free")
        if token is not None:
            token = token.strip()
            if not token or any(character in token for character in "\r\n\x00"):
                raise GithubAdapterError("token must be non-empty and header-safe")
        self._repository = "/".join(parts)
        self._token = token
        self._api_base = _base_url("api_base", api_base)
        self._web_base = _base_url("web_base", web_base)
        self._transport = transport or UrlLibGithubTransport()

    def repository(self) -> ForgeRepository:
        return ForgeRepository(
            provider="github",
            id=self._repository,
            web_url=f"{self._web_base}/{self._repository}",
        )

    def change_request(self, external_id: str) -> ForgeResult[ForgeChangeRequest]:
        number = _pull_number(external_id)
        result = self._get_json(f"/repos/{self._repository}/pulls/{number}")
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            payload = _mapping(result.value)
            merged_at = _time(payload.get("merged_at"))
            state = (
                ForgeChangeState.MERGED
                if merged_at is not None
                else ForgeChangeState(str(payload["state"]))
            )
            return ForgeResult.available(
                ForgeChangeRequest(
                    external_id=str(payload["number"]),
                    title=str(payload["title"]),
                    state=state,
                    source_branch=str(_mapping(payload["head"])["ref"]),
                    target_branch=str(_mapping(payload["base"])["ref"]),
                    head=GitObjectId(str(_mapping(payload["head"])["sha"])),
                    base=GitObjectId(str(_mapping(payload["base"])["sha"])),
                    web_url=str(payload["html_url"]),
                    author=_actor(payload["user"]),
                    draft=bool(payload.get("draft", False)),
                    updated_at=_required_time(payload["updated_at"]),
                    merged_at=merged_at,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            return ForgeResult.unavailable("GitHub returned malformed change-request data")

    def checks(self, head: GitObjectId) -> ForgeResult[tuple[ForgeCheck, ...]]:
        result = self._paged_object_list(
            f"/repos/{self._repository}/commits/{head}/check-runs",
            key="check_runs",
            query={"filter": "latest"},
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        if not result.value:
            return ForgeResult.missing("no checks reported for commit")
        try:
            values = tuple(_check(payload) for payload in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("GitHub returned malformed check data")
        return ForgeResult.available(tuple(sorted(values, key=lambda value: (value.name, value.external_id))))

    def workflow_runs(
        self, head: GitObjectId
    ) -> ForgeResult[tuple[ForgeWorkflowRun, ...]]:
        result = self._paged_object_list(
            f"/repos/{self._repository}/actions/runs",
            key="workflow_runs",
            query={"head_sha": str(head)},
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        if not result.value:
            return ForgeResult.missing("no workflow runs reported for commit")
        try:
            values = tuple(_workflow(payload) for payload in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("GitHub returned malformed workflow-run data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.name, value.external_id)))
        )

    def reviews(self, external_id: str) -> ForgeResult[tuple[ForgeReview, ...]]:
        number = _pull_number(external_id)
        result = self._paged_list(f"/repos/{self._repository}/pulls/{number}/reviews")
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            values = tuple(_review(payload) for payload in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("GitHub returned malformed review data")
        return ForgeResult.available(values)

    def comments(self, external_id: str) -> ForgeResult[tuple[ForgeComment, ...]]:
        number = _pull_number(external_id)
        general = self._paged_list(f"/repos/{self._repository}/issues/{number}/comments")
        if general.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(general.availability, detail=general.detail)
        review = self._paged_list(f"/repos/{self._repository}/pulls/{number}/comments")
        if review.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(review.availability, detail=review.detail)
        try:
            values = tuple(
                [*(_comment(payload, ForgeCommentKind.GENERAL) for payload in general.value),
                 *(_comment(payload, ForgeCommentKind.REVIEW) for payload in review.value)]
            )
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("GitHub returned malformed comment data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.created_at, value.external_id)))
        )

    def changed_files(
        self, external_id: str
    ) -> ForgeResult[tuple[ForgeChangedFile, ...]]:
        number = _pull_number(external_id)
        result = self._paged_list(f"/repos/{self._repository}/pulls/{number}/files")
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            values = tuple(_changed_file(payload) for payload in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("GitHub returned malformed changed-file data")
        return ForgeResult.available(tuple(sorted(values, key=lambda value: value.entry.path)))

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "WeftMark/0.0.1",
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get_json(
        self, path: str, *, query: Mapping[str, str] | None = None
    ) -> ForgeResult[Any]:
        suffix = "" if not query else "?" + urlencode(query)
        url = f"{self._api_base}{path}{suffix}"
        try:
            response = self._transport.get(url, headers=self._headers())
        except GithubTransportError:
            return ForgeResult.unavailable("GitHub transport unavailable")
        if response.status == 404:
            return ForgeResult.missing("GitHub resource not found")
        if response.status < 200 or response.status >= 300:
            return ForgeResult.unavailable(f"GitHub API unavailable (HTTP {response.status})")
        try:
            return ForgeResult.available(json.loads(response.body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ForgeResult.unavailable("GitHub returned invalid JSON")

    def _paged_list(self, path: str) -> ForgeResult[tuple[Mapping[str, Any], ...]]:
        values: list[Mapping[str, Any]] = []
        for page in range(1, 101):
            result = self._get_json(path, query={"per_page": "100", "page": str(page)})
            if result.availability is not ForgeAvailability.AVAILABLE:
                return ForgeResult(result.availability, detail=result.detail)
            if not isinstance(result.value, list):
                return ForgeResult.unavailable("GitHub returned malformed paginated data")
            try:
                batch = tuple(_mapping(value) for value in result.value)
            except TypeError:
                return ForgeResult.unavailable("GitHub returned malformed paginated data")
            values.extend(batch)
            if len(batch) < 100:
                return ForgeResult.available(tuple(values))
        return ForgeResult.unavailable("GitHub pagination exceeded safety limit")

    def _paged_object_list(
        self,
        path: str,
        *,
        key: str,
        query: Mapping[str, str] | None = None,
    ) -> ForgeResult[tuple[Mapping[str, Any], ...]]:
        values: list[Mapping[str, Any]] = []
        base_query = dict(query or {})
        for page in range(1, 101):
            params = {**base_query, "per_page": "100", "page": str(page)}
            result = self._get_json(path, query=params)
            if result.availability is not ForgeAvailability.AVAILABLE:
                return ForgeResult(result.availability, detail=result.detail)
            try:
                payload = _mapping(result.value)
                raw = payload[key]
                if not isinstance(raw, list):
                    raise TypeError
                batch = tuple(_mapping(value) for value in raw)
            except (KeyError, TypeError):
                return ForgeResult.unavailable("GitHub returned malformed paginated data")
            values.extend(batch)
            if len(batch) < 100:
                return ForgeResult.available(tuple(values))
        return ForgeResult.unavailable("GitHub pagination exceeded safety limit")


def _base_url(name: str, value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized.startswith(("https://", "http://")) or any(
        character in normalized for character in "\r\n\x00"
    ):
        raise GithubAdapterError(f"{name} must be an absolute HTTP(S) URL")
    return normalized


def _pull_number(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise GithubAdapterError("GitHub change-request id must be a positive integer") from error
    if number < 1:
        raise GithubAdapterError("GitHub change-request id must be a positive integer")
    return number


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("expected object")
    return value


def _time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expected timestamp string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp lacks timezone")
    return parsed


def _required_time(value: Any) -> datetime:
    parsed = _time(value)
    if parsed is None:
        raise ValueError("timestamp is required")
    return parsed


def _actor(value: Any) -> ForgeActor:
    payload = _mapping(value)
    return ForgeActor(f"github-user:{payload['id']}", str(payload["login"]))


def _run_status(value: Any) -> ForgeRunStatus:
    normalized = str(value).lower()
    return {
        "queued": ForgeRunStatus.QUEUED,
        "in_progress": ForgeRunStatus.IN_PROGRESS,
        "completed": ForgeRunStatus.COMPLETED,
        "waiting": ForgeRunStatus.WAITING,
        "pending": ForgeRunStatus.PENDING,
        "requested": ForgeRunStatus.REQUESTED,
    }.get(normalized, ForgeRunStatus.UNKNOWN)


def _conclusion(value: Any, *, completed: bool) -> ForgeConclusion | None:
    if not completed:
        return None
    normalized = "unknown" if value is None else str(value).lower()
    return {
        "success": ForgeConclusion.PASSED,
        "failure": ForgeConclusion.FAILED,
        "cancelled": ForgeConclusion.CANCELLED,
        "skipped": ForgeConclusion.SKIPPED,
        "neutral": ForgeConclusion.NEUTRAL,
        "action_required": ForgeConclusion.ACTION_REQUIRED,
        "stale": ForgeConclusion.STALE,
        "timed_out": ForgeConclusion.TIMED_OUT,
        "startup_failure": ForgeConclusion.STARTUP_FAILURE,
    }.get(normalized, ForgeConclusion.UNKNOWN)


def _check(value: Mapping[str, Any]) -> ForgeCheck:
    status = _run_status(value["status"])
    return ForgeCheck(
        external_id=str(value["id"]),
        name=str(value["name"]),
        status=status,
        conclusion=_conclusion(value.get("conclusion"), completed=status is ForgeRunStatus.COMPLETED),
        head=GitObjectId(str(value["head_sha"])),
        details_url=None if value.get("details_url") is None else str(value["details_url"]),
        started_at=_time(value.get("started_at")),
        completed_at=_time(value.get("completed_at")),
    )


def _workflow(value: Mapping[str, Any]) -> ForgeWorkflowRun:
    status = _run_status(value["status"])
    return ForgeWorkflowRun(
        external_id=str(value["id"]),
        name=str(value["name"]),
        event=str(value["event"]),
        status=status,
        conclusion=_conclusion(value.get("conclusion"), completed=status is ForgeRunStatus.COMPLETED),
        head=GitObjectId(str(value["head_sha"])),
        web_url=str(value["html_url"]),
        started_at=_time(value.get("run_started_at") or value.get("created_at")),
        completed_at=(
            _time(value.get("updated_at")) if status is ForgeRunStatus.COMPLETED else None
        ),
    )


def _review(value: Mapping[str, Any]) -> ForgeReview:
    state = {
        "PENDING": ForgeReviewState.PENDING,
        "COMMENTED": ForgeReviewState.COMMENTED,
        "APPROVED": ForgeReviewState.APPROVED,
        "CHANGES_REQUESTED": ForgeReviewState.CHANGES_REQUESTED,
        "DISMISSED": ForgeReviewState.DISMISSED,
    }.get(str(value["state"]).upper(), ForgeReviewState.UNKNOWN)
    commit = value.get("commit_id")
    return ForgeReview(
        external_id=str(value["id"]),
        author=_actor(value["user"]),
        state=state,
        body="" if value.get("body") is None else str(value["body"]),
        submitted_at=_time(value.get("submitted_at")),
        commit=None if commit is None else GitObjectId(str(commit)),
    )


def _comment(value: Mapping[str, Any], kind: ForgeCommentKind) -> ForgeComment:
    path = None
    line = None
    if kind is ForgeCommentKind.REVIEW:
        path = None if value.get("path") is None else str(value["path"])
        raw_line = value.get("line")
        line = None if raw_line is None else int(raw_line)
    return ForgeComment(
        external_id=str(value["id"]),
        author=_actor(value["user"]),
        kind=kind,
        body="" if value.get("body") is None else str(value["body"]),
        created_at=_required_time(value["created_at"]),
        updated_at=_required_time(value["updated_at"]),
        web_url=str(value["html_url"]),
        path=path,
        line=line,
    )


def _changed_file(value: Mapping[str, Any]) -> ForgeChangedFile:
    status = str(value["status"]).lower()
    kind = {
        "added": GitChangeKind.ADDED,
        "modified": GitChangeKind.MODIFIED,
        "removed": GitChangeKind.DELETED,
        "renamed": GitChangeKind.RENAMED,
        "copied": GitChangeKind.COPIED,
        "changed": GitChangeKind.TYPE_CHANGED,
        "unchanged": GitChangeKind.MODIFIED,
    }.get(status)
    if kind is None:
        raise ValueError("unknown GitHub file status")
    old_path = None
    if kind in {GitChangeKind.RENAMED, GitChangeKind.COPIED}:
        old_path = str(value["previous_filename"])
    return ForgeChangedFile(
        entry=GitDiffEntry(path=str(value["filename"]), kind=kind, old_path=old_path),
        additions=int(value["additions"]),
        deletions=int(value["deletions"]),
    )
