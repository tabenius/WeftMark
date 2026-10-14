# Cloudflare as a witness: recommendation, not a deployment

**Recommendation:** Cloudflare is useful as an off-host checkpoint witness and
availability layer, provided witness administration is separated from the
ledger writer. D1 alone is neither immutable storage nor a transparency log.
No witness keys, retention locks or trust roots were created by the D1 probe.

## Genesis: commit identity, not just zeroes

WeftMark's all-zero genesis digest denotes an empty chain. It does not uniquely
identify a ledger. Current v1 entry hashes also do not bind the SQL workspace
columns, so enrollment must explicitly bind identity and policy.

For **new** ledgers, a proposed first record (`ledger.genesis.v1`) should commit:

- Random permanent ledger UUID, workspace/security-boundary identity and format.
- Canonicalization version, owner authority and initial allowed signer keys.
- Repository-identity policy, witness identities, checkpoint cadence and quorum.
- Hash of the enrollment/policy manifest, with any external references immutable.

Then attest sequence **1** through Nostoi and register it with the witnesses.
Nostoi's current `nostoi-attestation-v1` validation explicitly refuses sequence
0. Do not call a zero-head receipt a valid Nostoi attestation, nor silently
change that format. The enrollment ceremony must pin the authority key and
manifest externally; first-writer-wins enrollment by itself is vulnerable to
squatting and is not ownership proof.

For an **existing** ledger, do not prepend a genesis event or rehash history.
Enroll its existing verified head with an explicit identity/policy manifest and
label it **first witnessed head**, not proof that its historical genesis was
witnessed. A subsequent identity-binding record may be proposed separately.
Restores keep the ledger UUID. Forks must be named new ledger identities with a
parent checkpoint reference, not silently reuse the original identity.

## Proposed checkpoint flow

1. A local clean snapshot yields exact sequence/digest and a policy/identity
   manifest. Nostoi produces/verifies the established attestation and signature.
2. A durable outbox submits the same checkpoint identity until acknowledged.
   Local commits are not coupled to an assumed distributed transaction.
3. A separately administered witness authenticates the submitter, verifies the
   allowed signing key and manifest, and verifies extension from its remembered
   head. An increasing sequence alone is not a consistency proof. Initially,
   full-chain validation plus matching the old checkpoint is sufficient; an
   incremental proof requires an explicit, tested verifier contract.
4. Exact duplicates replay the same witness receipt. A different digest at the
   same sequence, unknown key/policy, decreasing sequence or inconsistent
   extension is refused and recorded as a conflict, not resolved by overwriting.
5. A signed witness observation binds witness identity, ledger/enrollment
   identity, checkpoint digest, previous witness-receipt digest, witness receipt
   sequence and witness-observed time. Pin its key independently. This requires
   a versioned reviewed statement contract, not an improvised signature format.
6. The immutable receipt object is archived and exposed to independent monitors.
   Clients retain acknowledged receipt heads outside both the writer and witness.

Display **verified through checkpoint N; later entries not yet witnessed**.
Witness downtime leaves checkpoints pending/stale; do not fail open or relabel
pending writes as witnessed. High-risk review/release policy can require a
recent quorum checkpoint. Ordinary append availability may be a separate policy.

## Suggested Cloudflare roles

| Component | Role | What it does not prove |
| --- | --- | --- |
| Worker + Cloudflare Access/service identity | Small authenticated enrollment/checkpoint API | Access is not a signed receipt or independent administrator |
| Durable Object per ledger | Serialize checkpoint acceptance; monotonic head and idempotency state | Admin cannot rewrite it; durable storage is not a trust anchor |
| D1 | Queryable receipt index and inspection projection | Immutability, non-equivocation or trusted timestamps |
| R2 content-addressed objects + retention rule | Retain attestation/signature/receipt bytes, protect against ordinary overwrite/deletion | Owner-proof compliance WORM; administrator can remove rules |
| Independent monitors / second witness outside that account/provider | Remember heads, detect rollback and split views, cross-publish receipt digests | Completeness of original work or truth of evidence |

