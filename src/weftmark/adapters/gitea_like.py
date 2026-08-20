"""Shared read-side mapping for Gitea-family forge APIs.

Concrete Gitea and Forgejo adapters remain separate public classes. This module
only shares behavior that both fixture suites prove compatible.
"""

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


class GiteaLikeAdapterError(ValueError):
    """Raised for invalid local adapter configuration."""


class GiteaLikeTransportError(RuntimeError):
    """Raised when the configured HTTP transport cannot make an observation."""


@dataclass(frozen=True, slots=True)
class GiteaLikeHttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class GiteaLikeTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str]) -> GiteaLikeHttpResponse:
        """Perform one GET without interpreting provider semantics."""


class UrlLibGiteaLikeTransport:
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise GiteaLikeAdapterError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def get(self, url: str, *, headers: Mapping[str, str]) -> GiteaLikeHttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return GiteaLikeHttpResponse(
                    response.status,
                    response.read(),
                    dict(response.headers.items()),
                )
        except HTTPError as error:
            return GiteaLikeHttpResponse(
                error.code,
                error.read(),
                dict(error.headers.items()) if error.headers is not None else {},
            )
        except (URLError, TimeoutError, socket.timeout, OSError) as error:
            raise GiteaLikeTransportError("forge transport unavailable") from error


