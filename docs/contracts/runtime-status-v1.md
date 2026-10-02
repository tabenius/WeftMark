# Runtime observations and human attestation

WeftMark consumes optional `ragbaz.runtime-status.v1` snapshots produced by
Minotaur's host adapter. The format is owned by
`ragbaz-minotaur/doc/runtime-status-v1.md`, not by the ledger. Set
`RAGBAZ_RUNTIME_STATUS` to the snapshot path. `weftmark system [--snapshot FILE]`,
the TUI's `s` screen, and authenticated `GET /v0/system` expose the same adapter.
Missing components, unavailable probes and stale observations remain distinct
from failed evidence. Observations never grant a claim or review authority.

The read model is bounded to 1 MiB; observations expire after 120 seconds and
future timestamps are clock skew. Refreshes read one snapshot, do not scan the
host, start a service, execute evidence or sign. Control instructions refer to
Minotaur's explicit, configured and Nostoi-audited systemd control command.

## Native review and optional governance

Native human review/evidence workflows need no Ephor installation. Rebekah's
governed connector still refuses requested governance when Ephor is absent or
disabled. A native review must not be relabeled as an Ephor approval. Dash is the
runtime's authenticated governed-HITL surface; the local WeftMark CLI is the
native review path. A snapshot does not establish independent human review.

## Deliberate signing

```sh
weftmark ledger verify /path/to/ledger.jsonl
weftmark ledger attest /path/to/ledger.jsonl --principal you@host --key ~/.ssh/id_ed25519
weftmark ledger verify-attestation /path/to/ledger.jsonl --principal you@host \
  --allowed-signers /path/to/signers --fingerprint SHA256:PIN
```

These explicit CLI wrappers delegate to Nostoi with argv, inheriting the terminal
for SSH agent/hardware/passphrase use. No background surface gets the private
key. Attestation names the verified ledger head; it is not a Change Set test,
governance approval or review. Verification requires a fingerprint pin and
preserves Nostoi's current/stale coverage and failure exit codes. Existing
`ledger verify` reports chain integrity, and any sidecar information it returns
is signature-unchecked unless `verify-attestation` actually checked it.

Runtime snapshot publication intent can be logged in Nostoi independently of
WeftMark evidence. Local web and Dash projections read the snapshot; ordinary
page reads are not logged, and there is no fabricated web delivery receipt.
Tests in a dirty worktree are diagnostics. Only clean, exact-head evidence
recorded with `weftmark evidence run` is durable Change Set proof.
