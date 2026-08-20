"""Read-only GitLab implementation of the provider-neutral ForgePort."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from weftmark.application.ports.forge import (
    ForgeActor,
    ForgeAvailability,
    ForgeCapabilities,
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


class GitlabAdapterError(ValueError):
    """Raised for invalid local GitLab adapter configuration."""


class GitlabTransportError(RuntimeError):
    """Raised when the configured HTTP transport cannot make an observation."""


@dataclass(frozen=True, slots=True)
class GitlabHttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class GitlabTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str]) -> GitlabHttpResponse:
        """Perform one GET without interpreting GitLab semantics."""


class UrlLibGitlabTransport:
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise GitlabAdapterError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def get(self, url: str, *, headers: Mapping[str, str]) -> GitlabHttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return GitlabHttpResponse(
                    response.status,
                    response.read(),
                    dict(response.headers.items()),
                )
        except HTTPError as error:
            return GitlabHttpResponse(
                error.code,
                error.read(),
                dict(error.headers.items()) if error.headers is not None else {},
            )
        except (URLError, TimeoutError, socket.timeout, OSError) as error:
            raise GitlabTransportError("GitLab transport unavailable") from error


class GitlabForgeAdapter(ForgePort):
    """Observe one GitLab project without mutating forge state."""

    def __init__(
        self,
        project: str,
        *,
        token: str | None = None,
        api_base: str = "https://gitlab.com/api/v4",
        web_base: str = "https://gitlab.com",
        transport: GitlabTransport | None = None,
    ) -> None:
        parts = project.strip().split("/")
        if len(parts) < 2 or any(not part or part in {".", ".."} for part in parts):
            raise GitlabAdapterError("project must be a namespace/project path")
        if any("\x00" in part for part in parts):
            raise GitlabAdapterError("project must be NUL-free")
        if token is not None:
            token = token.strip()
            if not token or any(character in token for character in "\r\n\x00"):
                raise GitlabAdapterError("token must be non-empty and header-safe")
        self._project = "/".join(parts)
        self._project_id = quote(self._project, safe="")
        self._token = token
        self._api_base = _base_url("api_base", api_base)
        self._web_base = _base_url("web_base", web_base)
        self._transport = transport or UrlLibGitlabTransport()

    def repository(self) -> ForgeRepository:
        return ForgeRepository(
            provider="gitlab",
            id=self._project,
            web_url=f"{self._web_base}/{self._project}",
        )

    def capabilities(self) -> ForgeCapabilities:
        return ForgeCapabilities()

    def change_request(self, external_id: str) -> ForgeResult[ForgeChangeRequest]:
        iid = _merge_request_iid(external_id)
        result = self._get_json(
            f"/projects/{self._project_id}/merge_requests/{iid}"
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            payload = _mapping(result.value)
            merged_at = _time(payload.get("merged_at"))
            raw_state = str(payload["state"]).lower()
            state = (
                ForgeChangeState.MERGED
                if raw_state == "merged" or merged_at is not None
                else ForgeChangeState.OPEN
                if raw_state in {"opened", "reopened"}
                else ForgeChangeState.CLOSED
            )
            diff_refs = _mapping(payload["diff_refs"])
            return ForgeResult.available(
                ForgeChangeRequest(
                    external_id=str(payload["iid"]),
                    title=str(payload["title"]),
                    state=state,
                    source_branch=str(payload["source_branch"]),
                    target_branch=str(payload["target_branch"]),
                    head=GitObjectId(str(diff_refs["head_sha"])),
                    base=GitObjectId(str(diff_refs["base_sha"])),
                    web_url=str(payload["web_url"]),
                    author=_actor(payload["author"]),
                    draft=bool(
                        payload.get("draft", payload.get("work_in_progress", False))
                    ),
                    updated_at=_required_time(payload["updated_at"]),
                    merged_at=merged_at,
                )
            )
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                "GitLab returned malformed change-request data"
            )

    def checks(self, head: GitObjectId) -> ForgeResult[tuple[ForgeCheck, ...]]:
        result = self._paged_list(
            f"/projects/{self._project_id}/repository/commits/{head}/statuses",
            query={"all": "true"},
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        if not result.value:
            return ForgeResult.missing("no checks reported for commit")
        try:
            values = tuple(_check(value, head) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("GitLab returned malformed check data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.name, value.external_id)))
        )

    def workflow_runs(
        self, head: GitObjectId
    ) -> ForgeResult[tuple[ForgeWorkflowRun, ...]]:
        result = self._paged_list(
            f"/projects/{self._project_id}/pipelines",
            query={"sha": str(head)},
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        if not result.value:
            return ForgeResult.missing("no pipelines reported for commit")
        try:
            values = tuple(_pipeline(value) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("GitLab returned malformed pipeline data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: value.external_id))
        )

    def reviews(self, external_id: str) -> ForgeResult[tuple[ForgeReview, ...]]:
        iid = _merge_request_iid(external_id)
        result = self._get_json(
            f"/projects/{self._project_id}/merge_requests/{iid}/approvals"
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            payload = _mapping(result.value)
            raw = payload.get("approved_by", [])
            if not isinstance(raw, list):
                raise TypeError
            values = tuple(_approval(value) for value in raw)
        except (TypeError, ValueError):
            return ForgeResult.unavailable("GitLab returned malformed approval data")
        return ForgeResult.available(values)

    def comments(self, external_id: str) -> ForgeResult[tuple[ForgeComment, ...]]:
        iid = _merge_request_iid(external_id)
        result = self._paged_list(
            f"/projects/{self._project_id}/merge_requests/{iid}/discussions"
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            values: list[ForgeComment] = []
            for discussion in result.value:
                notes = _mapping(discussion).get("notes", [])
                if not isinstance(notes, list):
                    raise TypeError
                for note in notes:
                    payload = _mapping(note)
                    if bool(payload.get("system", False)):
                        continue
                    values.append(_comment(payload, self._project, iid, self._web_base))
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("GitLab returned malformed discussion data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.created_at, value.external_id)))
        )

    def changed_files(
        self, external_id: str
    ) -> ForgeResult[tuple[ForgeChangedFile, ...]]:
        iid = _merge_request_iid(external_id)
        result = self._paged_list(
            f"/projects/{self._project_id}/merge_requests/{iid}/diffs"
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            if any(
                bool(value.get("too_large", False)) or bool(value.get("collapsed", False))
                for value in result.value
            ):
                return ForgeResult.unavailable(
                    "GitLab merge-request diff is truncated"
                )
            values = tuple(_changed_file(value) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("GitLab returned malformed diff data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: value.entry.path))
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "WeftMark/0.0.1",
        }
        if self._token is not None:
            headers["PRIVATE-TOKEN"] = self._token
        return headers

    def _get_json(
        self, path: str, *, query: Mapping[str, str] | None = None
    ) -> ForgeResult[Any]:
        suffix = "" if not query else "?" + urlencode(query)
        url = f"{self._api_base}{path}{suffix}"
        try:
            response = self._transport.get(url, headers=self._headers())
        except GitlabTransportError:
            return ForgeResult.unavailable("GitLab transport unavailable")
        if response.status == 404:
            return ForgeResult.missing("GitLab resource not found")
        if response.status < 200 or response.status >= 300:
            return ForgeResult.unavailable(
                f"GitLab API unavailable (HTTP {response.status})"
            )
        try:
            return ForgeResult.available(json.loads(response.body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ForgeResult.unavailable("GitLab returned invalid JSON")

    def _paged_list(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
    ) -> ForgeResult[tuple[Mapping[str, Any], ...]]:
        values: list[Mapping[str, Any]] = []
        base_query = dict(query or {})
        for page in range(1, 101):
            params = {**base_query, "per_page": "100", "page": str(page)}
            result = self._get_json(path, query=params)
            if result.availability is not ForgeAvailability.AVAILABLE:
                return ForgeResult(result.availability, detail=result.detail)
            if not isinstance(result.value, list):
                return ForgeResult.unavailable("GitLab returned malformed paginated data")
            try:
                batch = tuple(_mapping(value) for value in result.value)
            except TypeError:
                return ForgeResult.unavailable("GitLab returned malformed paginated data")
            values.extend(batch)
            if len(batch) < 100:
                return ForgeResult.available(tuple(values))
        return ForgeResult.unavailable("GitLab pagination exceeded safety limit")


def _base_url(name: str, value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized.startswith(("https://", "http://")) or any(
        character in normalized for character in "\r\n\x00"
    ):
        raise GitlabAdapterError(f"{name} must be an absolute HTTP(S) URL")
    return normalized


def _merge_request_iid(value: str) -> int:
    try:
        iid = int(value)
    except (TypeError, ValueError) as error:
        raise GitlabAdapterError(
            "GitLab change-request id must be a positive integer"
        ) from error
    if iid < 1:
        raise GitlabAdapterError("GitLab change-request id must be a positive integer")
    return iid


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
    return ForgeActor(
        f"gitlab-user:{payload['id']}",
        str(payload.get("username") or payload.get("name")),
    )


def _status(value: Any) -> tuple[ForgeRunStatus, ForgeConclusion | None]:
    normalized = str(value).lower()
    terminal = {
        "success": ForgeConclusion.PASSED,
        "failed": ForgeConclusion.FAILED,
        "canceled": ForgeConclusion.CANCELLED,
        "cancelled": ForgeConclusion.CANCELLED,
        "skipped": ForgeConclusion.SKIPPED,
    }
    if normalized in terminal:
        return ForgeRunStatus.COMPLETED, terminal[normalized]
    return (
        {
            "running": ForgeRunStatus.IN_PROGRESS,
            "pending": ForgeRunStatus.PENDING,
            "created": ForgeRunStatus.QUEUED,
            "preparing": ForgeRunStatus.QUEUED,
            "scheduled": ForgeRunStatus.WAITING,
            "manual": ForgeRunStatus.WAITING,
            "waiting_for_callback": ForgeRunStatus.WAITING,
            "waiting_for_resource": ForgeRunStatus.WAITING,
            "canceling": ForgeRunStatus.IN_PROGRESS,
        }.get(normalized, ForgeRunStatus.UNKNOWN),
        None,
    )


def _check(value: Mapping[str, Any], head: GitObjectId) -> ForgeCheck:
    status, conclusion = _status(value["status"])
    return ForgeCheck(
        external_id=str(value["id"]),
        name=str(value.get("name") or value.get("context") or "default"),
        status=status,
        conclusion=conclusion,
        head=head,
        details_url=None
        if value.get("target_url") is None
        else str(value["target_url"]),
        started_at=_time(value.get("started_at") or value.get("created_at")),
        completed_at=_time(value.get("finished_at")),
    )


def _pipeline(value: Mapping[str, Any]) -> ForgeWorkflowRun:
    status, conclusion = _status(value["status"])
    completed_at = (
        _time(value.get("updated_at"))
        if status is ForgeRunStatus.COMPLETED
        else None
    )
    return ForgeWorkflowRun(
        external_id=str(value["id"]),
        name=str(value.get("name") or f"pipeline-{value['id']}"),
        event=str(value.get("source") or "unknown"),
        status=status,
        conclusion=conclusion,
        head=GitObjectId(str(value["sha"])),
        web_url=str(value["web_url"]),
        started_at=_time(value.get("created_at")),
        completed_at=completed_at,
    )


def _approval(value: Any) -> ForgeReview:
    payload = _mapping(value)
    user = _mapping(payload.get("user", payload))
    actor = _actor(user)
    return ForgeReview(
        external_id=f"approval:{actor.id}",
        author=actor,
        state=ForgeReviewState.APPROVED,
        body="GitLab approval",
        submitted_at=_time(payload.get("approved_at")),
    )


def _comment(
    value: Mapping[str, Any], project: str, iid: int, web_base: str
) -> ForgeComment:
    position = value.get("position")
    path = None
    line = None
    kind = ForgeCommentKind.GENERAL
    if isinstance(position, Mapping):
        kind = ForgeCommentKind.REVIEW
        path_value = position.get("new_path") or position.get("old_path")
        path = None if path_value is None else str(path_value)
        line_value = position.get("new_line") or position.get("old_line")
        line = None if line_value is None else int(line_value)
    note_id = str(value["id"])
    return ForgeComment(
        external_id=note_id,
        author=_actor(value["author"]),
        kind=kind,
        body=str(value.get("body") or ""),
        created_at=_required_time(value["created_at"]),
        updated_at=_required_time(value["updated_at"]),
        web_url=f"{web_base}/{project}/-/merge_requests/{iid}#note_{note_id}",
        path=path,
        line=line,
    )


def _changed_file(value: Mapping[str, Any]) -> ForgeChangedFile:
    if bool(value.get("renamed_file", False)):
        kind = GitChangeKind.RENAMED
        old_path = str(value["old_path"])
    elif bool(value.get("new_file", False)):
        kind = GitChangeKind.ADDED
        old_path = None
    elif bool(value.get("deleted_file", False)):
        kind = GitChangeKind.DELETED
        old_path = None
    else:
        kind = GitChangeKind.MODIFIED
        old_path = None
    additions, deletions = _diff_counts(str(value.get("diff") or ""))
    return ForgeChangedFile(
        entry=GitDiffEntry(
            path=str(value["new_path"]),
            kind=kind,
            old_path=old_path,
        ),
        additions=additions,
        deletions=deletions,
    )


def _diff_counts(diff: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions
