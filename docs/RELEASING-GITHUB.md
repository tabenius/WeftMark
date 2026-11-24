# Build and release policy

WeftMark builds and releases exclusively through **GitHub Actions on GitHub-hosted
Ubuntu runners**. Do not require self-hosted CI runners or write generated files
back to the main branch as part of a release.

## CI on main and pull requests

`.github/workflows/ci.yml` runs runtime tests, clean install checks, task and
assurance checks, and generates HTML/PDF documentation. A separate `linux-packages`
job builds and smoke-tests the standalone Linux amd64 executable and Debian
package. The focused CAD evidence integration smoke workflow remains independent.

## Create a release

### Exact-head native review is required

Before attestation or publication, the release workflow now validates the fresh
native-ledger evidence bundle with `scripts/check_release_review.py`. It requires
the release source SHA, a review for `alpha-release-evidence-cs` at that same SHA,
outcome `ready`, boolean `is_releasable: true`, and passing pytest/smoke results.
Missing, stale, follow-up-only or malformed evidence refuses release. Ancestry
alone is not review, and an author review is not independent security review.

**Provisioning blocker:** stock GitHub-hosted runners have no native ledger, so
releases fail closed until an authenticated, exact-head native-review transport
is deliberately configured. This patch does not invent that trust path or change
the GitHub-hosted-only policy. Normal CI and development builds remain available.
Do not auto-approve in CI, manufacture JSON reviews, accept an untrusted downloaded
bundle, or bypass the gate. The checker validates policy, not identity/signatures;
it only consumes the bundle freshly built from the authorized native ledger.

The version in `pyproject.toml` and `src/weftmark/__init__.py` must match
the semantic version tag. After a green main CI run, create and push a version
tag (`vX.Y.Z`). The `Release` workflow validates the tag, checks out that
exact immutable commit, reruns tests, builds, and creates a GitHub Release.

The same workflow may be dispatched manually with an **existing version tag**.
It does not change versions automatically or silently publish `main`.
Dispatch against that tag itself, not against a branch with a different tag input:

```bash
gh workflow run release.yml --ref vX.Y.Z -f tag=vX.Y.Z
```

The event ref, selected tag, peeled tag commit and checked-out commit must agree.
This keeps the GitHub Actions signing identity on `refs/tags/vX.Y.Z` so the
RAGBAZ importer can verify it. Manifest generation resolves source identity from
Git and refuses a branch dispatch, event-SHA mismatch or a different checkout
before writing `release.json`; it does not trust an input tag alone.

Release assets include:

- Python wheel and source distribution
- `weftmark-cli-X.Y.Z-linux-amd64`: standalone CLI executable
- `weftmark-tui-X.Y.Z-linux-amd64`: standalone Textual TUI executable
- `weftmark-cli_X.Y.Z_amd64.deb`: Debian package installing `/usr/bin/weftmark-cli` and `/usr/bin/weftmark`
- `weftmark-tui_X.Y.Z_amd64.deb`: Debian package installing `/usr/bin/weftmark-tui`
- Self-contained documentation HTML and A5 PDF
- `SHA256SUMS` checksums for all assets

The binary releases package the **base CLI** and **Textual TUI** as separate
executable artifacts. MCP is not yet distributed as a standalone executable.
Builds use PyInstaller and are currently Linux **amd64 only**. The executable
may still require compatible glibc/system facilities on the target distribution;
test the `.deb` on supported Debian/Ubuntu versions before claiming universal
compatibility. A Debian package of a bundled Python executable is not a system
package for every platform.

```bash
gh run list --workflow ci.yml
git tag v0.0.1
git push origin v0.0.1
gh release view v0.0.1
sha256sum -c SHA256SUMS

# Verify the signed release manifest. Pin the workflow identity and OIDC issuer;
# substitute the exact immutable tag you are verifying.
cosign verify-blob --bundle release.sigstore.json \
  --certificate-identity "https://github.com/tabenius/WeftMark/.github/workflows/release.yml@refs/tags/v0.1.0" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  release.json
```

The signature authenticates the tag workflow and the exact manifest bytes. It
does **not** prove human review, independent review, or a reproducible build;
those require separate release evidence.

Do not reuse tags for different source revisions. Require Actions checks before
release, maintain provenance of generated assets, and use signing/attestations
before treating releases as cryptographically authenticated supply-chain evidence.

## Supply-chain and platform validation

Release builds use the `build` dependency group (PyInstaller pinned in `uv.lock`) and GitHub artifact provenance attestations with `id-token: write` and `attestations: write`. GitHub-hosted CI tests clean `weftmark-cli` and `weftmark-tui` Debian installation in Debian 12 and Ubuntu 24.04 containers and runs a pseudo-terminal entrypoint smoke test. These checks do not establish that a complete interactive Textual workflow is usable on all terminals.
