# GALAXY HUB — platform architecture

Galaxy Hub is the umbrella: a local, zero-dependency **hub for your Claude Code work**,
organized as several focused **apps** behind one shell. **Stardrive is one app inside
it** — the prompt/chat interface — not the whole product. Everything still ships as
`stardrive.py` (stdlib only) + `index.html` (one self-contained page); loopback default,
read-only over `.claude`, zero external assets.

## The shell

- **Hub Home (launcher)** — the landing view: a grid of app cards (name · glyph ·
  one-line · one live stat, e.g. "142 sessions", "€3.10 this week"). This is the "wall
  hub" you open to.
- **App rail** — a persistent narrow left rail of app glyphs; current app highlighted;
  Galaxy Hub wordmark + glyph at the top; clicking Home returns to the launcher.
- Each **app** owns its own internal views/tabs (the current tab bar becomes per-app).
- **Command palette** (Ctrl-K) gains `Go to <app>` and keeps deep links to views.
- State: `#<app>/<view>` in the hash so links/reloads land in the right place.

## Apps — v1 taxonomy

| App | Glyph | What it is | Built from |
|---|---|---|---|
| **Stardrive** | 🚀 | The prompt interface — read, resume, and drive agents | existing **Chat** |
| **Nodes** | 🕸 | The session galaxy — see the shape of your work | existing **Graph · Tiles · Tree · Fusion** |
| **Vault** | 🔒 | Everything you've saved — memory, skills, prompts | existing **Memory · Skills** |
| **Dashboard** | 📊 | Usage, activity & cost analytics | **NEW** (roadmap → now) |
| **Orchestration** | 🛰 | Parallel / multi-agent runs | **FUTURE** |

- **Projects** is a cross-cutting filter surfaced in the Hub Home and inside
  Stardrive/Nodes — not its own app.
- Each app keeps a small sub-wordmark (e.g. "Stardrive" under the Galaxy Hub mark) so the
  product identities survive inside the platform.

## Brand

Extend the existing night-chart identity (space-navy `#0b1020`, atlas-gold `#d4a94f`,
meridian-teal `#4fd4c5`) to the platform. Wordmark **GALAXY HUB** with a galaxy/orbit
glyph; keep the light "parchment" theme. Apps may carry a per-app accent tint but share
the palette. Still zero external assets — glyphs inline SVG/unicode.

## Data / backend

- **Dashboard** needs one new endpoint — `GET /api/usage` → aggregates computed at index
  time from the session store: sessions & messages over time (by day), totals (sessions,
  messages, KB), per-project and per-cluster activity, and **cost/tokens if present** in
  stored `result` lines (`total_cost_usd`, `usage.input_tokens/output_tokens`) — omit
  those fields gracefully when the store doesn't carry them. All charts are **hand-rolled
  inline SVG** (no chart library — the zero-dependency law holds).
- Everything else in the backend is unchanged.

## Build order (waves)

1. **Shell** (now) — Hub Home + app rail + regroup the 8 tabs into Stardrive/Nodes/Vault;
   rebrand wordmark to Galaxy Hub. Pure reorganization — no feature lost, a11y preserved.
2. **Dashboard** — backend `/api/usage` (pulled forward from the roadmap now) + the
   Dashboard app with hand-rolled SVG charts.
3. **Orchestration** — parallel/multi-agent run app.
4. **Vault+** — prompt/snippet library, cross-app search.

## Guardrails

Same law: two auditable files, Python stdlib + one vanilla HTML, zero external
requests/assets, loopback default, read-only over `.claude`, all visualization
hand-rolled. The platform must not regress the security or accessibility work already
shipped.
