# WeftMark × Cline Kanban Integration
## Engineering Handoff

**Status:** Proposed integration architecture  
**Date:** 19 August 2026  
**Target:** Open-source, self-hostable, multi-agent software-work control plane with strong mobile/remote operation  
**Repositories:** `tabenius/WeftMark` and upstream `cline/kanban`

---

## 1. Purpose

This document hands off a proposed integration between **WeftMark** and **Cline Kanban**.

The core decision is:

> **Use Kanban as an interaction surface for WeftMark rather than making either product replace the other.**

Kanban already provides a strong human-facing environment for running many coding agents: task cards, per-task terminals and Git worktrees, parallel execution, diffs, review comments, dependency chains, commit/PR workflows, and a full Git interface.

WeftMark is intended to solve a different problem. It is a vendor-neutral control plane for **scope, Git lineage, evidence, handoff, review, provenance, and merge/release readiness**. It explicitly does not aim to become another coding agent, issue tracker, or build system.

The integration should therefore combine:

**Kanban's operational UX**

with

**WeftMark's coordination and evidence model.**

---

## 2. Product thesis

The integrated system should eventually feel like this:

```text
User opens phone/tablet/browser
            │
            ▼
      Kanban-style board
            │
 ┌──────────┼─────────────┐
 │          │             │
tasks    terminals      diffs
 │          │             │
 └──────────┼─────────────┘
            │
            ▼
         WeftMark
   coordination control plane
            │
 ┌──────────┼───────────────┐
 │          │               │
claims    evidence       readiness
scopes    provenance      handoffs
 │          │               │
 └──────────┼───────────────┘
            │
            ▼
       execution adapters
            │
 ┌──────────┼───────────┬─────────┐
 │          │           │         │
Codex    Claude Code  OpenCode   Cline
 │          │           │         │
 └──────────┼───────────┴─────────┘
            │
            ▼
 isolated workspace / container / VM
            │
            ▼
      Git branch/worktree
            │
            ▼
         GitHub PR
```

The board should tell a human:

- what is being worked on;
- by whom or by which agent;
- what the agent is doing;
- what changed;
- whether tests passed;
- whether the change conflicts with another task;
- what remains unresolved;
- whether it is safe to review;
- whether it is ready to merge.

Kanban already solves much of the first half.

WeftMark should provide the semantic basis for the second half.

---

## 3. The central domain mapping

The most important integration mapping is:

```text
Kanban task/card
        │
        ▼
WeftMark Change Set
```

But the two objects should **not become identical**.

A Kanban card is primarily a user-facing unit of work.

A WeftMark **Change Set** is the durable unit connecting intent to execution and review. The current WeftMark design associates it with repository, base revision, branch/worktree, agent session, scopes, commits, evidence requirements and review state.

Recommended mapping:

| Kanban concept | WeftMark concept | Authority |
|---|---|---|
| Board | Query/projection of active Change Sets | WeftMark |
| Card | Change Set presentation | WeftMark |
| Card title | Task/change description | WeftMark |
| Dependencies | Task/change dependencies | WeftMark |
| Running agent | Claim + actor + session | WeftMark |
| Task worktree | Change Set workspace | WeftMark lineage; Kanban manages UX |
| Branch | Change Set branch | WeftMark |
| Base commit | `base_sha` | WeftMark |
| Current commit | `head_sha` | WeftMark |
| Terminal | Worker-session surface | Kanban |
| Diff | Git projection | Shared |
| Diff comment | Review finding / agent feedback | Preferably WeftMark |
| Agent completion | Worker reports completion | Agent/Kanban |
| Tests | Evidence | WeftMark |
| PR | Forge reference on Change Set | WeftMark |
| Review result | Review verdict | WeftMark |
| Done column | Derived terminal state | WeftMark |

The board therefore becomes a **projection of domain state**, not the persistence layer defining domain truth.

---

## 4. Why this is preferable to directly merging the projects

A direct merger would create unnecessary coupling.

Kanban changes rapidly and is explicitly presented as a **research preview**. Its current strengths are execution, worktree management, agent monitoring and review UX.

WeftMark should remain usable when:

