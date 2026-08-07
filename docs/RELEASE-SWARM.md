# Release Swarm — runbook

A swarm of single-purpose opencode agents that ship this repo through the
`dev -> staging -> main` flow, with a human gate at prod. The orchestrator is the
**only** agent allowed to cross branch boundaries; every specialist is scoped to
one job and cannot both ship and approve.

## Agents (`.opencode/agent/`)

| Agent | Mode | Permission | Job |
|---|---|---|---|
| `release-orchestrator` | primary | bash, read, task, webfetch | owns the runbook; spawns everyone; merges via PRs; tags |
| `release-preflight` | subagent | bash, read | lint / typecheck / tests / build gate on branch tip |
| `release-changelogger` | subagent | bash, read, edit | conventional commits -> semver + CHANGELOG.md + version bump |
| `release-build-matrix` | subagent | bash, read | build/test matrix on the tag |
| `release-deployer` | subagent | bash, read | `deploy/deploy.sh <env>` on atlas-01 + `deploys.log` |
| `release-smoker` | subagent | bash, read, webfetch | health + smoke against staging/prod |
| `release-rollback` | subagent | bash, read | revert bad deploy + ntfy |

## Invocation

From this repo root (open the repo in opencode):

```
/release            # runs the swarm for staging
/release prod       # runs the swarm all the way to prod (staging must be green)
```

Or headless:

```bash
opencode run --agent release-orchestrator --auto "release staging"
```

Or the wrapper (Linux/WSL on this machine):

```bash
./deploy/release.sh staging
```

## Runbook (staging)

1. Compute `BASE_TAG` (last `v*` tag); collect `git log BASE_TAG..staging` + merged PRs.
2. `release-preflight` on `staging` tip. **Gate:** must PASS or abort.
3. `release-changelogger` -> propose semver, write CHANGELOG + version, open `release/vX.Y.Z -> staging` PR.
4. Merge that PR; tag `vX.Y.Z` on `staging`; push tag.
5. `release-build-matrix` on the tag. **Gate:** block on red.
6. `release-deployer` `deploy.sh staging`; record SHA in `deploys.log`.
7. `release-smoker` against staging. **Gate:** must PASS.
8. On staging pass -> PR `staging -> main`, merge.

## Runbook (prod — only after staging green)

9. `release-deployer` `deploy.sh prod`.
10. `release-smoker` against prod + 10-15 min error-rate window.
11. Any failure -> `release-rollback` + ntfy + mark FAILED + stop.
12. Green -> post release notes; record success; report SHA/version.

## Gate rule

Only the orchestrator may cross a branch boundary (steps 4, 8). No single agent
can both ship and approve: preflight/changelogger/build/deploy/smoke are
read-heavy or single-purpose.
