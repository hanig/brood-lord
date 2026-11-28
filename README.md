# brood-lord

A portable, standard-library-only skill suite for multi-agent research
engineering: plan work, dispatch coding agents and batch jobs, and refuse to
call anything done without evidence that something other than the worker
checked.

It ships ten authored `brood-*` skills plus eight vendored compatibility
skills. Every authored script runs on the Python standard library alone (no
third-party packages, no build step), and one installer puts the skills where
Claude Code, Codex, OpenCode, Pi and other local agents load them.

Browse the [documentation index](docs/README.md) for every document's status,
date and purpose. [`CLAUDE.md`](CLAUDE.md) holds the contributor rules and the
live suite floor.

This repository is private by owner decision (2026-11-05). It was public from
2026-09-24 until then, so treat everything pushed before that as published.
Never commit credentials, tokens or secrets, and never put cluster detail in
pull-request titles, bodies or comments.

---

## The one idea

A scheduler reporting `COMPLETED` is not evidence that work was produced.
Neither is a zero exit code, a "done" line in a log, or an agent saying it
finished. Each of those looks like success to every tool that reports it.

So every unit of work declares an **artifact contract** before it runs: what
"done" means, checked afterwards by something that did not do the work. An
agent's report of completion is a self-assertion, and these skills exist to
refuse self-assertions. Coordination without adjudication only moves the
unverified claim one layer up.

---

## Quick start

```bash
git clone https://github.com/hanig/brood-lord.git
cd brood-lord
./install.sh --dry-run --json      # inspect automatic selection; changes nothing
./install.sh --agent claude --agent codex --agent opencode --agent pi \
  --allow-org-shadow
./bin/doctor --json                # installed skills, provenance, health
```

Then, in an empty directory or a half-finished repository, ask your agent:

```
Use the brood-project skill to start a project here.
```

`brood-project` is the front door. It surveys the host, interviews you only
about what inspection cannot answer, writes `plan.json`, asks which coding
agent should run code units, shows the project overview and every issue title
for one approval, files them in Linear, and dispatches.

---

## The ten skills we wrote

| Skill | Purpose | Entry scripts |
|---|---|---|
| `brood-project` | Front door: survey, interview, plan, approve, file, dispatch, report. | `survey.py`, `tickets.py`, `linear_sync.py`, `report.py` |
| `brood-orchestrate` | Authorized operating loop: delegate, watch, adjudicate, merge, reconcile, report, hand off. | `merge_unit.py`, `watch_pr.py`, `flake_triage.py`, `wait_event.py`, `hourly_facts.py` |
| `brood-swarm` | Coordinator: validate a DAG, isolate each attempt, dispatch, advance, judge. | `swarm.py`, `unit.py`, `converge.py` |
| `brood-review-gate` | Adversarial multi-model review of code and of the claims made about it. | `review.py`, `committee.py`, `resolve_models.py` |
| `brood-conclave` | One frontier model per family deliberates to a ratified conclusion. | `conclave.py`, `allow.py` |
| `brood-verified-workflow` | Declare and verify what "done" means for one batch job. | `contract.py` |
| `brood-hillclimb` | Hill-climb a metric by pre-registered hypotheses and a paired A/B noise screen. | `hillclimb.py` |
| `brood-portable-handoff` | Capture and resume run state across machines and agents. | `handoff.py` |
| `brood-wandb-logging` | Make W&B runs readable; contracts, not W&B, judge outcomes. | `wbkit.py`, `audit_project.py` |
| `brood-debrief` | Collect project lessons, draft an owner-approved scrubbed packet, triage and file it. | `debrief.py`, `decisions_log.py`, `debrief_audit.py`, `debrief_attention.py` |

All ten are model-invocable: an agent may load one from a plain-language
request, and each is also available by name as a slash command (for example
`/brood-project`). No authored skill carries `disable-model-invocation`, and
none ships `agents/openai.yaml`. `lib/skill_agent_policy.py` still enforces
the pairing (flag and byte-exact `agents/openai.yaml` together, or neither)
for any skill that opts out later; the installer and `bin/doctor` apply the
same rule to installed copies.

