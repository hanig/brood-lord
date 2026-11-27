# Unit contract details

The generated declarations in `../SKILL.md` are authoritative. This file keeps
the longer failure explanations and examples. <!-- declaration: placement.reference-elaboration -->

## Code unit contract

Each code unit's prompt carries these five headings. <!-- declaration: plan.unit-contract -->
Every requirement names an owner decision, an observed failure with evidence, or the committee as its provenance; remove requirements without provenance. <!-- declaration: plan.unit-contract -->

- Claims: "The summary prints the number of processed records." <!-- declaration: plan.unit-contract -->
- Supported envelope and accepted limits: "UTF-8 JSON input; malformed input reports an error." <!-- declaration: plan.unit-contract -->
- Non-goals: "No new input formats." <!-- declaration: plan.unit-contract -->
- Acceptance tests: "Run the summary test on a two-record fixture and assert a count of 2." <!-- declaration: plan.unit-contract -->
- Requirement provenance: "Count and input scope: owner decision in PLAN.md; malformed-input error: committee decision in the ledger." <!-- declaration: plan.unit-contract -->

Before writing a contract for a unit that parses, walks, budgets or reads real project data, the orchestrator runs a read-only probe on the real data. Each task records that unit's cardinalities, match counts and accepted grammar as acceptance cases. Committed fixtures are synthetic reconstructions, never redacted copies. <!-- declaration: plan.unit-contract -->

## Kind boundaries

For `slurm`, the command is the work. Nesting `sbatch --wrap` submits an inner
job with no attempt binding while the outer job can exit successfully before
the real work runs. The command must never invoke `sbatch` or `srun`; scheduler <!-- declaration: slurm.command-boundary -->
flags belong in the unit's `sbatch` list. <!-- declaration: slurm.command-boundary -->

For code, `prompt` is the task text the coordinator hands the agent runner.
A coordinator dispatches that runner through the unit's backend: `lor session start`, or a headless `claude`, `codex` or `pi` turn (`paseo run` only on the legacy route). <!-- declaration: code.prompt-boundary -->
A provider flag placed there is a sentence for the agent to read, not runner
configuration. Provider, mode, model, thinking, and environment must remain <!-- declaration: code.prompt-boundary -->
unit fields. <!-- declaration: code.prompt-boundary -->

The default agent comes from `hanig-swarm/agents.json` (`default_by_orchestrator[orchestrator]` when top-level `orchestrator` is declared, otherwise failing closed to `default`, with any valid project snapshot applied). <!-- declaration: code.default-agent -->
That file records the owner's choice, the live-agent check behind it, and any `forbid_when_orchestrator` constraints. <!-- declaration: code.default-agent -->
A `lor`, `claude`, `codex` or `pi` backend needs no mode and refuses a restrictive one; a code unit that names no backend takes the legacy Paseo route, where an explicit mode <!-- declaration: code.configuration -->
is still required because provider vocabularies differ and default permissions <!-- declaration: code.configuration -->
can stop unattended work at its first write. The owner chooses unattended <!-- declaration: code.configuration -->
autonomy or a deliberate permission stall; the planner confirms the selected <!-- declaration: code.configuration -->
provider's spelling before recording and passing it verbatim. No positive list <!-- declaration: code.configuration -->
is portable or exhaustive. `bypass` is Claude's spelling and Codex rejects it, <!-- declaration: code.configuration -->
so the planner must ask rather than silently select a bypass. <!-- declaration: code.configuration -->

## Output location

The done predicate searches the attempt's exclusive write root. An absolute
output elsewhere can be produced correctly and remain structurally invisible.
Outputs must be relative to the run directory; use `SWARM_UNIT_DIR` when a tool <!-- declaration: outputs.attempt-relative -->
needs an absolute spelling and promotion when the bytes belong in a shared <!-- declaration: outputs.attempt-relative -->
location. <!-- declaration: outputs.attempt-relative -->

## Array failure

A Slurm array gives every task one unit attempt directory. The first task to
finish can replace the artifacts record while other tasks are incomplete, and
a dry run does not expose the fan-out. A unit must not combine `--array` with <!-- declaration: slurm.array-outputs -->
declared outputs; use one unit per shard or a separate merge unit over
per-task paths. <!-- declaration: slurm.array-outputs -->

## Code isolation and closure

`write_scopes` lets validation reject overlaps among concurrently runnable
units. It does not confine a process, so overlapping units must be ordered or <!-- declaration: code.write-scopes -->
given disjoint scopes. <!-- declaration: code.write-scopes -->

The coordinator instead gives each code attempt a verified worktree at the
anchored base. That isolates paths and permits concurrent attempts to share a
pull-request target, but it cannot isolate Unix principals. <!-- declaration: code.worktree-isolation -->

The coordinator creates the source branch. The unit must declare its repository <!-- declaration: code.target-branch -->
and `target_branch`, the destination into which its pull request merges; legacy
`branch` cannot substitute. <!-- declaration: code.target-branch -->

