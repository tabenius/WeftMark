# Audit-first evidence commands

WeftMark records an `evidence.requested` entry durably before starting a
command. The intent contains the Change Set id, a digest and count of argv, a
digest of the working directory, and the requested timeout; it deliberately
omits raw arguments and environment values. The outcome record includes the
intent sequence and digest. A request left without an outcome is not rerun
automatically: inspect the workspace and reconcile it first.

Evidence requests are bounded to 128 arguments, 16 KiB of argv text, 128
environment entries / 32 KiB of environment text, a 128-byte id, and a
15-minute timeout. The local evidence command is therefore bounded in payload
and runtime. Deployments that expose WeftMark through a network gateway should
also apply authentication, request-rate limits, and body limits at that edge.

To verify a WeftMark ledger with Nostoi's Rust verifier, install the `nostoi`
binary on `PATH` or set `WEFTMARK_NOSTOI` to its path, then run:

```sh
weftmark ledger verify .git/weftmark/ledger.jsonl
```

The command pins the `weftmark-ledger-v1` format and has a ten-second process
timeout. Existing WeftMark ledger reads and writes continue to use the native
adapter; this command adds an independent verification implementation.