- Kanban changes;
- Kanban disappears;
- another board client is preferred;
- the user works from a CLI;
- an Android-native client is introduced;
- a TUI is used;
- another agent orchestrator controls execution;
- agents are launched remotely;
- GitHub is replaced by GitLab, Forgejo or another forge.

This matches the existing WeftMark architectural principle that CLI, MCP, TUI and future web/tablet interfaces should invoke the **same application services**, while adapters isolate Git, GitHub, agents, CI and other providers.

Therefore:

```text
BAD

Kanban internals
     │
     ├── WeftMark-specific business logic
     ├── WeftMark-specific DB tables
     └── evidence logic scattered through UI


GOOD

Kanban UI
     │
     ▼
WeftMark adapter/client
     │
     ▼
WeftMark application API
     │
     ▼
WeftMark domain
```

---

## 5. What Kanban contributes

Kanban already contains several features that WeftMark should **reuse rather than rebuild**.

### 5.1 Board interaction

Kanban's card model is particularly suitable for:

- pending work;
- parallel work;
- dependency relationships;
- agent activity summaries;
- work awaiting human review.

### 5.2 Per-task Git worktrees

Kanban creates an ephemeral worktree for each task so agents can operate concurrently.

This maps almost perfectly onto a WeftMark Change Set.

The difference is that WeftMark should record:

```text
change_set
  id
  repo
  base_sha
  branch
  worktree
  worker
  agent_session
  scopes
  evidence
  review_state
```

rather than merely knowing that a worktree exists.

### 5.3 Terminal

Kanban has a strong terminal model.

Recent versions moved PTY state server-side, allowing terminal sessions to survive browser navigation and reconnection and to have multiple simultaneous viewers.

WeftMark should not build another terminal emulator.

Instead WeftMark should associate the terminal/session with:

```text
actor
session
change_set
runtime
workspace
started_at
ended_at
```

and let Kanban render it.

### 5.4 Diff and review

Kanban already supports worktree diffs, checkpoint diffs and inline review comments.

This should become a front-end to WeftMark review findings.

For example:

```yaml
finding:
  id: finding_91
  change_set: chg_01
  reviewer: human:xyzzy
  file: src/auth/session.ts
  lines: [74, 81]
  severity: concern
  text: Session fallback bypasses tenant boundary.
  status: open
```

The UI remains Kanban.

The durable review finding belongs to WeftMark.

---

## 6. What WeftMark contributes

This is where the integration becomes more than a Kanban fork.

### 6.1 Semantic scopes

Worktrees prevent agents from editing the same working tree.

They do **not** prevent semantic collisions.

Example:

```text
Change A
  edits frontend/login.ts

Change B
  edits api/session.ts

Git:
  no conflict

Worktrees:
  no conflict

But both alter:
  contract:authentication-session
```

WeftMark should detect this.

Its proposed scope model already includes concepts such as:

```text
file:
contract:
schema:
boundary:
```

specifically because two agents can edit different files while modifying the same behavioral contract.

Kanban should visualize these collisions.

Possible card indicator:

```text
⚠ scope collision

contract: tenant-authentication

also claimed by:
chg_017 — Rotate session credentials
```

This is a major differentiator.

---

## 7. Evidence becomes first-class board state

A task being completed by an agent is not equivalent to a verified change.

WeftMark already distinguishes evidence states such as:

```text
declared
running
passed
failed
unavailable
stale
superseded
```

and evidence types such as unit tests, integration tests, CI, benchmarks, security reviews, artifacts and deployment probes.

Kanban should expose this directly.

A card might show:

```text
┌──────────────────────────────────┐
│ Fix tenant authentication        │
│                                  │
│ Codex • running                  │
│                                  │
│ ✓ unit tests                     │
│ ✓ lint                           │
│ ✗ integration                    │
│ ? GitHub CI unavailable          │
│                                  │
│ ⚠ contract:tenant-auth           │
│                                  │
│ EVIDENCE INCOMPLETE              │
└──────────────────────────────────┘
```

This is preferable to treating tests as log text hidden inside the agent conversation.

---

## 8. Proposed board state model

