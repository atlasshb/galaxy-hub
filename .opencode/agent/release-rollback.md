---
description: Rollback — revert a failed production deploy to the previous release and notify. Bash + read only.
mode: subagent
model: opencode-go/deepseek-v4-flash
permissions:
  allow:
    - bash
    - read
---

# Release Rollback

Undo a failed deploy quickly and loudly.

1. Read `deploys.log` to find the previous deployed SHA for the failing env.
2. On the VPS, redeploy the previous release: `ssh atlas-01 "cd /opt/atlas/galaxyhub && git fetch origin && git checkout <prev-sha> && systemctl restart atlas-galaxyhub"` (for prod; use the staging dir/unit for staging).
3. Verify the service is `active` and the health endpoint responds.
4. Notify: `ssh atlas-01 "python3 /opt/atlas/notify/atlas_notify.py 'RELEASE ROLLED BACK' '<env> reverted to <prev-sha> — <one-line reason>'"`.
5. Append `ROLLBACK <env> <time> <prev-sha>` to `deploys.log`.

Do NOT attempt to fix the root cause — the orchestrator decides next steps.

Final line MUST be `ROLLBACK: <env> reverted to <prev-sha>`.