The `brood-` prefix is load-bearing. `install.sh` and `bin/doctor` read it as
the authorship namespace; everything else under `skills/` is vendored and
**left alone by `--uninstall`**.

---

## How a run works

The pipeline runs one direction:

1. **Survey.** `survey.py` records host, Python, schedulers, partitions,
   accounts, free disk and repository facts. Every fact in it is a question
   the interview must not ask.
2. **Interview and plan.** The interview ends when the plan can run, not when
   the questions run out. `plan.json` declares each unit's `kind` (`code`,
   `slurm`, `pipeline`), `needs`, `inputs`, `outputs` and retry exposure, plus
   the project's `code_agent`.
3. **Approve and file.** `tickets.py` drafts the Linear project and issues and
   requires one named approval before anything is transmitted;
   `linear_sync.py file` sends it.
4. **Dispatch and advance.** `swarm.py validate`, `run` and `advance`
   allocate an exclusive attempt root per unit, dispatch, and act only on the
   exit code of `unit.py check`.
5. **Review and merge.** The orchestrator gates each code attempt through
   `review.py`, merges through `merge_unit.py` after integration verification
   and green CI, and reconciles the tracker.
6. **Report.** `report.py` assembles the verdict from `plan.json`,
   coordinator state and receipts, never from narrative.

---

## Architecture

### Coding-agent backends (`brood-swarm`)

Code units run daemonless. Each attempt gets a coordinator-owned, verified Git
worktree, whichever backend runs in it:

- **Lor subagents.** `provider: "lor/<canonical_model_id>"` runs the unit as a
  native subagent of the coordinator's own Lor session. It needs
  `LOR_SESSION_ID` and `lor` on `PATH`; otherwise dispatch is refused before
  any attempt exists.
- **`claude` CLI.** Headless `claude -p` turns on the subscription login
  (Claude Enterprise OAuth). API keys and `LOR_*` variables are withheld.
- **`codex` CLI.** Headless `codex exec` turns on the ChatGPT subscription
  login, with the same withholding.
- **`pi` CLI, opt-in.** For OpenRouter models Lor does not serve. The key
  reaches the turn only through a `0600` file the wrapper reads and unlinks
  before exec; dispatch fails closed when the key is unset, output passes
  through a redacting pump, and each turn gets its own `--session-dir` inside
  the attempt run directory.

On a Slurm login or agent host, `claude`, `codex` and `pi` turns are submitted
with `sbatch --export=NONE` (`HANIG_SWARM_CLI_SLURM=auto|on|off`); a refused
submission fails closed, and heavy CLIs never run on the login node. Each turn
writes an exit receipt; a zero exit is lifecycle, not completion.

The project chooses its agent at start through top-level `code_agent` in
`plan.json`. `swarm.py set-agent` switches the agent mid-project for attempts
not yet created, per project or per unit, in a revisioned
`STATE_DIR/agent-overrides.json`. `plan.json` and its digest never change, and
each attempt freezes its backend, provider, effort and mode in its launch
intent.

Paseo is retired from the authored skills. `validate` and `set-agent` refuse
`backend: "paseo"`, and nothing authored needs a `paseo` binary when
`code_agent` names one of the four backends. Plans that name no agent keep the
legacy default route, and historical `backend="paseo"` attempts and overrides
stay readable in `status` and `set-agent --show` with `paseo` absent.

### Adversarial review gate (`brood-review-gate`)

The orchestrator runs `review.py` and `committee.py` from its own Lor session;
a worker never gates its own diff. Reviewers are prompted to refute claims,
and any refuted claim fails the gate. `REVIEW_PASS` means the named panel
failed to refute the claims; it is not proof.

- **Lor seats run in-process** and fail closed without `LOR_SESSION_ID`
  rather than falling back to a paid API. Only an explicit
  `HANIG_REVIEW_NO_LOR=1` routes pinned API seats to their API.
- **Default Lor seat.** When `lor model list` advertises `fern-0.1`, the
  `plan`, `standard` and `deep` profiles seat it at `xhigh` in place of
  `kimi-k3`; otherwise `kimi-k3` keeps its seat. `--only kimi-k3` selects it
  explicitly.
