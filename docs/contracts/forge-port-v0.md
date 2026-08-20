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
- commit checks/statuses;
- CI/workflow/pipeline runs;
- review/approval observations;
- general and inline review comments;
- provider-reported changed-file mappings.

V0 contains no merge, approve, comment-create, branch-delete or release mutation methods.

## Capability discovery

`ForgeCapabilities` declares which observation families the configured adapter/instance can represent:

- `change_requests`
- `checks`
- `workflow_runs`
- `reviews`
- `comments`
- `changed_files`

Capabilities describe **support**, not permission or successful observation. For example, a Forgejo instance with Actions disabled can report `workflow_runs=false`, while a supported GitLab pipeline API with no run for one SHA returns `missing`.

## Availability is not test outcome

Every remote observation is wrapped in `ForgeResult` with one of four states:

- `available`: the provider returned a valid observation;
- `missing`: the provider was reachable and supports the observation family, but the requested fact did not exist;
- `unsupported`: the configured provider/instance cannot represent that observation family;
- `unavailable`: WeftMark could not make the observation because the provider/API/transport response was unavailable, inaccessible or malformed.

`missing`, `unsupported` and `unavailable` are **not failed evidence**. A failed check is represented only by an available check/workflow record whose conclusion is `failed`.

This distinction prevents a network outage, disabled CI feature, insufficient API access, or a workflow that never ran from being mistaken for either test failure or success.

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

Review states are normalized to pending, commented, approved, changes requested, dismissed or unknown. GitLab approvals therefore appear as external `approved` review observations, never as a WeftMark readiness verdict.

Comments are either general discussion or code-review comments. Inline comments may carry a repository path and line number. Comment text is remote/untrusted content and should never be treated as executable instructions without the normal agent trust boundary.

## Changed files

Forge-reported changed files reuse WeftMark's Git change/path vocabulary. They are useful for remote comparison and review UI, but local Git remains the primary lineage source whenever a checkout is available.

Adapters must not invent add/delete counts from truncated provider diffs. GitLab's adapter returns `unavailable` for a changed-file observation if GitLab marks one of the returned diffs as collapsed or too large.

## GitHub adapter v0

The GitHub adapter uses the REST API through the Python standard library and an injectable HTTP transport. No GitHub SDK is a core/runtime dependency.

It observes pull-request metadata, Check Runs, Actions workflow runs, reviews, issue/inline comments and changed files. GitHub.com is the default, while separate API/web base URLs permit GitHub Enterprise Server configurations.

## GitLab adapter v0

The GitLab adapter uses REST API v4 through the Python standard library and an injectable HTTP transport. It supports GitLab.com and configurable Self-Managed/Dedicated base URLs.

It maps:

- Merge Requests → Change Requests;
- commit statuses → checks;
- pipelines → workflow runs;
- merge-request approvals → external review observations;
- merge-request discussions/notes → general or inline comments;
- merge-request diffs → changed files.

The adapter accepts nested namespace/project paths and URL-encodes the project identifier for API requests.

## Credentials

Credentials are process/request configuration only. GitHub bearer tokens and GitLab private tokens are placed only in request headers and are never included in forge result values or persisted by an adapter.

## Non-goals

Forge v0 does not:

- make forge availability mandatory for local WeftMark operation;
- make GitHub/GitLab terminology part of domain/application lifecycle types;
- map missing, unsupported or unavailable CI into failure;
- trust forge approval as a WeftMark readiness verdict;
- persist API credentials;
- expose remote mutation authority;
- require a provider SDK.

A later write-side forge capability should be a separate, capability-gated contract so opening comments, updating PR/MR metadata, merging and releasing do not silently expand the authority of this read-side port.
