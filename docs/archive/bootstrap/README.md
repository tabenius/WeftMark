# Historical bootstrap transport

The normal merge of `agent/bootstrap-from-zip` preserves all unique transport
bytes from `e0a7b7887ce000143e1aa67acb85d41d6f5667a3`.

- `.bootstrap_payload_zip/payload-00.b64` through `payload-12.b64` are retained
  unchanged at the repository root. This is only 13 of the planned 27 parts;
  part 05 also has 14,999 bytes rather than 15,000. Concatenation/base64 decode
  followed by gzip decompression raises `zlib.error: invalid code lengths set`.
  The prefix has not been proven redundant with current source or figures.
  Retention does not claim it is a valid complete archive. Recovering unique
  members is a separate archival task; do not deploy or overwrite current
  assets from these damaged bytes.
- The four workflow sources live here, with only an archival comment added.
  They reconstruct/replace repository trees, generate fallback figures, push
  historical main commits, or delete a transport branch. They are historical
  instructions, not current automation. Their original unmodified blobs remain
  in the merge parent.

Current project structure and deterministic build scripts were materialized on
main by `5dd2eb5b618a94e14a786f28e3926c0312481fef` and evolved substantially.
The modern pipeline is `.github/workflows/ci.yml`; neither it nor current
product code, documentation sources, figures, or immutable artifacts is
replaced by this archive merge. No historical workflow was executed.
