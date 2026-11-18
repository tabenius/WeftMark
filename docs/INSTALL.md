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

### Optional SQLite: released Nostoi, not a local Rust build

```bash
uv sync --locked --extra sqlite --no-build-package nostoi
```

The minimum binding version is `nostoi>=0.1.1,<0.2`; compatible newer 0.1.x
versions are allowed by package metadata. For reproducibility, `tool.uv.sources`
selects the reviewed release, and `uv.lock` records its hash. Linux amd64
CPython 3.11+ with glibc >=2.34 uses the 1.7 MB abi3 release wheel. Other
platforms use the pinned 0.1.1 release source and need Rust/Maturin. Linux
musl and older glibc require an explicit source override; `--no-build-package`
will not silently compile or choose a different package.

**pip does not read uv's source configuration.** Nostoi is distributed from
RAGBAZ releases, not assumed to exist on PyPI. On the supported Linux platform,
install the released binding first, then the WeftMark extra:

```bash
python -m pip install 'https://dl.ragbaz.cc/v1/nostoi/v0.1.1/nostoi-0.1.1-cp311-abi3-manylinux_2_34_x86_64.whl#sha256=e8ab2a8662bb214ca01454fac013dc504203dd92fa8e51a7386db9a15b4122aa'
python -m pip install '.[sqlite]'
```

For other platforms, install the selected release source first:

```bash
python -m pip install 'nostoi @ git+https://github.com/tabenius/nostoi.git@e45387189c9d20b51260589c0429936950ac4d93#subdirectory=bindings/python'
python -m pip install '.[sqlite]'
```

The external `nostoi` CLI is separate from the Python binding. Optional CAD
intake, ledger verification and SSH attestations need it; ordinary JSONL
operation does not. Use the released Debian package or, with curl and cosign
installed, download only the verified CLI:

```bash
python scripts/install_nostoi_release.py --output "$HOME/.local/bin/nostoi"
```

The helper refuses to overwrite an existing file, verifies the release's
Sigstore identity and source commit, checks the binary's size/SHA-256, and
removes temporary downloads. You may also set `WEFTMARK_NOSTOI` to an existing
compatible CLI instead of installing a second copy. Integration CI uses this
release CLI without checking out or compiling Nostoi.

To select a newer compatible release, update the uv wheel/source selection and
`tool.weftmark.nostoi-release` together, re-lock with uv 0.12.19, update the
verified wheel hash in the dependency test, and run hosted compatibility and
CAD integration checks. Version constraints are not permission to use mutable
`main` or silently replace a released artifact. Keep source/checkouts and Rust
only for binding development; they are not runtime requirements on the wheel
platform. Downloaded packages may be removed after installation and verification;
retain their version, immutable URL and SHA-256 for restoration.

## Installing from a built wheel

If you'd rather build a wheel first (for example, to install it somewhere
without a full clone):

```bash
uv build          # or: python -m build
pip install dist/weftmark-*.whl
```

Extras work the same way from a wheel — resolve the exact wheel filename
first, since pip doesn't glob-expand paths and a bracket suffix attached
directly to a glob pattern can confuse shell expansion instead of fixing
it:

```bash
WHEEL=$(ls dist/weftmark-*.whl)
pip install "${WHEEL}[mcp]"
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
