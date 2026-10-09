# Workspace attestation bundle

WeftMark owns [`../scripts/attest-workspace.py`](../scripts/attest-workspace.py)
and this directory. The signed files here are the original **2026-10-02**
workspace snapshot, relocated without changing their bytes. They are historical
evidence, not a current inventory or a release-readiness claim.

Start with [VERIFY.md](VERIFY.md) for verification and
[GUARANTEES.md](GUARANTEES.md) for the precise properties established.
Public-key exports belong in the delivery bundle; private keys do not.

From the WeftMark checkout, generate a separate new delivery:

```bash
python scripts/attest-workspace.py \
  --workspace .. \
  --output /path/to/new-delivery/attestations
```

Use `--help` for identity and reference-implementation overrides. The collector
refuses to overwrite an existing bundle unless `--replace` is explicit. Keep
new machine snapshots in box-local delivery/report directories unless they are
deliberately selected for publication.