- **CLI seats offload.** On a Slurm login or agent host, `claude` and `codex`
  seats run as `sbatch --export=NONE` jobs, choosing `<default>_batch` when
  the default partition is interactive. The login node never spawns either
  CLI.
- **Receipts.** Every result records provider, model and route, and each
  report carries per-seat `seat_routes`. The default profiles spend nothing on
  OpenRouter or OpenAI APIs.

Profiles, effort, reserves and tiebreak routing live in
[`reviewers.json`](skills/brood-review-gate/reviewers.json); read routing
there, not from prose. `--escalate` walks `fast`, `standard`, `deep`. Pass
every author with repeatable `--author PROVIDER/MODEL`; the gate excludes the
same canonical maker and exact model leaf across transports. The round ledger
enforces the round number per change identity; the orchestrator tracks the
three-round ceiling per plan unit.

`review.py`'s `--record-evidence` (L1; `review_evidence.py` is a module, no CLI) appends audit-only counterexample
evidence (`--head`, `--finding`, `--round`, `--method`, `--status`, optional
`--tree`, `--test-ref`, `--note`) bound to identity, head and candidate tree; it
never changes a verdict. `--require-premise-ack` (L2; module `review_premise.py`)
refuses round N >= 2 when one `defect_class` struck in rounds N-2 and N-1,
unless `--premise-memo FILE` or `--continue-patching REASON` acknowledges it.

`committee.py` keeps a persistent two- or three-member committee that plans
and then reviews its own plan's implementation, with the same author
exclusion and a tiebreak that falls back to `OWNER`.
[`PROTOCOL.md`](skills/brood-review-gate/PROTOCOL.md) states the full model.

### Deliberation (`brood-conclave`)

The conclave seats the top model from each of four families (GPT, Gemini,
Claude, Muse) as Lor subagents by default. Round 0 is blind, with web search
on. Later rounds cross-examine anonymized posts on the cruxes the moderator
lists, one seat per round plays devil's advocate, and a ratification vote
ends the panel. The moderator runs the protocol and never votes.

### Authority and mandates (`brood-orchestrate`)

Authority is owner-granted by merge, not by conversation. A session driving a
run reads [`docs/orchestrator-mandate.md`](docs/orchestrator-mandate.md),
shows the owner its enumerated grant and bounds, and gets confirmation before
operating unattended. The owner chooses the mandate's expiry: a UTC time with
no ceiling, or none, lasting until revoked. Research mode is the default
review mode: one review round per unit by default, with workers repairing
reproduced findings on the mandate's hard-block list. SWE mode applies only
when the owner selects it.

### Execution truth

- **Nothing judges except `unit.py check`.** `run` and `advance` act solely
  on its exit code; supporting judgment lives in `worktree.py`, `verify.py`
  and `converge.py`.
- **Isolation replaces attribution.** Each attempt gets an exclusive,
  never-reused write root. `DONE` needs declared outputs, the pinned
  pre-dispatch artifact basis, and kind execution evidence, such as an
  attempt-owned terminal `sacct` row for `slurm` units.
  `brood-verified-workflow` falls back to `scontrol show job` when `sacct`
  returns nothing.
- **Authority lives in coordinator state**, kept outside every operated
  worktree and repository, never inside them. Worktrees isolate paths, not
  Unix principals: a code agent running as the same user still has OS write
  permission on coordinator state files. One coordinator holds a plan at a time,
  through an advisory `flock` the kernel frees on process death.
- **Closure is fixed by kind.** `code` closes on a merged PR whose head
  matches the judged attempt head; `slurm` and `pipeline` close on a predicate
  receipt. A retry is always a fresh, empty attempt.
- **Merge needs verification.** `merge_unit.py` requires passing integration
  evidence for the exact judged head and observed target commit, plus green
  CI. There is no integration override.
- **Outward actions are gated once.** Filing needs one named approval;
  `promote` needs `--approver`. The coordinator imports no network module:
  tracker work flows through an idempotent outbox that an authorized session
  drains with `linear_sync.py`.

