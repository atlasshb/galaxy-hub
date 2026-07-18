# SESSION ATLAS v3 — CONSOLE (chat becomes a real Claude UI replacement)

v1 (tiles/tree/graph/fusion) and v2 (chat/projects/skills/memory) stay. v3 makes the
Chat tab feel like a first-class Claude client: tool calls visible (historic + live),
stoppable turns, turn cost/token stats, real markdown, a command palette, light/dark
themes, and a visual layer worth screenshotting. Same identity and law as before:
`session_atlas.py` stdlib-only, `index.html` self-contained, loopback default,
read-only over `.claude`.

## Backend (session_atlas.py)

### 1. `/api/transcript` — structured messages

Message shape becomes:

```json
{ "role": "user"|"assistant", "text": str, "ts": "ISO"|null,
  "tools": [ { "name": str, "input": str, "result": str|null, "isError": bool } ],
  "thinking": str|null }
```

- `tools` / `thinking` appear on assistant messages only; omit the keys entirely when empty/absent (keeps payload small).
- **tool_use blocks** (assistant `message.content` list, `type=="tool_use"`): `name` = block name; `input` = compact one-line summary of `block.input` — first present key of `command`, `file_path`, `path`, `pattern`, `url`, `prompt`, `description`, `query` wins (its string value), else `json.dumps(input)`; collapse whitespace, cap 200 chars. Max 20 tools per message.
- **tool_result blocks** (arrive in `user`-type lines, `message.content` list, `type=="tool_result"`, keyed by `tool_use_id`): build a `tool_use_id → (snippet, is_error)` map while streaming the file; attach to the matching tool entry. Snippet: string content as-is, or list content → concatenated `{"type":"text"}` block texts; collapse whitespace, cap 300 chars. `isError` from `is_error` (default false). Lines that contain ONLY tool_result blocks must NOT become user messages (current `extract_user_text` behavior already skips them — keep that).
- An assistant line with tool_use but **no text** is no longer skipped: emit it with `"text": ""` and its `tools`.
- **thinking blocks** (`type=="thinking"`, text under `thinking` key): concatenated per message, collapse to readable text, cap 1500 chars.
- Caps: `TRANSCRIPT_MAX_MSGS` / `TRANSCRIPT_MAX_CHARS` still govern `text` accounting as today; tool/thinking strings don't count toward the char budget (they're individually capped).
- Everything else (path safety, sidechain skip, truncation flag, title logic) unchanged.

### 2. Chat process control

- Immediately after `_sse_start()`, before forwarding CLI lines, emit
  `data: {"type":"atlas_started","pid":<proc.pid>}\n\n`.
- `POST /api/chat/stop`, body `{"pid": N}` — gated by `--enable-run` (403 otherwise, same error shape as `/api/chat`). Validate: pid must be an int AND a key in `ACTIVE_PROCS` (400 non-int, 404 unknown — **never** signal a pid that isn't in the registry). Action: `terminate()`, and if still alive after 3s, `kill()` (do the wait in a daemon thread so the response returns immediately). Respond `{"ok": true}`. The `/api/chat` streaming loop then finishes naturally (stdout EOF → `atlas_done`).
- Keep `GET /api/chat/active` as-is.

### 3. Nothing else moves

Indexer, clustering, data.json contract, `/api/projects`, `/api/skills`, `/api/memory*`, flags — untouched.

## Frontend (index.html)

### Chat rendering — historic and live share one component

- **Tool chips**: each tool renders as a collapsed row inside the assistant turn, in
  content order: `⚙ Name` + input summary (monospace, one line, ellipsis). Click →
  expands to show the result snippet (or "no result"); error results get a red-tinted
  border/label. Live streaming: chip appears on the `assistant` event's tool_use block
  with a pulsing/spinner state; the matching `tool_result` (arrives in `user`-type
  stream events, match by `tool_use_id`) flips it to done (✓) or error (✕) and fills
  the snippet.
- **Thinking**: collapsed "✳ thinking" row, click to expand the capped text. Subdued/italic styling.
- **Turn grouping**: consecutive assistant messages render as one visual group (avatar/label shown once).
- **Turn stats footer**: on the stream `result` event render a subtle footer line under the turn: `✓ 12.3s · $0.0421 · 8.1k in / 2.4k out · 3 turns` from `duration_ms`, `total_cost_usd`, `usage.input_tokens`/`usage.output_tokens`, `num_turns` (render only fields present; errors show `✕` + subtype).
- **Stop**: while streaming, the Send button becomes **Stop** → `POST /api/chat/stop {pid}` (pid from `atlas_started`). Keep composer re-enable logic driven by `atlas_done` exactly as now.
- **Typing indicator**: three pulsing dots in an assistant bubble from send until the first assistant/tool event.

### Markdown (still hand-rolled, escape-first, no libs)

Fences (with language label + hover copy button), inline code, bold, italic, `# ##
###` headings, `-`/`*` and `1.` lists (nesting not required), `> ` blockquotes,
`---` rule, `[text](http/https url)` links (`target="_blank" rel="noopener
noreferrer"`, reject other schemes). Escape HTML before transforming, as today.

### Command palette

`Ctrl+K` / `Cmd+K` (and a ⌘K hint button in the header) → centered overlay with a
search input. Sources, ranked by simple subsequence-fuzzy score: sessions (title +
project label → open in Chat), projects (→ Projects-style filter then Chat), skills
(→ Chat with `/name ` prefilled), actions (`Go to <each tab>`, `New session`,
`Toggle theme`, `Refresh index`). Top 12 results, ↑/↓ + Enter, Esc closes, click
works, type-to-filter live. Section label on each row (session/project/skill/action).

### Theme

Keep the dark palette as default. Add a light theme via `:root[data-theme="light"]`
overrides of the existing CSS variables (pick accessible equivalents; cluster palette
may stay shared). Toggle button in header (☾/☀), persisted to
`localStorage["atlas-theme"]`, applied before first paint (inline script in `<head>`
reading localStorage) to avoid flash.

### Visual polish (the reel-worthy layer — all vanilla CSS, zero external assets)

- Messages fade/slide in (~150ms); no animation on initial transcript load of 100+ messages (gate by a "live" flag).
- Bubbles: larger radius, subtle border, user bubbles accent-tinted; small round avatar badges (`C` / `You`) once per turn group.
- Header: subtle backdrop-blur / gradient edge; composer: rounded container with visible focus ring; styled scrollbars (WebKit + `scrollbar-color`).
- Sidebar: clearer active-session highlight, hover states.
- Responsive: below ~900px the chat sidebar becomes an overlay behind a ☰ toggle; header controls wrap; the edge slider may hide on narrow widths. No horizontal page scroll at 375px.

### Keyboard map

`Ctrl/Cmd+K` palette · `Esc` closes palette/skill popup/new-session panel ·
composer Enter/Shift+Enter unchanged.

## Guardrails

- `/api/chat/stop` only ever signals pids from `ACTIVE_PROCS`.
- Palette/links/markdown: escape first; no `javascript:` or other non-http(s) schemes.
- Still zero external requests, still read-only over `.claude`.

## Division of labor (build wave)

Backend agent owns `session_atlas.py` ONLY. Frontend agent owns `index.html` ONLY.
Single-author file lock as in v2. Both read this spec first.

Verification note: the machine's own store at `~/.claude/projects/` contains real
transcripts with tool_use/tool_result/thinking blocks — test `/api/transcript`
against it. For `/api/chat` tests, put a fake `claude` shim script on PATH that
prints canned stream-json lines (init → assistant text → assistant tool_use → user
tool_result → result) with small sleeps; never spawn the real CLI from tests.
