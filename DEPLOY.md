# Running Stardrive on Linux (incl. a remote desktop)

Stardrive is a single Python file (stdlib only) + one HTML file. There is nothing to
build and nothing to install. It runs anywhere Python 3.8+ runs; Linux is the primary
target and the reference platform.

## Requirements

- **Python 3.8+** (stdlib only — no `pip install`).
- The **`claude` CLI** on `PATH`, *only* if you want the Chat tab to actually drive
  Claude Code (the `--enable-run` feature). Everything else — browsing, clustering,
  the graph — works with no CLI and no network.

## Local run (30 seconds)

```bash
git clone https://github.com/atlasshb/session-atlas
cd session-atlas
python3 stardrive.py          # indexes your store, then serves
# open http://127.0.0.1:8877
```

It reads `~/.claude/projects` **read-only** and binds **loopback only** by default.

## Running on a remote desktop — the secure pattern

**Do not** expose Stardrive to your network. It has no authentication yet, and your
transcripts may contain secrets. Instead, keep it loopback-only on the remote box and
reach it over SSH — Stardrive never leaves `127.0.0.1`, so nobody but you can touch it.

On the remote desktop, keep it running (pick one):

```bash
# simplest: a detached tmux session
tmux new -d -s stardrive 'cd ~/session-atlas && python3 stardrive.py'
```

or as a **systemd user service** (survives logout, restarts on boot):

```ini
# ~/.config/systemd/user/stardrive.service
[Unit]
Description=Stardrive — local Claude session workspace
After=network.target

[Service]
WorkingDirectory=%h/session-atlas
ExecStart=/usr/bin/python3 %h/session-atlas/stardrive.py --port 8877
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now stardrive
loginctl enable-linger "$USER"     # keep it running when you're logged out
```

Then, **from your laptop**, tunnel loopback→loopback and browse it locally:

```bash
ssh -L 8877:127.0.0.1:8877 you@remote-desktop
# now open http://127.0.0.1:8877 on your laptop
```

That's it — the UI is on your laptop, the server and your session store stay on the
remote desktop, and the port is never open to anyone else.

## The Chat (run) feature on a remote box

`--enable-run` lets the Chat tab spawn the `claude` CLI with your permissions. Turn it
on only on a machine you trust, and only together with the loopback+SSH pattern above:

```bash
python3 stardrive.py --enable-run
```

**Never** combine `--enable-run` with a non-loopback `--bind` — that hands agent
execution to anyone who can reach the port. (Token-authenticated direct binding for
trusted tailnets is on the hardening roadmap; until it ships, use the SSH tunnel.)

## Keeping it fresh

The indexer re-runs automatically when `data.json` is older than 6h; the **Refresh**
button re-indexes on demand. `data.json` stays on your machine and is gitignored.

## Updating / removing

```bash
cd ~/session-atlas && git pull          # update
```

To remove: stop the service and delete the folder. Stardrive only ever writes
`data.json` and its log inside its own directory — it never writes into `~/.claude`.
