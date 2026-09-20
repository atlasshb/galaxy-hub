<div align="center">

# 🌌 GALAXY HUB

**A local hub for your Claude Code work — read and resume any session, see the shape of everything you've worked on, drive agents from one shell.**

</div>

---

## What Galaxy Hub actually is

Galaxy Hub is a **session hub for Claude Code / opencode work**, shipped as two
auditable files: `stardrive.py` (Python 3.8+, stdlib only — no `pip install`) and one
self-contained `index.html`. It reads your Claude Code transcript store
(`~/.claude/projects/*.jsonl`) read-only, indexes it into `data.json`, and serves a
small HTTP API plus the single-page hub shell.

A typical hardened deployment runs it as a systemd unit bound to loopback, behind a
reverse proxy that terminates TLS and enforces SSO, for example:

```
stardrive.py --bind 127.0.0.1 --port 8877 --token-file /path/to/token --enable-run --max-runs 8
```

Two access paths are usual:
- through an authenticating reverse proxy (path-relative API calls were added in commit
  `290d2b5` so the hub can be embedded under a sub-path), and
- directly on a private network interface only — because `--enable-run` lets the server
  execute the `claude` CLI, never expose that route publicly.

## Feature list (read out of the code)

Galaxy Hub is organized as a **shell + apps** (see HUB-SPEC.md). Per the code and specs:

- **Hub Home** — launcher grid of app cards with a live stat each; app rail on the left;
  command palette (Ctrl/Cmd+K); state kept in the URL hash (`#<app>/<view>`).
- **Stardrive** (🚀) — the prompt/chat app: renders any session as a threaded
  conversation (tool calls, thinking, per-turn cost/token stats), and — when
  `--enable-run` is set — lets you prompt into it. New or resumed sessions stream live
  over SSE through the `claude` CLI, spawned via direct `exec` (no shell), with a Stop
  control and `/`-skill autocomplete. Concurrency is capped by `--max-runs` (1–8,
  default 3; a hardened deployment may run the hard cap of 8) and each run is killed after
  `--run-timeout` seconds (default 900).
- **Nodes** (🕸) — the session galaxy: TF-IDF vectors (English + Dutch stopwords) over
  parsed transcripts, pairwise cosine similarity, union-find clustering. Four views:
  Graph (force-directed star-chart, hand-rolled ~150 lines of velocity-Verlet physics,
  no charting library), Tiles, Tree (project → topic → session), and Fusion (candidate
  same-thread session groups for manual merge/compact).
- **Vault** (🔒) — browses what Claude Code carries between sessions: memory and skills.
  Prompt/snippet library and cross-app search are roadmap, not shipped
  (per HUB-SPEC.md wave 4).
- **Dashboard** (📊) — `GET /api/usage` aggregates sessions/messages over time,
  per-project and per-cluster activity, and cost/token totals when the store carries
  `total_cost_usd` / `usage.input_tokens` / `usage.output_tokens`; charts are hand-rolled
  inline SVG. Per HUB-SPEC.md this endpoint and the Dashboard UI are the "wave 2" build
  and are stated in-repo as already shipped.
- **Orchestration** (🛰) — parallel/multi-agent runs. Per HUB-SPEC.md this is designed,
  not yet built (wave 3).
- **Corpus mode** (added 2026-09-16, `--corpus <dir>`) — in addition to the local
  `~/.claude` store, Galaxy Hub can index a second, normalized multi-source thread
  corpus laid out as `<corpus>/<device>/<source>/<thread>.json` (schema 1). Local
  sessions are labelled by `--local-device` (default: hostname); corpus sessions are
  exposed as synthetic projects `corpus.<device>.<source>` and are read-only / never
  resumable (`NONLOCAL_IDS`). Point it at any corpus directory. **UNKNOWN**: what populates that corpus directory
  (which harness/device sources feed it) — out of scope of `stardrive.py` itself.
