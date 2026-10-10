# Attestation: this workspace as a collection

**What this is:** a signed, tamper-evident statement of exactly what was in this
workspace at a moment in time — 15 git repositories at specific commits, plus what
the box was running and which tools produced it.

**What it is not:** a claim that the code is good, or a warranty. It says *this is
what was there*, signed by a key whose owner vouches for it. Reviewing the content
is still your job; that part is exactly what you have just done.

## Files

| file | what it is |
|---|---|
| `manifest.json` | the statement, human-readable. Its canonical form is what is hashed. |
| `workspace.jsonl` | a `nostoi-v1` hash chain. Record 1 **is** the manifest. |
| `workspace.jsonl.attestation.json` | what was signed: chain, format, seq, digest, and the pinned key fingerprint |
| `workspace.jsonl.attestation.sig` | ASCII-armored detached OpenPGP signature over the attestation document |
| `box0-genesis.pub.asc` | the signing key's public half |
| `ragbaz-root.pub.asc` | the organisation root key's public half |

You need `gpg`, Python 3, WeftMark's `scripts/verify-workspace.py`, a trusted
local copy of Nostoi's `contrib/python/nostoi.py`, and two fingerprints obtained
out of band: the box signer's and the organisation root's. Obtain the verifier
before going offline; it is not embedded in this historical bundle.
Verification then needs no network access. Run the following commands from this
directory.

## Verify it in four steps

Each step is independent. Do them in order; a failure at any step means the
statement does not describe what you were given.

### 1. The signature is good, and it is the key you expect

```bash
export GNUPGHOME=$(mktemp -d); chmod 700 "$GNUPGHOME"
gpg --batch --quiet --import ragbaz-root.pub.asc box0-genesis.pub.asc
gpg --batch --no-auto-key-retrieve --status-fd=1 \
  --verify workspace.jsonl.attestation.sig workspace.jsonl.attestation.json
```

Expect `Good signature from "RAGBAZ box genesis …"`.

**Expect the trust marker `[unknown]`, and do not be alarmed by it.** You have only
the public keys and no trust database, so GPG cannot vouch for the identity. That
is the point: you are not relying on GPG's web of trust, you are relying on a
fingerprint you obtained out of band. Check it against the one in step 2.

### 2. The fingerprints are the ones you were given

```bash
export EXPECTED_FINGERPRINT=78171B4532F8EFFBDFA2958F4315AE31DCC658B1
export EXPECTED_ROOT_FINGERPRINT=866ECB8348C0FE48E479736AF85CEFBA8B8E1040
python3 ../scripts/verify-workspace.py . \
  --expected-fingerprint "$EXPECTED_FINGERPRINT" \
  --expected-root-fingerprint "$EXPECTED_ROOT_FINGERPRINT" \
  --nostoi-python "${NOSTOI_PYTHON:-../../nostoi/contrib/python/nostoi.py}"
```

Obtain both fingerprints independently; the values above are the original
box-genesis and organisation-root identities. **The root is the point of this
step.** A `sig!` proves that the supplied root file certified the box key, but
on its own it says nothing about *whose* root file that is. Only the
out-of-band root fingerprint turns `ragbaz-root.pub.asc` into a named root.

The verifier compares the expected fingerprints with GPG's actual `VALIDSIG`
signer (or primary-key fingerprint) and the keys present in its own isolated
keyring, not merely with the JSON's claimed identity. It rejects a readable
manifest that differs from the signed record body, and rejects a supplied root
key that is not the pinned root.

### 3. The root you pinned actually vouched for that key

```bash
gpg --check-sigs --keyid-format long "$EXPECTED_FINGERPRINT" | grep -F "$EXPECTED_ROOT_FINGERPRINT"
```

Look for a non-self signature from the pinned root. It must read `sig!`, a
cryptographically checked certification, **not `sig L`**: `sig L` is a
local-only certification that would not travel with the key, and its absence
means this key was not vouched on your behalf. The verifier in step 2 enforces
exactly this; this command is the same fact shown directly.

