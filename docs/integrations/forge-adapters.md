# Forge adapter roadmap

WeftMark keeps hosted forge integration behind the read-only `ForgePort`. Provider terminology and feature differences belong inside adapters; Change Sets, evidence, review decisions and readiness remain WeftMark-owned.

## Implemented

- GitHub / GitHub Enterprise Server: `adapter:github-v0`
- GitLab.com / GitLab Self-Managed / GitLab Dedicated: `adapter:gitlab-v0` (current implementation slice)

## Next adapters

1. **Gitea** — common self-hosted forge; map pull requests, commit statuses, reviews/comments and changed files, with instance capability discovery for CI/Actions.
2. **Forgejo / Codeberg** — separate thin dialect rather than a blind Gitea alias; share mapping helpers only where fixture evidence proves compatibility.
3. **Bitbucket Cloud** — major hosted forge with pull requests, approvals/comments, diffstat and build/status APIs.
4. **Azure DevOps** — enterprise-important but more structurally distinct because organization/project/repository, policy/status and build concepts require more adapter-local mapping.

## Capability rule

Adapters must distinguish:

- **supported + observation exists** → `available`
- **supported + no observation exists** → `missing`
- **instance/provider cannot represent feature** → `unsupported`
- **observation could not be made** → `unavailable`

None of the last three states means failed CI.

## Write authority

This roadmap is read-side only. Future forge mutations (comment, approve, merge, release, rerun pipeline) require a separate capability-gated write contract and threat-model review.