- **Model routing via LiteLLM ("omnirouter")** — chat/run requests prefer an
  Anthropic-compatible endpoint through a LiteLLM gateway
  (`LITELLM_BASE_URL`, default `http://127.0.0.1:4100/v1`) because LiteLLM carries the
  fallback chain; it falls back to a direct z.ai endpoint only if LiteLLM doesn't answer.
  Commit `5ac019b` specifically fixed GLM-family models (`glm-*`) to route through
  LiteLLM instead of hitting a 404 on the old path, and added `--token-file` support in
  the same commit. `list_litellm_models()` does a best-effort, never-raising catalog
  fetch against `LITELLM_BASE_URL + "/models"`.
- **Accessibility** — keyboard-operable, ARIA semantics, reduced-motion support, light
  and dark themes, responsive to phone width, claimed in-repo as audited (README's own
  claim; no independent audit artifact was located in this pass — **UNKNOWN** whether a
  formal audit report exists anywhere else in the repo).

## Architecture

```
~/.claude/projects/*.jsonl  (+ optional --corpus dir)
        │  read-only
        ▼
stardrive.py (Python stdlib only)
  ├─ Indexer: parse transcripts/corpus → TF-IDF → cosine similarity → union-find
  │           clustering → data.json (+ /api/usage aggregate)
  ├─ HTTP API: /api/transcript, /api/chat (SSE), /api/usage, model catalog, etc.
  └─ --enable-run path: spawns `claude` CLI directly (exec, no shell), routed at
     the model layer through the LiteLLM gateway (omnirouter) at LITELLM_BASE_URL,
     falling back to z.ai direct if LiteLLM is unreachable
        │
        ▼
index.html — one self-contained page, the "shell": Hub Home + app rail render every
app (Stardrive, Nodes, Vault, Dashboard, [Orchestration]) from data.json / the API,
streaming chat back over SSE
```

Two files, zero runtime dependencies, zero external network requests from either the
server or the page (no CDN, fonts, or analytics). The re-indexer runs automatically
when `data.json` is older than 6h, or on-demand (`Refresh` button, or `--reindex-minute`
for a scheduled hourly rebuild while serving).

## Install / run

```bash
python3 stardrive.py            # index the store, then serve
# open http://127.0.0.1:8877
```

No `pip install`. Requires Python 3.8+, and the `claude` CLI if you want Stardrive to
actually run agents (`--enable-run`). For a production deploy as a systemd unit, see DEPLOY.md / DEPLOY-VPS.md for the
unit/deploy-script details (not re-verified line-by-line in this pass).

## Configuration flags (from `argparse` in `stardrive.py`, verified against source)

| Flag | Default | What |
|---|---|---|
| `--root PATH` | `~/.claude/projects` | Claude Code session store to index |
| `--corpus DIR` | off | also index a normalized multi-source thread corpus (`<dir>/<device>/<source>/*.json`); `--root` stays indexed as local |
| `--local-device NAME` | hostname | device label applied to `--root` sessions |
| `--bind IP` | `127.0.0.1` | interface to serve on |
| `--port N` | `8877` | port |
| `--index-only` | off | rebuild `data.json` and exit |
| `--serve` | off | serve without re-indexing first |
| `--reindex-minute N` | `-1` (off) | reindex every hour at minute N while serving |
| `--enable-run` | off | allow `POST /api/chat` to spawn the `claude` CLI |
| `--run-timeout N` | `900` | seconds before a spawned chat process is killed |
| `--max-runs N` | `3` | max concurrent chat/run processes; must be 1–8 |
| `--token TOKEN` | off | require this bearer token on every request (`Authorization: Bearer`, `?token=`, or `gh_token` cookie); required to bind non-loopback |
| `--token-file PATH` | off | read the bearer token from a file at startup instead of argv (keeps it out of `ps`/`/proc/<pid>/cmdline`); mutually exclusive with `--token` |

The flags above are example hardened values
given for this task; the token itself is not read here (per operator rule, tokens are
never printed/echoed).

## Security posture

- **Refuse-by-default on non-loopback bind.** The code explicitly checks
  `is_loopback_bind(args.bind)` before doing any indexing work: a non-loopback `--bind`
  with no `--token` is refused immediately at startup — "a non-loopback --bind with no
  --token would serve every session, unauthenticated, to anyone who can reach the
  port." Loopback binds with no token behave as before (no auth).
