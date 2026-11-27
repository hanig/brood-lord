# Changelog

## Unreleased — 2026-11-22

### Added — `brood-conclave`, the tenth authored skill (2026-11-27)

- **Multi-model panel:** folds the standalone `hanig-conclave` repository in as
  `skills/brood-conclave/` (model-invocable; not in `EXPLICIT_ONLY_SKILLS`, no
  `agents/openai.yaml`). One seat per model family from `families.json`, Lor subagents by
  default (`"lor": true`), blind round 0, anonymized cross-examination on moderator-listed
  cruxes, and a ratification vote. `scripts/conclave.py` names no model id.
- **Portable paths:** commands run as `python3 "$BROOD_CONCLAVE_DIR/scripts/conclave.py"`;
  `scripts/allow.py` writes allow rules for the installed copy it sits in, and `--remove`
  also takes out rules for the old `~/.claude/skills/hanig-conclave` path. Live state stays
  in `~/.local/state/hanig-conclave`, and `CONCLAVE_LOR` keeps its meaning. Reinstalling
  retires an owned `hanig-conclave` copy; the standalone repository's own symlink install
  is not owned, so it stays with a warning.

### Changed — Authored skills renamed from `hanig-*` to `brood-*`

- **Skill names (PR 2/2 of the brood rename):** the nine authored bundles now ship as
  `brood-debrief`, `brood-hillclimb`, `brood-orchestrate`, `brood-portable-handoff`,
  `brood-project`, `brood-review-gate`, `brood-swarm`, `brood-verified-workflow` and
  `brood-wandb-logging`. Reinstalling publishes each `brood-<suffix>` and then retires a
  sibling `hanig-<suffix>` only when it carries this repository's exact recorded ownership.
- **Unchanged on purpose:** runtime state (`~/.local/state/hanig-swarm`,
  `~/.local/state/hanig-review-gate`, `~/.hanig-committee`, `.hanig/`), Git refs under
  `refs/hanig-swarm*`, the `hanig-rev-` Slurm job prefix, the `hanig-debrief: ` issue title
  prefix that de-duplicates filed packets, and every `HANIG_*` setting (still read after its
  `BROOD_*` twin). Live runs continue without migration.

### Added — Dedicated environments per model line and for orchestration

Field evidence: STAGE ran coordinator cron, hourly facts, merge helpers, and probes from
shared `workbook` conda until 2026-11-22 while STAGE and MaxToki had separate venvs; moving
orchestration to `~/.local/state/<project>/orch-venv` (`uv`, Python 3.12; stdlib-only scripts)
decoupled orchestration from personal conda state.

- **Planning & validation (`hanig-project`, `hanig-swarm`):** Top-level `environments`
  (`path`, `python`, `lock_file`, `build`, optional `model_line`, `kind`), unit/runtime
  `environment` and `model_line` in `swarm.py schema`, `swarm_plan_rules.py`, and `plan_digest()`.
  `swarm.py validate` lexically refuses shared/system Python envs, undeclared envs, missing
  `orchestration`/code bindings when `environments` is declared, workloads targeting
  `orchestration`, cross-env canaries, and cross-model-line sharing without shell parsing or statting.
- **Survey & interview (`hanig-project`):** `survey.py` records `machine.python_env`
  (`schema_version: 4`) and discovers root/subproject lock files inside its bounded walk;
  interview asks once for orchestration's env and recommends `~/.local/state/<project>/orch-venv`.
- **Orchestration loop, cron & startup check (`hanig-orchestrate`, `hanig-swarm`):**
  Added `orch_env.py` (`check` CLI and mismatch warnings in `hourly_facts.py`, `merge_unit.py`,
  `reconcile.py`, `wait_event.py`, `wait_report.py`). Updated `swarm-cron.sh` (requiring explicit
  absolute `ORCH_PYTHON`, never bare `python3`), `protocol-details.md`, and `operating-loop.md`
  (`advance` and `hourly_facts` cron/launchd templates).
- **Delegation, migration & declarations:** Updated `dispatch-preamble.md` (unit env
  placeholders, `environment: "none"`, new-model-line venvs), added the 4-step migration
  runbook in `operating-loop.md`, and updated `declarations.json` + regenerated `SKILL.md`.