### Credential boundary

**`models.json` is routing metadata, not a credential grant.** A model listed
there can be considered for dispatch; that does not let a worker call it.
`OPENAI_API_KEY` and `OPENROUTER_API_KEY` stay coordinator-side: review and
committee programs use them, and `child_environment.py` strips both exact
names, with the rest of its denylist, from every coordinator child. The `pi`
backend's key-file handoff above is the one designed exception.

---

## CLI reference

Each skill finds its programs through `BROOD_<SKILL>_DIR` (falling back to
`HANIG_<SKILL>_DIR`), set to the directory of the loaded `SKILL.md`. Paths
below are relative to that directory.

### brood-project

```sh
python3 scripts/survey.py --repo . --out .swarm/survey.json
python3 scripts/tickets.py draft   plan.json --team TEAM --out .swarm/tickets.json
python3 scripts/tickets.py check   plan.json .swarm/tickets.json
python3 scripts/tickets.py approve .swarm/tickets.json --approver NAME
python3 scripts/report.py  .          --out report.html      # ends every run
python3 scripts/report.py  . --state-dir "$STATE" --root "$RUNS" --out report.html
```

Tracker filing, draining and ad-hoc issues:

```sh
python3 "$HANIG_PROJECT_DIR/scripts/tickets.py" draft plan.json --repository OWNER/REPO
python3 "$HANIG_PROJECT_DIR/scripts/tickets.py" approve tickets.json --approver NAME
python3 "$HANIG_PROJECT_DIR/scripts/linear_sync.py" file --draft tickets.json [--preview] [--adopt-checked]
python3 "$HANIG_PROJECT_DIR/scripts/linear_sync.py" replay OPERATION_ID --draft tickets.json
python3 "$HANIG_PROJECT_DIR/scripts/linear_sync.py" drain \
  --binding .hanig/linear-binding.json --state-dir STATE [--state-dir OTHER_STATE] [--dry-run] [--full]
python3 "$HANIG_PROJECT_DIR/scripts/linear_sync.py" audit \
  --binding .hanig/linear-binding.json --out AUDIT
python3 "$HANIG_PROJECT_DIR/scripts/linear_sync.py" issue new \
  --binding .hanig/linear-binding.json --title 'Follow-up' --body-file body.md \
  --blocked-by ARC-123 --approver NAME [--preview]
python3 "$HANIG_PROJECT_DIR/scripts/linear_sync.py" issue edit ARC-124 \
  --binding .hanig/linear-binding.json --add-blocked-by ARC-125 \
  --remove-blocked-by ARC-123 --approver NAME
python3 "$HANIG_PROJECT_DIR/scripts/linear_sync.py" issue replay OPERATION_ID \
  --binding .hanig/linear-binding.json
```

The draft carries the plan's DAG as `blockedBy` and previews edges to remove,
decided from a tracker read-back rather than the previous draft. An
unacknowledged intent means no confirmation either way.

### brood-orchestrate

```sh
python3 "$HANIG_ORCHESTRATE_DIR/scripts/merge_unit.py" plan.json \
  --state-dir "$STATE" --unit impl --pr 123 --approver "Operator" \
  --verify-integration --verification-timeout 1800
python3 "$HANIG_ORCHESTRATE_DIR/scripts/flake_triage.py" --repo "$REPO" \
  --head "$HEAD" --base "$BASE" --test tests.test_example.TestCase.test_method \
  --python python3 --pr 123 --json
```

`--verify-integration` runs no merge: it runs the target's pinned verifiers in
a disposable candidate-merge checkout and records the result in the
coordinator's verification journal. The plain invocation then admits that
evidence and issues one guarded merge. An optional
`STATE_DIR/verification-execution.json` sends verification to an
owner-authorized host, directly or through a Slurm allocation.

`watch_pr.py --repo OWNER/REPO --pr 123 [--once] [--json] [--patch-id] [--interval S] [--max-polls N]`
is the read-only PR-status oracle: exit 0 ready, 10 pending, 11 conflicts, 12 threads, 13 CI failed, 14 cancelled
zero-step flake, 15 unknown, 16 fork, 2 error. It mutates nothing.

