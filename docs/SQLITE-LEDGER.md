# Transactional SQLite ledger v1

**Status:** implemented adapter and executable contracts, not a deployed ledger
service or an automatic storage migration. The CLI still defaults to JSONL.
The source plan is `tasks/36-sqlite-ledger.weft.yml`.

## Installation and use

```bash
uv sync --locked --extra sqlite
```

Source installation builds Nostoi's Rust Python extension through Maturin; a
Rust toolchain is required until compatible wheels are distributed. The SQLite
extra and test extra pin Nostoi to commit
`b043bf953eeda3ea720adfa91f1b2177c0ab1143`. Ordinary JSONL installations do not
require Nostoi. `uv.lock` records the same source identity.

```python
from datetime import datetime, timezone
from weftmark.adapters.sqlite_ledger import SqliteLedger
from weftmark.application.ledger import LedgerService

ledger = SqliteLedger.create(
    "/private/workspace/ledger.db", workspace_id="workspace-a", ledger_id="main"
)
service = LedgerService(ledger)
entry = service.record(kind="observation", entity_id="repo:stable-id",
                       payload={"head": "observed-git-sha"},
                       recorded_at=datetime.now(timezone.utc))
```

`create` refuses an existing file. Reopen using `SqliteLedger(path, ...)`;
provision another stream explicitly with `create_stream()`. A missing or foreign
schema is an error, never an invitation to create tables. Owner-only local files
are required; a symlinked database or direct parent directory is refused. Keep
the whole containing directory private, including WAL and SHM sidecars.

## Contracts and identity

- `LedgerPort` is unchanged: `append`, `append_if_head`, validated `entries`.
- `IdempotentLedgerPort` adds `head` and `append_idempotent` without imposing
  new methods on the existing JSONL adapter.
- `LedgerHead` binds workspace ID, ledger ID, sequence and digest. IDs are
  explicit stable labels, not storage paths or authenticated identities.
- One workspace is an authorization boundary. Multiple repositories/worktrees
  can use a stream, with stable repository IDs in their domain records.
- Streams in different workspaces have no implicit distributed transaction.
  Cross-workspace references must retain the referenced identity and digest.

`append_idempotent(draft, operation_id=..., expected_digest=...,
principal_id=...)` binds the entire normalized draft, principal, stream and
expected digest into an immutable receipt. Operation IDs are hashed, limited
to 256 UTF-8 bytes, and scoped to a workspace/ledger. Exact retries return the
original entry even after the head advances. A reused key with different
content, principal or expectation raises `LedgerIdempotencyConflict`. A failed
CAS does not reserve a key. Unconditional local appends use bounded CAS retries;
the network contract requires an explicit expected digest.

## Schema and transaction design

Canonical DDL: `src/weftmark/adapters/sql/001_ledger.sql`, shipped as package data.
It is versioned `weftmark.ledger-sql.v1`, with no implicit upgrade:

| Table | Primary key | Role |
| --- | --- | --- |
| `weftmark_ledger_schema` | singleton | Component/version guard |
| `weftmark_ledger_streams` | workspace, ledger | Identity, format, cached head |
| `weftmark_ledger_entries` | workspace, ledger, sequence | Canonical v1 record and indexed fields |
| `weftmark_ledger_receipts` | workspace, ledger, operation key | Principal and complete request binding |

Sequences are capped at JavaScript's safe integer maximum. Payload integers may
span signed/unsigned 64-bit range, because payloads and records travel as JSON
**strings**, not parsed JavaScript numbers. This adapter deliberately refuses
floats and larger integers before passing them to Nostoi. SQL needs no hash UDF,
JSON functions, `RETURNING`, `STRICT` tables or extension-specific types.

The portable write batch conditionally inserts the candidate against both head
sequence and digest, inserts its receipt, advances the matching head, then reads
the receipt. All statements must commit together or roll back together. Triggers
guard monotonic links, identity, immutable entries/receipts and receipt-bound
head updates. Hashes are computed and checked in trusted application code, not
SQL. SQL-only writers are not supported and must not bypass this adapter.

`AtomicBatchExecutor.batch` is the adapter seam. Read batches must have one
consistent snapshot; write batches must be ordered, all-or-nothing, and routed
to the authoritative database. The current implementation validates the entire
chain and receipt set on reads and before appends: O(history) work and memory,
not an indexed large-history service. API pages bound responses, not validation
cost. Hosts must impose request size limits, rate limits and quotas before use.

## SQLite-family deployment profiles

| Host | Integration | Verification status |
| --- | --- | --- |
| CPython `sqlite3` / system libsqlite3 | `SqliteLedger` uses explicit transactions, WAL, `synchronous=FULL`, foreign keys and busy timeout | Executable local tests |
| Other libsqlite bindings | Implement `AtomicBatchExecutor`, including one-snapshot reads and full rollback | Portable contract; no separate driver shipped |
| Cloudflare D1 | Apply DDL through migrations; bridge a trusted plan to ordered `db.batch` prepared/bound statements | D1-shaped batch contract tested locally; no live D1 deployment test |
| libSQL / Turso | Bridge to its transactional batch or transaction API; use primary/authoritative reads and never stale replica heads | Portable contract; no live service test |
| Litestream | Replicate the local SQLite WAL externally; restore to a separate file and verify before cutover | Local verified backup tested; replication/restore not deployed here |

