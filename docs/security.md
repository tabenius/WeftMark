# WeftMark prototype threat model

Status: implementation review candidate for `security-threat-model`.

This document describes the trust boundaries and minimum secure defaults of the
current WeftMark prototype. It covers local coordination, the append-only
ledger, command evidence, handoffs, the stdio MCP surface, the loopback HTTP
surface, and read-only forge adapters. It does not claim that the prototype is a
multi-tenant service, an identity provider, a secret store, a sandbox, or a
release-signing system.

## Security objectives

WeftMark should fail closed when it cannot establish the lineage, authority, or
evidence needed for an operation. Its security objectives are:

1. keep credentials and raw command output out of durable coordination records;
2. bind evidence and review decisions to an exact Change Set and Git head;
3. make ledger corruption and stale evidence visible;
4. separate read access, coordination writes, command execution, review, and
   release authority;
5. treat repository, forge, worker, and model-provided text as untrusted data;
6. preserve unavailable, unsupported, missing, failed, and stale as distinct
   states; and
7. refuse network exposure or mutation authority unless the operator enables a
   separately protected boundary.

## Assets and invariants

The protected assets are task intent, Change Set lineage, semantic claims,
evidence metadata, review decisions, handoffs, repository contents, forge
credentials, local control tokens, and the authority to execute commands or
change coordination state.

The following invariants are security-sensitive:

- A claimed actor or session identifier is provenance, not proof of a human or
  model's real-world identity.
- A ledger digest chain is tamper-evident under normal local operation; it is
  not a signature and cannot stop a local account that can replace the ledger
  from recomputing the entire chain.
- Successful command exit is evidence about one recorded invocation, not proof
  that arbitrary prose about that invocation is true.
- Forge approvals and forge CI are external observations. They never become a
  WeftMark review decision or release authorization automatically.
- A `ready` review remains bound to its recorded head and evidence snapshot.
  Head movement or obsolete required evidence must prevent reuse as current
  readiness.
- A capability grants only the named application operation. Read access, claim
  writes, handoff writes, and command execution are separate powers.

## Actors and trust boundaries

### Human operator and local operating-system account

The current local deployment trusts the operating-system account that launches
WeftMark to select the repository, ledger, token files, forge endpoints, and
write capabilities. WeftMark does not defend against root or an attacker with
equivalent control of that account. Operators should use a dedicated account or
isolated workspace when workers with different trust levels share a machine.

Actor IDs, session IDs, reviewer IDs, and producer IDs are caller-supplied
provenance labels; they are not cryptographic authentication. Until an
authenticated identity adapter is added, policy must not interpret those
strings as organizational membership or proof that implementation and review
were performed by separate people.

### Repository and worktree

Repository files, diffs, branch names, commit messages, generated fixtures, and
tool output are untrusted inputs. They may contain prompt injection, terminal
control sequences, misleading test names, oversized data, symlinks, or text
that resembles instructions. A worker or reviewer must not execute repository
text merely because it appears in a forge comment, handoff, or projected board.

The local command-evidence runner is not a sandbox. It uses an argument vector
with `shell=False`, requires a clean bound worktree, confines the working
directory to that worktree, and applies a timeout, but the invoked program has
the permissions and inherited environment of the WeftMark process. Command
execution therefore requires a distinct high-risk capability and an external
sandbox when code is not trusted.

### Local ledger

The JSONL adapter uses a process lock, monotonic sequence, SHA-256 digest chain,
`0600` file mode, compare-and-append support, and refusal of a symlinked ledger
or immediate ledger directory. The application layer rejects secret-shaped
keys and text before append. Readers validate every record and fail closed on
invalid JSON, sequence changes, or a broken chain.

These controls detect accidental corruption and unsophisticated replacement;
they do not provide non-repudiation, rollback protection, trusted timestamps,
remote replication, or protection from the owning account. Durable assurance
requires an independently retained head digest or a future signed/exported
evidence mechanism.

### Handoffs and portable records

Handoffs carry bounded continuation data and references to evidence and review
records. Secret assignments, token prefixes, URL credentials, and private keys
are refused. A credential reference may be recorded when it does not contain
the credential itself. Receivers must still treat goals, failures, next actions,
diff excerpts, and imported records as untrusted assertions until they verify
local lineage and evidence.

### MCP stdio boundary

MCP v0 uses stdio. The launching parent process and its configuration are the
transport authentication boundary. There is no network listener and no MCP
user authentication in the prototype. Read tools are registered by default;
write tools are registered only for explicitly granted capabilities, and the
application service checks the same capability again. Command evidence has its
own `evidence-exec` capability and is not implied by another write capability.

MCP tool annotations and model reasoning are not authorization mechanisms. An
untrusted MCP client that can drive an enabled process may exercise every
capability granted to that process. Run separate least-privilege MCP processes
for materially different workers, and do not enable write capabilities for a
read-only client.

### Loopback HTTP boundary

HTTP v0 refuses non-loopback binds. Remote or mobile access requires an
authenticated TLS reverse proxy or secure tunnel outside WeftMark. Loopback is
an exposure reduction, not user authentication: other processes running as
local users may be able to connect.

The read projection can use an optional bearer token. Control is disabled by
default and requires both a dedicated write-token file and one or more explicit
write capabilities. Requests use constant-time token comparison, strict JSON
handling, bounded request bodies, and idempotency keys for mutations. A read
token does not grant write authority unless an operator deliberately reuses the
same value.

Token files are read by the trusted local process. The prototype does not
currently enforce their ownership or mode, so operators must provision them
with least-privilege filesystem permissions and must not pass tokens in command
arguments, URLs, task plans, or ledger payloads.

### Remote forge adapters

