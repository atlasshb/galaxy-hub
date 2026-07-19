# Deploying Galaxy Hub on a VPS behind a domain (e.g. `D2D.jouwenergieadvies.nl`)

> This puts Galaxy Hub on the public internet. Read the security section **first** — it
> serves your Claude Code transcripts, which may contain prompts, code, and secrets.

## ⚠️ Security — non-negotiable for a public deploy

On a public domain the only things between the internet and your data are **TLS** and a
**token**. So:

- **TLS is mandatory** — Caddy (below) provisions a Let's Encrypt certificate automatically.
- **`--token` is mandatory** — a strong random secret; every request must present it.
- **Keep `--enable-run` OFF.** With it on, anyone who has the token can run the `claude`
  CLI — i.e. execute code and drive agents **on atlas01**. That is remote-code-execution-
  as-a-service gated only by a token. Do not enable it on a public box unless you fully
  accept that and trust everyone with the token.
- The token appears in the first URL you open (`?token=…`), so open it privately and
  don't paste that URL anywhere. Rotate the token if it leaks.
- **Whose sessions?** Galaxy Hub shows the `~/.claude/projects` of the OS user it runs as
  on atlas01 — not your laptop's. Run the service as the user whose Claude Code history
  you actually want to see. If your sessions live on your laptop, this instance won't show
  them unless that store is synced to atlas01.

## 0. DNS

Point an **A** record `D2D.jouwenergieadvies.nl` → atlas01's public IPv4 (add an **AAAA**
for IPv6 if the box has one). Confirm it resolves before continuing:
```bash
dig +short D2D.jouwenergieadvies.nl
```

## 1. Install prerequisites (Ubuntu/Debian, as a sudo user)

```bash
sudo apt update && sudo apt install -y python3 git curl ufw
# Caddy (official repo) — gives automatic HTTPS
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

## 2. Get Galaxy Hub + generate a token

```bash
sudo git clone https://github.com/atlasshb/galaxy-hub /opt/galaxy-hub
TOKEN=$(openssl rand -hex 24)
echo "GH_TOKEN=$TOKEN" | sudo tee /etc/galaxy-hub.env >/dev/null
sudo chmod 600 /etc/galaxy-hub.env
echo "OPEN THE SITE ONCE WITH:  https://D2D.jouwenergieadvies.nl/?token=$TOKEN"
```

## 3. systemd service — loopback bind + token (Caddy does TLS/proxy)

Replace `USER=youruser` with the account whose `~/.claude` you want to serve (**not root**).

```ini
# /etc/systemd/system/galaxy-hub.service
[Unit]
Description=Galaxy Hub
After=network.target

[Service]
User=youruser
EnvironmentFile=/etc/galaxy-hub.env
WorkingDirectory=/opt/galaxy-hub
# NOTE: no --enable-run. Add it ONLY if you accept RCE-by-token (see Security).
ExecStart=/usr/bin/python3 /opt/galaxy-hub/stardrive.py --bind 127.0.0.1 --port 8877 --token ${GH_TOKEN}
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now galaxy-hub
sudo systemctl status galaxy-hub --no-pager
```

The server binds `127.0.0.1:8877` only — never directly exposed. The `--token` makes it
require auth even on loopback, because Caddy will expose it publicly.

## 4. Caddy — automatic HTTPS + reverse proxy

```caddyfile
# /etc/caddy/Caddyfile
D2D.jouwenergieadvies.nl {
    reverse_proxy 127.0.0.1:8877
}
```

```bash
sudo systemctl reload caddy         # fetches the Let's Encrypt cert on first request
sudo ufw allow 80,443/tcp           # open only the web ports
sudo ufw deny 8877/tcp              # never expose the app port directly
```

## 5. Open it

Visit **once**: `https://D2D.jouwenergieadvies.nl/?token=<TOKEN>` — a HttpOnly cookie keeps
you signed in, so later visits are just `https://D2D.jouwenergieadvies.nl/`. Anyone without
the token gets `401`.

## Updating

```bash
cd /opt/galaxy-hub && sudo git pull && sudo systemctl restart galaxy-hub
```

## Removing

```bash
sudo systemctl disable --now galaxy-hub
sudo rm /etc/systemd/system/galaxy-hub.service /etc/galaxy-hub.env
sudo rm -rf /opt/galaxy-hub
# remove the Caddyfile block + reload caddy
```

---

_Runs entirely on atlas01. Nothing here reaches back to any cloud service — Galaxy Hub
still makes zero external requests; Caddy is the only internet-facing piece and only for
TLS + proxy._
