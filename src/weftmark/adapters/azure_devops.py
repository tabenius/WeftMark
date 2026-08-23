"""Read-only Azure DevOps implementation of the provider-neutral ForgePort."""

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
from weftmark.application.ports.git import GitObjectId


class AzureDevopsAdapterError(ValueError):
    """Raised for invalid local Azure DevOps adapter configuration."""


class AzureDevopsTransportError(RuntimeError):
    """Raised when the configured HTTP transport cannot make an observation."""


@dataclass(frozen=True, slots=True)
class AzureDevopsHttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class AzureDevopsTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str]) -> AzureDevopsHttpResponse:
        """Perform one GET without interpreting Azure DevOps semantics."""


class UrlLibAzureDevopsTransport:
    """Dependency-free transport for Azure DevOps observations."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise AzureDevopsAdapterError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def get(self, url: str, *, headers: Mapping[str, str]) -> AzureDevopsHttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return AzureDevopsHttpResponse(
                    response.status,
                    response.read(),
                    dict(response.headers.items()),
                )
        except HTTPError as error:
            return AzureDevopsHttpResponse(
                error.code,
                error.read(),
                dict(error.headers.items()) if error.headers is not None else {},
            )
        except (URLError, TimeoutError, socket.timeout, OSError) as error:
            raise AzureDevopsTransportError(
                "Azure DevOps transport unavailable"
            ) from error


class AzureDevopsForgeAdapter(ForgePort):
    """Observe one Azure Repos repository without mutation authority."""

    def __init__(
        self,
        repository: str,
        *,
        token: str | None = None,
        api_base: str = "https://dev.azure.com",
        web_base: str = "https://dev.azure.com",
        transport: AzureDevopsTransport | None = None,
    ) -> None:
        parts = repository.strip().split("/")
        if len(parts) != 3 or any(not part or part in {".", ".."} for part in parts):
            raise AzureDevopsAdapterError(
                "repository must be organization/project/repository"
            )
        if any(any(character in part for character in "\r\n\x00") for part in parts):
            raise AzureDevopsAdapterError("repository must be header-safe and NUL-free")
        if token is not None:
            token = token.strip()
            if not token or any(character in token for character in "\r\n\x00"):
                raise AzureDevopsAdapterError("token must be non-empty and header-safe")
        self._organization, self._project, self._repository = parts
        self._identity = "/".join(parts)
        self._organization_path = quote(self._organization, safe="")
        self._project_path = quote(self._project, safe="")
        self._repository_path = quote(self._repository, safe="")
        self._token = token
        self._api_base = _base_url("api_base", api_base)
        self._web_base = _base_url("web_base", web_base)
        self._transport = transport or UrlLibAzureDevopsTransport()

    def repository(self) -> ForgeRepository:
        return ForgeRepository(
            provider="azure-devops",
            id=self._identity,
            web_url=(
                f"{self._web_base}/{self._organization_path}/"
                f"{self._project_path}/_git/{self._repository_path}"
            ),
        )

    def capabilities(self) -> ForgeCapabilities:
        # Azure iteration changes expose paths and kinds but not the exact
        # per-file line counts required by ForgeChangedFile v0.
        return ForgeCapabilities(changed_files=False)

    def change_request(self, external_id: str) -> ForgeResult[ForgeChangeRequest]:
        number = _pull_number(external_id)
        result = self._get_json(self._pull_path(number))
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            raw_payload, _headers = result.value
            payload = _mapping(raw_payload)
            raw_state = str(payload["status"]).lower()
            state = {
                "active": ForgeChangeState.OPEN,
                "abandoned": ForgeChangeState.CLOSED,
                "completed": ForgeChangeState.MERGED,
            }.get(raw_state)
            if state is None:
                raise ValueError("unknown pull request status")
            closed_at = _time(payload.get("closedDate"))
            merged_at = closed_at if state is ForgeChangeState.MERGED else None
            updated_at = closed_at or _required_time(payload["creationDate"])
            return ForgeResult.available(
                ForgeChangeRequest(
                    external_id=str(payload["pullRequestId"]),
                    title=str(payload["title"]),
                    state=state,
                    source_branch=_branch(payload["sourceRefName"]),
                    target_branch=_branch(payload["targetRefName"]),
                    head=GitObjectId(
                        str(_mapping(payload["lastMergeSourceCommit"])["commitId"])
                    ),
                    base=GitObjectId(
                        str(_mapping(payload["lastMergeTargetCommit"])["commitId"])
                    ),
                    web_url=self._pull_web_url(number),
                    author=_actor(payload["createdBy"]),
                    draft=bool(payload.get("isDraft", False)),
                    updated_at=updated_at,
                    merged_at=merged_at,
                )
            )
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                "Azure DevOps returned malformed pull-request data"
            )

    def checks(self, head: GitObjectId) -> ForgeResult[tuple[ForgeCheck, ...]]:
        result = self._paged_values(
            f"{self._git_path}/commits/{head}/statuses",
            query={"$top": "1000"},
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        if not result.value:
            return ForgeResult.missing("no commit statuses reported for commit")
        try:
            values = tuple(_check(value, head) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                "Azure DevOps returned malformed commit-status data"
            )
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.name, value.external_id)))
        )

    def workflow_runs(
        self, head: GitObjectId
    ) -> ForgeResult[tuple[ForgeWorkflowRun, ...]]:
        result = self._paged_values(
            self._build_path,
            query={
                "repositoryId": self._repository,
                "repositoryType": "TfsGit",
                "sourceVersion": str(head),
                "queryOrder": "finishTimeDescending",
                "$top": "100",
            },
        )
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        if not result.value:
            return ForgeResult.missing("no builds reported for commit")
        try:
            values = tuple(_build(value) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("Azure DevOps returned malformed build data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.name, value.external_id)))
        )

    def reviews(self, external_id: str) -> ForgeResult[tuple[ForgeReview, ...]]:
        number = _pull_number(external_id)
        result = self._paged_values(f"{self._pull_path(number)}/reviewers")
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            values = tuple(_review(number, value) for value in result.value)
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable(
                "Azure DevOps returned malformed reviewer data"
            )
        return ForgeResult.available(values)

    def comments(self, external_id: str) -> ForgeResult[tuple[ForgeComment, ...]]:
        number = _pull_number(external_id)
        result = self._paged_values(f"{self._pull_path(number)}/threads")
        if result.availability is not ForgeAvailability.AVAILABLE:
            return ForgeResult(result.availability, detail=result.detail)
        try:
            values = tuple(
                _comment(number, thread, comment, self._pull_web_url(number))
                for thread in result.value
                for comment in _human_comments(thread)
            )
        except (KeyError, TypeError, ValueError):
            return ForgeResult.unavailable("Azure DevOps returned malformed thread data")
        return ForgeResult.available(
            tuple(sorted(values, key=lambda value: (value.created_at, value.external_id)))
        )

    def changed_files(
        self, external_id: str
    ) -> ForgeResult[tuple[ForgeChangedFile, ...]]:
        _pull_number(external_id)
        return ForgeResult.unsupported(
            "Azure DevOps iteration changes omit the exact per-file line counts "
            "required by ForgeChangedFile v0"
        )

    @property
    def _git_path(self) -> str:
        return (
            f"/{self._organization_path}/{self._project_path}/_apis/git/"
            f"repositories/{self._repository_path}"
        )

    @property
    def _build_path(self) -> str:
        return f"/{self._organization_path}/{self._project_path}/_apis/build/builds"

    def _pull_path(self, number: int) -> str:
        return f"{self._git_path}/pullRequests/{number}"

    def _pull_web_url(self, number: int) -> str:
        return f"{self.repository().web_url}/pullrequest/{number}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "WeftMark/0.0.1"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get_json(
        self, path: str, *, query: Mapping[str, str] | None = None
    ) -> ForgeResult[tuple[Any, Mapping[str, str]]]:
        params = {**dict(query or {}), "api-version": "7.1"}
        url = f"{self._api_base}{path}?{urlencode(params)}"
        try:
            response = self._transport.get(url, headers=self._headers())
        except AzureDevopsTransportError:
            return ForgeResult.unavailable("Azure DevOps transport unavailable")
        if response.status == 404:
            return ForgeResult.missing("Azure DevOps resource not found")
        if response.status < 200 or response.status >= 300:
            return ForgeResult.unavailable(
                f"Azure DevOps API unavailable (HTTP {response.status})"
            )
        try:
            return ForgeResult.available(
                (json.loads(response.body.decode("utf-8")), response.headers)
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ForgeResult.unavailable("Azure DevOps returned invalid JSON")

    def _paged_values(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
    ) -> ForgeResult[tuple[Mapping[str, Any], ...]]:
        values: list[Mapping[str, Any]] = []
        continuation: str | None = None
        for _ in range(100):
            params = dict(query or {})
            if continuation is not None:
                params["continuationToken"] = continuation
            result = self._get_json(path, query=params)
            if result.availability is not ForgeAvailability.AVAILABLE:
                return ForgeResult(result.availability, detail=result.detail)
            try:
                raw_payload, headers = result.value
                payload = _mapping(raw_payload)
                raw_values = payload["value"]
                if not isinstance(raw_values, list):
                    raise TypeError("value must be a list")
                values.extend(_mapping(value) for value in raw_values)
                continuation = _header(headers, "x-ms-continuationtoken")
                if continuation is not None and any(
                    character in continuation for character in "\r\n\x00"
                ):
                    raise TypeError("continuation token is not header-safe")
            except (KeyError, TypeError):
                return ForgeResult.unavailable(
                    "Azure DevOps returned malformed paginated data"
                )
            if not continuation:
                return ForgeResult.available(tuple(values))
        return ForgeResult.unavailable("Azure DevOps pagination exceeded safety limit")


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
        raise AzureDevopsAdapterError(f"{name} must be an absolute HTTP(S) URL")
    return normalized


def _pull_number(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise AzureDevopsAdapterError(
            "Azure DevOps change-request id must be a positive integer"
        ) from error
    if number < 1:
        raise AzureDevopsAdapterError(
            "Azure DevOps change-request id must be a positive integer"
        )
    return number


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("expected object")
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.casefold()
    return next(
        (str(value) for key, value in headers.items() if str(key).casefold() == expected),
        None,
    )


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


def _branch(value: Any) -> str:
    branch = str(value)
    prefix = "refs/heads/"
    return branch[len(prefix) :] if branch.startswith(prefix) else branch


def _actor(value: Any) -> ForgeActor:
    payload = _mapping(value)
    identifier = payload.get("id")
    login = payload.get("uniqueName") or payload.get("displayName")
    if identifier is None or login is None:
        raise KeyError("Azure DevOps actor identity is incomplete")
    return ForgeActor(f"azure-devops-user:{identifier}", str(login))


def _git_status(value: Any) -> tuple[ForgeRunStatus, ForgeConclusion | None]:
    normalized = str(value).lower()
    if normalized == "succeeded":
        return ForgeRunStatus.COMPLETED, ForgeConclusion.PASSED
    if normalized == "failed":
        return ForgeRunStatus.COMPLETED, ForgeConclusion.FAILED
    if normalized == "error":
        return ForgeRunStatus.COMPLETED, ForgeConclusion.STARTUP_FAILURE
    if normalized == "notapplicable":
        return ForgeRunStatus.COMPLETED, ForgeConclusion.SKIPPED
    if normalized == "pending":
        return ForgeRunStatus.PENDING, None
    return ForgeRunStatus.UNKNOWN, None


def _check(value: Mapping[str, Any], head: GitObjectId) -> ForgeCheck:
    status, conclusion = _git_status(value["state"])
    context = _mapping(value["context"])
    name = str(context["name"])
    genre = context.get("genre")
    if genre:
        name = f"{genre}/{name}"
    return ForgeCheck(
        external_id=str(value["id"]),
        name=name,
        status=status,
        conclusion=conclusion,
        head=head,
        details_url=(
            None if value.get("targetUrl") is None else str(value["targetUrl"])
        ),
        started_at=_time(value.get("creationDate")),
        completed_at=(
            _time(value.get("updatedDate"))
            if status is ForgeRunStatus.COMPLETED
            else None
        ),
    )


def _build_state(
    status_value: Any, result_value: Any
) -> tuple[ForgeRunStatus, ForgeConclusion | None]:
    status = str(status_value).lower()
    if status == "notstarted":
        return ForgeRunStatus.QUEUED, None
    if status == "postponed":
        return ForgeRunStatus.WAITING, None
    if status in {"inprogress", "cancelling"}:
        return ForgeRunStatus.IN_PROGRESS, None
    if status != "completed":
        return ForgeRunStatus.UNKNOWN, None
    result = "unknown" if result_value is None else str(result_value).lower()
    conclusion = {
        "succeeded": ForgeConclusion.PASSED,
        "partiallysucceeded": ForgeConclusion.NEUTRAL,
        "failed": ForgeConclusion.FAILED,
        "canceled": ForgeConclusion.CANCELLED,
    }.get(result, ForgeConclusion.UNKNOWN)
    return ForgeRunStatus.COMPLETED, conclusion


def _build(value: Mapping[str, Any]) -> ForgeWorkflowRun:
    status, conclusion = _build_state(value["status"], value.get("result"))
    definition = _mapping(value.get("definition", {}))
    reason = str(value.get("reason") or "unknown")
    web_url = value.get("url")
    links = value.get("_links")
    if links is not None:
        web_url = _mapping(_mapping(links).get("web", {})).get("href") or web_url
    if web_url is None:
        raise KeyError("build URL is missing")
    return ForgeWorkflowRun(
        external_id=str(value["id"]),
        name=str(definition.get("name") or value["buildNumber"]),
        event=reason,
        status=status,
        conclusion=conclusion,
        head=GitObjectId(str(value["sourceVersion"])),
        web_url=str(web_url),
        started_at=_time(value.get("startTime") or value.get("queueTime")),
        completed_at=(
            _time(value.get("finishTime"))
            if status is ForgeRunStatus.COMPLETED
            else None
        ),
    )


def _review(number: int, value: Mapping[str, Any]) -> ForgeReview:
    vote = int(value.get("vote", 0))
    if vote >= 5:
        state = ForgeReviewState.APPROVED
    elif vote <= -5:
        state = ForgeReviewState.CHANGES_REQUESTED
    else:
        state = ForgeReviewState.PENDING
    author = _actor(value)
    return ForgeReview(
        external_id=f"{number}:{author.id}",
        author=author,
        state=state,
        body="",
    )


def _human_comments(thread: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    comments = thread.get("comments", [])
    if not isinstance(comments, list):
        raise TypeError("thread comments must be a list")
    return tuple(
        _mapping(comment)
        for comment in comments
        if not bool(_mapping(comment).get("isDeleted", False))
        and str(_mapping(comment).get("commentType", "text")).lower() in {"1", "text"}
    )


def _comment(
    number: int,
    thread: Mapping[str, Any],
    value: Mapping[str, Any],
    pull_web_url: str,
) -> ForgeComment:
    context = thread.get("threadContext")
    context_payload = {} if context is None else _mapping(context)
    raw_path = context_payload.get("filePath")
    path = None if raw_path is None else str(raw_path).lstrip("/")
    right_start = context_payload.get("rightFileStart")
    left_start = context_payload.get("leftFileStart")
    position = right_start if right_start is not None else left_start
    line = None if position is None else int(_mapping(position)["line"])
    thread_id = int(thread["id"])
    created_at = _required_time(value["publishedDate"])
    return ForgeComment(
        external_id=f"{thread_id}:{value['id']}",
        author=_actor(value["author"]),
        kind=(
            ForgeCommentKind.GENERAL if path is None else ForgeCommentKind.REVIEW
        ),
        body=str(value.get("content") or ""),
        created_at=created_at,
        updated_at=_time(value.get("lastUpdatedDate")) or created_at,
        web_url=f"{pull_web_url}?_a=files&discussionId={thread_id}",
        path=path,
        line=line,
    )
