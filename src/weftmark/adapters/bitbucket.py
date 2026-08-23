"""Read-only Bitbucket Cloud implementation of the provider-neutral ForgePort."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
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


class BitbucketAdapterError(ValueError):
    """Raised for invalid local Bitbucket adapter configuration."""


class BitbucketTransportError(RuntimeError):
    """Raised when the configured HTTP transport cannot make an observation."""


@dataclass(frozen=True, slots=True)
class BitbucketHttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class BitbucketTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str]) -> BitbucketHttpResponse:
        """Perform one GET without interpreting Bitbucket semantics."""


class UrlLibBitbucketTransport:
    """Dependency-free transport for Bitbucket Cloud observations."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise BitbucketAdapterError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def get(self, url: str, *, headers: Mapping[str, str]) -> BitbucketHttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return BitbucketHttpResponse(
                    response.status,
                    response.read(),
                    dict(response.headers.items()),
                )
        except HTTPError as error:
            return BitbucketHttpResponse(
                error.code,
                error.read(),
                dict(error.headers.items()) if error.headers is not None else {},
            )
        except (URLError, TimeoutError, socket.timeout, OSError) as error:
            raise BitbucketTransportError("Bitbucket transport unavailable") from error


class BitbucketForgeAdapter(ForgePort):
    """Observe one Bitbucket Cloud repository without mutation authority."""

    def __init__(
        self,
        repository: str,
        *,
        token: str | None = None,
        api_base: str = "https://api.bitbucket.org/2.0",
        web_base: str = "https://bitbucket.org",
        transport: BitbucketTransport | None = None,
    ) -> None:
        parts = repository.strip().split("/")
        if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
            raise BitbucketAdapterError("repository must be workspace/repository")
        if any(any(character in part for character in "\r\n\x00") for part in parts):
            raise BitbucketAdapterError("repository must be header-safe and NUL-free")
        if token is not None:
            token = token.strip()
            if not token or any(character in token for character in "\r\n\x00"):
                raise BitbucketAdapterError("token must be non-empty and header-safe")
        self._repository = "/".join(parts)
        self._repository_path = "/".join(quote(part, safe="") for part in parts)
        self._token = token
        self._api_base = _base_url("api_base", api_base)
        self._web_base = _base_url("web_base", web_base)
        self._transport = transport or UrlLibBitbucketTransport()

    def repository(self) -> ForgeRepository:
        return ForgeRepository(
            provider="bitbucket",
            id=self._repository,
            web_url=f"{self._web_base}/{self._repository_path}",
        )

    def capabilities(self) -> ForgeCapabilities:
        return ForgeCapabilities()

    def change_request(self, external_id: str) -> ForgeResult[ForgeChangeRequest]:
        number = _pull_number(external_id)
        result = self._get_json(self._pull_path(number))
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            payload = _mapping(result.value)
            raw_state = str(payload["state"]).upper()
            state = {
                "OPEN": ForgeChangeState.OPEN,
                "MERGED": ForgeChangeState.MERGED,
                "DECLINED": ForgeChangeState.CLOSED,
                "SUPERSEDED": ForgeChangeState.CLOSED,
            }.get(raw_state)
            if state is None:
                raise ValueError("unknown pull request state")
            source = _mapping(payload["source"])
            destination = _mapping(payload["destination"])
            merged_at = (
                _required_time(payload["updated_on"])
                if state is ForgeChangeState.MERGED
                else None
            )
            return ForgeResult.available(
                ForgeChangeRequest(
                    external_id=str(payload["id"]),
                    title=str(payload["title"]),
                    state=state,
                    source_branch=str(_mapping(source["branch"])["name"]),
                    target_branch=str(_mapping(destination["branch"])["name"]),
                    head=GitObjectId(str(_mapping(source["commit"])["hash"])),
                    base=GitObjectId(str(_mapping(destination["commit"])["hash"])),
                    web_url=_link(payload, "html"),
                    author=_actor(payload["author"]),
                    draft=bool(payload.get("draft", False)),
                    updated_at=_required_time(payload["updated_on"]),
                    merged_at=merged_at,
                )
            )
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                "Bitbucket returned malformed change-request data"
            )

    def checks(self, head: GitObjectId) -> ForgeResult[tuple[ForgeCheck, ...]]:
        result = self._paged_values(
            f"/repositories/{self._repository_path}/commit/{head}/statuses"
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        if not result.value:
            return ForgeResult.missing("no build statuses reported for commit")
        try:
            values = tuple(_check(value, head) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("Bitbucket returned malformed build-status data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.name, value.external_id)))
        )

    def workflow_runs(
        self, head: GitObjectId
    ) -> ForgeResult[tuple[ForgeWorkflowRun, ...]]:
        result = self._paged_values(
            f"/repositories/{self._repository_path}/pipelines/",
            query={"target.commit.hash": str(head), "sort": "-created_on"},
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        if not result.value:
            return ForgeResult.missing("no pipelines reported for commit")
        try:
            values = tuple(_pipeline(value) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("Bitbucket returned malformed pipeline data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.name, value.external_id)))
        )

    def reviews(self, external_id: str) -> ForgeResult[tuple[ForgeReview, ...]]:
        number = _pull_number(external_id)
        result = self._get_json(self._pull_path(number))
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            payload = _mapping(result.value)
            head = GitObjectId(
                str(_mapping(_mapping(payload["source"])["commit"])["hash"])
            )
            participants = payload.get("participants", [])
            if not isinstance(participants, list):
                raise TypeError("participants must be a list")
            values = tuple(
                _review(number, _mapping(participant), head)
                for participant in participants
                if str(_mapping(participant).get("role", "")).upper() == "REVIEWER"
            )
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("Bitbucket returned malformed approval data")
        return ForgeResult.available(values)

    def comments(self, external_id: str) -> ForgeResult[tuple[ForgeComment, ...]]:
        number = _pull_number(external_id)
        result = self._paged_values(f"{self._pull_path(number)}/comments")
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            values = tuple(
                _comment(value)
                for value in result.value
                if not bool(value.get("deleted", False))
            )
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("Bitbucket returned malformed comment data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.created_at, value.external_id)))
        )

    def changed_files(
        self, external_id: str
    ) -> ForgeResult[tuple[ForgeChangedFile, ...]]:
        number = _pull_number(external_id)
        result = self._paged_values(f"{self._pull_path(number)}/diffstat")
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            values = tuple(_changed_file(value) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("Bitbucket returned malformed diffstat data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: value.entry.path))
        )

    def _pull_path(self, number: int) -> str:
        return f"/repositories/{self._repository_path}/pullrequests/{number}"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "WeftMark/0.0.1",
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get_json(
        self, path: str, *, query: Mapping[str, str] | None = None
    ) -> ForgeResult[Any]:
        suffix = "" if not query else "?" + urlencode(query)
        return self._get_json_url(f"{self._api_base}{path}{suffix}")

    def _get_json_url(self, url: str) -> ForgeResult[Any]:
        try:
            response = self._transport.get(url, headers=self._headers())
        except BitbucketTransportError:
            return ForgeResult.unavailable("Bitbucket transport unavailable")
        if response.status == 404:
            return ForgeResult.missing("Bitbucket resource not found")
        if response.status < 200 or response.status >= 300:
            return ForgeResult.unavailable(
                f"Bitbucket API unavailable (HTTP {response.status})"
            )
        try:
            return ForgeResult.available(json.loads(response.body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ForgeResult.unavailable("Bitbucket returned invalid JSON")

    def _paged_values(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
    ) -> ForgeResult[tuple[Mapping[str, Any], ...]]:
        params = {**dict(query or {}), "pagelen": "100"}
        next_url: str | None = f"{self._api_base}{path}?{urlencode(params)}"
        values: list[Mapping[str, Any]] = []
        for _ in range(100):
            if next_url is None:
                return ForgeResult.available(tuple(values))
            if not _safe_page_url(next_url, self._api_base):
                return ForgeResult.unavailable(
                    "Bitbucket returned an unsafe pagination URL"
                )
            result = self._get_json_url(next_url)
            if result.availability is not ForgeAvailability.AVAILABLE:
                return ForgeResult(result.availability, detail=result.detail)
            try:
                payload = _mapping(result.value)
                raw_values = payload["values"]
                if not isinstance(raw_values, list):
                    raise TypeError("values must be a list")
                values.extend(_mapping(value) for value in raw_values)
                raw_next = payload.get("next")
                if raw_next is not None and not isinstance(raw_next, str):
                    raise TypeError("next must be a URL")
                next_url = raw_next
            except (KeyError, TypeError):
                return ForgeResult.unavailable(
                    "Bitbucket returned malformed paginated data"
                )
        return ForgeResult.unavailable("Bitbucket pagination exceeded safety limit")


def _base_url(name: str, value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character in normalized for character in "\r\n\x00")
    ):
        raise BitbucketAdapterError(f"{name} must be an absolute HTTP(S) URL")
    return normalized


def _safe_page_url(value: str, api_base: str) -> bool:
    candidate = urlsplit(value)
    base = urlsplit(api_base)
    path_prefix = base.path.rstrip("/") + "/"
    return (
        candidate.scheme == base.scheme
        and candidate.netloc == base.netloc
        and candidate.username is None
        and candidate.password is None
        and not candidate.fragment
        and candidate.path.startswith(path_prefix)
    )


def _pull_number(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise BitbucketAdapterError(
            "Bitbucket change-request id must be a positive integer"
        ) from error
    if number < 1:
        raise BitbucketAdapterError(
            "Bitbucket change-request id must be a positive integer"
        )
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


def _link(value: Mapping[str, Any], name: str) -> str:
    return str(_mapping(_mapping(value["links"])[name])["href"])


def _actor(value: Any) -> ForgeActor:
    payload = _mapping(value)
    identifier = payload.get("uuid") or payload.get("account_id")
    login = payload.get("nickname") or payload.get("display_name")
    if identifier is None or login is None:
        raise KeyError("Bitbucket actor identity is incomplete")
    return ForgeActor(f"bitbucket-user:{identifier}", str(login))


def _build_state(value: Any) -> tuple[ForgeRunStatus, ForgeConclusion | None]:
    normalized = str(value).upper()
    if normalized == "SUCCESSFUL":
        return ForgeRunStatus.COMPLETED, ForgeConclusion.PASSED
    if normalized == "FAILED":
        return ForgeRunStatus.COMPLETED, ForgeConclusion.FAILED
    if normalized == "STOPPED":
        return ForgeRunStatus.COMPLETED, ForgeConclusion.CANCELLED
    if normalized == "INPROGRESS":
        return ForgeRunStatus.IN_PROGRESS, None
    if normalized == "PENDING":
        return ForgeRunStatus.PENDING, None
    return ForgeRunStatus.UNKNOWN, None


def _check(value: Mapping[str, Any], head: GitObjectId) -> ForgeCheck:
    status, conclusion = _build_state(value["state"])
    identifier = value.get("uuid") or value.get("key")
    if identifier is None:
        raise KeyError("build status lacks identity")
    return ForgeCheck(
        external_id=str(identifier),
        name=str(value.get("name") or value["key"]),
        status=status,
        conclusion=conclusion,
        head=head,
        details_url=None if value.get("url") is None else str(value["url"]),
        started_at=_time(value.get("created_on")),
        completed_at=(
            _time(value.get("updated_on"))
            if status is ForgeRunStatus.COMPLETED
            else None
        ),
    )


def _pipeline_state(value: Mapping[str, Any]) -> tuple[ForgeRunStatus, ForgeConclusion | None]:
    state = str(value.get("name", "UNKNOWN")).upper()
    if state != "COMPLETED":
        return {
            "IN_PROGRESS": ForgeRunStatus.IN_PROGRESS,
            "PENDING": ForgeRunStatus.PENDING,
            "PAUSED": ForgeRunStatus.WAITING,
        }.get(state, ForgeRunStatus.UNKNOWN), None
    result = str(_mapping(value.get("result", {})).get("name", "UNKNOWN")).upper()
    conclusion = {
        "SUCCESSFUL": ForgeConclusion.PASSED,
        "FAILED": ForgeConclusion.FAILED,
        "ERROR": ForgeConclusion.STARTUP_FAILURE,
        "STOPPED": ForgeConclusion.CANCELLED,
        "EXPIRED": ForgeConclusion.TIMED_OUT,
    }.get(result, ForgeConclusion.UNKNOWN)
    return ForgeRunStatus.COMPLETED, conclusion


def _pipeline(value: Mapping[str, Any]) -> ForgeWorkflowRun:
    status, conclusion = _pipeline_state(_mapping(value["state"]))
    target = _mapping(value["target"])
    trigger = _mapping(value.get("trigger", {}))
    identifier = value.get("uuid") or value.get("build_number")
    if identifier is None:
        raise KeyError("pipeline lacks identity")
    return ForgeWorkflowRun(
        external_id=str(identifier),
        name=str(value.get("build_number") or identifier),
        event=str(trigger.get("name") or target.get("type") or "unknown"),
        status=status,
        conclusion=conclusion,
        head=GitObjectId(str(_mapping(target["commit"])["hash"])),
        web_url=_link(value, "html"),
        started_at=_time(value.get("created_on")),
        completed_at=(
            _time(value.get("completed_on"))
            if status is ForgeRunStatus.COMPLETED
            else None
        ),
    )


def _review(number: int, value: Mapping[str, Any], head: GitObjectId) -> ForgeReview:
    user = _actor(value["user"])
    approved = bool(value.get("approved", False))
    return ForgeReview(
        external_id=f"{number}:{user.id}",
        author=user,
        state=ForgeReviewState.APPROVED if approved else ForgeReviewState.PENDING,
        body="",
        commit=head if approved else None,
    )


def _comment(value: Mapping[str, Any]) -> ForgeComment:
    inline = value.get("inline")
    inline_payload = {} if inline is None else _mapping(inline)
    path = inline_payload.get("path")
    raw_line = inline_payload.get("to") or inline_payload.get("from")
    created_at = _required_time(value["created_on"])
    return ForgeComment(
        external_id=str(value["id"]),
        author=_actor(value["user"]),
        kind=(
            ForgeCommentKind.GENERAL
            if inline is None
            else ForgeCommentKind.REVIEW
        ),
        body=str(_mapping(value.get("content", {})).get("raw", "")),
        created_at=created_at,
        updated_at=_time(value.get("updated_on")) or created_at,
        web_url=_link(value, "html"),
        path=None if path is None else str(path),
        line=None if raw_line is None else int(raw_line),
    )


def _changed_file(value: Mapping[str, Any]) -> ForgeChangedFile:
    status = str(value["status"]).lower()
    kind = {
        "added": GitChangeKind.ADDED,
        "modified": GitChangeKind.MODIFIED,
        "removed": GitChangeKind.DELETED,
        "renamed": GitChangeKind.RENAMED,
    }.get(status)
    if kind is None:
        raise ValueError("unknown Bitbucket diffstat status")
    old = value.get("old")
    new = value.get("new")
    old_path = None if old is None else str(_mapping(old)["path"])
    new_path = None if new is None else str(_mapping(new)["path"])
    path = new_path or old_path
    if path is None:
        raise KeyError("diffstat path is missing")
    return ForgeChangedFile(
        entry=GitDiffEntry(
            path=path,
            kind=kind,
            old_path=old_path if kind is GitChangeKind.RENAMED else None,
        ),
        additions=int(value["lines_added"]),
        deletions=int(value["lines_removed"]),
    )
