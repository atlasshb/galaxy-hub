# STARDRIVE — brand & identity brief

Stardrive is a public open-source project. Its identity should be **ownable and
reel-worthy**: a cartographer's instrument for your AI work history — not a GitHub
clone, not a Claude imitation. Metaphor: **star charts / atlases** — your sessions
are a territory; Stardrive charts it.

## The law still applies

Zero external assets. Every glyph is inline SVG or unicode, every color a CSS
variable, no fonts beyond the system stack (a good monospace stack for data:
`ui-monospace, 'Cascadia Code', 'SF Mono', Consolas, monospace`).

## Palette

Dark ("night chart", default):
- bg: deep space-navy ink `#0b1020` (subtle blue-violet cast, NOT neutral gray)
- panels: `#121831`-ish, borders a desaturated indigo `#26304f`
- text: starlight off-white `#dde3f0`; muted: `#8a93ad`
- **accent: atlas gold `#d4a94f`** (compass brass) — primary actions, wordmark glyph, focus rings
- secondary accent: meridian teal `#4fd4c5` — links, edge highlights, live/streaming states
- The 12-color cluster palette stays but tuned to sit well on navy (jewel tones over pure hues).

Light ("parchment map"):
- bg: warm paper `#f6f1e7`, panels `#fdfaf3`, borders `#d8cfba`
- text: ink `#2b2820`; same gold accent (darkened for contrast, e.g. `#a07c28`), teal darkened similarly
- Must pass reasonable contrast (AA-ish) — tune values, keep the temperament.

## Marks & motifs

- **Wordmark**: `STARDRIVE` in caps with letter-spacing, preceded by a small
  compass-rose glyph (inline SVG, gold, ~16px; 4-point star in a thin circle is
  enough). The glyph alone is the favicon (inline SVG data URI).
- **Graph = star chart**: nodes get a soft glow (shadowBlur in the node's cluster
  color); the graph background carries a *very faint* dotted grid or 2–3 faint
  great-circle/meridian arcs (canvas-drawn, opacity ≤ 0.05, cheap — draw once per
  frame, no perf hit). Zoomed-out clusters should read as constellations.
- **Legend styled as a map legend**: thin gold rule at top, small-caps title.
- **Microcopy** uses light cartography vocabulary where it fits naturally:
  "legend", "regions" for clusters in labels, "chart" in the graph HUD. Subtle —
  navigation words, never gimmick sentences.

## Voice

Calm, precise, instrument-like. No exclamation marks in UI copy. Empty states
explain and orient ("No sessions charted yet — run the indexer").

## Application order

Applies to every view (header, tiles, tree, fusion, chat), both themes. The
teaching-graph wave (GRAPH-SPEC.md) should land already dressed in this identity.