### brood-swarm

```sh
python3 scripts/swarm.py schema                      # every unit field
python3 scripts/swarm.py validate plan.json [--check-seeds]
python3 scripts/swarm.py run      plan.json [--dry-run]
python3 scripts/swarm.py advance  plan.json
python3 scripts/swarm.py status   plan.json [--json]
python3 scripts/swarm.py set-agent plan.json --state-dir DIR --backend codex --model MODEL [--unit ID]
python3 scripts/swarm.py set-agent plan.json --state-dir DIR --show [--json]
python3 scripts/swarm.py scope-check plan.json --state-dir DIR --unit ID [--head REV] [--json]
python3 scripts/swarm.py abandon plan.json --state-dir DIR --unit ID \
        --approver NAME --reason TEXT
python3 scripts/swarm.py supersede plan.json --state-dir DIR --unit ID --by SUCCESSOR \
        --approver NAME --reason TEXT
python3 scripts/swarm.py promote  plan.json --unit ID --approve --approver NAME
python3 scripts/swarm.py outbox            [--all] [--json]
python3 scripts/swarm.py usage [--unit ID] [--since YYYY-MM-DD] [--tz UTC] [--json]

python3 scripts/unit.py allocate --root /external/swarm-runs --task ID --kind slurm \
                                 --command CMD --output PATH [--gpu-hours N]
python3 scripts/unit.py bind  <unit_dir> --job-id 12345
python3 scripts/unit.py check <unit_dir> [--json]
```

`swarm.py continue PLAN --state-dir DIR --unit OLD --new-id NEW --prompt-file FILE [--head-ref REF] [--repoint-dependents] [--tickets DRAFT] [--write]`
copies OLD with a new id, prompt, description, author history and seed from
coordinator state and live Git. Preview leaves `plan.json` and coordinator
state unchanged, though Git may still update fetch metadata such as
`FETCH_HEAD` when resolving remote refs; `--write` appends
atomically. Omit either flag when its update is not needed. Use
`swarm.py continue --repoint-dependents --tickets DRAFT` with the required
arguments above to prepare both updates: repointing replaces OLD with NEW in
dependent `needs` and refuses any dependent with recorded attempts, and the
existing DRAFT gets a copied entry without filing tracker issues.

`converge.py check` answers whether a training run converged or merely
stopped; a unit that spent its budget without meeting its criterion becomes
`NEEDS_HUMAN`, closing no ticket and releasing no dependent. Unit exit codes:

```
DONE 0   RUNNING 1   FAILED 2   PREEMPTED 3   INCOMPLETE 4   NEEDS_HUMAN 5
USAGE_ERROR 64
```

### brood-verified-workflow

```sh
python3 scripts/contract.py init   --command CMD --output PATH [--input PATH] \
                                   [--predicate EXPR] [--write-scope DIR] \
                                   [--require-production-evidence]
python3 scripts/contract.py submit <run_dir> [--sbatch-arg ARG]
python3 scripts/contract.py record <run_dir> --exit-code N [--job-id ID]
python3 scripts/contract.py check  <run_dir> [--json] [--watchdog SECS]
```

### brood-review-gate

```sh
python3 scripts/review.py --kind plan --file design.md
python3 scripts/review.py --kind implementation --staged --escalate --round 1 \
  --claim "This change cannot make an honest run fail."
python3 scripts/review.py --list          # live provider probe
python3 scripts/review.py --size --range BASE..HEAD  # offline PR size preflight
python3 scripts/resolve_models.py --project .            # adopt point releases per project
python3 scripts/resolve_models.py --project . --dry-run  # list and select only
```

Exit 0 is `REVIEW_PASS`; 2 (`REVIEW_UNAVAILABLE`) and 3 (`REVIEW_PARTIAL`)
mean unreviewed, and 7 (`REVIEW_CLAIMS_REFUTED`) is not a pass.

### brood-conclave

