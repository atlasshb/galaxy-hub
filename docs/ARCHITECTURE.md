# Galaxy Hub — Control Plane Architecture (v4)

Galaxy Hub is the single replacement for `claude` as a day-to-day cockpit AND the
aggregation point for the Atlas AI client stack. It does **not** reimplement what
good OSS already does — it assembles it and gives it one face, one router, one
memory map.

## Env / branch / deploy matrix

| Env | Branch | Host (caddy) | Port | Checkout dir | systemd unit |
|---|---|---|---|---|---|
| dev | `dev` | localhost only | 8880 | developer machine | none (run in place) |
| staging | `staging` | `galaxy-staging.atlascorporation.org` | 8879 | `/opt/atlas/galaxyhub-staging` | `atlas-galaxyhub-staging` |
| prod | `main` | `galaxy.atlascorporation.org` | 8877 | `/opt/atlas/galaxyhub` | `atlas-galaxyhub` |

Flow: feature → `dev` → PR → `staging` → validate on galaxy-staging → PR → `main`
→ `./deploy/deploy.sh prod` on atlas-01.

## Component stack (what is assembled inside the hub)

| Layer | Component | Role | Source |
|---|---|---|---|
| Server | `stardrive.py` | HTTP+SSE control plane (stdlib only) | this repo |
| Web UI | `index.html` | single-file SPA: Hub/Stardrive/Nodes/Orchestration/Dashboard | this repo |
| Engines | `claude` (default) · `opencode` (v4) · `codex` (detected) | headless agent runners, engine-swappable via `POST /api/chat {"engine": ...}` | CLI binaries on host |
| Router | LiteLLM (`atlas-litellm`) = the Atlas "omnirouter" | one key, many upstreams: DevPass LLM Gateway, OpenCode Go x2, Ollama local | `/api/router/models` surfaces the catalog |
| Web client | opencode `serve` / `web` / `attach` | browser UI + local TUI attached to a remote server | opencode |
| Mobile client | happy (`npm i -g happy`) | phone/web wrapper over `claude`/`codex` with E2E encryption | slopus/happy |
| Assistant gateway | OpenClaw Gateway | always-on agent, voice/mobile, scheduled skills | openclaw |
| IDE client | Cline (VS Code) | window-per-project agent | cline |
| Memory | Nodes + Fusion graph · second-brain MCP | session galaxy map + knowledge layer | this repo + MCP |
| Swarm / release | Orchestration board · swarm-leader · ruflo release-swarm | parallel runs, release automation | this repo + ruvnet/ruflo |

## What was retired

- **LibreChat** (`atlas-librechat` + mongo) — stopped. Its URL now redirects to Galaxy Hub.
- **Open Web UI** (`atlas-openwebui`) — stopped. `chat.atlascorporation.org` redirects to Galaxy Hub.
- Config/data volumes preserved as backups before teardown; services can be
  restored from the backup if ever needed.

## Engine abstraction (`POST /api/chat`)

Request: `{prompt, resume?, cwd?, permissionMode?, engine?: "claude"|"opencode"|"auto", model?}`

- `claude` (default, back-compat): `claude -p <prompt> --output-format stream-json --verbose`
- `opencode`: `opencode run --format json [--model provider/model] [--session <id>] [--auto] --dir <cwd> <prompt>`
- `model` is engine-specific: `provider/model` for opencode; `ANTHROPIC_MODEL` env
  at server start for claude.
- `GET /api/engines` reports which engines are installed on the host.
- `GET /api/router/models` returns the LiteLLM catalog (best-effort).

## Roadmap

- [x] Orchestration app (parallel runs board), token auth, Dashboard
- [x] v4 engines (`opencode`) + omnirouter catalog endpoint
- [ ] Install `opencode` CLI on atlas-01; wire Orchestration to launch mixed-engine runs
- [ ] Galaxy Hub apps for opencode web + happy (link/app-card), Cline via ACP
- [ ] Release-swarm agent (ruflo pattern) on the Orchestration board
- [ ] Multi-store: read opencode `opencode.db` sessions in Nodes/Fusion (Phase 4)
