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

The version in `pyproject.toml` and `src/weftmark/__init__.py` must match
the semantic version tag. After a green main CI run, create and push a version
tag (`vX.Y.Z`). The `Release` workflow validates the tag, checks out that
exact immutable commit, reruns tests, builds, and creates a GitHub Release.

The same workflow may be dispatched manually with an **existing version tag**.
It does not change versions automatically or silently publish `main`.

Release assets include:

- Python wheel and source distribution
- `weftmark-X.Y.Z-linux-amd64`: self-contained Linux executable (CLI)
- `weftmark_X.Y.Z_amd64.deb`: Debian/Ubuntu package installing `/usr/bin/weftmark`
- Self-contained documentation HTML and A5 PDF
- `SHA256SUMS` checksums for all assets

The binary currently packages the **base WeftMark CLI**, not a full Rust-native
replacement, and not optional TUI/MCP processes as separate executables.
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
```

Do not reuse tags for different source revisions. Require Actions checks before
release, maintain provenance of generated assets, and use signing/attestations
before treating releases as cryptographically authenticated supply-chain evidence.
