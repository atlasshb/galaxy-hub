# The Claude Code UI landscape — and where Stardrive fits

Survey of the open-source ecosystem as of July 2026 (50+ projects reviewed). Curated;
starred counts approximate at survey time.

## The four crowded categories

**Full clients / GUIs driving Claude Code** — [opcode](https://github.com/winfunc/opcode)
(ex-Claudia, ~22k★, Tauri), [CloudCLI / claudecodeui](https://github.com/siteboon/claudecodeui)
(~13k★, web+PWA, multi-CLI), [Happy Coder](https://github.com/slopus/happy) (~23k★,
e2e-encrypted mobile), [claude-code-viewer](https://github.com/d-kimuson/claude-code-viewer)
(~1.3k★, web client with git ops), plus a dozen smaller web/desktop/TUI shells
([Nimbalyst](https://github.com/nimbalyst/nimbalyst), FlyCrys, claude-code-rust, …).

**Session/history viewers** — 15+ tools independently parsing `~/.claude/projects`
JSONL: [claude-code-history-viewer](https://github.com/jhlee0409/claude-code-history-viewer)
(~1.9k★, 28 assistants), [universal-session-viewer](https://github.com/tad-hq/universal-session-viewer)
(full-text search, continuation chains), [claude-code-log](https://github.com/daaain/claude-code-log),
[claude-code-transcripts](https://github.com/simonw/claude-code-transcripts), and many more.
All of them render sessions as **lists** — chronological, searchable, per-project.

**General multi-provider chat UIs** — [Open WebUI](https://github.com/open-webui/open-webui)
(~146k★), [LobeChat](https://github.com/lobehub/lobe-chat) (~80k★),
[LibreChat](https://github.com/danny-avila/LibreChat) (~41k★), NextChat, big-AGI. All
support Anthropic models; none touch the local Claude Code session store.

**Orchestrators / multi-session managers** — [Vibe Kanban](https://github.com/BloopAI/vibe-kanban)
(~27k★, sunsetting), [claude-code-router](https://github.com/musistudio/claude-code-router)
(~36k★), [Claude Squad](https://github.com/smtg-ai/claude-squad) (~8k★ TUI),
CCManager, agent-orchestrator, … — git-worktree isolation per agent is the dominant
pattern here.

## What nobody else does

Across all four categories, **no surveyed tool maps the session store**:

- no topic clustering of sessions,
- no similarity graph ("these two sessions are the same thread of work"),
- no fusion/merge candidates,
- no *explanatory* graph — why sessions relate, what a cluster is about.

Every viewer answers "show me my sessions." Stardrive answers **"show me the
shape of my work."** That is the lane, and it is empty.

Second structural differentiator: **the stack**. The ecosystem is React/Tauri/
Electron/Node — CloudCLI alone pulls hundreds of npm packages next to your
transcripts (which contain your prompts, code, and possibly secrets). Stardrive
is two auditable files — Python stdlib + one vanilla HTML — zero network, loopback
by default, read-only over the store. Trust is a feature.

## What we deliberately share with the field

Streaming chat with tool-call rendering, session resume, cost/token visibility,
skills integration — table stakes for the client pillar, implemented in v2/v3.

## Borrow-list (future waves, informed by the survey)

- **Usage/cost analytics view** — a dozen standalone dashboards exist for this;
  clear recurring pain point. Fits our read-only indexer naturally.
- **Continuation-chain detection** (universal-session-viewer) — pairs well with
  fusion candidates.
- **Remote/mobile access** — Happy-style e2e relay is out of scope; our answer
  stays "bind to your tailnet if you trust it."

## Ecosystem notes

- Churn is high: Crystal→Nimbalyst, Claudia→opcode, Vibe Kanban sunsetting,
  Terragon shut down, claude-code-webui archived. Single-file simplicity is also a
  survival strategy.
- The JSONL schema is undocumented; every viewer reverse-engineers it. Our parser
  (caps, malformed-line tolerance, sidechain rules) is that work — kept in one
  place, `stardrive.py`.
