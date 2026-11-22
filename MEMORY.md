# MEMORY.md — multi-agent-skills

Read [CLAUDE.md](CLAUDE.md) for repository rules and architecture, and
[docs/plan-field-reports.md](docs/plan-field-reports.md) for the live plan.
Verify any outstanding item against the current code before acting.

For an orchestrator takeover, read [the handoff](docs/handoff-2026-09-20.md)
and [the current mandate](docs/orchestrator-mandate.md) before operating.
A snapshot neither supplies authority nor establishes current run status.

For suite size, `CLAUDE.md` owns the guarded lower bound; run unittest
discovery for the exact current total.

## Generated facts

The block below is preserved from its recorded commit. It is historical
snapshot data, not a claim about the current checkout or running work.

<!-- handoff:facts:begin sha=4a2935d3fe6a9e1d3df5d4b97365e32888b3a123 -->

**Recent commits** (regenerated in full: no previous marker):
- `4a2935d3 docs(skills): trim hanig-orchestrate, hanig-review-gate, and hanig-wandb-logging SKILL.md bloat (ARC-5105) (#411)`
- `8dc0a7d5 refactor(review): extract CLI handlers, escalation policy, and _main phases to review_cli.py (ARC-5110) (#410)`
- `758f48d6 docs(skills): trim hanig-swarm and hanig-project SKILL.md bloat and update drift.coordinator-size (ARC-5101) (#409)`
- `f414c86c refactor(review): extract round-ledger and verdict-schema subsystems from review.py (ARC-5104) (#408)`
- `c93086c3 refactor(swarm): extract advance retry, claims, dispatch, and final-intent phases to swarm_advance.py (ARC-5100) (#407)`
- `200780a6 refactor(swarm): extract advance reconciliation, recovery, judgment, and settlement to swarm_advance.py (ARC-5099) (#406)`
- `f622dc1b refactor(swarm): extract _submit phases to swarm_submit.py (ARC-5108) (#405)`
- `e167a4d5 refactor(swarm): extract remaining validate_plan phases to swarm_validate.py (ARC-5102) (#404)`
- `8d7efdea refactor(swarm): extract validate_plan schema, retry, limits, and DAG phases (ARC-5107) (#403)`
- `8fe55ac4 perf(tests): speed up test_x_bfs_exhaustive fixtures, expand SPLIT, refresh durations (ARC-5109) (#402)`
- `11381534 docs: trim CLAUDE.md below 18 KB and fix README/field-report drift (ARC-5106) (#401)`
- `49e9a585 ci: adopt scripts/run_tests.py in release-validation with XDG parity (ARC-5098) (#400)`
- `6edb2f65 Amend the mandate for multi-host custody and a Sonnet concurrence alternate (#399)`
- `5297fa2f Document author exclusion, panel reserves, and D5 host custody (#397)`
- `e8d8fef9 Give archive fixtures headroom under host load (#398)`
- `9b5cd776 Persist the declared host in D5 custody (#396)`
- `77f1f263 Declare the host for D5 session anchors and add the exec launcher (ARC-5091) (#395)`
- `22daa115 Exclude authors by canonical model identity and restore panels from declared reserves (ARC-5089) (#394)`
- `87081c54 Add an uncertified Antigravity installer adapter (#393)`
- `c62b8de6 Seat Gemini Neon in committee and planning profiles (#392)`
- `273f932b Move Sol and Astra seats and the code author to the 6.2 releases (ARC-5058) (#391)`
- `23d3a487 Add a one-shot coordinator state event waiter (ARC-5040) (#389)`
- `80f78f7a Record triage for debrief packet 5309ec23 (#388)`
- `ce4d99e2 Preserve Slurm settle windows during accounting outages (#387)`
- `3db8c426 Stop Linear commands at a rate limit and report what remains (ARC-5039) (#386)`

**Uncommitted paths** (0):
- none

**Open contracts** (0):
- none found under this directory

<!-- handoff:facts:end -->
