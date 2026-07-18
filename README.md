# Stardrive

**A full local workspace for Claude Code — the UI it should have shipped with.**

Claude Code stores every conversation as a `.jsonl` transcript, but the UI only shows you a flat list. Stardrive is a zero-dependency local workspace that reads your session store (read-only) and gives you:

- 💬 **Chat** — read any session as a proper thread, then **prompt right there**: new sessions or resume existing ones, streamed live through the Claude Code CLI, with `/` skill autocomplete
- 📁 **Projects** — browse every project folder Claude Code knows, jump into a session in any of them
- 🧩 **Skills** & 🧠 **Memory** — see every skill and memory file you've built, one click from using them
- 🗂️ **Tiles** — sessions auto-grouped into topic clusters (TF-IDF, no API calls, fully local)
- 🌳 **Tree** — project → topic → session hierarchy
- 🕸️ **Graph** — an Obsidian-style force-directed graph of your sessions; edges are topic similarity
- 🔀 **Fusion candidates** — groups of sessions above a similarity threshold (default 80%) that are probably the *same* thread, ready to merge/compact by hand

Everything runs locally. No cloud, no telemetry, no dependencies beyond Python 3 stdlib and one HTML file.

## Quickstart

```bash
python stardrive.py            # index your store, then serve
# open http://127.0.0.1:8877
```

| Option | Default | What |
|---|---|---|
| `--root PATH` | `~/.claude/projects` | Claude Code session store location |
| `--bind IP` | `127.0.0.1` | interface to serve on |
| `--port N` | `8877` | port |
| `--index-only` | | rebuild `data.json` and exit |
| `--serve` | | serve without re-indexing |
| `--enable-run` | off | allow the Chat tab to actually run the `claude` CLI |
| `--run-timeout N` | `900` | seconds before a chat process is killed |

The **Refresh** button in the UI re-indexes on demand. `data.json` (your indexed session metadata) stays on your machine and is gitignored — never commit it.

> ⚠️ **`--enable-run` executes the Claude Code CLI with your permissions.** It is off by default; without it the Chat tab is read-only (browse threads, no prompting). Never combine `--enable-run` with a non-loopback `--bind` unless everyone on that network is trusted — anyone who can reach the port can drive an agent on your machine.

## How it works

1. Streams the top-level `*.jsonl` transcripts (capped reads, tolerant of malformed lines), extracts each session's title and the first user messages.
2. Builds TF-IDF vectors (English + Dutch stopwords), computes pairwise cosine similarity.
3. Union-find clustering over the similarity graph; top terms label each cluster.
4. A single self-contained `index.html` renders tiles/tree/graph/fusion from `data.json` — the graph physics is ~150 lines of hand-rolled velocity Verlet, no libraries.

## Privacy

Your transcripts contain your prompts, code, and possibly secrets. Stardrive:
- opens the store **read-only** and never modifies it,
- binds to **loopback by default** — bind a LAN/tailnet interface only if you understand who can reach it,
- makes **zero external requests** (UI and server).

## Contributing

This is a community project of [AI HUB Tilburg](https://github.com/atlasshb). Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Good first targets: embedding-based similarity as an optional backend, session archiving actions, dark/light theming, multi-store support.

## License

[MIT](LICENSE)