```sh
python3 scripts/conclave.py resolve
python3 scripts/conclave.py doctor --refresh
python3 scripts/conclave.py new BOARD --brief brief.md [--attach fig.png] [--no-lor]
python3 scripts/conclave.py round BOARD 0 [--seats S] [--dry-run]
python3 scripts/conclave.py ratify BOARD
```

### brood-portable-handoff, brood-debrief and brood-hillclimb

```sh
python3 scripts/handoff.py capture run1 run2 --out handoff.json
python3 scripts/handoff.py resume handoff.json [--base DIR] [--watchdog SECS]
python3 scripts/handoff.py memory .
python3 scripts/decisions_log.py append --log L --run-id R --kind K --claim C [--evidence-ref E]
python3 scripts/decisions_log.py verify --log L [--expect-head SHA256]
python3 scripts/debrief_audit.py --decisions L --workspace DIR
python3 scripts/debrief_attention.py pack --decisions L --audit A [--out F]
python3 scripts/debrief_attention.py validate --report F [--pack P]
python3 scripts/hillclimb.py init --state-dir S --harness F --metric M --min-effect X --direction lower
python3 scripts/hillclimb.py record --state-dir S --id H --runs-json R --checklist-json C
```

Debrief drafting is offline; only `debrief_file.py` reaches the network, after
one named approval of the exact packet.

---

## Install

```bash
./install.sh [--agent NAME ... | --exclude-agent NAME ... | --prefix DIR]
             [--mode copy|link] [--only NAME] [--dry-run] [--json]
             [--force] [--uninstall] [--allow-org-shadow]
             [--allow-vendored-shadow] [--include-vendored]
```

- **Targets.** With no selector, the installer plans every detected agent's
  preferred skill root. `--agent` (repeatable) picks an explicit set and is
  the bootstrap route for an absent CLI; `--prefix` is the legacy single-root
  spelling. `--only` limits the install to named skills.
- **Copy by default.** `--mode link` is for skill development only; a link
  into a live checkout changes under every branch switch.
- **Validate before writing.** Frontmatter, skill identity, script syntax and
  the invocation policy are checked before any destination is touched.
  Org-managed name collisions abort unless `--allow-org-shadow` is passed;
  copies of authored skills also arrive through the org catalog, so pass it
  deliberately.
- **Ownership.** `--uninstall` removes only what an exact copy marker or a
  binding sidecar proves this repository installed.
- **Certification.** Uncertified or stale agent versions are planned as
  `unverified`, with warnings on stderr even with `--json`. Selection is not
  a compatibility pass; see
  [`docs/cross-agent-acceptance.md`](docs/cross-agent-acceptance.md) and
  [`docs/agent-migration.md`](docs/agent-migration.md).

Run `--dry-run --json` before a broad install: it reports each target,
physical root, verification state, shared consumers, actions and conflicts.
`./install.sh --dry-run --migrate-from DIR` previews a migration read-only.

### The brood rename and backward compatibility

- **Settings.** Every `HANIG_<X>` setting the authored scripts read also has
  a `BROOD_<X>` spelling. A non-blank `BROOD_<X>` wins; otherwise `HANIG_<X>`
  applies as before. `BROOD_SKILL_DEP_ROOTS` parents are searched before
  `HANIG_SKILL_DEP_ROOTS` parents.
- **Legacy installs.** After publishing `brood-<suffix>`, the installer
  retires a sibling `hanig-<suffix>` only when it carries this repository's
  exact recorded ownership. An unowned sibling stays untouched with a warning,
  and `--dry-run` reports `would-retire-legacy`.
- **Runtime state keeps its names** so live projects continue without
  migration: `~/.local/state/hanig-swarm`, `~/.local/state/hanig-review-gate`,
  `~/.hanig-committee`, `~/.local/state/hanig-conclave` and `.hanig/`, plus
  Git refs under `refs/hanig-swarm*` and the `hanig-rev-` Slurm job prefix.

Relative entries in `HANIG_SKILL_DEP_ROOTS` (or `BROOD_SKILL_DEP_ROOTS`) are
anchored to the loaded skill directory, never to the project's working
directory.

### Diagnostics