- **Bearer token auth** — `--token` (or `--token-file`, preferred, to avoid the token
  leaking via `ps`/`/proc/<pid>/cmdline`) is checked on every request, accepted via
  `Authorization: Bearer`, `?token=`, or a `gh_token` cookie.
- **Host header allowlist** — `ALLOWED_HOSTS` is computed from the actual bind/port at
  startup (`compute_allowed_hosts`) and checked per-request, defending against
  DNS-rebinding.
- **`--enable-run` is the privileged switch.** Off by default; when on, `POST
  /api/chat` can spawn the `claude` CLI. The spawn path uses direct `exec`, not a
  shell, so prompt text can't be interpreted as shell syntax. This is exactly why the
  recommended posture: a deployment running with `--enable-run` should be reachable
  directly only over a private network — never bound to a public interface — with an
  authenticating reverse proxy fronting it instead of exposing the
  raw port.
- **Read-only over the transcript store** — the server never writes into
  `~/.claude`; only the `claude` CLI does, and only when the operator actually prompts.
- **Concurrency cap** — `--max-runs` (hard cap 8) bounds how many chat/run subprocesses
  can run at once, and `--run-timeout` kills a hung one.
- **Corpus mode read-only guarantee** — corpus-sourced sessions are tracked in
  `NONLOCAL_IDS` and are never resumable/executable, only browsable.
- **UNKNOWN**: exact request-size/rate limiting, if any, beyond the concurrency cap —
  not located in the slices read.

## Relation to the other spec docs in the repo

- **HUB-SPEC.md** — the platform-level architecture doc this README is aligned to: the
  shell/app taxonomy (Stardrive/Nodes/Vault/Dashboard/Orchestration), build waves, and
  the "two auditable files, zero dependencies" guardrail. This README's app list and
  wording track it directly.
- **BUILD-SPEC.md, CONSOLE-SPEC.md, GRAPH-SPEC.md** — **UNKNOWN** in detail beyond
  filenames; not opened in this pass (BUILD-SPEC presumably governs the build/release
  process referenced by the swarm-release commits in the git log; GRAPH-SPEC presumably
  specifies the Nodes force-directed graph behaviour described above; CONSOLE-SPEC
  presumably specifies a console/terminal-style app or view not otherwise confirmed
  here).
- **ORCH-SPEC.md** — presumably the detailed spec for the not-yet-built Orchestration
  app; **UNKNOWN** in detail, not opened in this pass.
- **LANDSCAPE.md** — presumably the competitive-landscape doc backing the README's
  "every other tool renders a list" positioning; **UNKNOWN** in detail, not opened.
- **DEPLOY.md / DEPLOY-VPS.md** — deployment guidance (local/remote-desktop via SSH
  tunnel per the existing README, and VPS deployment respectively); referenced but not
  re-verified line-by-line here. A production systemd unit and its environment-specific
  file selection are corroborated by git log entries (`49af08b`
  "deploy.sh picks env-specific unit file", `60b1061` "prod unit enables run lanes").
- **CONTRIBUTING.md** — contribution rules; not re-verified here, assumed unchanged
  from the existing README's summary ("zero runtime dependencies, read-only over the
  store, privacy first").
- **BRAND-SPEC.md** — the visual identity (space-navy / atlas-gold / meridian-teal
  palette, wordmark) referenced by HUB-SPEC.md's "Brand" section; not re-verified here.

## What changed since the last README pass (from git log, most recent first)

- `5ac019b` — fix: route `glm-*` models through LiteLLM; add `--token-file` support.
- `71b6ec1` — feat: release swarm (orchestrator + 6 specialist agents, runbook,
  `/release` command).
- `49af08b` — fix: `deploy.sh` picks the env-specific unit file (staging no longer
  gets the prod unit).
- `290d2b5` — feat: path-relative API calls, enabling embedding Galaxy Hub inside Atlas
  OS at `/os/apps/galaxy`.
- `60b1061` — deploy: prod unit enables run lanes; `deploy.sh` installs the env unit
  from the repo.
- `73cf019` — feat: fold OpenClaw into Galaxy Hub (hub card with SSO link) +
  ARCHITECTURE update.

## License

MIT (per existing README/LICENSE file — not independently re-verified in this pass).
