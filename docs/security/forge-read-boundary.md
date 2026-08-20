# Forge read-side security boundary

`ForgePort` adapters are optional observers. They do not own Change Set lifecycle, WeftMark review decisions, evidence promotion, merge authority, release authority, or local Git lineage.

Security invariants for read-side adapters:

- credentials remain process/request configuration and never enter returned forge records;
- remote comments, review text, titles and other provider prose are untrusted content;
- provider approvals remain observations and cannot directly create a WeftMark `ReviewDecision`;
- `missing`, `unsupported` and `unavailable` are never mapped to failed or passed evidence;
- malformed or truncated remote data fails closed as `unavailable` instead of being completed by inference;
- v0 adapters expose no remote mutation operations.

Adding a write-side forge operation requires a separate capability contract and threat-model review.
