# WeftMark

<p align="center">
  <img src="assets/weftmark.svg" alt="WeftMark logo" width="280">
</p>

**WeftMark is an open-source prototype control plane for human and AI software
work:** a vendor-neutral ledger for task intent, scope, Git lineage, evidence,
handoff, review, and merge/release readiness.

It grows out of the operational lessons encoded in **RAGBAZ Frog**, but it is
not another coding agent, issue tracker, or build system. WeftMark concentrates
on the question that becomes scarce when many workers can write code at once:

> **Can we explain why a worker was allowed to change something, what actually
> changed, what evidence says it works, who reviewed it, and whether it is
> genuinely ready to merge or release?**

## Repository status

This repository contains a working **local-first prototype** and its executable
design record. The full test suite passes locally at this branch head (441
tests), but WeftMark is not yet a packaged alpha or a production control plane.
The project deliberately keeps `implemented`, `verified`, `reviewed`, and
`releasable` separate.

What works today:

- Native dependency-aware task intent, explicit promotion from imported Frog
  intent, and atomic task-to-Change-Set claim composition.
- Change Sets bound to Git lineage, file and semantic scopes, leased claims,
  scope collision detection, and dirty-worktree observation.
- Exact-head command evidence, review decisions, continuation handoffs,
  lifecycle policy, portable bundles, and offline bundle verification.
- CLI, MCP, loopback-only HTTP reads, a stable Kanban projection, and a
  dependency-free tablet/phone review client.
- Read-only Frog snapshot receipts, eligibility planning, promotion, and native
  claim workflows for an incremental migration rather than a flag-day rewrite.
- A provider-neutral runtime port. The ACP stdio adapter, named provider
  registry, and claim-gated `weftmark runtime` CLI are implemented and locally
  verified on this branch, and remain in review.

Current gates:

- Independent security review of the threat model and ACP callback/process
  boundary.
- Cross-agent dogfood with durable evidence and handoff at an exact Git head.
- Unified native/Frog task projection, parity reporting, and a cutover runbook.
- Packaging, installation smoke tests, release evidence, and an explicit alpha
  decision.

- `src/weftmark/` contains the local runtime and application/domain layers.
- `tests/` contains executable runtime and contract evidence used by CI.
- `web/review/` contains the dependency-free read-only tablet/phone review client.
- `docs/weftmark.mdx` remains the editable source of truth for the product and
  architecture paper.
- `docs/artifacts/weftmark_rev0.html` and `weftmark_rev0.pdf` preserve the
  immutable revision-0 design report.
- `tasks/*.weft.yml` contain the dependency-aware implementation plan and task
  evidence requirements.
- `assurance/facts.json` records machine-readable capability state used to keep
  public claims from silently outrunning implementation and verification.
- `THIRD_PARTY_NOTICES.md` records the policy and inventory point for upstream
  license and attribution obligations.
- `AGENTS.md` specifies the small YAML-compatible task dialect used by the task
  plan.
- `assets/weftmark.svg` is the canonical white-background vector mark;
  `assets/weftmark-on-black.svg` is the dark-background variant.
- `ragbaz.component.json` follows the RAGBAZ component-manifest convention while
  continuing to mark WeftMark as a **prototype**, not production software.

<!-- assurance:begin -->
### Assurance snapshot

This table is generated from `assurance/facts.json`; `implemented` is not
treated as `verified`, and nothing is marked releasable without explicit
release evidence.

| Capability | Implemented | Verified | Reviewed | Releasable |
| --- | --- | --- | --- | --- |
| Change Set lifecycle and Git lineage | yes | yes | — | — |
| Semantic scopes and local claims | yes | yes | — | — |
| Evidence, review, and handoff workflow | yes | yes | yes | — |
| Kanban/mobile read projection | yes | yes | yes | — |
| Loopback-only HTTP read surface | yes | yes | yes | — |
| Semantic scope blockers in board status | yes | yes | yes | — |
| Tablet/phone read-only review surface | yes | yes | yes | — |

<!-- assurance:end -->

## Try it locally

WeftMark currently targets Python 3.11+ and has no runtime dependency beyond
the standard library. From a checkout:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"

python scripts/validate_tasks.py
python -m pytest -q
weftmark --help
```

A minimal native workflow is intentionally explicit:

```bash
weftmark --repo . task create example \
  --title "Example change" \
  --why "make the intended outcome inspectable" \
  --what "change the declared surface and record evidence" \
  --priority p1 \
  --scope file:src/example.py \
  --scope contract:example-v0

weftmark --repo . task next
weftmark --repo . task claim example --agent local-worker --session terminal-1
weftmark --repo . status
```

Runtime providers are opt-in and never selected implicitly. A claimed task can
be handed to an ACP-speaking executable with either a JSON config file or an
explicit argument vector:

```bash
weftmark --repo . runtime start example \
  --provider local-acp \
  --prompt "Implement the claimed task and preserve its acceptance criteria." \
  --runtime-provider 'local-acp=["my-acp-agent","--stdio"]'