class GiteaLikeForgeAdapter(ForgePort):
    """Internal common implementation; public adapters select the provider dialect."""

    provider = "gitea-like"

    def __init__(
        self,
        repository: str,
        *,
        token: str | None = None,
        api_base: str,
        web_base: str,
        actions_supported: bool = True,
        transport: GiteaLikeTransport | None = None,
    ) -> None:
        parts = repository.strip().split("/")
        if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
            raise GiteaLikeAdapterError("repository must be owner/name")
        if any("\x00" in part for part in parts):
            raise GiteaLikeAdapterError("repository must be NUL-free")
        if token is not None:
            token = token.strip()
            if not token or any(character in token for character in "\r\n\x00"):
                raise GiteaLikeAdapterError("token must be non-empty and header-safe")
        self._owner, self._repo = parts
        self._repository = "/".join(parts)
        self._token = token
        self._api_base = _base_url("api_base", api_base)
        self._web_base = _base_url("web_base", web_base)
        self._actions_supported = bool(actions_supported)
        self._transport = transport or UrlLibGiteaLikeTransport()

    def repository(self) -> ForgeRepository:
        return ForgeRepository(
            provider=self.provider,
            id=self._repository,
            web_url=f"{self._web_base}/{self._repository}",
        )

    def capabilities(self) -> ForgeCapabilities:
        return ForgeCapabilities(workflow_runs=self._actions_supported)

    def change_request(self, external_id: str) -> ForgeResult[ForgeChangeRequest]:
        index = _pull_index(external_id)
        result = self._get_json(self._repo_path(f"pulls/{index}"))
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            payload = _mapping(result.value)
            merged = bool(payload.get("merged", False))
            merged_at = _time(payload.get("merged_at"))
            state = (
                ForgeChangeState.MERGED
                if merged
                else ForgeChangeState.OPEN
                if str(payload["state"]).lower() == "open"
                else ForgeChangeState.CLOSED
            )
            if state is ForgeChangeState.MERGED and merged_at is None:
                return ForgeResult.unavailable(
                    f"{self.provider} returned merged change without merged_at"
                )
            head = _mapping(payload["head"])
            base = _mapping(payload["base"])
            return ForgeResult.available(
                ForgeChangeRequest(
                    external_id=str(payload.get("number", payload.get("index", index))),
                    title=str(payload["title"]),
                    state=state,
                    source_branch=str(head["ref"]),
                    target_branch=str(base["ref"]),
                    head=GitObjectId(str(head["sha"])),
                    base=GitObjectId(str(base["sha"])),
                    web_url=str(payload["html_url"]),
                    author=_actor(payload["user"], self.provider),
                    draft=bool(payload.get("draft", False)),
                    updated_at=_required_time(payload["updated_at"]),
                    merged_at=merged_at,
                )
            )
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                f"{self.provider} returned malformed change-request data"
            )

    def checks(self, head: GitObjectId) -> ForgeResult[tuple[ForgeCheck, ...]]:
        result = self._paged_list(
            self._repo_path(f"commits/{head}/statuses"),
            query={"sort": "recentupdate"},
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        if not result.value:
            return ForgeResult.missing("no commit statuses reported for commit")
        try:
            values = tuple(_check(value, head) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                f"{self.provider} returned malformed commit-status data"
            )
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.name, value.external_id)))
        )

    def workflow_runs(
        self, head: GitObjectId
    ) -> ForgeResult[tuple[ForgeWorkflowRun, ...]]:
        if not self._actions_supported:
            return ForgeResult.unsupported(
                f"{self.provider} Actions are disabled or unsupported for this instance"
            )
        result = self._paged_object_list(
            self._repo_path("actions/runs"),
            key="workflow_runs",
            query={"head_sha": str(head)},
            not_found_is_unsupported=True,
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        if not result.value:
            return ForgeResult.missing("no workflow runs reported for commit")
        try:
            values = tuple(_workflow(value) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                f"{self.provider} returned malformed workflow-run data"
            )
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: value.external_id))
        )

    def reviews(self, external_id: str) -> ForgeResult[tuple[ForgeReview, ...]]:
        index = _pull_index(external_id)
        result = self._paged_list(self._repo_path(f"pulls/{index}/reviews"))
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            values = tuple(_review(value, self.provider) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                f"{self.provider} returned malformed review data"
            )
        return ForgeResult.available(values)

    def comments(self, external_id: str) -> ForgeResult[tuple[ForgeComment, ...]]:
        index = _pull_index(external_id)
        general = self._paged_list(self._repo_path(f"issues/{index}/comments"))
        if general.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(general.availability, detail=general.detail)
        reviews = self._paged_list(self._repo_path(f"pulls/{index}/reviews"))
        if reviews.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(reviews.availability, detail=reviews.detail)
        values: list[ForgeComment] = []
        try:
            values.extend(
                _general_comment(value, self.provider) for value in general.value
            )
            for review in reviews.value:
                review_id = str(review["id"])
                comments = self._paged_list(
                    self._repo_path(f"pulls/{index}/reviews/{review_id}/comments")
                )
                if comments.availability is not ForgeAvailability.AVAILABLE:
                    return ForgeResult(comments.availability, detail=comments.detail)
                values.extend(
                    _review_comment(value, self.provider) for value in comments.value
                )
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                f"{self.provider} returned malformed comment data"
            )
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.created_at, value.external_id)))
        )

    def changed_files(
        self, external_id: str
    ) -> ForgeResult[tuple[ForgeChangedFile, ...]]:
        index = _pull_index(external_id)
        result = self._paged_list(self._repo_path(f"pulls/{index}/files"))
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            values = tuple(_changed_file(value) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                f"{self.provider} returned malformed changed-file data"
            )
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: value.entry.path))
        )

    def _repo_path(self, suffix: str) -> str:
        return f"/repos/{self._owner}/{self._repo}/{suffix}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "WeftMark/0.0.1"}
        if self._token is not None:
            headers["Authorization"] = f"token {self._token}"
        return headers

    def _get_json(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        not_found_is_unsupported: bool = False,
    ) -> ForgeResult[Any]:
        suffix = "" if not query else "?" + urlencode(query)
        try:
            response = self._transport.get(
                f"{self._api_base}{path}{suffix}", headers=self._headers()
            )
        except GiteaLikeTransportError:
            return ForgeResult.unavailable(f"{self.provider} transport unavailable")
        if response.status == 404:
            if not_found_is_unsupported:
                return ForgeResult.unsupported(
                    f"{self.provider} endpoint unsupported by configured instance"
                )
            return ForgeResult.missing(f"{self.provider} resource not found")
        if response.status < 200 or response.status >= 300:
            return ForgeResult.unavailable(
                f"{self.provider} API unavailable (HTTP {response.status})"
            )
        try:
            return ForgeResult.available(json.loads(response.body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ForgeResult.unavailable(f"{self.provider} returned invalid JSON")

    def _paged_list(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
    ) -> ForgeResult[tuple[Mapping[str, Any], ...]]:
        values: list[Mapping[str, Any]] = []
        base_query = dict(query or {})
        for page in range(1, 101):
            params = {**base_query, "page": str(page), "limit": "100"}
            result = self._get_json(path, query=params)
            if result.availability is not ForgeAvailability.AVAILABLE:
                return ForgeResult(result.availability, detail=result.detail)
            if not isinstance(result.value, list):
                return ForgeResult.unavailable(
                    f"{self.provider} returned malformed paginated data"
                )
            try:
                batch = tuple(_mapping(value) for value in result.value)
            except TypeError:
                return ForgeResult.unavailable(
                    f"{self.provider} returned malformed paginated data"
                )
            values.extend(batch)
            if len(batch) < 100:
                return ForgeResult.available(tuple(values))
        return ForgeResult.unavailable(
            f"{self.provider} pagination exceeded safety limit"
        )

    def _paged_object_list(
        self,
        path: str,
        *,
        key: str,
        query: Mapping[str, str] | None = None,
        not_found_is_unsupported: bool = False,
    ) -> ForgeResult[tuple[Mapping[str, Any], ...]]:
        values: list[Mapping[str, Any]] = []
        base_query = dict(query or {})
        for page in range(1, 101):
            params = {**base_query, "page": str(page), "limit": "100"}
            result = self._get_json(
                path,
                query=params,
                not_found_is_unsupported=not_found_is_unsupported,
            )
            if result.availability is not ForgeAvailability.AVAILABLE:
                return ForgeResult(result.availability, detail=result.detail)
            try:
                payload = _mapping(result.value)
                raw = payload[key]
                if not isinstance(raw, list):
                    raise TypeError
                batch = tuple(_mapping(value) for value in raw)
            except (KeyError, TypeError):
                return ForgeResult.unavailable(
                    f"{self.provider} returned malformed paginated data"
                )
            values.extend(batch)
            if len(batch) < 100:
                return ForgeResult.available(tuple(values))
        return ForgeResult.unavailable(
            f"{self.provider} pagination exceeded safety limit"
        )


def _base_url(name: str, value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized.startswith(("https://", "http://")) or any(
        character in normalized for character in "\r\n\x00"
    ):
        raise GiteaLikeAdapterError(f"{name} must be an absolute HTTP(S) URL")
    return normalized


def _pull_index(value: str) -> int:
    try:
        index = int(value)
    except (TypeError, ValueError) as error:
        raise GiteaLikeAdapterError(
            "change-request id must be a positive integer"
        ) from error
    if index < 1:
        raise GiteaLikeAdapterError("change-request id must be a positive integer")
    return index


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("expected object")
    return value


def _time(value: Any) -> datetime | None:
    if value in {None, ""}:
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


def _actor(value: Any, provider: str) -> ForgeActor:
    payload = _mapping(value)
    login = payload.get("login") or payload.get("username")
    if login is None:
        raise ValueError("actor login missing")
    return ForgeActor(f"{provider}-user:{payload['id']}", str(login))


def _status(value: Any) -> tuple[ForgeRunStatus, ForgeConclusion | None]:
    normalized = str(value).lower()
    terminal = {
        "success": ForgeConclusion.PASSED,
        "failure": ForgeConclusion.FAILED,
        "error": ForgeConclusion.FAILED,
        "skipped": ForgeConclusion.SKIPPED,
        "warning": ForgeConclusion.NEUTRAL,
        "cancelled": ForgeConclusion.CANCELLED,
        "canceled": ForgeConclusion.CANCELLED,
    }
    if normalized in terminal:
        return ForgeRunStatus.COMPLETED, terminal[normalized]
    return (
        {
            "pending": ForgeRunStatus.PENDING,
            "queued": ForgeRunStatus.QUEUED,
            "in_progress": ForgeRunStatus.IN_PROGRESS,
            "running": ForgeRunStatus.IN_PROGRESS,
            "waiting": ForgeRunStatus.WAITING,
            "requested": ForgeRunStatus.REQUESTED,
        }.get(normalized, ForgeRunStatus.UNKNOWN),
        None,
    )


def _check(value: Mapping[str, Any], head: GitObjectId) -> ForgeCheck:
    status, conclusion = _status(value["status"])
    return ForgeCheck(
        external_id=str(value["id"]),
        name=str(value.get("context") or "default"),
        status=status,
        conclusion=conclusion,
        head=head,
        details_url=None
        if not value.get("target_url")
        else str(value["target_url"]),
        started_at=_time(value.get("created_at")),
        completed_at=(
            _time(value.get("updated_at"))
            if status is ForgeRunStatus.COMPLETED
            else None
        ),
    )


def _workflow(value: Mapping[str, Any]) -> ForgeWorkflowRun:
    status, conclusion = _status(value["status"])
    return ForgeWorkflowRun(
        external_id=str(value["id"]),
        name=str(value.get("name") or value.get("display_title") or f"run-{value['id']}"),
        event=str(value.get("event") or "unknown"),
        status=status,
        conclusion=conclusion,
        head=GitObjectId(str(value["head_sha"])),
        web_url=str(value["html_url"]),
        started_at=_time(value.get("run_started_at") or value.get("started_at")),
        completed_at=(
            _time(value.get("updated_at"))
            if status is ForgeRunStatus.COMPLETED
            else None
        ),
    )


def _review(value: Mapping[str, Any], provider: str) -> ForgeReview:
    raw_state = str(value.get("state") or "unknown").upper()
    state = {
        "APPROVED": ForgeReviewState.APPROVED,
        "PENDING": ForgeReviewState.PENDING,
        "COMMENT": ForgeReviewState.COMMENTED,
        "REQUEST_CHANGES": ForgeReviewState.CHANGES_REQUESTED,
        "REQUEST_REVIEW": ForgeReviewState.PENDING,
    }.get(raw_state, ForgeReviewState.UNKNOWN)
    reviewer = value.get("user") or value.get("reviewer")
    return ForgeReview(
        external_id=str(value["id"]),
        author=_actor(reviewer, provider),
        state=state,
        body=str(value.get("body") or ""),
        submitted_at=_time(value.get("submitted_at")),
        commit=(
            None
            if not value.get("commit_id")
            else GitObjectId(str(value["commit_id"]))
        ),
    )


def _general_comment(value: Mapping[str, Any], provider: str) -> ForgeComment:
    return ForgeComment(
        external_id=str(value["id"]),
        author=_actor(value["user"], provider),
        kind=ForgeCommentKind.GENERAL,
        body=str(value.get("body") or ""),
        created_at=_required_time(value["created_at"]),
        updated_at=_required_time(value.get("updated_at") or value["created_at"]),
        web_url=str(value["html_url"]),
    )


def _review_comment(value: Mapping[str, Any], provider: str) -> ForgeComment:
    line_value = value.get("new_position") or value.get("old_position")
    return ForgeComment(
        external_id=str(value["id"]),
        author=_actor(value.get("user") or value.get("reviewer"), provider),
        kind=ForgeCommentKind.REVIEW,
        body=str(value.get("body") or ""),
        created_at=_required_time(value["created_at"]),
        updated_at=_required_time(value.get("updated_at") or value["created_at"]),
        web_url=str(value["html_url"]),
        path=None if not value.get("path") else str(value["path"]),
        line=None if not line_value else int(line_value),
    )


def _changed_file(value: Mapping[str, Any]) -> ForgeChangedFile:
    raw_status = str(value["status"]).lower()
    kind = {
        "added": GitChangeKind.ADDED,
        "modified": GitChangeKind.MODIFIED,
        "deleted": GitChangeKind.DELETED,
        "removed": GitChangeKind.DELETED,
        "renamed": GitChangeKind.RENAMED,
        "copied": GitChangeKind.COPIED,
        "changed": GitChangeKind.MODIFIED,
    }.get(raw_status, GitChangeKind.MODIFIED)
    old_path = None
    if kind in {GitChangeKind.RENAMED, GitChangeKind.COPIED}:
        old_path = str(value["previous_filename"])
    return ForgeChangedFile(
        entry=GitDiffEntry(
            path=str(value["filename"]),
            kind=kind,
            old_path=old_path,
        ),
        additions=int(value.get("additions", 0)),
        deletions=int(value.get("deletions", 0)),
    )
