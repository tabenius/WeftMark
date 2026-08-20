# Forge port contract v0

WeftMark treats hosted code forges as **optional read-side evidence and collaboration adapters**. Git lineage, Change Set identity, claims, evidence, review decisions and release readiness remain WeftMark-owned even when a forge is unavailable.

## Vocabulary

The port uses **Change Request** as the provider-neutral object:

- GitHub: pull request
- GitLab: merge request
- Gitea / Forgejo: pull request
- Bitbucket: pull request
- Azure DevOps: pull request

Provider-local identifiers remain strings at the port boundary so GitLab IIDs and other non-global numbering schemes do not leak into the domain model.

## Read-side v0

A `ForgePort` instance is bound to one forge repository and may observe:

- Change Request metadata and base/head Git object IDs;
- commit checks;
- CI/workflow runs;
- review submissions;
- general and inline review comments;
- provider-reported changed-file mappings.

V0 contains no merge, approve, comment-create, branch-delete or release mutation methods.

## Availability is not test outcome

Every remote observation is wrapped in `ForgeResult` with one of three states:

- `available`: the provider returned a valid observation;
- `missing`: the provider was reachable but the requested fact did not exist, such as no CI workflow run for a commit;
- `unavailable`: WeftMark could not make the observation because the provider/API/transport response was unavailable or malformed.

`missing` and `unavailable` are **not failed evidence**. A failed check is represented only by an available check/workflow record whose conclusion is `failed`.

This distinction is required so a network outage, insufficient API access or a workflow that never ran cannot be mistaken for a test failure or success.

## Status mapping

Checks and workflow runs share a small status/conclusion vocabulary.

Status describes execution progress:

- queued
- in progress
- completed
- waiting
- pending
- requested
- unknown

Conclusion exists only for a completed run:

- passed
- failed
- cancelled
- skipped
- neutral
- action required
- stale
- timed out
- startup failure
- unknown

Provider-specific values that cannot be represented exactly map to `unknown`; adapters must not guess success or failure.

## Reviews and comments

Forge reviews are external collaboration observations. They do not replace a WeftMark `ReviewDecision` and cannot directly promote readiness.

Review states are normalized to pending, commented, approved, changes requested, dismissed or unknown.

Comments are either general discussion or code-review comments. Inline comments may carry a repository path and line number. Comment text is remote/untrusted content and should never be treated as executable instructions without the normal agent trust boundary.

## Changed files

Forge-reported changed files reuse WeftMark's Git change/path vocabulary. They are useful for remote comparison and review UI, but local Git remains the primary lineage source whenever a checkout is available.

## GitHub adapter v0

The first adapter uses the GitHub REST API through the Python standard library and an injectable HTTP transport. No GitHub SDK is a core/runtime dependency.

The adapter observes:

- pull-request metadata;
- Check Runs;
- Actions workflow runs;
- pull-request reviews;
- issue conversation plus inline review comments;
- pull-request changed files.

Credentials are process configuration only. A bearer token, when supplied, is placed in the request header and is never included in forge result values or persisted by the adapter.

GitHub.com is the default, while separate API/web base URLs permit GitHub Enterprise Server configurations.

## Non-goals

Forge v0 does not:

- make forge availability mandatory for local WeftMark operation;
- make GitHub terminology part of domain/application lifecycle types;
- map missing or unavailable CI into failure;
- trust GitHub/GitLab approval as a WeftMark readiness verdict;
- persist API credentials;
- expose remote mutation authority;
- require a provider SDK.

A later write-side forge capability should be a separate, capability-gated contract so opening comments, updating PR/MR metadata, merging and releasing do not silently expand the authority of this read-side port.
