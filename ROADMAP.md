# Stardrive — roadmap

Where Stardrive is headed. The north star: **the only tool that maps the shape of your
Claude Code work, not just lists it** — kept to two auditable, zero-dependency files.

## Phase 0 — Ship (now)
- [x] v3 client (chat, tool chips, stop, palette, themes) + graph teach layer
- [x] Security hardening (RCE + DNS-rebinding/CSRF closed), accessibility pass
- [x] README with screenshots + architecture diagram; deploy guide
- [ ] Rename the repo to `stardrive`; announce to the community
- [ ] Optional Forgejo mirror

## Phase 1 — Trust & polish
- **`--token` auth** for non-loopback binds — unlocks safe LAN/tailnet use (today the
  guidance is "SSH tunnel only"). Bearer token required whenever `--bind` isn't loopback.
- **Windows spawn hardening** — resolve/exec `claude.exe` directly; treat `.cmd`/`.ps1`
  shell-wrap (cmd.exe quoting, BatBadBut class) as a last resort. Closes the one
  remaining platform residual.
- **A short demo GIF** in the README (graph → click → story panel → Ask-about-this).

## Phase 2 — Deepen the intelligence (the differentiator)
- **Usage & cost analytics view** — the ecosystem's single most-duplicated tool; our
  read-only indexer is perfectly placed. Tokens/cost over time, by project, by cluster.
- **Continuation-chain detection** — link sessions that are literally the same thread
  resumed; strengthens Fusion beyond topic similarity.
- **Timeline view** — "what did I work on this week / that day," sessions on a time axis.
- **Cross-session recall** — "where did I deal with auth?" → jump straight to the moment.

## Phase 3 — Similarity upgrade (optional, opt-in)
- **Embeddings backend** — a local model as an alternative to TF-IDF for better
  clustering across paraphrase and mixed languages. TF-IDF stays the zero-dependency
  default; embeddings are an explicit opt-in so the "two auditable files" law holds.

## Phase 4 — Reach
- **Mobile-friendly read-only view** (PWA) for browsing your map on the go.
- **Multi-store support** — several machines / roots in one atlas.
- **Export** a session (or a cluster) as a clean, self-contained HTML page to share.

## Non-goals
No cloud service, no telemetry, no account system, no heavyweight frontend framework.
If a feature can't be done in Python stdlib + one HTML file (backend optional extras
aside), it doesn't belong here.

_Contributions welcome — Phase 2 items are the best on-ramp. See CONTRIBUTING.md._
