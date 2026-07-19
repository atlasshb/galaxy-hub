# ORCHESTRATION — parallel runs board (Galaxy Hub app)

The 🛰 Orchestration app: launch several Claude Code runs at once and watch them stream
side by side. Built on the existing chat spawn/stream/stop machinery — no new execution
model, same guardrails. Two files as always; `--enable-run` still gates all execution.

## Backend (stardrive.py)

### 1. Run registry (so the board can list/reconnect)

Today `_handle_chat` tracks `ACTIVE_PROCS` (pid→proc) only. Add a parallel `RUNS`
registry so a client can enumerate what's running (survives a page reload):
- On spawn, record `RUNS[pid] = {"pid", "prompt": <cap 120 chars>, "cwd", "resume",
  "started": <ISO from the request-handler clock>, "status": "running"}` under
  `ACTIVE_LOCK`, alongside the existing registration. Remove on exit (same `finally`).
- `GET /api/chat/active` — EXTEND (backward-compatible) to return
  `{"count": N, "max": MAX_CHAT_PROCS, "runs": [ {pid, prompt, cwd, resume, started, status}, ... ]}`.
  (Was a bare count — keep `count` so nothing breaks; add `max` + `runs`.)
- No per-run history is persisted; this is live state only. Never store prompt beyond the cap.

### 2. Configurable concurrency

Orchestration wants more than 3 lanes. Add `--max-runs N` (default 3, **hard cap 8** —
reject higher with a clear startup error) setting `MAX_CHAT_PROCS`. The existing atomic
slot reservation (RESERVED_SLOTS + ACTIVE_LOCK, the TOCTOU fix) is reused unchanged, so
the 429-when-full contract still holds — the board just has more lanes.

### 3. Nothing else moves

`/api/chat` and `/api/chat/stop` behavior, transcript/usage endpoints, auth, the Host/
CSRF/token guards — all unchanged. `/api/chat/stop` still only signals registry pids.

## Frontend (index.html)

New **Orchestration** app (🛰 in the rail, card on Hub Home). Reuses the chat rendering
component (tool chips / thinking / turn-stats) and the SSE + `stardrive_started` /
`stardrive_done` / `/api/chat/stop` machinery — do NOT fork them.

### Launch panel
- A multi-prompt composer: N prompt rows (add/remove rows; a textarea per row), a shared
  **cwd** picker (reuse the projects dropdown + free-text path) and **permission mode**
  select, and **"Launch all"**. Each row may also set resume=<session id> (optional; default new).
- On launch: fire one `POST /api/chat` per prompt. Respect the server cap — read `max`
  from `/api/chat/active`; start up to `max` immediately, **queue** the rest and start
  each queued run when a lane frees (`stardrive_done` on any card → dequeue next). Show a
  429 gracefully (re-queue).

### Run cards (grid, responsive)
Each run = a card:
- header: prompt snippet (title), a status pill (**queued** / **running** (spinner) /
  **done ✓** / **error ✕** / **stopped**), and a **Stop** button while running
  (→ `/api/chat/stop {pid}`, pid from that card's `stardrive_started`).
- body: live transcript via the shared renderer — assistant text + tool chips (pending→
  done/error) + thinking, scrollable within the card.
- footer: the turn-stats line on `result` (duration · cost · tokens · turns).
- "Open in Stardrive" link once a session_id is known (from `system/init`), to continue
  that run in the full chat app.
- A **collapse/expand** control per card (collapsed shows just header + status); a global
  "Stop all running" button.

### On reload
Call `/api/chat/active` on app open; if runs exist, render reconnect cards from the
registry metadata (status + prompt), noting "reconnected — live output resumes on next
event" (we can't replay missed SSE, but the card shows it's alive and Stop still works).

### A11y / theme / responsive
Status pills have text (not color only); cards keyboard-reachable; live output announced
sparingly via the existing `srAnnouncer` (e.g. "run 2 complete", throttled); theme-aware;
cards reflow to 1 column < 900px; no h-scroll at 375px. Reduced-motion respected.

## Isolation (v1 decision — flagged)

v1 runs execute in the chosen `cwd`, exactly like single chat. **Parallel runs targeting
the same repo can collide on edits** — the UI shows a subtle warning when two+ runs share
a cwd. Git-worktree isolation (a real fix) is a **v2 opt-in**, out of scope here. Document
this in the launch panel.

## Guardrails

Same law: `--enable-run` gates everything; `/api/chat/stop` only signals registry pids;
zero external requests; read-only over `.claude`; hard concurrency cap 8. Two files only.

## Division of labor

Backend agent owns `stardrive.py` (registry + `--max-runs`). Frontend agent owns
`index.html` (the Orchestration app). Single-author lock. Both read this spec + HUB-SPEC.
