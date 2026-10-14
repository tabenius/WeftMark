# Isolated D1 conformance probe

This is a **short-lived diagnostic SQL bridge**, not WeftMark's ledger API or a
witness service. Use only a new synthetic-data database. It has no user identity
provider and must not bind a production ledger. The random probe bearer token
grants all allowlisted operations against this test database, including the
deliberate trigger-refusal test. Keep the token outside source/evidence.

Preparation generates an exact SQL allowlist from the existing Python ledger,
copies the unchanged DDL, and hashes the script, Worker, allowlist and DDL. The
Worker rejects unknown statements, unsafe numeric parameters and oversized
requests. It expires two hours after preparation. Each batch starts a fresh D1
`first-primary` session and executes through transactional `session.batch`.
No SQL plan from an untrusted product client should reach this interface.

## Reproduce

Use an authorized Wrangler installation; replace the placeholders with a new
resource's identifiers. `uv sync --locked --extra test` supplies the Python
client and Nostoi. Do not reapply schema SQL to an existing populated database.

```bash
wrangler whoami
wrangler d1 create <unique-test-database> --location weur --update-config=false
uv run --locked --extra test python scripts/verify_d1_ledger.py prepare \
  --directory <new-private-deploy-directory> --database-id <new-db-id> \
  --database-name <unique-test-database> --account-id <authorized-account> \
  --worker-name <unique-test-worker>
wrangler d1 execute DB --remote --config <deploy-directory>/wrangler.json \
  --file <deploy-directory>/schema.sql --yes
wrangler deploy --config <deploy-directory>/wrangler.json
```

Generate a random token into a mode-0600 external file; feed its contents to
`wrangler secret put PROBE_TOKEN --config ...` through stdin, never argv. No
token is needed for schema deployment. An unconfigured Worker fails closed.

```bash
uv run --locked --extra test python scripts/verify_d1_ledger.py verify \
  --url https://<unique-test-worker>.<account>.workers.dev \
  --token-file <private-token-file> --report <external-report-path>
```

Verify only from a clean committed revision. `verify` checks the deployed code
digest and revision against the current checkout; that is an authenticated
deployment observation, not a cryptographic Cloudflare attestation. Retain the
Wrangler version/deployment UUID separately. Do not print credentials or record
them in ledger evidence. Reports contain only public resource identifiers,
synthetic workspace IDs and hashes. Report local and provider timestamps as
observations; this host's clock is not an independent trusted timestamp.

## Resource lifecycle

After capturing evidence, disable the Worker by deleting its `PROBE_TOKEN`
secret, then verify the endpoint refuses access. The two-hour expiry is a second
fail-closed control, not automatic deletion. Retain the tiny synthetic D1 database
for inspection unless deletion has been approved. No production resources,
routes, R2 retention settings or witness keys should be changed by this probe.
