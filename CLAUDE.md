# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This repository is private by owner decision (2026-11-05); it was public from 2026-09-24 until then, so treat everything already pushed as published. It carries cluster hostnames, partition/account names and measured host probes; never commit credentials, tokens or secrets, and never add further cluster detail to PR titles, bodies or comments.

If you are driving a run rather than editing this repository, read `docs/orchestrator-mandate.md` first. Authority is owner-granted by merge, not by conversation, and a new session must show the owner its enumerated grant and bounds and get confirmation before operating unattended. Until confirmed you may read, survey, plan, run the gate and report, and you may not dispatch, merge, spend or mutate a tracker.

`MEMORY.md` is a dated snapshot, not live truth; verify any owed item against the tree before acting on it. `docs/plan-field-reports.md` is the live plan and `README.md` is the CLI reference (same caveat on measurements). Routing truth is the `enabled` flags in `skills/hanig-review-gate/reviewers.json`, the code-unit default in `skills/hanig-swarm/agents.json`, and `models.json`, never a roster quoted in prose. No script should name a model: `tests/test_no_hardcoded_models.py` fails on any tracked authored Python string literal equal to a model id declared in those three files (excluding prose/fixtures); an undeclared model id typed directly is not caught (ARC-1175 closed 2026-10-11).

## Commands

<!-- docs-truth:suite-lower-bound -->
Full suite: at least 1,500 tests discoverable by unittest. Run the command below for the exact current total.

Only the standalone marker above declares the live suite total. A dated or
historical count in this canonical document must have the standalone marker
`<!-- docs-truth:historical -->` immediately above its prose paragraph. Other
documents reference this floor and keep their count-shaped prose advisory.

```sh
python3 scripts/run_tests.py                     # full suite in parallel, one process per module
python3 scripts/run_tests.py test_outbox         # chosen modules, same isolation
python3 scripts/run_tests.py --fast              # inner loop: leaves out tests/slow_tests.json
python3 -m unittest discover -s tests            # full suite, serial
python3 -m unittest tests.test_swarm             # one module
python3 skills/hanig-swarm/scripts/swarm.py schema           # every unit field, before writing a plan
python3 skills/hanig-review-gate/scripts/review.py --kind implementation --staged --round 1 \
  --claim "This change cannot make an honest run fail."
./install.sh --dry-run --json --agent claude     # plan an install, changes nothing
./bin/doctor --json                              # what is installed, from where, still runs
./bin/probe.sh                                   # read-only environment probe for a new host
python3 tests/native_agent_validation.py         # release-time host harness, not a unit test
```

`scripts/run_tests.py` runs exactly the modules discovery loads, each in its own process with fresh `HOME`, `XDG_CONFIG_HOME` and `TMPDIR` (no other inherited env), slowest first by `tests/durations.json`. Modules in `SPLIT` run interleaved test-id chunks and must share no cross-test state. A module fails if it fails, errors or collects zero tests. `--fast` omits `tests/slow_tests.json` (inner loop only; verify changes with the full run). Refresh both files with `--update-durations` (Python 3.12+ for per-test times).

The review gate derives implementation identity from `origin`, target merge-base and branch (`--target REF` for targets other than `origin/main`; `--plan-name NAME --unit UNIT` for detached checkouts). Round 1 starts a new identity only; subsequent rounds need the next round number and `--dispositions FILE`. The gate counts per identity; the orchestrator tracks the 3-round ceiling per logical plan unit across attempts and continuations in `PLAN.md`. A continuation branch is a new gate identity, not a fresh lineage budget; exhaustion invokes the mandate's committee step.

`python3 -m unittest tests.test_review` (and discovery including `test_review.py`) runs the whole module through its supervised worker; `-k` does not narrow it. Fully qualified class or method names (e.g. `tests.test_review.TestVerdictSchema.test_empty_object_is_not_a_review`) bypass the worker and run in-process without session/journal-manifest audit. A named class matching zero methods fails instead of passing silently.

Automatic install selection uses executable presence and reports adapter certification separately: uncertified or stale-dated versions are planned as visibly `unverified`, emitting certification warnings to stderr even with `--json`. Explicit `--agent` is the offline/bootstrap route. Copy is the default; `--mode link` is for skill development. Check `--dry-run --json` before real installs; `--allow-vendored-shadow` and `--allow-org-shadow` handle shadow conflicts explicitly.

No linter, no formatter, no build. Standard library only (`tests/test_stdlib_imports.py` rejects third-party imports across authored skill scripts and `lib/`). Python floors: 3.7 (`contract.py`, `handoff.py`), 3.8 (swarm, review, installer), **3.9 binding CI floor** (release validation pins 3.9; never read 3.10+ APIs like `sys.stdlib_module_names` unguarded), and 3.10 host floor (andromeda/chimera). Test against 3.10 (and 3.9 for CI compatibility), not only the newest Python. Scheduler-isolation fixtures replace `PATH` rather than prepending, so host Slurm binaries cannot leak in.

## Structure

