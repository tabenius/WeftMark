# WeftMark engineering review — CLI/TUI release transition

## Evidence reviewed

The runtime uses separate entry points `weftmark.cli.main:main` and
`weftmark.tui.app:main`. The primary CI builds on GitHub-hosted Ubuntu;
the release workflow builds from an immutable version tag. Linux packaging
constructs separate PyInstaller CLI and Textual TUI images and Debian packages.
The CAD/Nostoi smoke workflow independently checks the evidence boundary.

## Prioritized refactoring / hardening work

1. **P0 — unify build and release execution.** The current `ci.yml` and
   `release.yml` duplicate dependency provisioning and packaging. Convert
   the verified build into a reusable workflow (`workflow_call`) so release
   consumes artifacts built and tested from the exact tag SHA. Prevent an
   untested tag from publishing by enforcing required CI checks or rerunning
   every release gate before publishing.
2. **P0 — package provenance.** Add GitHub artifact attestations and a
   trusted signing identity. SHA256SUMS is useful for transport corruption
   but, when served alongside the binaries, cannot independently prove
   origin. Never call unsigned binaries cryptographically authenticated.
3. **P0 — dependency reproducibility.** Pin the PyInstaller build tool in
   the lockfile and freeze third-party Git references. CI currently installs
   an additional pinned PyInstaller version outside `uv.lock`; move it into
   an explicit build dependency group. Lock compiler/linker and target
   distribution support as release platforms grow.
4. **P1 — split the package build function into reusable manifest-driven
   targets.** Maintain one list of entrypoints, dependencies, executable
   names, Debian package names, and smoke tests for CLI/TUI/MCP. Derive
   upload patterns and checksums from that manifest to prevent asset drift.
5. **P1 — separate review projections from approval policy.** Keep imported
   CAD evidence, Nostoi receipts, stale-head status and human review
   decisions independent. Add an authenticated artifact resolver and
   immutable source-snapshot identity before importing CAD tests into
   release readiness.
6. **P1 — add distro compatibility smoke.** Test the Debian payload and
   TUI startup on clean Ubuntu LTS and Debian stable containers or VMs,
   with PTY-based terminal smoke tests. `--help` alone does not exercise
   the full Textual render loop.
7. **P2 — centralize CLI/TUI common services.** Keep argument parsing and
   presentation in separate entrypoints, and move duplicated workspace
   resolution, ledger error handling, and command response shaping into
   application-layer functions. Avoid TUI importing CLI modules.
8. **P2 — release architectures.** Extend to Linux arm64 and other target
   environments using explicit build matrices. Do not label amd64
   PyInstaller binaries portable across all glibc versions.
9. **P2 — review UI localization.** Externalize reviewer strings and
   format provenance/limitations without implicit approval semantics.
   Test offline, narrow-screen and keyboard-only navigation.

## Current gates

- GitHub-hosted CI: required before any tag release.
- CLI/TUI .deb and binary `--help`: automated smoke tests.
- Separate CAD/Nostoi integrity smoke: automated.
- Production claims: **not established** by these tests alone.
- Version tags must be immutable and match the package version.

Do not create a GitHub Release until the newest CLI/TUI CI job has passed and
the release workflow has been exercised on an actual version tag.
