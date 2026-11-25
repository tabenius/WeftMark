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

Hosted runners do not receive a private ledger. The signed-receipt ceremony below
transports only the minimal native review. Its dedicated public key must first be
explicitly enrolled in `scripts/release_review_policy.json`; the initial null pin refuses
release. Do not auto-approve in CI, manufacture reviews, use another product's key,
or accept trust policy from the downloaded artifact. Normal CI remains available.

### Dedicated review-key and hosted provisioning ceremony

Release is now a deliberate **manual workflow dispatch**, not an automatic tag
publication. First enroll the public policy in reviewed source, then tag that
source, create an actual native release review at its exact head, sign its minimal
receipt locally, run intake on the tag, and dispatch release with that successful
intake run ID. The private signing key and complete ledger never enter GitHub CI.

1. From your own interactive terminal, create a dedicated encrypted key:

   ```sh
   python3 scripts/setup_release_review_key.py \
     --key "$HOME/.config/weftmark/signing/review_ed25519" \
     --policy-output /tmp/opencode/weftmark-review-policy-proposed.json
   ```

   The ceremony prompts for a passphrase locally, rejects an unencrypted key,
   derives the public half from the private key, and proposes only public policy.
   Do not put passphrases in chat, argv, tracked files or CI. Compare the public
   fingerprint, explicitly replace the null policy with this proposal, review the
   diff and commit it **before** tagging release source. Keep and test an encrypted
   independent recovery copy; the tool does not claim to create one. No Android,
   APT, root or box key is silently reused.

2. Create the genuine native `alpha-release-evidence-cs` review through the native
   workflow, with current required evidence at the clean tagged source head. Then:

   ```sh
   uv run --locked python scripts/provision_release_review.py export \
     --repo . --tag vX.Y.Z --output /tmp/opencode/weftmark-review.json
   ssh-keygen -Y sign -f "$HOME/.config/weftmark/signing/review_ed25519" \
     -n weftmark-release-review-v1 /tmp/opencode/weftmark-review.json
   ```

   Export refuses a missing/stale/not-ready native review. Signing is a deliberate
   local operator action, never an evidence runner or CI auto-approval. The minimal
   receipt contains repository/head/tag, native review identifiers/outcome and a
   maximum seven-day validity window, not private prompts, ledger paths or test logs.

3. Base64-encode `weftmark-review.json` and its `.sig` into a JSON object with keys
   `receipt_base64` and `signature_base64`. Submit that public object using
   `gh workflow run release-review-intake.yml --ref vX.Y.Z --json` on stdin.
   Intake validates the dedicated fingerprint, SSHSIG namespace/principal, exact
   source/tag, expiry and native ready state, then uploads a seven-day artifact.
   Missing enrollment, unsigned/altered data and cross-head receipts fail closed.

4. After intake succeeds, dispatch:

   ```sh
   gh workflow run release.yml --ref vX.Y.Z -f tag=vX.Y.Z -f review_run_id=INTAKE_RUN_ID
   ```

   Release checks the intake's repository/workflow/tag/head/event/success before
   downloading its exact-head artifact, re-verifies the offline signature against
   source-controlled public policy, builds fresh test/smoke evidence, and injects
   only the authenticated review into that fresh bundle before the release gate.
   A review receipt never substitutes for CI tests, package checks or attestation.

The signature authenticates the dedicated key holder's review statement; it does
not establish independent human/security review, external timestamping or full
ledger provenance. The exporter reads native authority, but the signature cannot
prevent a dishonest key holder from asserting false facts. Repo policy enrollment,
key custody and independent review requirements remain explicit owner decisions.

The version in `pyproject.toml` and `src/weftmark/__init__.py` must match
the semantic version tag. After a green main CI run, create and push a version
tag (`vX.Y.Z`). After signed review intake succeeds, manually dispatch `Release`.
It validates the tag and review run, checks out that exact immutable commit,
reruns tests, builds, and creates a GitHub Release.

The workflow must be dispatched manually with an **existing version tag** and
the successful exact-head review intake run ID.
It does not change versions automatically or silently publish `main`.
Dispatch against that tag itself, not against a branch with a different tag input:

```bash
gh workflow run release.yml --ref vX.Y.Z -f tag=vX.Y.Z -f review_run_id=INTAKE_RUN_ID
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
