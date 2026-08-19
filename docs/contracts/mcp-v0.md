# MCP surface contract v0

WeftMark exposes an optional **stdio Model Context Protocol (MCP)** server so coding agents can read and mutate the same application model used by the CLI, HTTP/Kanban surface, and local workflows.

MCP is an interface adapter, not a second implementation of WeftMark semantics.

## Packaging and transport

Core WeftMark remains dependency-free at runtime. MCP support is an optional package extra:

```text
pip install 'weftmark[mcp]'
```

The `weftmark-mcp` entry point uses the official Python MCP SDK and serves **stdio only** in v0.

Stdio is deliberate: the parent process and its launch configuration are the transport trust boundary. V0 does not expose MCP over a network listener. A future Streamable HTTP MCP surface requires its own authentication, authorization, origin, and deployment threat model rather than inheriting assumptions from the loopback Kanban HTTP server.

## Read-only default

Starting `weftmark-mcp` without `--write-capability` registers only read tools.

Read tools:

| Tool | Purpose |
| --- | --- |
| `weft_workspace_status` | Change Sets, claims, evidence counts, blockers and readiness |
| `weft_task_list` | native task intent |
| `weft_task_next` | advisory dependency/conflict-aware next-task selection |
| `weft_task_eligibility` | explain one task's current eligibility |
| `weft_change_show` | one Change Set through the shared status model |
| `weft_evidence_list` | durable command evidence |
| `weft_review_list` | durable review decisions/findings |
| `weft_handoff_list` | durable handoffs without chat replay |

Read tools use the same application serializers/read models as other WeftMark interfaces.

## Write capabilities

Write tools are **not registered at all** unless their capability is granted when the MCP process starts.

```text
weftmark-mcp --write-capability claim --write-capability handoff
```

V0 capabilities:

| Capability | Tool | Authority |
| --- | --- | --- |
| `claim` | `weft_task_claim` | `ControlService` → native TaskClaim/Claim workflows |
| `release` | `weft_claim_release` | `ControlService` → ClaimService |
| `handoff` | `weft_handoff_create` | `ControlService` → clean-head LocalWorkflow handoff |
| `scope-audit` | `weft_scope_audit` | record scope audit/semantic-change observations |
| `evidence-exec` | `weft_evidence_run` | execute local command evidence in a bound clean worktree |

The application facade checks the capability again even though an ungranted tool is absent from `tools/list`. Tool registration is therefore defense in depth, not the only permission check.

## MCP annotations are not authorization

Read tools advertise read-only/idempotent/closed-world hints. Write tools advertise their mutating/destructive/idempotent characteristics where applicable.

These annotations are advisory metadata for MCP clients. WeftMark never treats a client's interpretation of those hints as a security boundary. Process capabilities and application-service rules remain authoritative.

## Claim and release

`weft_task_claim` and `weft_claim_release` use the same durable `ControlService` as the Kanban HTTP write bridge.

Consequences:

- native task eligibility and semantic scope ownership rules are unchanged;
- explicit Change Set/claim identities make retries recoverable;
- an idempotency key is required for actual mutation;
- identical retries return the prior durable result;
- reusing a key for different intent fails closed;
- raw idempotency keys are not persisted in the ledger.

Both tools support a non-mutating `dry_run` mode. A claim dry-run reports task eligibility and proposed identities; release dry-run reports the current claim plus requested owner/reason.

## Handoffs

`weft_handoff_create` creates the same clean-head durable handoff as the CLI/HTTP surface.

It does not transfer prior chat or terminal history. The handoff-context budget/materializer remains a separate step when a receiving runtime/provider is selected.

## Scope audit

`weft_scope_audit` records actual changed paths plus caller-declared semantic changes through the existing scope-audit workflow.

It does **not** mutate declared Change Set scopes or bypass semantic claim ownership. `dry_run=true` parses and reports the proposed semantic scope observations without refreshing Git or writing an audit record.

## Evidence execution

`evidence-exec` is intentionally separate from ordinary coordination capabilities because it can launch a local process.

`weft_evidence_run`:

- is absent unless `--write-capability evidence-exec` is granted;
- defaults to `dry_run=true`;
- requires an existing bound Change Set/worktree when executed;
- inherits the clean-worktree requirement from `LocalEvidenceRunner`;
- executes with `shell=False`;
- can only use a working directory inside the bound worktree;
- records stdout/stderr by digest/artifact reference rather than copying output into MCP context;
- supports explicit argument-index redaction for recorded command provenance;
- does not allow MCP callers to inject environment variables in v0.

The tool may still execute commands with side effects when `dry_run=false`. MCP annotations mark it destructive/non-idempotent, but the real control is the explicit `evidence-exec` process capability.

## Error behavior

Application/domain exceptions are tool failures visible to the calling model through normal MCP tool-error handling. The MCP layer does not reinterpret a failed command as failed evidence unless the evidence workflow itself produced that state.

Protocol metadata must never promote evidence, review readiness, merge state, or release state.

## Non-goals

V0 does not:

- expose MCP over remote HTTP;
- use MCP tool annotations as authorization;
- provide arbitrary ledger writes;
- provide merge/release commands;
- expose runtime-provider start/stop/switch commands;
- inject environment variables into evidence commands;
- make chat history canonical coordination state;
- duplicate CLI/HTTP business logic inside the MCP adapter.

Runtime provider switching may be added later as another explicit capability once a configured provider registry is available to the MCP process.
