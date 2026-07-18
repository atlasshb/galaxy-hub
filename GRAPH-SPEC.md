# STARDRIVE v3.1 — GRAPH THAT TEACHES

> "The goal isn't a graph that wows you with how complex your codebase is — it's a
> graph that quietly teaches you how every piece fits together." — Understand-Anything

v3.1 applies that philosophy to the session graph: every node click should *explain* —
what this session is about, where it fits, why it connects to its neighbors — and every
question a node raises should be one click from asking Claude itself. Additive wave on
top of v3 (CONSOLE-SPEC.md); same files, same single-author locks, same guardrails.

## Backend (stardrive.py) — one addition

`data.json` sessions gain `"tw"`: the session's top-25 TF-IDF terms as
`[["term", 0.312], ...]` pairs (weight = the L2-normalized vector value, 3 decimals,
sorted desc). Reuse the existing vector/`top_terms` machinery — this is the same data
`terms` comes from, just deeper and with weights. Everything else in the data.json
contract (including the `edges` `[i, j, w]` format) is UNCHANGED. Existing `terms`
(top-5, unweighted) stays for backward compatibility.

## Frontend (index.html) — the teach layer

All shared-term logic is client-side from `tw`: the *link explanation* for an edge
(i, j) = intersection of the two sessions' `tw` term sets, ranked by `min(w_i, w_j)`,
top 3. Code defensively: `tw` may be absent (old data.json) — degrade to current
behavior, never break.

1. **Cluster legend** — collapsible overlay on the Graph view listing each cluster:
   color dot, label, size, sorted by size, top ~12 with a "+N more" expander. Click a
   legend row → isolate that cluster (its nodes full alpha, rest dimmed to ~10%);
   click again or Esc → clear. Active row visibly highlighted.
2. **Focus mode** — click a node: pin focus (node + direct neighbors full alpha +
   labels on, rest dimmed — like today's hover, but held) AND open the story panel.
   Double-click: expand the focus set by one BFS ring (repeatable). Esc or
   background click clears. Hover behavior inside focus mode only affects
   focused nodes.
3. **Node story panel** — replace the raw-stats detail panel *content* for graph
   clicks (the panel component itself stays) with a narrative block:
   - One synthesized sentence: "Mostly about **term1**, **term2** and **term3** —
     part of the '<cluster label>' group (N related sessions), last touched
     <relative time>." (plain string building, no LLM).
   - **Connections list**: each neighbor above the current edge threshold, sorted by
     weight: title, weight bar, and "linked by: term, term" from the shared-term
     rule above (or "weak topical overlap" when the intersection is empty).
     Clicking a connection re-focuses the graph on that node.
   - Existing facts (project, mtime, msgs, size, `claude --resume` line) stay below.
   - New button **"Ask about this session"** next to "Open in Chat": switches to
     Chat with that session selected AND the composer prefilled with "Recap this
     session: what was done, what was decided, and what state is it in now?" —
     user presses Enter to send (never auto-send).
4. **Weighted search** — when `tw` is present, graph search ranks/highlights by
   summed weight of matched terms (query tokens prefix-match against `tw` terms,
   plus title substring as today). Matched nodes full alpha with labels; others
   dimmed proportionally to score. Other views keep substring behavior.
5. **Graph HUD hint line** — one muted line: "click: focus · double-click: expand ·
   esc: clear · /: search". Palette (Ctrl+K) session entries may reuse the weighted
   scorer when trivial to share.

## Explicit non-goals (v3.1)

No edge-format change, no server-side search endpoint, no auto-generated tours, no
LLM calls at index time — the narrative is template text from data already computed.

## Verification

Backend: data.json validates (every session has ≤25 `tw` pairs, weights desc, 3dp);
existing v1 fields byte-identical semantics. Frontend: legend isolate/clear, focus +
BFS expand + Esc, story panel narrative + connections with shared terms on a real
store, Ask-about-this-session prefill, weighted search visibly re-ranking, and a
data.json WITHOUT `tw` (delete the key from a copy) still renders the old experience
without console errors.