`CallbackBatchExecutor` accepts a host callback that consumes the versioned
`SqlPlan.to_payload()` shape and returns one `{success: true, results: [...]}`
object per statement, including empty results for DML. Declaring `atomic=True`
is a host promise, not a distributed guarantee the callback can prove. Do not
expose that SQL-plan bridge to untrusted callers.

Illustrative D1 host bridge (not deployed or packaged as a Worker):

```javascript
async function executeTrustedPlan(db, plan) {
  // Only trusted host-generated plans; never accept raw SQL from an API user.
  const statements = plan.statements.map(s => db.prepare(s.sql).bind(...s.params));
  return await db.batch(statements);
}
```

Use D1 sessions/primary routing with read-your-writes guarantees where required.
Check target D1 batch/query/response limits before deploying: full-history
snapshots can exceed them, and must fail rather than truncate. A Python Nostoi
extension does not run inside a Worker; a Worker implementation must separately
integrate Nostoi's portable Rust/WASM implementation and pass conformance tests.
Do not replace canonicalization with `JSON.stringify` or a new signing format.
DDL and trigger compatibility on each target remain deployment acceptance gates.

## Opaque API

Machine-readable contracts:

- `docs/contracts/ledger-v1.schema.json` (JSON Schema 2020-12)
- `docs/contracts/ledger-v1.openapi.yaml` (OpenAPI 3.1)
- `src/weftmark/adapters/ledger_api.py` (transport-neutral handlers)

The routes are head GET and entries GET/POST under
`/v1/workspaces/{workspace_id}/ledgers/{ledger_id}`. Clients submit intent and
expected head, not database paths, dialects, SQL or authoritative actor names.
Pages carry canonical record strings, snapshot head and a continuation cursor.
An append response's `accepted_head` identifies its entry, not the latest head
on an older replay.

The host must authenticate a credential, construct `Principal` from trusted
policy, and grant workspace `read` or `append` permission. Authorization happens
before resolving storage. Client payload author/reviewer/executor labels remain
claims, not permissions. There is no identity provider, token parser, delegation
issuer or HTTP listener in this slice. Authenticated principal is retained in
the receipt separately from payload claims. Application secret refusal is
reused; it is a heuristic, not a guarantee that all sensitive data is detected.

Request size is capped at 256 KiB of canonical request data, each record at
256 KiB, and each page at 200 records and 1 MiB of escaped record data plus its
envelope. A real HTTP host must additionally cap raw bytes before parsing and
reject duplicate keys. Handler errors are opaque: 400 invalid request, 401
unauthenticated, 403 forbidden, 409 head/key conflict, 503 storage unavailable
or corrupt. There is no automatic fallback or repair.

## Nostoi reuse, migration and trust limits

Nostoi supplies canonical JSON and native full-chain `weftmark-ledger-v1`
verification. Its own SQLite append store uses another record format and a
custom hash UDF, so it cannot simply replace WeftMark's storage on D1. We reuse
the codec/verifier rather than create a competing chain/signing protocol.

Explicit `import_jsonl(text)` validates all supported records before an atomic
import into an empty stream; record digests and sequences remain unchanged.
Whitespace can normalize, but unsupported values are refused, never coerced.
`export_jsonl()` emits verified Nostoi-compatible records. Source JSONL files
are untouched. Imports add receipts labeled `import`; they do not fabricate
historical authenticated actors. No Frog authority is copied or overridden.

`backup(destination)` makes and verifies a private, standalone SQLite online
backup, refusing overwrite. It is a database snapshot, not an off-host trust
anchor. Restores should open at a new path, verify SQLite integrity/foreign keys,
then check all streams and compare trusted external checkpoint heads before
switching consumers. Litestream cannot grant distributed multi-writer leases.

Chain integrity and SQL triggers do **not** stop the database-owning account
from dropping triggers, rewriting a whole chain or returning a valid prefix.
`verify_checkpoint(trusted_head)` detects truncation or a fork against an
externally remembered head; the caller must authenticate and retain that
checkpoint independently. It does not prove freshness, completeness, payload
truth or evidence truth. Receipt/principal metadata is deliberately outside the
historical v1 record hash, so a record-head checkpoint does **not** authenticate
receipts against a malicious database owner. Strong provenance needs separately
signed receipt coverage or a future versioned envelope, not a silent change to
existing entry digests. Signed/witnessed checkpoints remain separate work;
use Nostoi's existing attestation facilities rather than inventing another
signature scheme when that work is authorized.

## Evidence

```bash
uv run --locked --extra test python -m pytest tests/adapters/test_sqlite_ledger.py tests/adapters/test_ledger_api.py tests/contracts/test_sql_ledger.py
uv run --locked --extra test python -m pytest
uv run --locked python scripts/validate_tasks.py
```

Tests cover replay/CAS races, transaction rollback, principal binding, stream
isolation, immutable records, corruption, prefix rollback/forks against trusted
heads, exact JSONL digests, backup, numeric refusal and schema-shaped responses.
Independent public-contract review and real remote-host conformance are still
required before release.
