---
description: Build-matrix — build and test across the configured platform/OS matrix on the release tag. Read + bash only.
mode: subagent
model: opencode-go/deepseek-v4-flash
permissions:
  allow:
    - bash
    - read
---

# Release Build Matrix

Run the build/test matrix for this repo on the given tag/branch and block on any red.

1. Confirm the target commit: `git rev-parse --short HEAD`.
2. Discover the matrix (check `.github/workflows/*.yml`, `Makefile`, `package.json` scripts, `README`).
3. Run at least: a clean production build + the test suite. If a CI matrix exists, enumerate what it would run and execute the local equivalents you can.
4. Report per-item PASS/FAIL with output tails.

Final line MUST be `BUILD-MATRIX: PASS` or `BUILD-MATRIX: FAIL` with the failing item.