`./bin/doctor --json` reports what is installed, from where, whether each
script still compiles, whether installed copies still satisfy the invocation
policy, and per-agent readiness. For the full agent document, run
`python3 skills/brood-project/scripts/agent_diagnostics.py --json`.
`./bin/probe.sh` is a read-only environment probe for a new host.

---

## Vendored skills

`skills/` also carries `paseo`, `paseo-advisor`, `paseo-committee`,
`paseo-handoff`, `paseo-loop`, `pi-fleet`, `start-a-sprint` and `agent-bus`,
copied **verbatim** from upstream. Authored workflows never edit them, so an
upstream re-sync stays an honest diff. Corrections go in `docs/upstream-*.md`
with a pinning test, and
`tests/test_skill_capabilities.py` pins every vendored file against
`docs/upstream-manifest.json`. The authored skills do not depend on them at
runtime. `bin/bus`, `bin/agent-manager`, `bin/agent-view` and `models.json`
came across with them; `bin/bus` carries one tracked local correction,
described in `docs/upstream-agent-bus-model-registry.md` and pinned by
`tests/test_bus_models.py`.

### Agent bus executable, in this checkout

The executable, model registry, and runtime state are three separate things.
`install.sh` installs skill directories only. With no selector it plans the
preferred physical roots of all eligible detected agents; `--agent` chooses an
explicit set and `--prefix` chooses one compatibility root. It deploys none of
those three runtime artifacts.

#### Executable discovery

In this checkout the executable is `bin/bus`. Set
`MULTI_AGENT_SKILLS_CHECKOUT` to the checkout's absolute path and use
`$MULTI_AGENT_SKILLS_CHECKOUT/bin/bus` from elsewhere. The vendored skills'
fixed `~/.agent-bus/bin/bus` path belongs to upstream's install layout, not to
this repository's installer.

#### Model-routing registry input

`bus models` reads `models.json` from `AGENT_BUS_HOME`; it does not read the
repository-root copy directly. The root `models.json` is a starter input to
copy into an explicitly chosen bus state directory and review before relying
on its routing data. It is not an executable-discovery mechanism.

#### Runtime state and cache ownership

Every bus invocation creates `sessions`, `inbox`, `cursors`, and `cache` under
`AGENT_BUS_HOME`. Live model-routing signals are cached there too. Therefore a
one-off check should use disposable state, while a real messaging session
should use a deliberately chosen durable state directory. Neither case should
write runtime state into this checkout merely because the executable lives
here.

#### Disposable model-routing check

After exporting `MULTI_AGENT_SKILLS_CHECKOUT` as the absolute checkout path,
set `AGENT_BUS_SCRATCH` to an existing writable directory whose resolved path
is outside both the checkout and `$HOME`. The block checks that precondition,
then can run from any other directory. Its error-exit subshell makes every
setup failure abort without changing the caller's shell options. It copies the
shipped registry only into a private temporary state directory. A disposable
`HOME` inside that same directory contains any side effects from live-signal
collectors that `models` invokes. The EXIT and signal traps remove the printed
directory on success, failure, or interruption, and refuse to delete anything
except the temporary child they allocated.

```bash
(
set -eu
checkout="$(CDPATH= cd -- "${MULTI_AGENT_SKILLS_CHECKOUT:?set the checkout path}" && pwd -P)"
home_dir="$(CDPATH= cd -- "$HOME" && pwd -P)"
scratch="$(CDPATH= cd -- "${AGENT_BUS_SCRATCH:?set external scratch}" && pwd -P)"
test -w "$scratch"
case "$scratch" in
  /|"$home_dir"|"$home_dir"/*|"$checkout"|"$checkout"/*)
    echo "AGENT_BUS_SCRATCH must resolve outside HOME and the checkout" >&2
    exit 2
    ;;
esac
bus_bin="$checkout/bin/bus"
test -x "$bus_bin"
bus_state=""
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ -n "$bus_state" ]; then
    case "$bus_state" in
      "$scratch"/agent-bus.*)
        rm -rf "$bus_state" || status=1
        ;;
      *)
        echo "refusing to clean unexpected path: $bus_state" >&2
        status=1
        ;;
    esac
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
bus_state="$(mktemp -d "$scratch/agent-bus.XXXXXX")"
printf 'disposable AGENT_BUS_HOME=%s\n' "$bus_state" >&2
mkdir "$bus_state/home"
cp "$checkout/models.json" "$bus_state/models.json"
chmod 600 "$bus_state/models.json"
HOME="$bus_state/home" AGENT_BUS_HOME="$bus_state" "$bus_bin" models --json
)
```