Do not directly equate board columns with agent process state.

Use derived WeftMark states.

Recommended first model:

```text
BACKLOG
   │
   ▼
CLAIMED
   │
   ▼
IMPLEMENTING
   │
   ▼
EVIDENCE INCOMPLETE
   │
   ├─────────────┐
   │             │
   ▼             ▼
REVIEW       BLOCKED
   │             │
   │             └──► IMPLEMENTING
   ▼
READY
   │
   ▼
MERGED
```

Additional useful terminal states:

```text
ABANDONED
SUPERSEDED
STALE — REVALIDATE
READY WITH FOLLOW-UP
```

These states should not necessarily all become physical Kanban columns.

Some are better represented as badges or filters.

For example:

```text
Columns:
Backlog | Active | Review | Ready | Done

Badges:
BLOCKED
STALE
EVIDENCE INCOMPLETE
SCOPE CONFLICT
CI UNAVAILABLE
```

This avoids turning the board into a state-machine diagram.

---

## 9. Source-of-truth rules

The integration should establish strict ownership rules early.

### WeftMark owns

- Change Set identity;
- task/change dependency graph;
- claims;
- actors;
- agent/session provenance;
- base and head revisions;
- declared scopes;
- semantic collisions;
- evidence requirements;
- evidence results;
- review findings;
- review verdict;
- handoffs;
- readiness;
- merge/release policy;
- event/audit history.

### Kanban owns

- board layout;
- panel layout;
- terminal rendering;
- terminal viewport state;
- diff visualization;
- user interaction details;
- responsive/mobile presentation;
- temporary UI state.

### Git owns

- commits;
- branches;
- trees;
- blobs;
- merge ancestry.

### Forge owns

- remote PR object;
- remote checks;
- remote comments;
- repository permissions.

### Execution runtime owns

- process;
- filesystem namespace;
- CPU/memory;
- containers/VMs;
- network namespace.

WeftMark should reference all of these without pretending to replace them.

---

## 10. Proposed WeftMark service API

Exact implementation language and transport are intentionally not prescribed here.

The first interface could be HTTP/JSON plus an event stream.

Conceptually:

```text
GET    /api/projects
GET    /api/changes
POST   /api/changes

GET    /api/changes/:id
PATCH  /api/changes/:id

POST   /api/changes/:id/claim
POST   /api/changes/:id/release

POST   /api/changes/:id/start
POST   /api/changes/:id/stop

GET    /api/changes/:id/scopes
POST   /api/changes/:id/scopes

GET    /api/changes/:id/evidence
POST   /api/changes/:id/evidence

GET    /api/changes/:id/findings
POST   /api/changes/:id/findings

POST   /api/changes/:id/review

POST   /api/changes/:id/handoff

GET    /api/changes/:id/events
GET    /api/events
```

Real-time changes could use:

```text
WebSocket
Server-Sent Events
```

or another lightweight subscription transport.

Do not make the browser poll SQLite.

---

## 11. Event model

The UI should update from immutable or append-friendly domain events wherever practical.

Example:

```json
{
  "event": "evidence.recorded",
  "change_set": "chg_01a7",
  "timestamp": "2026-08-19T12:41:03Z",
  "producer": "github-actions",
  "subject_sha": "91f...",
  "evidence": {
    "kind": "integration",
    "status": "failed",
    "name": "integration-two-host"
  }
}
```

Useful events include:

```text
change.created
change.updated

claim.acquired
claim.released

scope.claimed
scope.released
scope.collision_detected

worker.started
worker.stopped

commit.observed
head.changed

evidence.declared
evidence.started
evidence.recorded
evidence.stale

finding.created
finding.resolved

review.requested
review.recorded

handoff.created
handoff.accepted

pr.opened
pr.updated

change.ready
change.blocked
change.merged
```

Kanban subscribes and updates cards.

This also makes a future Android client straightforward.

---

## 12. Execution architecture

A key improvement over stock Kanban should be optional **runtime isolation per Change Set**.

Kanban worktrees provide source-tree separation.

That is not equivalent to process isolation.

Two agents with different worktrees can still collide over:

- TCP ports;
- Docker;
- databases;
- `/tmp`;
- caches;
- environment variables;
- credentials;
- background processes;
- browser state;
- external test environments.

Target model:

```text
Change Set
    │
    ├── Git worktree
    │
    └── Runtime
         │
         ├── process namespace
         ├── filesystem boundary
         ├── network policy
         ├── credentials
         └── resource limits
```

Possible adapters:

```text
local-process
docker
podman
remote-ssh
cloud-vm
kubernetes
sandbox-provider
```

The first implementation does not need all of these.

Start with:

```text
local-process
docker
```

while keeping the interface abstract.

---

## 13. Worker abstraction

The worker should be replaceable.

Define something conceptually similar to:

```text
WorkerAdapter
    start(change_set, runtime, handoff?)
    resume(session)
    send(session, message)
    interrupt(session)
    status(session)
    stop(session)
```

Implementations might include:

```text
CodexAdapter
ClaudeCodeAdapter
OpenCodeAdapter
ClineAdapter
ACPAdapter
HumanTerminalAdapter
```

Where ACP works, prefer using it rather than creating a new universal coding-agent protocol. WeftMark's architecture already explicitly favors using ACP and MCP through adapters.

---

## 14. Handoff between agents

WeftMark handoffs are particularly valuable in the Kanban UI.

A user should be able to do:

```text
Card
 └─ ⋮
     └─ Hand off
          ├─ Codex
          ├─ Claude Code
          ├─ OpenCode
          └─ Human
```

The receiving worker should not need the previous chat transcript.

The WeftMark handoff should contain the compact state needed to continue:

```text
change_set
base/head
branch
workspace
active scopes
important decisions
evidence state
open findings
known failures
next action
```

This matches the existing WeftMark concept of generating handoffs from the ledger instead of from human memory.

This could become one of the strongest features of the combined product.

---

## 15. GitHub integration

Kanban already exposes commit/open-PR operations to the user.

In the integrated system, opening a PR should also update the Change Set:

```yaml
forge:
  provider: github
  repository: tabenius/example
  pull_request: 42
  base: main
  head: weft/chg_01
```

GitHub checks should then become WeftMark evidence.

Example:

```text
GitHub Actions
      │
      ▼
GitHub adapter
      │
      ▼
evidence
  kind: CI
  producer: github-actions
  commit: abc123
  status: passed
```

If the Change Set receives another commit:

```text
abc123 → def456
```

evidence against `abc123` may automatically become:

```text
STALE
```

unless the evidence policy declares it reusable.

This is much stronger than simply showing a green GitHub check icon.

---

## 16. Review and merge rules

A Kanban **Commit** or **Open PR** button is an execution action.

A WeftMark **READY** verdict is a policy decision.

Do not collapse the two.

Example:

```text
Agent says:
DONE

Kanban says:
worktree clean
PR opened

GitHub says:
CI passed

WeftMark says:
BLOCKED

Reason:
required security review missing
```

This distinction is central to WeftMark.

The current product design explicitly separates implementation, testing, CI verification, review and merge/release readiness.

---

## 17. Mobile and Android strategy

Kanban is currently particularly useful here.

Recent versions added:

- mobile-responsive layouts;
- remote file browsing;
- Git clone by repository URL;
- HTTPS/passcode authentication;
- device-code authorization for remote Cline sessions;
- PWA installation;
- persistent server-side terminal sessions.

The initial Android strategy should therefore be:

```text
Do not port agents to Android.

Run:
  WeftMark
  Kanban
  agents
  Git
  runtime sandboxes

on:
  workstation / server / VPS

Run:
  board
  diff viewer
  review
  terminal viewport
  notifications

on:
  Android browser/PWA
```

Later:

```text
PWA
   ↓
thin Android shell
   ↓
native integration where valuable
```

Possible native additions:

- Android share intent → attach context to Change Set;
- notifications;
- biometric unlock;
- SSH/VPN integration;
- file picker;
- camera/image attachment;
- voice instructions;
- background notification channel.

The core application protocol should remain independent of Android.

---

## 18. Security requirements

Remote access makes the integrated product substantially more security-sensitive.

