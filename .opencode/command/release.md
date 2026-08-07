---
description: Run the release swarm for this repo (spawns release-orchestrator and its specialist agents). Optionally give a target like staging (default) or prod.
---

Launch the release swarm. Use the Task tool to invoke the `release-orchestrator` subagent with the target argument (staging or prod, default staging). Let it run the full runbook (see docs/RELEASE-SWARM.md) and report each gate. Do not skip its gates.