```

The ACP callback policy confines WeftMark-served file operations and automatic
permissions to the disposable worktree. It is not an operating-system sandbox;
the configured provider executable still runs with the invoking user's OS
identity and must be trusted or separately sandboxed.

## From Frog to WeftMark

Frog remains an active migration source and a reference implementation. The
transition is incremental: immutable snapshots are imported as observations,
dependency-eligible intent is selected, and an operator explicitly promotes and
claims work under native WeftMark authority. Imported assignments and locks do
not become local authority.

| Frog has demonstrated | WeftMark makes first-class |
| --- | --- |
| tasks, deps, claims | Change Sets around transient worker sessions |
| file/repo locks | file **and semantic/contract scopes** |
| agents and sessions | actor/session provenance bound to changes |
| event log and causality | evidence ledger with immutable lineage |
| build/test orchestration | typed proof: passed, failed, unavailable, stale |
| workspace hand coordination | explicit, portable handoff records |
| board/status views | reviewer-facing readiness decisions |
| MCP and remote workspaces | adapters around one vendor-neutral application model |

The next migration milestone is a dual-read task projection and parity report,
followed by cross-agent dogfood and a documented cutover. Automatic bidirectional
sync is intentionally out of scope; any future publish-back path must be
explicit, idempotent, and auditable.

## Documentation build

The documentation source uses a conservative **MDX-compatible subset**: YAML
front matter, Markdown, and raw HTML blocks. It deliberately avoids executable
JSX components, so the build stays deterministic and does not require a Node
runtime. Pandoc renders the source to a single self-contained HTML file, and
WeasyPrint produces the A5 PDF from that same HTML/CSS.

Requirements:

- Python 3.11+
- Pandoc
- packages in `requirements-docs.txt`

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-docs.txt

make html       # build/weftmark.html, self-contained
make pdf        # build/weftmark_A5.pdf via WeasyPrint
make logo       # PNG + lossless WebP derivatives at 512 px
make tasks      # validate tasks/*.weft.yml
make all
```

See `docs/BUILD.md` for the source/artifact policy.

## Logo assets

The SVGs are the editing sources. Raster formats are reproducible derivatives:

```bash
python scripts/build_logo.py --size 512
```

This writes:

```text
assets/generated/weftmark-512.png
assets/generated/weftmark-512.webp
assets/generated/weftmark-on-black-512.png
assets/generated/weftmark-on-black-512.webp
```

The vector mark is a cleaned, flat-vector interpretation of the selected rev0
concept. The SVGs are canonical; PNG and WebP files are reproducible derivatives.

## Task plan

Humans and agents should start with `AGENTS.md` and `tasks/00-bootstrap.weft.yml`.
The initial graph currently covers:

1. reproducible repository/docs/assets foundations;
2. Change Set, evidence, handoff, and review domain models;
3. semantic/contract scopes and locks;
4. local Git lineage and optional code-forge adapters;
5. evidence policies and reviewer-facing readiness summaries;
6. CLI, MCP, terminal, and tablet/mobile read surfaces;
7. a deliberate transition/dogfood path from Frog; and
8. security, packaging, licensing, and release evidence for an open-source alpha.

Validate the graph with:

```bash
python scripts/validate_tasks.py
python scripts/check_assurance_docs.py
```

## Design principles

- **Workers are replaceable.** Claude Code, Codex, OpenCode, local
  Ollama/Mistral-backed workers, and humans are actors/adapters - not the product
  ontology.
- **Git is lineage, not the whole workflow.** Commits and branches are durable
  facts, but evidence and review state need their own model.
- **Done is not evidence.** Implemented, locally tested, CI-verified, reviewed,
  merge-ready, and release-proven are separate states.
- **Unavailable is not failed.** A CI job that never ran is not a failing test.
- **Semantic collisions matter.** Two agents can edit different files and still
  change the same protocol, schema, or security boundary.
- **Handoffs are data.** Continuation should not depend on recovering a chat
  transcript.
- **Open interfaces first.** The core should function locally and remain useful
  when model vendors, IDEs, agent harnesses, and forge providers change.

## License

WeftMark is licensed under the **Apache License, Version 2.0**. See `LICENSE`.
The license permits use, modification, redistribution, and commercial use subject
to its terms, and includes an express patent license from contributors for patent
claims necessarily infringed by their contributions.

For inbound contributions, WeftMark currently follows Apache-2.0 section 5:
unless a contributor explicitly states otherwise, a contribution intentionally
submitted for inclusion in WeftMark is submitted under Apache-2.0 without
additional terms. This repository does not currently impose a separate
Contributor License Agreement. A contribution may be explicitly designated
"Not a Contribution" as described by the license, and a future separate written
agreement may supersede this default for parties that enter one.

Redistributors and downstream adaptations must preserve the license and
applicable notices, retain relevant attribution, and mark modified files where
the license requires it. Third-party code and assets retain their own license
obligations; see `THIRD_PARTY_NOTICES.md` for the repository policy. This is
particularly relevant to planned integration with Apache-2.0 projects such as
Cline Kanban: upstream license and `NOTICE` obligations remain applicable even
when the licenses are compatible.

Apache-2.0 does not grant trademark rights except for the limited descriptive
uses stated in the license.