WeftMark can ultimately:

- assign work;
- start agents;
- create worktrees;
- start processes;
- expose terminals;
- cause Git commits;
- trigger PRs;
- collect evidence;
- influence merge readiness.

The existing WeftMark design already calls for least privilege, provider-specific secret stores, authenticated/auditable remote execution and separation of merge/release powers from editing powers.

Minimum requirements:

### Authentication

Remote board access must require authentication.

Do not rely solely on obscurity or an unguessable port.

### Authorization

Capabilities should eventually distinguish:

```text
view
comment
prompt agent
start worker
stop worker
modify task
modify scope
approve review
open PR
merge
release
admin
```

### Credentials

Do not store provider secrets in the WeftMark event ledger.

Store references to credentials managed elsewhere.

### Agent permissions

Worker credentials should be scoped to the Change Set whenever possible.

### Terminal exposure

A remote terminal is effectively remote shell access.

Treat it accordingly.

### Evidence provenance

Evidence produced by the worker being evaluated should be distinguishable from independently produced evidence.

Example:

```text
self-attested:
  agent ran pytest

independent:
  GitHub Actions ran pytest from pushed commit
```

Both are useful.

They are not equivalent.

---

## 19. Licensing gate

This must happen before copying code between repositories.

Cline Kanban is published under **Apache 2.0**.

The current WeftMark repository explicitly says that **no project license has yet been selected** and warns not to assume open-source redistribution rights until a license is committed.

Therefore the first integration phase should avoid copying substantial Kanban source into WeftMark.

Prefer:

```text
protocol integration
API adapter
separate processes
experimental external client
```

until the WeftMark license decision is resolved.

A permissive WeftMark license compatible with Apache-2.0 reuse would make deeper integration much simpler.

---

## 20. Recommended integration strategy

Use three progressively deeper levels.

### Level 1 — External integration

No Kanban fork.

```text
Kanban
   │
adapter/plugin/bridge
   │
WeftMark API
```

Goal:

Prove the domain mapping.

Capabilities:

- list WeftMark Change Sets as cards;
- display status/evidence;
- jump from card to existing Kanban task;
- synchronize branch/worktree identity.

This is the safest first experiment.

### Level 2 — WeftMark-aware Kanban

Maintain a small downstream patch or integration package.

Add:

- evidence badges;
- scope collision badges;
- readiness state;
- handoff action;
- review findings;
- WeftMark event subscription.

The board becomes genuinely WeftMark-native while keeping most Kanban UX intact.

### Level 3 — Shared reusable UI components

Only after experience proves which abstractions are stable.

Potentially extract:

```text
board
terminal
diff viewer
Git history
mobile navigation
```

from Kanban-derived code into reusable frontend packages.

Do **not** begin here.

Premature UI extraction risks coupling WeftMark to Kanban internals before the application API is proven.

---

## 21. Phase plan

### Phase 0 — Foundation and licensing

Deliverables:

- select and commit WeftMark open-source license;
- document Kanban Apache-2.0 compatibility;
- record architectural decision: WeftMark is source of truth;
- define Change Set lifecycle;
- define API/versioning strategy;
- define actor/session identity model.

Exit criterion:

> We can explain exactly which system owns every important piece of state.

### Phase 1 — Read-only Kanban projection

Implement a small WeftMark HTTP service.

Endpoints:

```text
GET /changes
GET /changes/:id
GET /changes/:id/evidence
GET /changes/:id/events
```

Build a minimal Kanban-side adapter.

Show:

- title;
- state;
- worker;
- branch;
- base/head;
- evidence summary;
- scope warning;
- readiness.

No mutation yet.

Exit criterion:

> A WeftMark project can be observed from a Kanban-style board without duplicating domain state.

### Phase 2 — Claims and worktrees

Support:

```text
claim
release
start
stop
```

Make creation of a running task establish:

```text
task
→ Change Set
→ claim
→ branch/worktree
→ worker session
```

Record every resulting identity in WeftMark.

Exit criterion:

> Starting a card creates an auditable WeftMark execution lineage.

### Phase 3 — Evidence

Capture:

- local commands;
- unit tests;
- integration tests;
- artifacts;
- GitHub CI.

Attach every result to a commit.

Automatically mark old evidence stale when appropriate.

Exit criterion:

> Board status can distinguish "agent says done" from "verified".

### Phase 4 — Review

Connect Kanban diff comments to WeftMark findings.

Implement verdicts:

```text
READY
READY WITH FOLLOW-UP
BLOCKED
STALE — REVALIDATE
EVIDENCE INCOMPLETE
```

Exit criterion:

> Human review produces durable structured state rather than merely chat/comments.

### Phase 5 — Handoff

Support agent switching:

```text
Codex → Claude
Claude → OpenCode
agent → human
human → agent
```

without reconstructing context from chat history.

Exit criterion:

> Another worker can continue a Change Set from a generated WeftMark handoff.

### Phase 6 — Runtime isolation

Introduce runtime adapter interface.

First:

```text
local
docker
```

Associate runtime identity with Change Set.

Exit criterion:

> Two parallel Change Sets can run potentially conflicting services without unintentionally sharing runtime state.

### Phase 7 — Remote/mobile hardening

Build on Kanban's existing responsive UI, remote access and PWA work.

Add:

- strong auth;
- device management;
- notification stream;
- mobile review flow;
- reconnect-safe agent controls.

Exit criterion:

> A user can safely supervise and review multi-agent work from Android while execution remains remote.

---

## 22. Suggested module boundaries

A plausible WeftMark implementation remains close to its current design:

```text
weftmark/
  domain/
    tasks.py
    changesets.py
    claims.py
    scopes.py
    evidence.py
    findings.py
    reviews.py
    handoffs.py
    actors.py
    sessions.py

  application/
    change_service.py
    worker_service.py
    evidence_service.py
    review_service.py
    handoff_service.py

  adapters/
    git.py
    github.py
    ci.py
    acp.py
    mcp.py

    runtime/
      local.py
      docker.py

    workers/
      codex.py
      claude.py
      opencode.py
      cline.py

  interfaces/
    cli/
    mcp/
    http/
    events/
```

Kanban integration should live outside the domain layer.

For example:

```text
integrations/
  kanban/
```

or as a separate package/repository.

---

## 23. First implementation backlog

The next agent should work in approximately this order.

### WMK-KAN-001 — License gate

Select the WeftMark license and document compatibility with Apache-2.0 Kanban.

### WMK-KAN-002 — Formalize Change Set schema

Ensure the schema contains:

```text
id
task
repo
base_sha
head_sha
branch
worktree
state
actor
session
runtime
```

### WMK-KAN-003 — Define projection schema

Create a stable JSON representation suitable for a board client.

Example:

```json
{
  "id": "chg_01",
  "title": "Fix tenant authentication",
  "state": "evidence_incomplete",
  "worker": {
    "kind": "codex",
    "session": "session_42"
  },
  "git": {
    "base": "7c3...",
    "head": "91f...",
    "branch": "weft/tenant-auth"
  },
  "evidence": {
    "passed": 3,
    "failed": 1,
    "unavailable": 1,
    "stale": 0
  },
  "collisions": [
    "contract:tenant-authentication"
  ],
  "review": {
    "verdict": null
  }
}
```

### WMK-KAN-004 — Minimal HTTP API

Implement:

```text
GET /changes
GET /changes/:id
```

### WMK-KAN-005 — Event stream

Expose Change Set updates to clients.

### WMK-KAN-006 — Kanban architecture spike

Before modifying Kanban deeply, identify its current internal boundaries for:

- task persistence;
- card state;
- worktree creation;
- worker launch;
- PTY management;
- diff service;
- Git service;
- frontend/server protocol.

Produce a short mapping document.

Do not assume these internals are stable.

### WMK-KAN-007 — Read-only board prototype

Render WeftMark Change Sets inside a Kanban-derived board view.

### WMK-KAN-008 — Evidence badge prototype

Add:

```text
✓ PASS
✗ FAIL
? UNAVAILABLE
↻ STALE
```

### WMK-KAN-009 — Scope collision prototype

Show semantic-scope conflicts directly on cards.