### 4. The signature is bound to the chain, and the chain is unbroken

```bash
export NOSTOI_PYTHON="${NOSTOI_PYTHON:-../../nostoi/contrib/python/nostoi.py}"
python3 - <<'EOF'
import importlib.util, json, os
# the verifier is the reference implementation; fetch it from the project's
# repository, or use the `nostoi` CLI: `nostoi verify workspace.jsonl`
spec = importlib.util.spec_from_file_location("n", os.environ["NOSTOI_PYTHON"])
n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)

att = json.load(open("workspace.jsonl.attestation.json"))
records = [json.loads(l) for l in open("workspace.jsonl") if l.strip()]
# The attestation names a sequence. Compare that record, not the first one;
# a later appended snapshot is signed too and record 1 is not its statement.
rec = next(r for r in records if r["seq"] == att["seq"])
print("chain ok      :", n.verify("workspace.jsonl")["ok"])
print("seq bound     :", rec["seq"] == att["seq"])
print("digest bound  :", n.digest(rec) == att["digest"])
manifest = json.load(open("manifest.json"))
print("manifest bound:", n.canonical(manifest) == n.canonical(rec["body"]))
EOF
```

All four must be `True`. The verifier in step 2 enforces these checks and exits
nonzero on failure. This step proves the
signature covers *this* record, and that the record has not been edited since.
A valid signature over a document that does not match the chain proves nothing.

## What the signature does and does not prove

**Proves:** the manifest was signed by the holder of that private key; the manifest
has not been altered since; that key is vouched for by the organisation root you
pinned out of band; the record is part of an unbroken chain back to genesis.

**Does not prove:**

- **Freshness.** `anchored_at` is `null`: this statement is **not anchored**. A
  signature proves authorship, not time. Someone who compromised the key could
  backdate a statement, and nothing here would reveal it. Anchoring is the
  mechanism for that and it is not configured here.
- **Quality.** Nothing here says the code is correct, safe or fit for purpose.
- **Completeness of your copy.** It says what the attesting box held; it cannot say
  that you were handed all of it. Compare against what you received.

## Reading the manifest

- `repos[]` — one entry per repository: `origin`, `branch`, `head` (full commit),
  `dirty_files`, and `clean`. `clean: false` records the presence of uncommitted
  work; the manifest does not hash or embed that work's contents.
- `repo_key` — frog's identifier. `repo_key_portable: true` means it is derived
  from the origin URL and means the same thing everywhere. **If it is false the
  value is a hash of an absolute path on the attesting box and is meaningless to
  you.** This is a known wart in the project's identity scheme, not a defect in
  this manifest; it is marked rather than hidden.
- `instance` — what the box was running at the time, including the running
  container image and the SHA-256 of the runtime's `flake.lock`.
- `tools` — versions of the tooling that produced the statement.

## Re-running or re-scoping

The collector is owned by WeftMark at `scripts/attest-workspace.py`. This
directory contains the historical 2026-10-02 bundle; moving it did not change
the signed document, chain, signature, or exported public keys. Its signed
`chain` locator remains `attestations/workspace.jsonl`, relative to the WeftMark
repository when inspecting this bundle. It is a historical snapshot, not a
statement of the current checkout.

From the WeftMark repository, produce a new dated bundle with:

```bash
python scripts/attest-workspace.py --workspace .. --output /path/to/delivery/attestations
```

The collector refuses an existing bundle unless `--replace` is explicit. It
attests **every** git repository present. If you are delivering a subset, change
the repository list before running it — a statement covering repositories outside
the engagement is a liability rather than thoroughness.

Each run starts a fresh chain, so `workspace.jsonl` holds the current statement
only. For an ongoing engagement, append rather than replace: the record `kind` is
`workspace.collection` and each record is a dated snapshot.
