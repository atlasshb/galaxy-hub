# SESSION ATLAS v2 — WORKSPACE (full Claude Code shell)

v1 (tiles/tree/graph/fusion) stays. v2 turns Session Atlas into a full local **workspace
UI replacing the Claude Code interface**: read any session as a chat thread, PROMPT
(new or resume) through the Claude Code CLI, browse projects/folders, skills, and memory.
Same identity: `session_atlas.py` (stdlib only) + `index.html` (self-contained). Same
privacy law: loopback default, read-only over `.claude` (the CLI process does the writing).

## New server endpoints (session_atlas.py)

All JSON; errors as `{"error": str}` with 4xx/5xx.

- `GET /api/projects` → `[{ "dir": "<raw dirname>", "label": "<cwd or prettified>", "sessions": N, "lastActivity": "ISO" }]` (from the scan cache; recompute cheap at index time, store alongside data.json in memory).
- `GET /api/transcript?project=<dir>&id=<sessionId>` → `{ "id", "title", "messages": [{"role":"user"|"assistant","text":str,"ts":"ISO"}] }`. Parse the jsonl: user/assistant types, NOT sidechains, text blocks only (same extraction rules as indexer; assistant `message.content` blocks `type=="text"`). Cap 500 messages / 2 MB text; if capped set `"truncated": true`. Path safety: `project` and `id` must match `^[A-Za-z0-9._-]+$`; join and verify the resolved path is inside the store root (realpath prefix check) — 400 otherwise.
- `GET /api/skills` → `{ "skills": [{"name", "description"}] }` from `~/.claude/skills/*/SKILL.md` (frontmatter name/description; fall back to dirname) plus `~/.claude/commands/*.md` if present.
- `GET /api/memory` → list of `{name, file, description}` from `~/.claude/projects/C--/memory/*.md` frontmatter... GENERALIZE: scan `<store-root>/*/memory/*.md`. `GET /api/memory/read?file=<name>` → raw markdown (same path-safety rules; read-only, no write endpoint).
- `POST /api/chat` → **SSE stream**. Body: `{ "prompt": str, "resume": "<sessionId>"|null, "cwd": str|null, "permissionMode": "default"|"acceptEdits"|"bypassPermissions" }`.
  - GATED: only if server started with `--enable-run`; otherwise 403 `{"error":"run disabled; start with --enable-run"}`.
  - Spawns the Claude Code CLI headless: `claude -p <prompt> --output-format stream-json --verbose` (+ `--resume <id>` when resuming, `--permission-mode <mode>` when not default), `cwd` = requested cwd (validate it exists) else home. Resolve the binary with `shutil.which("claude")`; on Windows also try `claude.cmd`/`claude.exe`. 500 with clear error if not found.
  - Response: `Content-Type: text/event-stream`, chunked; forward each stdout line as `data: <line>\n\n` verbatim (the CLI already emits JSON per line: system/init carries session_id, assistant events carry message content, result carries final). On process exit emit `data: {"type":"atlas_done","code":N}\n\n`. Kill the child if the client disconnects (BrokenPipeError → terminate()). Timeout: kill after `--run-timeout` seconds (default 900).
  - Concurrency: max 3 concurrent chat processes (429 beyond). Track PIDs; `GET /api/chat/active` → count.
  - RECON.md in this repo documents the exact flag behavior verified on this machine — backend builder MUST read it and adapt if flags differ.
- Keep: `GET /` `GET /data.json` `POST /refresh`.

New CLI flags: `--enable-run`, `--run-timeout N`. Existing `--root/--bind/--port` unchanged.

## UI (index.html) — new tabs, existing 4 stay

Tab order: **Chat | Projects | Skills | Memory | Tiles | Tree | Graph | Fusion**. Same dark theme.

- **Chat** (the centerpiece — this replaces the Claude UI):
  - Left sidebar: session list grouped by cluster (from data.json), search box on top, sorted by mtime desc inside groups; "+ New session" button with a cwd picker (dropdown fed by /api/projects labels + free-text path input) and permission-mode select.
  - Main pane: selected session's transcript via /api/transcript rendered as chat bubbles (user right/accent, assistant left/panel; monospace for fenced code blocks — minimal markdown: fences + inline code + bold; no external md lib).
  - Composer at bottom: textarea (Enter=send, Shift+Enter=newline), type `/` → skill autocomplete popup (from /api/skills, inserts `/name `). Send → POST /api/chat with resume=<selected session id> (or null for new), stream SSE via fetch + ReadableStream, append assistant text deltas live to the thread; on `system/init` of a NEW session capture session_id and show "session <id8> started". `atlas_done` → re-enable composer. If server replies 403 → banner "Run disabled — start server with --enable-run".
- **Projects**: card grid from /api/projects (label, session count, last activity); click → filters the Chat sidebar to that project and switches to Chat.
- **Skills**: list with name + description; click → open Chat with `/name ` pre-filled in composer.
- **Memory**: two-pane: list from /api/memory; click → rendered markdown (same minimal renderer) read-only.
- Detail panel (v1) gains buttons: "Open in Chat" (switch to Chat tab with that session selected).

## Guardrails
- `/api/chat` = arbitrary agent execution ⇒ OFF by default (`--enable-run` opt-in), document loudly in README. Never enable together with a non-loopback bind unless the operator understands the network is trusted (tailnet).
- Path-traversal checks on every file-serving endpoint as specified.
- Transcript/memory endpoints stay read-only; server never writes into `.claude`.

## Division of labor (build wave)
- Backend agent owns `session_atlas.py` ONLY. Frontend agent owns `index.html` ONLY. Nobody touches both (single-author file lock).
- Both read this spec + RECON.md first. Frontend may stub-test against static JSON.
