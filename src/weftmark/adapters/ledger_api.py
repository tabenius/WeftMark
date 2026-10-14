"""Transport-neutral handlers for the versioned opaque ledger HTTP contract.

The host authenticates the caller and constructs Principal. These handlers do
not implement an identity provider, bearer-token parser, HTTP listener or SQL
execution endpoint. Authorization precedes resolution of any storage object.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Callable, Mapping

from weftmark.application.ledger import LedgerService, LedgerServiceError
from weftmark.application.ports.ledger import (
    LedgerContractError, LedgerHead, LedgerHeadChanged, LedgerIdempotencyConflict,
)
from weftmark.adapters.ledger_codec import (
    LedgerCodecError, canonical, draft_content, parse,
)
from weftmark.adapters.sql_ledger import DIGEST, LedgerStorageError, SqlLedger, identifier

REQUEST_SCHEMA = "weftmark.ledger-append.v1"
RESULT_SCHEMA = "weftmark.ledger-result.v1"
MAX_BODY_BYTES = 262144
MAX_PAGE_BYTES = 1048576
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    workspace_permissions: Mapping[str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class ApiResult:
    status: int
    body: dict


def head_payload(head: LedgerHead) -> dict:
    return {"workspace_id": head.workspace_id, "ledger_id": head.ledger_id,
            "sequence": head.sequence, "digest": head.digest}


class _Denied(ValueError):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


class LedgerApi:
    def __init__(self, resolve: Callable[[str, str], SqlLedger]):
        self.resolve = resolve

    def _authorize(self, principal, workspace, ledger, permission) -> SqlLedger:
        identifier(workspace)
        identifier(ledger)
        if principal is None:
            raise _Denied(401, "unauthenticated")
        identifier(principal.subject)
        if permission not in principal.workspace_permissions.get(workspace, frozenset()):
            raise _Denied(403, "forbidden")
        result = self.resolve(workspace, ledger)
        if (result.workspace_id, result.ledger_id) != (workspace, ledger):
            raise LedgerStorageError("resolver returned a different ledger identity")
        return result

    @staticmethod
    def _result(workspace, ledger, kind, action) -> ApiResult:
        base = {"schema": RESULT_SCHEMA, "workspace_id": workspace, "ledger_id": ledger, "kind": kind}
        try:
            return ApiResult(200, dict(base, ok=True, data=action()))
        except _Denied as error:
            status, code, message = error.status, error.code, error.code
        except LedgerIdempotencyConflict:
            status, code, message = 409, "idempotency_conflict", "operation ID was used for a different request or principal"
        except LedgerHeadChanged:
            status, code, message = 409, "head_changed", "reload the ledger head before submitting a new operation"
        except (LedgerCodecError, LedgerContractError, LedgerServiceError, ValueError, TypeError, KeyError):
            status, code, message = 400, "invalid_request", "invalid or unsupported ledger request"
        except LedgerStorageError:
            status, code, message = 503, "storage_unavailable", "ledger storage is unavailable or inconsistent"
        return ApiResult(status, {"schema": RESULT_SCHEMA, "kind": "error", "ok": False,
                                  "error": {"code": code, "message": message}})

    def head(self, principal: Principal | None, workspace_id: str, ledger_id: str) -> ApiResult:
        return self._result(workspace_id, ledger_id, "head", lambda: {
            "head": head_payload(self._authorize(principal, workspace_id, ledger_id, "read").head())})

    def entries(self, principal: Principal | None, workspace_id: str, ledger_id: str,
                *, after: str = "0", limit: int = 100) -> ApiResult:
        def read():
            ledger = self._authorize(principal, workspace_id, ledger_id, "read")
            if not isinstance(after, str) or not re.fullmatch(r"0|[1-9][0-9]{0,15}", after) or int(after) > 2**53 - 1:
                raise ValueError("invalid cursor")
            if type(limit) is not int or not 1 <= limit <= 200:
                raise ValueError("limit must be 1-200")
            head, _, rows = ledger.snapshot()
            records, size = [], 0
            for row in rows:
                if row["sequence"] <= int(after):
                    continue
                record = row["record_json"]
                # Bound the actual escaped JSON response, not just source bytes.
                record_size = len(canonical(record).encode())
                if len(records) == limit or size + record_size > MAX_PAGE_BYTES:
                    break
                records.append(record)
                size += record_size
            sequence = parse(records[-1])["sequence"] if records else int(after)
            return {"head": head_payload(head), "records": records,
                    "next_cursor": str(sequence) if records and sequence < head.sequence else None}
        return self._result(workspace_id, ledger_id, "entries", read)

    def append(self, principal: Principal | None, workspace_id: str, ledger_id: str,
               body: dict) -> ApiResult:
        def write():
            ledger = self._authorize(principal, workspace_id, ledger_id, "append")
            if not isinstance(body, dict) or set(body) != {"schema", "operation_id", "expected_digest", "draft"}:
                raise ValueError("unexpected request fields")
            if body["schema"] != REQUEST_SCHEMA or len(canonical(body).encode()) > MAX_BODY_BYTES:
                raise ValueError("invalid schema or oversized request")
            expected = body["expected_digest"]
            if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
                raise ValueError("network appends require an expected digest")
            draft = body["draft"]
            if not isinstance(draft, dict) or set(draft) != {"kind", "entity_id", "payload_json", "recorded_at"}:
                raise ValueError("invalid draft fields")
            if not isinstance(draft["kind"], str) or not 1 <= len(draft["kind"]) <= 120:
                raise ValueError("invalid kind")
            if not isinstance(draft["entity_id"], str) or not 1 <= len(draft["entity_id"]) <= 1024:
                raise ValueError("invalid entity identity")
            timestamp = draft["recorded_at"]
            if not isinstance(timestamp, str) or not TIMESTAMP.fullmatch(timestamp):
                raise ValueError("timestamp must be RFC3339 with timezone")
            payload = parse(draft["payload_json"])
            if canonical(payload) != draft["payload_json"]:
                raise ValueError("payload_json must be canonical")

            class Destination:
                def append(self, safe_draft):
                    return ledger.append_idempotent(safe_draft, operation_id=body["operation_id"],
                        expected_digest=expected, principal_id=principal.subject)

            # Reuse application secret refusal; never grant authority from a
            # client-supplied payload actor/session/reviewer label.
            entry = LedgerService(Destination()).record(kind=draft["kind"], entity_id=draft["entity_id"],
                payload=payload, recorded_at=datetime.fromisoformat(timestamp))
            record = canonical(dict(draft_content(entry.draft), sequence=entry.sequence,
                previous_digest=entry.previous_digest, digest=entry.digest))
            return {"record_json": record, "accepted_head": head_payload(
                LedgerHead(workspace_id, ledger_id, entry.sequence, entry.digest))}
        return self._result(workspace_id, ledger_id, "append", write)
