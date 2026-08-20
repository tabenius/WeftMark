# Forge adapter roadmap

WeftMark keeps hosted forge integration behind the read-only `ForgePort`. Provider terminology and feature differences belong inside adapters; Change Sets, evidence, review decisions and readiness remain WeftMark-owned.

## Implemented / current slice

- GitHub / GitHub Enterprise Server: `adapter:github-v0`
- GitLab.com / GitLab Self-Managed / GitLab Dedicated: `adapter:gitlab-v0`
- Gitea: `adapter:gitea-v0` (current slice)
- Forgejo / Codeberg: `adapter:forgejo-v0` (current slice)

Gitea and Forgejo use separate public adapter classes. They share an internal mapping layer only where fixtures prove compatible behavior, so later API or Actions divergence does not turn into a breaking ForgePort change.

## Next adapters

1. **Bitbucket Cloud** — major hosted forge with pull requests, approvals/comments, diffstat and build/status APIs.
2. **Azure DevOps** — enterprise-important but structurally distinct because organization/project/repository, policy/status and build concepts require more adapter-local mapping.
3. **Gerrit** — valuable for large engineering organizations, but its change/revision/review model differs enough that it should be mapped deliberately rather than forced into GitHub-like assumptions.
4. **SourceHut / other focused forges** — evaluate after the main hosted and self-hosted families, using capability discovery to represent intentionally smaller collaboration/CI surfaces.

## Capability rule

Adapters must distinguish:

- **supported + observation exists** → `available`
- **supported + no observation exists** → `missing`
- **instance/provider cannot represent feature** → `unsupported`
- **observation could not be made** → `unavailable`

None of the last three states means failed CI.

## Write authority

This roadmap is read-side only. Future forge mutations (comment, approve, merge, release, rerun/trigger pipeline) require a separate capability-gated write contract and threat-model review.