`lib/` is the installer: `skill_installer.py` plans and `skill_lifecycle.py` mutates, keeping discovery and writes at the edges so unsafe requests fail before touching any destination. It installs skill directories only (no Paseo, bus, `models.json`, routing state, credentials or connectors); `--uninstall` removes only what an exact copy marker or binding sidecar proves this repo installed.

`lib/skill_agent_policy.py --write|--check` keeps `disable-model-invocation: true` skills (`EXPLICIT_ONLY_SKILLS`) paired with a byte-exact `agents/openai.yaml`; the installer and `bin/doctor` reject a missing or altered installed copy.

`skills/` holds two populations classified by the `hanig-` prefix (`install.sh` and `bin/doctor`): 9 authored bundles (`hanig-project`, `hanig-swarm`, `hanig-verified-workflow`, `hanig-review-gate`, `hanig-portable-handoff`, `hanig-orchestrate`, `hanig-debrief`, `hanig-wandb-logging`, `hanig-hillclimb`) and 8 vendored verbatim bundles (`paseo*`, `pi-fleet`, `agent-bus`, `start-a-sprint`, untouched by `--uninstall`). `hanig-orchestrate` deliberately does NOT restate the mandate grant (`docs/orchestrator-mandate.md` is live authority).

The bus executable is `bin/bus` (`$MULTI_AGENT_SKILLS_CHECKOUT/bin/bus`). Never create `~/.agent-bus/bin/bus` to satisfy vendored prose. `bin/bus` carries one tracked local patch pinned by `tests/test_bus_models.py`.

## Architecture

The pipeline runs one direction: `hanig-project/survey.py` -> interview -> `plan.json` (declaring `inputs`, `outputs`, `kind`, retry exposure) -> `swarm.py validate` / `run` / `advance` -> `report.py` (evidence-assembled verdict from `plan.json`, coordinator state and `receipt.json`, never narrative). `validate` checks declared facts, not shell behavior; use declared `runtime` plus canary/allocation-local preflight for compute-node resolution.

**Nothing judges except `unit.py check`.** `run` and `advance` allocate, submit, bind and act solely on its exit code. Supporting judgment lives in `worktree.py` (code), `verify.py` (authorized verifier) and `converge.py` (metrics gate; unmet criterion -> `NEEDS_HUMAN`, closing no ticket and releasing no dependent). Never move judging into the coordinator.

**Isolation replaces attribution, not execution evidence.** Each attempt gets an exclusive, never-reused write root via `mkdir(exist_ok=False)`. DONE requires declared outputs, the coordinator-pinned per-attempt pre-dispatch artifact basis (absence fails closed; `tests/test_pre_dispatch_artifacts.py`), and kind execution evidence (e.g. attempt-owned terminal-OK `sacct` row for `slurm`). Never omit the basis clause. Exclusivity is a trusted-writer convention (same-UID writers and live descendants are declared limits). Process-observation attribution is forbidden; per-attempt provenance (`worktree.py:24` `judge_artifacts`, pre-dispatch digest, anchored launch intent, inode-bound worktree identity) is required—do not delete it.

**Authority lives in coordinator state, never in agent-writable files.** Per-attempt launch records and receipts are audit-only (`tests/test_record_is_not_authority.py`). `coordinator_paths.py` keeps state and attempt roots outside every operated Git worktree and refuses in-repo paths before creating parents.