Forge adapters are optional and read-only. Tokens remain request-local
configuration, endpoint failures become unavailable observations, unsupported
features remain distinct from missing data, and provider approvals never become
local readiness automatically. Adapter result values must not contain tokens.

Forge API responses, comments, review text, paths, links, identities, check
names, and status descriptions are remote untrusted data. They may be stale,
malicious, truncated, or inconsistent with local Git. Callers must bind relevant
observations to repository and commit identity before using them as evidence.
Operators must allowlist the configured forge host and use TLS; a configurable
enterprise base URL must not be derived from repository-controlled text. Forge
write operations remain outside v0 and require a new capability contract and
threat-model review.

## Abuse cases and mitigations

| ID | Abuse case | Current mitigation | Residual risk / required response |
| --- | --- | --- | --- |
| WM-T01 | A secret is placed in a task, handoff, command argument, or ledger payload. | Ledger and handoff services reject common secret forms; evidence arguments can be redacted; raw stdout/stderr are represented by digests. | Pattern matching is not complete. Use secret references and external secret stores; rotate any exposed value. |
| WM-T02 | A worker claims another actor or reviewer identity. | Actor/session/reviewer IDs remain visible provenance. | IDs are not authenticated. Do not use them for separation-of-duties claims without an identity adapter or independent review evidence. |
| WM-T03 | A worker rewrites, truncates, or reorders the local ledger. | Full-chain validation, monotonic sequence, locking, private mode, and symlink refusal fail closed. | The owning account can replace and recompute the chain. Retain a head digest or signed bundle outside that account for stronger assurance. |
| WM-T04 | Old passing evidence is replayed after the branch moves. | Evidence is bound to a commit; obsolete or stale required evidence blocks current readiness. | Policies and consumers must use the latest lineage observation and must not display an old decision as current. |
| WM-T05 | A forge approval or green status bypasses WeftMark review. | Forge state is a read-only external observation and never auto-promotes readiness. | Human interfaces must keep provider approval, evidence satisfaction, WeftMark review, and release authority visually distinct. |
| WM-T06 | A forge comment, task, diff, or handoff injects instructions into an agent. | Remote and repository text is classified as untrusted data; no forge write API exists in v0. | Agent harnesses must isolate quoted data from control instructions and require normal scope/capability checks. |
| WM-T07 | An MCP client invokes a mutation or arbitrary command. | Write tools require explicit per-process capabilities; command execution is a separate capability checked in the service. | The parent process is trusted and command execution is not sandboxed. Use a least-privilege process and external sandbox. |
| WM-T08 | A local HTTP endpoint is exposed to a network or used with a read token for writes. | Non-loopback binds are refused; control needs a dedicated write token and explicit capability. | Loopback peers are not mutually authenticated. Use a TLS/authenticated proxy or tunnel for any remote access and separate token values. |
| WM-T09 | An attacker uses a symlink or path escape to redirect writes or command execution. | Ledger target/immediate directory symlinks are refused; evidence cwd must resolve inside the bound worktree. | Ancestor replacement and privileged filesystem races are outside the local-account threat model. Use protected directories and isolated workspaces. |
| WM-T10 | A provider outage is reported as failed CI, or absent CI is reported as success. | `unavailable`, `unsupported`, `missing`, `failed`, and `stale` are separate states. | UI and policy adapters must preserve the distinctions and fail closed for required evidence. |
| WM-T11 | Oversized requests, output, or remote responses exhaust local resources. | HTTP control bodies and evidence runtime are bounded; provider calls use bounded page/request behavior where implemented. | Raw command output is captured in memory before hashing and the ledger has no quota. Run untrusted commands in a resource-limited environment and monitor storage. |
| WM-T12 | A dependency or generated artifact is substituted during packaging or release. | Git lineage and evidence can identify the source head; release evidence is planned separately. | The prototype does not yet provide signed releases, an SBOM, or a reproducible package gate. Do not claim release integrity before `alpha-release-evidence` is complete. |

## Minimum secure defaults

The following defaults are mandatory for the prototype:

- local-only operation must work without forge or model-provider credentials;
- secrets are supplied through process configuration or protected files and are
  never copied into task, evidence, review, handoff, bundle, or ledger payloads;
- the ledger remains under a private directory with a `0600` file and is not
  placed in a shared or repository-controlled symlink path;
- MCP uses stdio, starts read-only, and enables only individually named write
  capabilities;
- `evidence-exec` is enabled only for a process whose caller and command policy
  are trusted, preferably inside an external sandbox;
- HTTP binds only to loopback; control stays disabled without a separate write
  token and explicit capabilities; remote transport terminates authenticated
  TLS before reaching the loopback listener;
- forge adapters remain read-only, use allowlisted TLS endpoints, keep tokens
  request-local, and map outages to unavailable rather than failed evidence;
- review and release consumers reject obsolete-head decisions and missing,
  failed, unavailable, or stale required evidence; and
- independent review is recorded separately from implementation before this
  threat-model task can be marked done.

## Security review checklist

An independent reviewer should verify the four declared boundaries
(`agent-identity`, `credential-access`, `remote-forge`, and `mcp-write`) against
the implementation and tests, then record:

- whether the stated non-guarantees are accurate;
- whether any credential can enter a returned or durable value;
- whether all mutation and command paths enforce capabilities in application
  code rather than relying on UI or protocol metadata;
- whether stale lineage, corrupted ledgers, unavailable providers, and review
  blockers fail closed;
- whether newly added network or forge operations widen the model; and
- any accepted residual risk, owner, and follow-up task.

Implementation and passing tests are not independent review evidence. Until
that review is recorded, `security-threat-model` remains in `review`, not
`done`.
