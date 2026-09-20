# Security Policy

## Reporting a vulnerability

Report privately to **info@atlascorporation.org**. Please do not open a public issue
for a vulnerability. Expect an acknowledgement within a few working days.

## Threat model

Galaxy Hub reads your Claude Code transcript store and, when `--enable-run` is set,
**executes the `claude` CLI on the host**. Treat any instance with `--enable-run` as
equivalent to a shell on that machine.

### Built-in protections

- The server refuses to bind a non-loopback address without `--token`/`--token-file`.
- Bearer-token auth on every request: `Authorization: Bearer`, `?token=`, or the
  `gh_token` cookie, compared in constant time.
- Host-header allowlist, to blunt DNS-rebinding.
- CLI processes are spawned with direct `exec` — never through a shell.
- The transcript store is opened read-only.
- Concurrency is capped by `--max-runs` (1-8) and each run is killed after
  `--run-timeout` seconds.

### Deployment guidance

- Never expose an `--enable-run` instance directly to the public internet. Bind it to
  loopback or a private interface and front it with an authenticating reverse proxy.
- Keep the token in a file (`--token-file`), not in the process arguments, so it does
  not appear in `ps` output.
- The token grants command execution. Rotate it if it is ever exposed.

## Supported versions

Only the latest commit on `main` is supported.
