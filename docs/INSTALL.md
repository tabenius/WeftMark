# Installing WeftMark

WeftMark is not yet published to PyPI. Install it from a local clone —
either directly (editable or not) or from a wheel you build yourself.

## Requirements

- Python 3.11, 3.12, or 3.13.
- Git (WeftMark reads and observes your repository's Git history; core local
  workflows — changesets, evidence, review, handoff — never require network
  access).

## Core install (from a clone)

```bash
git clone https://github.com/tabenius/WeftMark.git
cd WeftMark
pip install .
```

For local development on WeftMark itself, install it editable instead so
changes to `src/` take effect without reinstalling:

```bash
pip install -e .
```

This installs the `weftmark` CLI with **no extra dependencies** — every
forge adapter (GitHub, GitLab, Bitbucket, Gitea, Forgejo, Azure DevOps),
the ACP runtime adapter, and the local HTTP control surface use only the
Python standard library. Nothing here depends on a model-provider SDK.

## Optional extras

Two capabilities are opt-in because they pull in real third-party
dependencies:

```bash
pip install '.[mcp]'   # the weftmark-mcp server (Model Context Protocol)
pip install '.[tui]'   # weftmark tui, the terminal reviewer (Textual)
```

Both can be installed together: `pip install '.[mcp,tui]'`.

## Installing from a built wheel

If you'd rather build a wheel first (for example, to install it somewhere
without a full clone):

```bash
uv build          # or: python -m build
pip install dist/weftmark-*.whl
```

Extras work the same way from a wheel, quoted so the shell doesn't expand
the brackets:

```bash
pip install 'dist/weftmark-*.whl[mcp]'
```

## Verifying your install

```bash
weftmark --help
```

should print the top-level command list (`status`, `tui`, `bundle`,
`task`, `changeset`, `claim`, `scope`, `evidence`, `review`, `handoff`,
...).

A minimal end-to-end check, run inside any Git repository with at least one
commit (a fresh `git init` with zero commits won't work — `changeset create`
needs a HEAD to work from):

```bash
weftmark changeset create smoke-cs --goal "first change set" --scope "file:**"
weftmark evidence run smoke-cs --kind test --command echo ok
weftmark review create smoke-cs --author "$(whoami)" --require test
```

The last command should print a `ready` outcome. This is the same
changeset/evidence/review command sequence that `scripts/smoke_install.py`
runs automatically against a fresh, no-extras wheel install on every
supported Python version as part of this project's own CI — not literally
the same install invocation as `pip install .` above (the script tests the
wheel-install path), but the same tested command sequence once installed.
Run it yourself locally with `make smoke`.