**Closure authority is fixed by kind and not configurable.** `code` closes on a merged PR whose head matches the coordinator-judged attempt head; `slurm` and `pipeline` close on a predicate receipt. Approver-named `swarm.py abandon` and `swarm.py supersede` set terminal `ABANDONED`/`SUPERSEDED` dispositions (`SUPERSEDED` closes on the successor's bound merge receipt; neither counts as `DONE` for `needs`). Keep evidence labels exact: tracker ack and merge observation are attested (never verified); an authorized verifier receipt requires base-commit policy, verifier digest and head/claim binding; `REVIEW_PASS` means reviewers failed to refute claims, not proof.

**A retry is a fresh attempt.** `max_attempts` defaults to 1; retries rerun in a fresh empty attempt. `retry.mode: "resume"` is refused unless a checkpoint survives failure, carries an atomic marker, is validated before reuse, skips completed work and yields complete outputs. Bounded `continuation` nudges stay inside their attempt and are neither a retry nor a unit state.

**One coordinator node per plan.** Exclusion is an advisory `flock` on the state dir (kernel-freed on process death; NEVER add a heartbeat or TTL). Cross-node exclusion is uncertified; output claims arbitrate only within one run root; unknown scheduler liveness never releases a claim.

**The swarm coordinator has no network imports.** `swarm.py`, `tickets.py` and `drain_contract.py` import neither `review.py`/`committee.py` nor `linear_api`/`linear_sync.py`. Tracker work flows through an idempotent outbox drained by an authorized session via `linear_sync.py` (`drain`, `file --draft`, `replay`, `issue new|edit|replay`); `tracker_audit.py` works offline. Note: `issue new`/`edit` confirm by exact rendered-description read-back, so Markdown constructs Linear re-renders (bare URLs, `- ` bullets, blank lines before trailers) read back `INCOMPLETE` and block `issue replay` (L1 digest redesign closed by owner 2026-10-11). An unacknowledged intent means no confirmation either way (never "unfiled"); false acknowledgment is strictly worse than missing. `child_environment.py` strips 23 exact env names (no suffix matching); Paseo's daemon keys and `$HOME` pass through as recorded accepted residue.

**Outward-facing actions are gated, each once.** Plan filing requires one explicit approval showing project + all issue titles; `promote` requires `--approver NAME`; literal `swarm autopilot` is the only whole-run bypass. An approval without a named approver is not an approval. Under a confirmed `docs/orchestrator-mandate.md`, the orchestrator already holds the grant to file, update and close issues in the project—do not re-ask per issue.

`needs`, `inputs`, `outputs`, `sbatch` and `requires_verification` must be JSON lists; `validate` raises `PlanError` on strings. Preserve this refusal in every new reader.

Authored skills locate programs via `HANIG_<SKILL>_DIR` (directory of the loaded `SKILL.md`) and `skill_paths.py`, with `HANIG_SKILL_DEP_ROOTS` (relative to the loaded skill's logical dir, never cwd) for mixed/link installs; never search agent stores. Canonical resolver: `skills/hanig-orchestrate/scripts/skill_paths.py`—edit there and copy byte-for-byte to every `skills/hanig-*/scripts/skill_paths.py` (`tests/test_skill_paths.py`).

## Editing rules this repo paid for

Do not report a change complete or assert code works until `hanig-review-gate` passes (exit 0); exit 2 (`REVIEW_UNAVAILABLE`) or 3 (`REVIEW_PARTIAL`) is unreviewed. Provider keys live in `~/.zshrc` (invoke via `zsh -ic` or export explicitly). Reproduce every finding before acting. Repeatable `--author PROVIDER/MODEL` excludes declared authors in `review.py`/`committee.py` by canonical maker and exact model leaf; profile-selected reviews/committees substitute ordered reserves from `reviewers.json` (`--only`, `--escalate`, `--member` skip reserve substitution); split tiebreaks exclude authors/prior voters and fall back to an eligible different-maker reserve or `OWNER`. `--escalate` walks `fast` -> `standard` -> `deep`, stopping on a failing tier with quorum.

Never edit vendored skill docs: put corrections in `docs/upstream-*.md` plus a pinning test (like `bin/bus`), and routing in `~/.paseo/orchestration-preferences.json` (`examples/`). `tests/test_skill_capabilities.py::test_vendored_payload_matches_offline_manifest` pins all 8 vendored bundles + `bin/bus` against `docs/upstream-manifest.json` (plus optional `origin/main` check).

An invariant written in prose is not an invariant. Run any test a docstring claims enforces a rule before trusting it (`docs/audit-protocol-enforcement.md` catalogues actual enforcement).

**Test a guard through the thing that consumes it.** Emitted output is not delivery (e.g. stdout hooks that only reach transcripts). Exercise every guard through its real harness, inspect what the consumer receives, and verify mutating delivery fails the test (unenforced generally).

**Bind a mutation to its intended repository and branch before making it.** Verify repo and branch before editing and push an explicit source SHA to an explicit destination ref rather than trusting ambient state (unenforced locally; branch protection on `main` is the repository-level closure).

**Report status with the time observed, and never read activity as progress.** Running, progressing, permission-blocked and last-observed are four distinct facts; a watcher must track work evidence and permission events separately from liveness, and never present a stale observation as current (unenforced; ARC-691).

**Preserve a blocked attempt's work before any cleanup path runs.** Cleanup depends on a completed, restore-checked snapshot carrying base identity, content digest and validation result; failed preservation retains work in place and neither resumes nor verifies it. Enforced for coordinator cleanup by `tests/test_arc682_recovery.py` (with bounded legacy migration).

**Never normalize a value you are about to decide with.** Truncating, resolving, flattening or case-folding is for human display after the decision, never before (e.g. bounded ref comparison, flattened offsets, resolved `.git` pointer classification). Partly enforced in `worktree.py` via `tests/test_attempt_authority.py` (`RENDERED_REFUSAL_FUNCTIONS`).

Check every fix by mutation: revert the fix and the test must fail. Sweep mechanically for siblings instead of fixing only the visible instance. Correct persisted state as well as the forward path.

Keep `if __name__ == "__main__"` at the bottom of every test file (a misplaced block once hid later classes while the suite stayed green). A test must never be satisfiable by its own source text.

## Host facts that bite

Cluster plans are not portable: of 35 partitions across the three clusters, almost none repeat, and `--mem` is required on lambda only. Usernames and home paths vary across chimera, lambda and andromeda—never hardcode either. `claude` and `node` sit behind conda prefixes in `.bashrc`, so non-interactive `ssh` misses them; use `bash -lic`. Use `chimera-login` rather than `chimera` (`RemoteCommand sh_dev` rejects passed commands). Lambda's `/tmp` is not writable; stage to `$HOME`. Git is 2.34+ on clusters and 2.23 on Mac—avoid `git init -b`, `git switch` and GNU-only `sed`/`readlink` flags. Org-managed skills do not reach clusters; skills must be self-sufficient. See `docs/clusters.md` and `docs/probes/`.
