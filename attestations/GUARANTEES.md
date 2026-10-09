# What each verification actually guarantees

A verification step proves something specific and never anything adjacent to it.
Most disappointment with attestation comes from treating a check that proves
**authenticity** as if it proved **freshness**, or a check that proves
**integrity** as if it proved **completeness**.

Read the guarantee you actually got, not the one you wanted.

## The guarantees, named

Each row is a distinct property with a distinct mechanism. Holding one tells you
nothing about the others.

| # | guarantee | what it establishes | established by | present in this delivery |
|---|---|---|---|---|
| 1 | **Signature validity** | the document was signed by the holder of a private key | `gpg --verify` | **yes** |
| 2 | **Content integrity** | the document has not changed since signing | detached signature over the bytes | **yes** |
| 3 | **Chain binding** | the signature covers *this* record, and the record is in an unbroken chain to genesis | digest recomputed and compared | **yes** |
| 4 | **Identity pinning** | the key is the one whose fingerprint you obtained out of band | comparing a pinned fingerprint | **yes** |
| 5 | **Delegation** | the key was vouched for by the organisation root | an exportable certification on the key | **yes** |
| 6 | **Freshness** | the signature existed at or before a stated time | anchoring / transparency log / timestamp authority | **NO** |
| 7 | **Revocation status** | the key was not revoked at signing time | a revocation checkpoint | **NO** |
| 8 | **Delivery completeness** | you were handed everything the statement covers | out-of-band comparison | **NO** |
| 9 | **Source provenance** | the bytes came from the claimed upstream repository | a build attestation (SLSA/in-toto), not a statement | **NO** |
| 10 | **Quality** | the code is correct, safe, fit for purpose | review, tests, analysis | **NO** |
| 11 | **Non-repudiation** | the signer cannot deny signing | not achievable with GPG signatures | **NO** |
| 12 | **Intent** | the signer meant what the words say | not a technical property | **NO** |

Guarantees 1–5 are what this delivery provides, and they form a complete chain of
custody for **the statement**. They say nothing about the code the statement
describes.

## The distinction that matters most

**A signed statement is self-asserted.** The box signs its own account of what it
contains. Guarantees 1–5 establish that *this statement is authentic and
unaltered* — not that it is *true*.

Closing the gap between "unaltered" and "true" requires moving trust off the
asserting machine. That is guarantee 9, and it needs a build system that attests
from outside: a CI runner with its own identity producing an in-toto statement
about an artifact digest. Until then, the trust root is *the operator of the box*
— which for a client delivery means: you are trusting that the person who ran the
box described it honestly, and the signature proves you can pin that trust to one
key rather than to a hostname.

## Freshness, specifically

A signature proves authorship, not time. If the private key were compromised, an
attacker could sign a manifest dated arbitrarily far in the past, and every check
above would still pass. `anchored_at` in this delivery is `null`, which is the
honest encoding of "no external witness to time".

The fix is an **anchor**: publish the record digest somewhere the signer cannot
rewrite — an append-only object store with object lock, a transparency log, or a
third party's timestamp. Then the statement's *existence by date T* is provable
even if the key later leaks. Anchoring is a configuration choice, not a code
change; it is simply not configured here.

## Deliver the weakest honest claim

If you have guarantees 1–5 and nothing else, say this:

> "This is the state of the workspace at the time shown, signed by a key whose
> fingerprint I give you. The statement has not been altered since signing. I have
> not anchored it, so it carries no proof of *when* it was signed, and it is my own
> account of what was on the machine."

Not: "verified", "certified", "authentic source", "proven correct". Each of those
implies a guarantee you do not hold.

## Common confusions

**"The signature verified, so the content is genuine."** The signature covers the
*manifest*. Whether the manifest's `head` values are the real commits in the real
repositories is a separate question — guarantee 9.

**"It's in a tamper-evident chain, so nothing can be removed."** A chain is
append-only by construction, so a *later* run cannot alter an *earlier* record. But
WeftMark's `scripts/attest-workspace.py` starts a fresh chain per output bundle
and refuses an existing bundle unless `--replace` is explicit. The chain proves each
statement was not edited after the fact; it does not prove the chain contains every
snapshot that ever existed. For an ongoing engagement, append rather than replace.

**"The key is trusted, so the signer is authorised."** `trusted`, `ultimate` and
friends are properties of *your local trust database*, not of the signature. In the
verification steps the key shows `[unknown]`, correctly: you have no trust
database for it. Trust comes from the pinned fingerprint you were given
separately — guarantee 4 — not from GPG's opinion.

**"It's signed by the organisation root."** The root *vouches* for the key
(guarantee 5); it did not sign the manifest. Reading the wrong signature line and
concluding the root attests to the content is an easy and serious error.

**"The digest matches, so the code is what was reviewed."** Only if you reviewed
*this* manifest, and only for what it lists. Repositories marked `clean: false`
contained uncommitted work at the time; only its presence/count is in the
manifest digest, not its file contents. A commit ID cannot identify those bytes.

## Strengthening it, in order of value

1. **Append, don't replace** — one chain per engagement, each record a dated
   snapshot. Cheap.
2. **Anchor** — one destination, one command. Converts "no proof of time" into
   proof of time.
3. **Scope the statement** — attest only what is being delivered. A statement
   covering repositories outside the engagement is a liability.
4. **Build attestations from CI** — moves the trust root off the operator's box
   and upgrades guarantee 9 from absent to real. The largest single improvement.
5. **Pin by digest in CI too** — the published image currently has no `.revision`
   label, so an image pin records what is running but attests to nothing about its
   origin.
