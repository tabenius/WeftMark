# CAD → Nostoi → WeftMark: preliminary evidence intake

The application adapter `src/weftmark/application/cad_evidence.py` accepts a
versioned CAD test-evidence candidate, a byte-exact KiCad JSON report, an
existing Change Set, and an explicit independent Nostoi verification signal.
It writes a distinct `cad.evidence.imported` ledger record, **not** an
`evidence` outcome used by readiness policy.

## Trust contract

1. KiCad remains authoritative for a saved schematic or PCB. The CAD checker
   emits `ragbaz.cad.evidence/1`, including hashes of both the source and
   the original, unmodified KiCad JSON report.
2. CAD's Nostoi bridge verifies the report bytes, verifies the local ledger,
   appends `cad.evidence.completed` with hashes and verdict (no board contents),
   then exports `ragbaz.cad.weftmark-evidence-candidate/1`.
3. A trusted import controller must verify the Nostoi chain **and the specific
   event body and receipt**, authenticate the Change Set, and retain the original
   report. The application adapter currently accepts an explicit
   `nostoi_chain_verified` assertion and does not itself perform this verification.
   **Do not expose this function directly to an untrusted browser/API caller.**
4. The adapter checks format, report hash, source binding, no-approval metadata,
   clean Change Set state and duplicate IDs; it adds an inert ledger record
   with the Git head observed at import.

The imported candidate does **not** satisfy test evidence requirements in the
review service; this is deliberate until a verified receipt-to-event linkage,
source-revision identity and stale-evidence behavior are implemented and tested.
No user approval, signing, CAD mutation or release state is granted.

For now, all import orchestration is local and application-owned. There is no
direct HTTP API, CLI command, or background ingestion. Tests exercise the
validation and ledger boundary independently of the optional KiCad installation.

## Follow-up integration gates

- Verify a specific Nostoi event against a trusted head/checkpoint, not merely
  a caller-provided Boolean.
- Bind CAD's source SHA to a stored immutable CAD snapshot corresponding to
  the review's Change Set.
- Make the import replay-safe across crash-after-ledger-write by adding an
  idempotent receipt key.
- Add explicit read-only review projections, then wire evidence readiness
  subject to human-authenticated review and source freshness checks.

## Trusted local controller (new)

`src/weftmark/application/cad_verified_intake.py` provides `import_verified_cad_evidence` for trusted, local orchestration. It accepts a candidate and paths to the *saved* KiCad source, exact original JSON report and Nostoi chain. It independently computes source/report SHA-256, invokes `nostoi verify` and `nostoi show <seq> --json` without a shell, requires the displayed record to be verified and its sequence/digest to match the receipt, and calls the application import boundary with the verified record. The source digest is rechecked immediately before ledger intake.

The low-level `import_cad_evidence` remains a trusted internal adapter; only the controller should be exposed to operator-owned commands. Neither function is an HTTP endpoint or a reviewer-approval interface. A valid local chain can still have been truncated or replaced; a trusted externally anchored head is needed before making stronger continuity claims.

The imported record remains a distinct `cad.evidence.imported` observation and cannot independently satisfy a normal WeftMark evidence requirement or release readiness.