### WMK-KAN-010 — End-to-end dogfood

Use the integration to implement its own next feature.

Required lifecycle:

```text
task
→ claim
→ branch/worktree
→ implementation
→ evidence
→ PR
→ review
→ merge
```

This is already the process WeftMark proposes to dogfood.

---

## 24. Acceptance scenario

A useful first vertical slice is:

### Step 1

User creates:

```text
Add tenant session expiry enforcement
```

### Step 2

WeftMark creates:

```text
Change Set chg_27
```

with:

```text
base_sha
required evidence
contract:tenant-authentication
```

### Step 3

The Kanban UI shows a card.

### Step 4

User presses **Start with Codex**.

### Step 5

WeftMark:

```text
claims change
creates/records worktree
records actor/session
starts runtime
launches Codex
```

### Step 6

Kanban displays the live terminal and activity.

### Step 7

Codex changes code.

WeftMark updates `head_sha`.

### Step 8

Local tests run.

```text
unit: passed
integration: failed
```

Card displays:

```text
EVIDENCE INCOMPLETE
```

rather than "done".

### Step 9

User opens the diff on Android and comments on three lines.

The comments become WeftMark findings.

### Step 10

User chooses:

```text
Hand off → Claude Code
```

WeftMark generates a handoff from durable state.

Claude resumes the same Change Set.

### Step 11

Integration test passes.

PR is opened.

GitHub CI passes.

### Step 12

WeftMark records:

```text
READY
```

### Step 13

Authorized user merges.

Final ledger contains:

```text
intent
claim
worker sessions
scope
base/head lineage
commits
test evidence
CI evidence
review findings
handoff
review verdict
PR
merge
```

That end-to-end scenario should be the initial product benchmark.

---

## 25. Non-goals

Do not initially:

- rewrite Kanban;
- implement another terminal emulator;
- implement another diff engine;
- invent a universal agent protocol;
- make WeftMark an LLM agent;
- make WeftMark a Git replacement;
- turn SQLite into a distributed database;
- put agent chat transcripts at the center of provenance;
- port Codex/Claude/OpenCode runtimes to Android;
- build Kubernetes support before local/Docker execution works;
- make every WeftMark state a Kanban column;
- automatically merge merely because an agent reports completion.

---

## 26. Important design principle

The combined product should optimize for **replaceability**.

Any of these should be replaceable:

```text
Kanban UI
Codex
Claude Code
OpenCode
Cline
GitHub
Docker
Android client
CI provider
```

without destroying historical Change Set records.

The durable object is:

```text
intent
+
scope
+
lineage
+
execution provenance
+
evidence
+
review
+
handoff
+
decision
```

That is WeftMark.

---

## 27. Desired outcome

If successful, the integration produces something more ambitious than either project alone:

> **A mobile-capable, self-hostable control plane for parallel AI software engineering in which every unit of work has isolated execution, explicit scope, Git lineage, durable agent provenance, test/CI evidence, review findings, portable handoffs, and an explainable merge-readiness decision.**

Kanban supplies an unusually strong starting point for the **human control surface**.

WeftMark supplies the missing **coordination and trust model**.

The first implementation should therefore avoid asking:

> "How do we put WeftMark features into Kanban?"

and instead ask:

> **"What stable WeftMark application API would let Kanban become an excellent client while still allowing a CLI, TUI, Android app or future UI to do the same work?"**

That API boundary is the first thing worth proving.

---

## References

### WeftMark

- [`README.md`](../../README.md)
- [`docs/chapters/08-5-core-concepts.mdx`](../chapters/08-5-core-concepts.mdx)
- [`docs/chapters/09-6-proposed-weftmark-architecture.mdx`](../chapters/09-6-proposed-weftmark-architecture.mdx)
- [`docs/chapters/10-7-implementation-perspective.mdx`](../chapters/10-7-implementation-perspective.mdx)

### Cline Kanban

- Upstream repository: <https://github.com/cline/kanban>
- README: <https://github.com/cline/kanban/blob/main/README.md>
- Changelog: <https://github.com/cline/kanban/blob/main/CHANGELOG.md>
