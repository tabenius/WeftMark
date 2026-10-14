# Remote Cloudflare D1 verification

## Scope and result

The existing portable SQLite ledger was exercised against a **real remote D1
database**, through actual Worker `D1DatabaseSession.batch` transactions, not a
local emulator. The unchanged DDL imported successfully (16 D1 queries, four
tables). All **19** conformance assertions passed on the initial deployment.
The clean-head repeat is recorded separately as native WeftMark deployment
evidence; consult that exact-head record rather than treating this narrative as
a signed cloud attestation.

The diagnostic database is isolated, contains only synthetic test entries, and
is not WeftMark's active coordination ledger. No production data or existing
Frog/WeftMark storage was changed. This verifies the current SQL adapter's small
history behavior on D1, not a complete hosted ledger product or witness.

## Resource identity and provenance

- Account: existing authorized `ragbaz` Cloudflare account.
- D1 name: `weftmark-ledger-conformance-20261015`.
- D1 ID: `a2a4f7cd-c5fb-4e83-8b47-b5bebc8debe3`; creation hint WEUR.
- Schema import reported production serving region WEUR, colo LIS, primary.
- Worker: `weftmark-ledger-probe-20261015.ragbaz.workers.dev`.
- Deployment CLI: Wrangler `4.141.0`; compatibility date `2026-10-01`.
- Predecessor ledger implementation: `fdf307014e3ad58aecf3dbdcc86ed0a248a8d5d9`.
- Nostoi: pinned dependency commit `b043bf953eeda3ea720adfa91f1b2177c0ab1143`.
- Initial successful diagnostic Worker version: `a4498a82-a675-4468-89f9-f70bb811a9d5`.

Each preparation binds source revision and a digest of script, Worker, generated
allowlist and unchanged DDL. Verification refuses a different deployed revision
or code digest. Generated configuration, credential and full reports live
outside Git. Worker version IDs remain deployment observations, not signatures
by Cloudflare over our evidence. Provider/local clock observations must not be
upgraded to independent trusted timestamps.

## Assertions verified remotely

1. Unauthenticated request is refused; authenticated identity is available.
2. Deployed code digest and source revision match the verifier checkout.
3. Unallowlisted SQL is refused before D1 execution.
4. Empty stream has sequence zero and the expected all-zero genesis digest.
5. Unsigned 64-bit maximum and non-ASCII/supplementary Unicode round-trip exactly.
6. Replay returns the original entry after the head advances.
7. Another principal cannot reuse the same operation ID.
8. Stale expected heads are refused without mutation.
9. An injected mid-batch failure is reported, rolls back entry/receipt/head,
   and does not poison the failed operation's subsequent successful retry.
10. Existing immutability triggers refuse updates.
11. A second workspace remains isolated.
12. Four concurrent CAS requests produce exactly one winning append.
13. Four concurrent identical requests replay exactly one new entry.
14. Native Nostoi validation of the full returned chain and JSONL export passes.

The verifier counts the individually named checks within these groups as 19.
Every run uses fresh workspace names; repeats do not modify prior test streams.

## Deployment issues and limits

Cloudflare refused Python's default user agent with HTTP 403. A named
`weftmark-d1-conformance/1.0` user agent resolved that edge rejection. The bridge
disables redirects so bearer credentials cannot be forwarded to another origin.

The Python client computes and verifies canonical records using Nostoi locally;
the Worker only binds allowlisted SQL and preserves record/payload JSON as text.
This does **not** prove Nostoi Python bindings run inside Workers, nor implement
Worker-native signing, full user authentication, release policy or the opaque
ledger product API. Fresh primary sessions are used per batch; read-replica
lag/failover behavior was not exercised.

The complete-history snapshot remains O(history). D1 plan-specific quotas,
large-history limits, load/cost, outages, restoration and cross-region behavior
need further acceptance tests. No R2 retention rule or witness was deployed.
The probe is intentionally not installed as a reusable production SQL endpoint.

## Reproduce and retire

See `cloudflare/d1-probe/README.md` and `scripts/verify_d1_ledger.py`. Local tests
run the same conformance path plus Worker authentication, expiry, size-limit,
statement-allowlist and primary-session guards. The live run is an explicit
deployment evidence command, not an automatic test-suite dependency on a cloud
credential.

After recording clean-head evidence, delete the probe's `PROBE_TOKEN` secret and
verify access is refused. The two-hour fail-closed expiry is a second control.
Retain the tiny test database and Worker definition for inspection; deletion
requires a separate explicit resource-lifecycle decision. Cloudflare witnessing
is contemplated separately in `docs/CF-WITNESS.md`.