The `~/.agent-bus/bin/bus` commands in the vendored `agent-bus`, `paseo`,
`pi-fleet`, and `start-a-sprint` skills assume upstream's installer has placed
the executable there. They do not describe a path this repository installs.
Until upstream discovers the executable instead of hardcoding an install
layout, agents using only this repository need the checkout-local path above.
Do not create `~/.agent-bus/bin/bus` merely to make the instruction true.
The upstream-ready report is in `docs/upstream-agent-bus-path-discovery.md`.

### Orchestration preferences for the vendored Paseo skills

The vendored `paseo*` and `pi-fleet` skills refuse to choose a provider until
they read `~/.paseo/orchestration-preferences.json`. To use them, copy the
example into place on the machine that runs agents; `install.sh` never
deploys it:

```bash
mkdir -p ~/.paseo && cp examples/orchestration-preferences.json ~/.paseo/
```

The file holds provider strings only and cannot set reasoning effort. A
well-formed string is not evidence that an agent comes up on the intended
model: launch through `bus launch-worker`, which stops a mismatched agent, or
check a running one with
`bus await --expect-provider/--expect-model/--expect-thinking`, which exits 4
on a mismatch.

---

## Tests

```bash
python3 scripts/run_tests.py                     # full suite in parallel, one process per module
python3 scripts/run_tests.py --fast              # inner loop; verify with the full run
python3 -m unittest discover -s tests            # full suite, serial
python3 tests/native_agent_validation.py         # release-time host harness, not a unit test
```

Standard library only; no network or cluster required. `CLAUDE.md` declares
the suite floor and the Python floors (3.9 is the binding CI floor).
`tests.test_review` always runs its whole module through a supervised worker,
so `-k` does not narrow it. Keep each test file's `__main__` guard at the
bottom, and never let a test be satisfiable by its own source text.

---

## Layout

```
install.sh                  copy-based installer (--mode link for development)
bin/doctor                  what is installed, from where, and does it still run
bin/probe.sh                read-only environment probe for a new host
bin/bus                     vendored agent bus, one tracked local correction
skills/brood-project/       survey.py, tickets.py, report.py, Linear sync and diagnostics
skills/brood-orchestrate/   merge_unit.py, watch_pr.py, flake_triage.py, wait_event.py
skills/brood-swarm/         swarm.py, unit.py, converge.py, cli_adapters.py, agents.json
skills/brood-review-gate/   review.py, committee.py, reviewers.json, PROTOCOL.md
skills/brood-conclave/      conclave.py, allow.py, families.json, templates/
skills/brood-verified-workflow/  contract.py
skills/brood-hillclimb/     hillclimb.py
skills/brood-portable-handoff/   handoff.py
skills/brood-wandb-logging/      wbkit.py, audit_project.py
skills/brood-debrief/       debrief.py, decisions_log.py, debrief_audit.py, debrief_attention.py, debrief_file.py
lib/skill_installer.py      installer planning; lib/skill_lifecycle.py mutates
lib/skill_agent_policy.py   --check|--write [--json] [--skills-root DIR]: disable-model-invocation pairing
models.json                 model routing metadata (not a credential grant)
examples/                   reference config to copy elsewhere; nothing reads it here
docs/                       plans, mandate, runbooks, upstream notes; index in docs/README.md
tests/                      stdlib unittest
```

Start with [`docs/orchestrator-mandate.md`](docs/orchestrator-mandate.md) to
drive a run, [`docs/plan-field-reports.md`](docs/plan-field-reports.md) for
the live plan, and
[`docs/audit-protocol-enforcement.md`](docs/audit-protocol-enforcement.md) for
which written rules are actually enforced.