## Dedicated environments per model line

Every model/code line and orchestration runs in its own dedicated Python environment, never a shared personal or system env (conda `base`/`workbook`, `/usr/bin/python3`, `/opt/homebrew`, `~/.venv`). <!-- declaration: env.dedicated -->

Declare top-level `environments` in `plan.json` (`path`, `python`, `lock_file`, `build`, optional `model_line`, `kind`):

```json
"environments": {
  "orchestration": {"kind": "orchestration", "path": "/home/USER/.local/state/PROJECT/orch-venv", "python": "3.12", "lock_file": "none", "build": "uv venv --python 3.12 /home/USER/.local/state/PROJECT/orch-venv"},
  "stage": {"model_line": "stage", "path": "/home/USER/proj/.venvs/stage", "python": "3.12", "lock_file": "requirements.stage.lock", "build": "uv venv --python 3.12 /home/USER/proj/.venvs/stage && uv pip sync --python /home/USER/proj/.venvs/stage/bin/python requirements.stage.lock"},
  "maxtoki": {"model_line": "maxtoki", "path": "/home/USER/proj/.venvs/maxtoki", "python": "3.12", "lock_file": "requirements.maxtoki.lock", "build": "uv venv --python 3.12 /home/USER/proj/.venvs/maxtoki && uv pip sync --python /home/USER/proj/.venvs/maxtoki/bin/python requirements.maxtoki.lock"}
}
```

Each `slurm`, `pipeline`, and `code` unit binds to its model-line env via `environment`, `runtimes.ID.environment`, or (for `slurm`/`pipeline`) an absolute entrypoint under `ENV_PATH/bin/` or `ENV_PATH/scripts/` (or exact `ENV_PATH` prefix for `conda`; unqualified conda names and wrappers/launchers outside `bin/`/`scripts/` such as `ENV_PATH/libexec/...` require explicit binding when `environments` is declared), or declares `environment: "none"` (or `runtime: "none"`) for non-Python units; declared env dirs must be disjoint, whitespace-free, and include one `orchestration` entry (refusing shared personal/system dirs including `/usr/libexec` and exact `$HOME/.local` user-site while permitting deeper project paths like `$HOME/.local/state/PROJECT/orch-venv`, with exact-case POSIX system prefixes); `environment: "none"` refuses any non-container path inside a declared environment directory or active plan-used Python environment root (container images are isolated from host Python envs); for units not declaring `environment: "none"`, `bin/` or `scripts/` directly under a venv-layout directory (`venv`, `.venv*`, `*-venv`, `*_venv`, or direct children of `.venvs`/`venvs`/`virtualenvs`/`.virtualenvs`) or a conda distribution `envs/NAME` directory is lexically reserved for that Python environment (refused for undeclared bound wrappers/`uv`/pipelines, checked for shared root names, and tracked for cross-model-line sharing; placing standalone non-Python binaries directly under venv-layout `bin/`/`scripts/` for Python-bound units is outside the supported lexical envelope); non-container `runtime.entrypoint` values must be whitespace-free (put arguments in `command`, never `entrypoint`, and never use shell quotes, globs, or `env` wrappers): clean absolute executables for `direct`/`wrapper`/`uv` (bare `uv` or versioned `uv-*` outside undeclared venv/conda env dirs allowed for `uv`; explicitly bound wrappers inside the bound env outside nested venv/conda subdirs allowed; non-Python executables and standalone native `micromamba` under `direct` or `wrapper` outside other declared envs and undeclared venv/conda env dirs allowed for bound `pipeline` units), clean bare non-Python names or absolute paths for `path`, unqualified conda names or absolute conda prefixes/executables for `conda` (explicitly bound unqualified conda names rely on preflight unless colliding with another declared conda `envs/NAME` dir or binding; legacy `conda` paths ending in `/` parse as prefix directories, those ending in `bin/PYTHON_EXE` or `scripts/PYTHON_EXE` parse as executables under their grandparent prefix unless declared verbatim in top-level `environments`, and other absolute legacy `conda` paths parse as prefix directories), and valid project-scoped Lmod module names for `module` (never exact shared Python/conda components at top level or below shared categories, and never absolute paths when `environments` is declared; site-specific Lmod compiler/prerelease suffixes are verified by canary/preflight rather than offline regexes, and legacy plans omitting `environments` still accept clean non-shared absolute paths); `swarm.py validate` checks declarations lexically (no shell parsing, statting, or `conda info --envs` lookup, so legacy plans omitting `environments` cannot correlate unqualified conda names with absolute prefixes or distinguish unrecognized console scripts from sharable native binaries in non-venv-named dirs unless an active unit establishes that Python root), refusing shared/undeclared envs, unbound units when `environments` is declared, workloads targeting `orchestration`, and cross-model-line sharing. <!-- declaration: env.dedicated -->
