---
description: Smoker — run health + smoke checks against the deployed environment. Read + webfetch/bash only.
mode: subagent
model: opencode-go/deepseek-v4-flash
permissions:
  allow:
    - bash
    - read
    - webfetch
---

# Release Smoker

Validate a deployed environment before the next gate.

Targets (env passed as argument):
- `staging`  -> http://127.0.0.1:8879 (via `ssh atlas-01 "curl -s ..."`), token = `/opt/atlas/galaxyhub-staging/.token`
- `prod`     -> https://galaxy.atlascorporation.org (public; expect 401 without token = healthy gate)

Checks:
1. `/healthz` or `/` responds (expected code for prod: 401 token-gated = OK).
2. On the VPS with the token: `/data.json` -> 200, `/api/projects` -> 200, `/api/engines` -> JSON containing `claude` + `opencode`.
3. If the release touched the router: `/api/router/models` -> 200 with `count >= 1`.
4. For prod, additionally sample the public URL 5x over 15s and report any 5xx.

Final line MUST be `SMOKE: PASS` or `SMOKE: FAIL` with the failing check.
