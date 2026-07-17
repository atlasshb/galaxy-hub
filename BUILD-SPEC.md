# SESSION ATLAS — build spec (v1)

The missing layer over Claude Code's flat session list: auto topic-clustering, tiles,
folder/topic tree, Obsidian-style graph, and fusion (merge/compact) candidates.
Read-only over the session store. Local-first, loopback by default.

## Paths
- App dir: repo root
- Deliverables: `session_atlas.py` (indexer + HTTP server, Python 3 stdlib ONLY), `index.html` (self-contained UI, zero external requests)
- Generated: `data.json`, `atlas_sessions.log`
- Source store (READ-ONLY): `~/.claude/projects/<projdir>/*.jsonl` — TOP-LEVEL files only. Never recurse into subdirectories (subagents/, workflows/ transcripts live there). Never write/modify anything under `.claude`.

## data.json contract (both builders MUST match exactly)
```json
{
  "generated": "2026-07-17T15:00:00Z",
  "nFiles": 570,
  "sessions": [
    { "i": 0, "id": "aaaabbbb-....", "title": "Example session title",
      "project": "home-code-myapp", "projectLabel": "~/code/myapp", "mtime": "2026-07-17T14:21:00Z",
      "msgs": 42, "kb": 139, "cluster": 3, "terms": ["fleet","ollama","tailscale"] }
  ],
  "clusters": [ { "id": 0, "label": "odoo / invoice / mollie", "size": 14 } ],
  "edges": [ [0, 17, 0.412] ]
}
```
- `edges`: cosine similarity, 3 decimals, only pairs with w >= 0.15, i<j, session indices.
- `clusters` sorted by size desc; singletons get their own cluster.
- `terms`: top 5 TF-IDF terms of that session.

## session_atlas.py
1. **Scan**: iterate project dirs, top-level `*.jsonl`. Per file (utf-8, `errors='replace'`, tolerate BOM): stream max 4000 lines or 5 MB.
   - Skip lines that fail json.loads (count, don't crash).
   - `title`: first line with `"type":"summary"` → its `summary` field. Else first real user message, cleaned (strip leading `instruction:`, collapse whitespace, max 70 chars). Else id prefix.
   - User text for clustering: lines with `type=="user"`, NOT `isSidechain:true`, NOT `queue-operation`. `message.content` is a string OR a list of blocks — take only `{"type":"text"}` blocks. Skip text starting with `<system-reminder>`, skip tool_result content. Collect up to first 20 user messages, cap 6000 chars total. Weight the title ×3 (prepend it 3 times to the doc).
   - `msgs`: count of user+assistant type lines (within line cap).
2. **Vectorize**: tokenize lowercase `[a-z][a-z0-9_-]{2,}`; drop tokens >30 chars; drop uuid/hex-looking tokens (`^[0-9a-f-]{8,}$`); stopword list: common English AND Dutch (de,het,een,en,van,ik,je,dat,is,niet,met,voor,naar,ook,maar,als,dan,wat,kan,moet,zijn,heb,the,and,for,with,that,this,you,not,are,but,can,all,use,...aim ~120 words). TF-IDF (tf=count/len, idf=ln(N/df)), L2-normalize, sparse dict dot for cosine. All pairs is fine (~600 docs).
3. **Cluster**: union-find over edges with w >= 0.30. Cluster label = top-3 TF-IDF terms of concatenated member docs, joined " / ".
4. **Titles override**: if `titles_override.json` exists (`{"<sessionId>": "title"}`), those titles win.
5. **Server**: `ThreadingHTTPServer`. Bind `--bind` (default `127.0.0.1`) on `--port` (default `8877`); on bind failure fall back to loopback (log it). Routes: `GET /` → index.html (no-cache), `GET /data.json`, `POST /refresh` → synchronous re-index, respond `{"ok":true,"nSessions":N,"secs":S}`. Any exception during refresh → 500 with error string, keep serving old data.
6. CLI: `--index-only`, `--serve` (default: index if data.json missing/older than 6h, then serve). Log to `atlas_sessions.log`, truncate when >1 MB. No third-party imports. Handle: empty files, 0-session store, unicode, file locked by running session (open with read share — normal `open()` is fine on Windows for files being appended).

## index.html
One file, vanilla JS/CSS, dark theme (bg `#0d1117`, panels `#161b22`, border `#30363d`, text `#c9d1d9`, accent `#58a6ff`, muted `#8b949e`; 12-color cluster palette). Fetches `/data.json` on load. NO external fonts/CDNs.

Header bar: `SESSION ATLAS` wordmark, stats (sessions / clusters / generated), live search input, edge-threshold slider (0.15–0.60, default 0.30), Refresh button (POST /refresh then reload data), view tabs: **Tiles | Tree | Graph | Fusion**.

- **Tiles**: responsive grid of cluster cards (header = cluster label + count, colored dot), body = session chips (title, relative time). Click chip → right-side detail panel: title, project, mtime, msgs, size, terms, its strongest 5 neighbors with weights, and a copyable `claude --resume <id>` line.
- **Tree**: collapsible: project → cluster → sessions (count badges).
- **Graph** (the centerpiece — Obsidian feel): full-viewport `<canvas>`, hand-rolled force sim (velocity Verlet: pairwise repulsion ~O(n²) is fine at ~600 nodes, spring force on edges above slider threshold, weak center gravity, friction 0.85, warm 300 ticks then settle). Node radius ∝ 2+2·ln(1+msgs), fill = cluster color. Wheel zoom (toward cursor), drag-pan background, drag nodes. Hover: highlight node + neighbors, dim rest to 15% alpha, show labels of highlighted. Labels always on when zoom > 1.6. Click node → same detail panel. Slider changes → rebuild springs live.
- **Fusion**: connected components over edges with w >= fusion slider (0.50–0.95, default 0.80 — separate slider in this view). Each group: member titles + ids, shared top terms, and a proposal box: "Same topic — candidates to merge/compact" with a copy button for the id list. READ-ONLY: propose, never merge.

Search filters every view live (title + terms substring). Empty/failed data.json → friendly error state with retry.

## Guardrails
- Read-only over `.claude`. Loopback bind by default — never default to 0.0.0.0. No external requests from UI. No merging/deleting sessions in v1 (propose-first).