Serialize each ledger's acceptance in its Durable Object, but treat R2 and D1
publication as separate recoverable operations. A checkpoint should not be
acknowledged until its durable receipt/outbox is safely retained. Partial writes
must be repairable by stable receipt identity. Serving and signing fresh
checkpoints while archived receipts are lost must fail safe.

Do not share writer and witness account-wide credentials. Use narrowly scoped
deployment/runtime roles, a separate witness account controlled by another
administrator, pinned keys and rotation records signed by the prior authority.
A non-extractable WebCrypto key alone is not protection against an administrator
who can deploy code that uses that key to sign arbitrary statements. Initial
deployment can use a separately operated signer invoking Nostoi; Worker-native
signing must first prove compatibility with Nostoi's established formats.

## Reuse Nostoi, carefully

Reuse its canonicalization, `weftmark-ledger-v1` verifier and
`nostoi-attestation-v1` sidecars. Preserve allowed-signers/fingerprint checks and
distinguish a verified signature from a merely canonicalized document. The
pinned Python binding's portable attestation checker does not verify SSH
signatures by itself; the existing signing/verification tooling or a proven
compatible portable implementation is required.

A Worker cannot simply execute `ssh-keygen`, and raw Ed25519 signatures from
WebCrypto are not automatically Nostoi-compatible SSH signature envelopes.
The witness statement/enrollment bindings need a separately reviewed protocol
extension or compatible manifest mechanism. This assessment does not claim
such a signed witness API already exists.

Record-head checkpoints cover v1 **record contents**, not principal/receipt
metadata outside those hashes. To witness authenticated request provenance,
also commit a versioned receipt-manifest root and verify its binding. Otherwise
the database owner can alter principal receipts without changing a record head.

## Guarantees and attack tests

Test valid-prefix rollback, a fully recomputed fork, conflicting enrollment,
same-sequence equivocation, stale but valid signed responses, timestamp
backdating, key compromise/rotation, policy substitution, replayed receipts,
lost R2/D1 writes and replacement of witness code. Retain a second independent
copy of heads so a restored witness cannot quietly reset its monotonic state.

A signature proves which key signed bytes, not when those bytes existed. A
witness's signed observation gives time according to that witness's trust
model; it is not a Cloudflare-issued timestamp authority certificate. HTTP
`Date`, CF-Ray, Worker version IDs, D1 bookmarks and TLS do not substitute for
an independently verifiable timestamp/non-equivocation mechanism. Stronger
requirements should cross-anchor receipts in another independently controlled
witness or established transparency/timestamp service.

Neither witnessing nor retention proves omitted events were ever submitted,
that evidence is true, or that a user/agent was authorized to perform work.
These remain separate domain and policy checks.

## Sources and current limits

- [D1 batch and Sessions API](https://developers.cloudflare.com/d1/worker-api/d1-database/): batches roll back together; `first-primary` starts current on the primary.
- [D1 Time Travel](https://developers.cloudflare.com/d1/reference/time-travel/): recovery window is plan-dependent (7 days free / 30 days paid), not indefinite anchoring; restores are explicitly supported.
- [R2 bucket locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/): retention prevents normal overwrite/deletion, but configuration rules can be removed by an authorized administrator.
- Nostoi `docs/ATTESTATIONS.md` and `crates/nostoi-core/src/attestation.rs` at dependency commit `b043bf953eeda3ea720adfa91f1b2177c0ab1143`: canonical sidecars, signature/time distinction and sequence-zero refusal.

Cloudflare sources were retrieved during this assessment. No bucket lock,
witness signer, external monitor or independent administrative split was tested
or activated. The remote probe verifies ledger SQL behavior only; see
`docs/D1-VERIFICATION.md`.
