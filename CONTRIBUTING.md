# Contributing to Stardrive

Community project of AI HUB Tilburg. PRs welcome from everyone.

## Ground rules

1. **Zero runtime dependencies.** `stardrive.py` is Python 3 stdlib only; `index.html` is one self-contained file with no CDN/external requests. PRs that add a package manager will be declined — that constraint is the product.
2. **Read-only over the store.** Nothing may ever write into `~/.claude`. Features that mutate sessions (merge, archive, delete) must be propose-first: show the user the exact action, let them execute it.
3. **Privacy first.** Never commit `data.json`, logs, or any transcript content — including in tests and screenshots. Redact session titles in screenshots you attach to issues.
4. **Small PRs.** One feature or fix per PR, with a short before/after note. Test against a real store (`--index-only` must complete clean) before opening.

## Ideas that are welcome

- Optional embedding backend (local model) behind the same `data.json` contract
- Timeline / calendar view
- Cross-machine store merging (multiple `--root`s)
- Session search across transcript bodies
- Light theme

## Dev loop

```bash
python stardrive.py --index-only   # rebuild data.json
python stardrive.py --serve        # serve on 127.0.0.1:8877
```

No build step. Edit, refresh browser, done.
