# WeftMark

<p align="center">
  <img src="assets/weftmark.svg" alt="WeftMark logo" width="280">
</p>

**WeftMark is the proposed RAGBAZ control plane for multi-agent software work:**
a vendor-neutral ledger for scope, Git lineage, evidence, handoff, review, and
merge/release readiness.

It grows out of the operational lessons encoded in **RAGBAZ Frog** without
trying to become another coding agent, issue tracker, or build system. Frog has
already proven useful primitives around agent identity, task claiming, locks,
dependency-aware scheduling, affected builds, workspace federation, MCP, event
history, and causality. WeftMark narrows the next product boundary around the
question that becomes scarce when many agents can write code at once:

> **Can we explain why a worker was allowed to change something, what actually
> changed, what evidence says it works, who reviewed it, and whether it is
> genuinely ready to merge or release?**

## Repository status

This repository begins as an **executable design and implementation plan**.
There is not yet a WeftMark runtime. The current contents are intentionally
honest about that distinction:

- `docs/weftmark.mdx` is the editable source of truth for the product and
  architecture paper.
- `docs/artifacts/weftmark_rev0.html` and `weftmark_rev0.pdf` preserve the
  original self-contained report as historical revision 0 artifacts.
- `tasks/*.weft.yml` contain the dependency-aware initial implementation plan.
- `AGENTS.md` specifies the small YAML-compatible task dialect used by those
  files.
- `assets/weftmark.svg` is the canonical white-background vector mark;
  `assets/weftmark-on-black.svg` is the dark-background variant.
- `scripts/` contains deterministic documentation, logo, and task-validation
  tooling.
- `ragbaz.component.json` follows the RAGBAZ component-manifest convention used
  by Frog while marking WeftMark as a **prototype**, not production software.

## From Frog to WeftMark

Frog remains a useful reference implementation and a source of hard-earned
workflow lessons. The intended transition is conceptual rather than a blind
rewrite or schema fork.

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

The implementation plan therefore starts with domain concepts and evidence
semantics, then adds Git/forge adapters and surfaces. It does **not** begin by
building a web dashboard or another agent loop.

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
concept. `assets/weftmark-selected-rev0.png` is retained as a visual design
reference, not as the canonical source.

## Task plan

Humans and agents should start with `AGENTS.md` and `tasks/00-bootstrap.weft.yml`.
The initial graph currently covers:

1. reproducible repository/docs/assets foundations;
2. Change Set, evidence, handoff, and review domain models;
3. semantic/contract scopes and locks;
4. local Git lineage and optional code-forge adapters;
5. evidence policies and reviewer-facing readiness summaries;
6. CLI, MCP, terminal, and later tablet read surfaces;
7. a deliberate transition/dogfood path from Frog; and
8. security, packaging, licensing, and release evidence for an open-source alpha.

Validate the graph with:

```bash
python scripts/validate_tasks.py
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

No project license has been selected yet. The implementation plan contains an
explicit `open-source-license` gate. Until a `LICENSE` file is committed, do not
assume that the repository itself grants open-source redistribution rights.
