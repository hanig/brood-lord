# Operating loop detail

## Dedicated orchestration environment and startup check

Set `ORCH_PYTHON` and `HANIG_ORCH_PYTHON` to the declared absolute orchestration interpreter (e.g. `/home/USER/.local/state/PROJECT/orch-venv/bin/python`) and invoke every orchestrator, coordinator, and tracker helper through `"$ORCH_PYTHON"`, never bare `python3` from PATH. <!-- declaration: env.dedicated -->

Run the read-only startup check once when opening or taking over a session: <!-- declaration: env.dedicated -->

```bash
export ORCH_PYTHON="/home/USER/.local/state/PROJECT/orch-venv/bin/python"
export HANIG_ORCH_PYTHON="$ORCH_PYTHON"
"$ORCH_PYTHON" "$HANIG_ORCHESTRATE_DIR/scripts/orch_env.py" check --plan plan.json
```

`orch_env.py check` warns on stderr when session `python3` or the running interpreter resolves outside `environments.orchestration` or into a shared env (conda `base`/`workbook`); pass `--strict` to exit 1. Helper scripts (`hourly_facts.py`, `merge_unit.py`, `reconcile.py`, `wait_event.py`, `wait_report.py`) also warn on stderr on mismatch. <!-- declaration: env.dedicated -->

## Coordinator event wakeup

```bash
"$ORCH_PYTHON" "$HANIG_ORCHESTRATE_DIR/scripts/wait_event.py" --state-dir "$STATE"
```

Repeat `--state-dir DIR` for several runs; `--snapshot FILE` defaults to `wait-event-snapshot.json` in the first directory's parent. Snapshots live outside watched directories. The one-shot waiter emits TRANSITION and operator-needed STATE arrivals with local observation times. It observes coordinator state only: it does not detect a stalled or permission-blocked agent, take a coordinator lock, or make network, agent or scheduler calls. <!-- declaration: watch.proof -->

`--once` exits 0 with events, 3 without, or 2 when all sources are unreadable. Otherwise poll with `--interval SECONDS` (default 60); `--timeout SECONDS` exits 3 without events. UNREADABLE diagnostics go to stderr once per source. `--json` emits JSON lines, including an END summary. The atomic snapshot has mode 0600. Delivery is at least once: events go to stdout before the snapshot is persisted. If persistence fails after output, the waiter exits nonzero and warns on stderr that the event may repeat; the next run may deliver it again. Conflicting directory-alias histories in an old snapshot are refused intact; choose a fresh `--snapshot FILE` to rebaseline. Concurrent waiters sharing a snapshot are outside the envelope. <!-- declaration: watch.proof -->

## Scheduled facts and one-shot wake

On wake read, show and summarize; act on ATTENTION (including ready work) under
the mandate, then re-arm one waiter. No model calls or authority are supplied. <!-- declaration: report.scheduled-facts -->

Install hanig-project beside the loaded skill, or set HANIG_SKILL_DEP_ROOTS.
Tracker is offline; STALE remains visible.
State-root children hold plan.json and state/swarm-state.json; link a plan beside state/ if it lives elsewhere.
The --binding and --audit arguments are optional together; omit both when no tracker binding is configured.
Repeat --agent-root DIR to include agent working directories outside --repo and --state-root.
The repeatable `--status-file 'runs/*/STATUS'` adds file progress; ** produces a usage error.
The last STATUS or STATUS: record (optionally timestamped or bulleted) is shown
with its mtime. Exact values in repeatable `--attention-status awaiting_coordinator`
and `--terminal-status done` control attention and liveness; other values are live.
File sources allow omission of --state-root; agent scope then uses --repo
and --agent-root. Reads use at most the last 1 MiB.
Paseo is optional: `--paseo PATH` lists legacy Paseo agents when that
executable resolves, and a host without it gets a plain `paseo not on PATH`
line, not an ATTENTION. A running code unit bound to a direct-CLI run is read
from `STATE_DIR/cli-runs/ATTEMPT/` (`run.json` and the current turn's exit
receipt), without probing a process or the scheduler.
Substitute operator paths in these direct-command scheduler examples.

macOS launchd example (`hourly_facts.py`): bootstrap the edited plist with
launchctl bootstrap gui/$(id -u) FILE. Call the declared absolute orchestration
interpreter, never bare `python3` from PATH. <!-- declaration: env.dedicated -->

```xml
<plist version="1.0"><dict>
  <key>Label</key><string>local.orchestrator.hourly-facts</string>
  <key>EnvironmentVariables</key><dict>
    <key>HANIG_ORCHESTRATE_DIR</key><string>/skills/hanig-orchestrate</string>
    <key>HANIG_ORCH_PYTHON</key><string>/Users/USER/.local/state/PROJECT/orch-venv/bin/python</string>
  </dict>
  <key>ProgramArguments</key><array>
    <string>/Users/USER/.local/state/PROJECT/orch-venv/bin/python</string>
    <string>/skills/hanig-orchestrate/scripts/hourly_facts.py</string>
    <string>--state-root</string><string>/coordinator</string>
    <string>--output-dir</string><string>/reports</string>
    <string>--repo</string><string>/checkout</string>
    <string>--binding</string><string>/binding.json</string>
    <string>--audit</string><string>/audit.json</string>
    <string>--gh</string><string>/bin/gh</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>55</integer></dict>
</dict></plist>
```

Linux cron (`advance` and `hourly_facts.py`, both calling the declared absolute
orchestration interpreter): <!-- declaration: env.dedicated -->

```cron
*/5 * * * * ORCH_PYTHON="/home/USER/.local/state/PROJECT/orch-venv/bin/python" HANIG_ORCH_PYTHON="/home/USER/.local/state/PROJECT/orch-venv/bin/python" "/skills/hanig-swarm/scripts/swarm-cron.sh" "/coordinator/PROJECT"
55 * * * * HANIG_ORCHESTRATE_DIR="/skills/hanig-orchestrate" HANIG_ORCH_PYTHON="/home/USER/.local/state/PROJECT/orch-venv/bin/python" "/home/USER/.local/state/PROJECT/orch-venv/bin/python" "/skills/hanig-orchestrate/scripts/hourly_facts.py" --state-root "/coordinator" --output-dir "/reports" --repo "/checkout" --binding "/binding.json" --audit "/audit.json" --gh "/bin/gh"
```

If any substituted path contains `%`, escape it as `\%` in crontab entries (cron treats unescaped `%` as a command/input split even inside quotes).

macOS launchd (`advance` via `swarm-cron.sh`):

```xml
<plist version="1.0"><dict>
  <key>Label</key><string>local.orchestrator.swarm-advance</string>
  <key>EnvironmentVariables</key><dict>
    <key>ORCH_PYTHON</key><string>/Users/USER/.local/state/PROJECT/orch-venv/bin/python</string>
    <key>HANIG_ORCH_PYTHON</key><string>/Users/USER/.local/state/PROJECT/orch-venv/bin/python</string>
  </dict>
  <key>ProgramArguments</key><array><string>/skills/hanig-swarm/scripts/swarm-cron.sh</string><string>/coordinator/PROJECT</string></array>
  <key>StartInterval</key><integer>300</integer>
</dict></plist>
```

At :55, reports label the next hour and replace hourly files and latest.md atomically.
The next report supersedes missed runs. ATTENTION marks facts, never judgments. <!-- declaration: report.scheduled-facts -->
Source observations use local times; agent liveness does not imply progress. <!-- declaration: report.scheduled-facts -->
Older inactive units are counted. Stopped same-plan author_history predecessors show
continued by their successor, without ATTENTION (amendment 1, 2026-11-09).
SUBMITTED and RUNNING predecessors remain live, with bound-agent checks.

```bash
"$ORCH_PYTHON" "$HANIG_ORCHESTRATE_DIR/scripts/wait_report.py" --output-dir /reports
```

The waiter polls each minute. Quiet reports are persisted in .seen.json; the first
unseen report with live work or ATTENTION wakes it. Preserve .seen.json across restarts.
A host-local lock serializes consumption before printing; a crash then can lose a wake,
superseded by the next report. Malformed records/reports stop: restore valid bytes and
re-arm. The installer deploys no scheduler entries.

## The coordinator pass

Source inspection of `swarm.py` shows that `cmd_advance` returns `cmd_run`, `cmd_run` calls `advance` once under the state lock, and then prints that the coordinator is exiting. `advance` rechecks live units, settles state, dispatches ready units, emits tracker intents, and returns. Repetition comes from the host scheduler. <!-- declaration: loop.advance -->

The session-side cycle adds remote observation, review, adjudication, merge, linear_sync.py tracker operations, and reporting around that offline pass. <!-- declaration: loop.quiescence -->

## Watching and idleness

Liveness, work evidence, permission events, and observation time remain separate fields in a report. <!-- declaration: watch.facts -->

The worker record and permission events provide evidence that directory timestamps and a running label do not. <!-- declaration: watch.source -->

A watcher is armed in the dispatching turn, tested against a condition already present, and its watch set is derived from coordinator state. Arming it later is the same defect as omitting it, because nothing else wakes the session when a worker finishes and `swarm.py status` records neither an observation time nor a unit age. <!-- declaration: watch.proof -->

## Native Lor coding attempts

A `lor` attempt runs as a subagent of the coordinator's own Lor session, so the coordinator starts no detached watcher holding Lor credentials for it. Lor wakes the parent session when the subagent finishes; the next `swarm.py advance` pass judges the attempt from Lor's session record and the coordinator-owned worktree. <!-- declaration: watch.lor-attempts -->

Between passes, wait on coordinator state and Lor together: <!-- declaration: watch.lor-attempts -->

```bash
"$ORCH_PYTHON" "$HANIG_ORCHESTRATE_DIR/scripts/wait_event.py" --state-dir "$STATE" --poll-lor
```

`--poll-lor` also polls every live `lor` attempt in coordinator state through the installed `hanig-swarm` Lor adapter and emits `lor_session_finished` once a session leaves `running`. Exit 4 means Lor rejected the session token: report a Lor authentication error that needs the owner, never "still running" and never the no-event exit 3. A Lor session the coordinator cannot observe stays `INCOMPLETE` rather than `DONE` or `FAILED`, and its worktree is kept while the session may still be running. <!-- declaration: watch.lor-attempts -->

## Switching the coding agent mid-project

The plan's `code_agent` records the project-start choice; `swarm.py set-agent` changes it for attempts not yet dispatched without touching `plan.json` or `plan_digest`. Inspect first, then switch, then confirm: <!-- declaration: code.agent-switch -->

```bash
SWARM="$HANIG_SWARM_DIR/scripts/swarm.py"
"$ORCH_PYTHON" "$SWARM" set-agent plan.json --state-dir "$STATE" --show
"$ORCH_PYTHON" "$SWARM" set-agent plan.json --state-dir "$STATE" --backend lor --model CANONICAL_MODEL_ID --thinking high
"$ORCH_PYTHON" "$SWARM" set-agent plan.json --state-dir "$STATE" --backend claude --model MODEL --thinking high --unit UNIT
"$ORCH_PYTHON" "$SWARM" set-agent plan.json --state-dir "$STATE" --backend codex --model MODEL --thinking xhigh
"$ORCH_PYTHON" "$SWARM" set-agent plan.json --state-dir "$STATE" --backend pi --model PROVIDER/MODEL --unit UNIT
"$ORCH_PYTHON" "$SWARM" set-agent plan.json --state-dir "$STATE" --clear --unit UNIT
```

Each successful switch bumps the revision of `STATE_DIR/agent-overrides.json`, written under the project lock, and prints every code unit's effective agent with its disposition: undispatched units take the new agent on their next attempt, in-flight attempts keep the backend frozen in their launch intent, and terminal units are unchanged. A run can therefore hold attempts on several backends at once, including legacy `paseo` attempts dispatched before the retirement; servicing, judging, continuation, stop, and cleanup each follow the attempt's own frozen backend, so a switch never interrupts work already running. <!-- declaration: code.agent-switch -->

A unit that pins `provider` or `model` in the plan outranks a project-wide switch; move it with `--unit`. A refused switch prints why and writes nothing, for example any Paseo target (`paseo backend is retired`), a Lor target without `LOR_SESSION_ID` and `lor` on PATH, or a direct-CLI target whose binary is not on PATH. On a Slurm login host the coordinator submits each `claude`, `codex` or `pi` turn with `sbatch --export=NONE`; a `lor` worker sends heavy work through `sbatch` itself. <!-- declaration: code.agent-switch -->

## Owner-facing report times

Validate `HANIG_REPORT_TZ` against the host's IANA zone database: `date` can silently accept invalid `TZ`. Use UTC and announce the fallback once per session when unset or invalid. Quotes keep their timestamps; conversions belong outside quotations. <!-- declaration: report.timezone -->

Convert a Unix timestamp using the validated zone; use `TZ=UTC` for the fallback. <!-- declaration: report.timezone -->

```sh
# macOS / BSD date
TZ="$HANIG_REPORT_TZ" date -r 1793102400 '+%Y-%m-%d %H:%M:%S %Z'
# GNU date
TZ="$HANIG_REPORT_TZ" date -d @1793102400 '+%Y-%m-%d %H:%M:%S %Z'
```

## Preservation, dispatch, and merge

Recovery uses a separate ref because moving the coordinator's judged branch destroys the stable comparison route. A restorable snapshot includes base identity, content digest, full porcelain including untracked paths, and a restore check. <!-- declaration: preservation.before-cleanup -->

Dispatch preserves the destination meaning of `target_branch`, the completion protocol, complete output, current source head, and the shared-stash prohibition. <!-- declaration: dispatch.mechanics -->

Merge admission compares the full judged and pull-request heads, reads the diff, waits for terminal green checks, and records any weaker judgment basis. <!-- declaration: merge.requirements -->

## Guarded merge and reconciliation

```bash
"$ORCH_PYTHON" "$HANIG_ORCHESTRATE_DIR/scripts/merge_unit.py" plan.json \
  --state-dir "$STATE" --unit impl --pr 123 --approver "Operator" \
  --root "$RUNS" --dry-run
```

Remove `--dry-run` to execute after inspecting the preview. The operator must supply a nonblank approver and, when coordinator state has no recorded root, `--root`; a conflicting root is refused. The plan digest and attempt anchors are checked before forge access. Worker files provide no authority. Supervisor loss before `supervision-finished`, or accounting expiry after a positive observation, leaves the ordinary retrieval path blocked. A named operator can resolve that launch using `--verify-integration --retrieve-remote-evidence --remote-recovery-attestation FILE --approver NAME` on the same `merge_unit.py` command. This path records an **attested**, never verified, resolution in the external coordinator ledger before using it. It neither requests a merge nor contacts the remote host, and retains the stage. An unreachable supervisor is not proof of death. Missing evidence never establishes quiescence. The operator must establish that the supervisor cannot submit, run or publish, that every launch job and descendant is fenced against execution and publication, and that administrators have prevented privileged requeue or resubmission of this launch. Record the concrete investigation and enforcement evidence for each assertion. Killing a supervisor alone, a terminal accounting row alone, an empty queue, or a successful cancellation request is insufficient. The trust boundary is the named operator's truthful investigation and durable fencing, not authentication or independent verification of their testimony. A false attestation or subsequently removed fence can admit evidence while a writer remains live; this residual risk is explicitly operator-owned. `FILE` is a JSON object with exactly `binding`, `supervisor`, `jobs`, and `privileged_requeue`. Each of the latter three contains exactly `{"status": "fenced", "evidence": "concrete investigation and enforcement record"}`. Values such as `unreachable` refuse. `binding` contains `launch_id`, `stage`, `request_sha256`, and `receipts_sha256` for the selected coordinator ledger run. The digests use SHA-256 of UTF-8 JSON with sorted keys and separators `(',', ':')`, as returned by `remote_verify.recovery_binding(run)`. Read these values from `STATE/remote-verifications/*.json`, matching the exact unit and candidate basis; a binding copied from a different launch or receipt set refuses. Retrieve first to preserve every complete, validated, bound claim receipt in coordinator state. Recovery refuses missing, incomplete, conflicting or invalid receipts and never creates or upgrades one; a completed FAIL remains FAIL. Slurm PASS still requires recorded terminal-success execution evidence. A read-only scheduler observation after supervisor loss can retain that success without granting quiescence; absent recorded success, attestation cannot supply it. A separate coordinator-owned success observation binds the launch, request, receipts, every discovered job ID, and the final empty-queue snapshot. Stale null markers and accounting expiry cannot erase it. Newly observed jobs, live/requeue indications, or contrary non-null results block its reuse until a fresh complete success observation covers every retained job identity. The saved snapshot is historical execution evidence, not continuous supervision; a wholly unobserved requeue before fencing remains part of the operator-owned residual risk. Missing or partial historical metadata cannot create this proof. The operator resolution substitutes only for the missing lifecycle authority. Repeated retrieval uses the journaled authority and existing receipts without further remote calls, and publication still passes the operator's original observation fence. The remote supervisor marker is never fabricated, and this recovery route supplies no automatic cleanup acknowledgment. <!-- declaration: code.merge-command -->

Before a new merge, at least one CI check must exist and every check must report `SUCCESS`; failed or unavailable reads also refuse. An exit-0 scope report must be an object with in_scope status, matching unit/attempt/head, a valid base ID, a scope list of strings, and empty outside/deletion lists; malformed success cannot be waived. Scope exits 1 and 2 require `--allow-unchecked-scope "reason"`, recorded with the approver and scope result (or verbatim malformed stdout/stderr on a nonzero exit) in a durable `merge-unit-OPERATION.json` in the state directory. An already merged matching PR is reconciled without reapplying current CI or scope policy; a different head or target refuses. New intents retain captured scope stdout/stderr in addition to the parsed report; legacy intents lacking raw fields retain their original observations without reconstructing lost text. Original precondition observations remain in an existing intent, while a newly observed historical merge does not fabricate them. An exact existing receipt is reused, and an older receipt with the wrong repository spelling is supplemented by the correct anchored URL. Dry-run prints the conditional command sequence with placeholders for the unobserved PR URL and commit IDs, performs no forge calls or state writes, and exits 2. Ordinary exit 0 means the receipt exists and advance returned success. To resolve an investigated uncertain request, pass `--abandon-intent OPERATION_ID --approver "Operator" --reason "Investigation outcome"`. The named operation must be the current unresolved intent for this attempt and PR. OPEN at the judged head records abandonment and exits 0 without merging or advancing; MERGED at that head reconciles instead, while a different head or target refuses. The atomic, fsynced `merge-abandonment-OPERATION.json` beside the retained intent records the original intent, who, why, the observed PR, and UTC observation time. The intent is marked `resolved_by_abandonment`; a crash between record publication and marking is repaired from the record on the next invocation. The next ordinary call can create one successor intent with a new operation ID and rechecks scope and CI. A second abandonment of the old ID refuses, including after a successor was created. <!-- declaration: code.merge-command -->


The command never resubmits an unresolved merge request: a queued request, a lost response, or a crash between intent persistence and transmission leaves a durable operation for inspection. Rerun after GitHub reports MERGED to record and advance, or explicitly abandon the investigated OPEN request using the recorded path above. Neither action deletes the intent. Abandonment attests an OPEN observation; it cannot prove an earlier queued request will never execute, so the named operator retains responsibility for investigating that uncertainty. The intent and abandonment journals retain the same-node trusted-writer boundary. The local coordinator lease is released before advance acquires it; advancement can still halt or retain a unit for its existing verification policy. A successful advance does not assert that every unit closed. <!-- declaration: limit.merge-command -->

Lambda Slurm verification through `STATE_DIR/verification-execution.json` is the default merge-verification route. <!-- declaration: merge.requirements -->

## Declaring a Slurm verification host

In the external coordinator state's `verification-execution.json`, declare
`verification_host` with `ssh_alias`, `executor: "slurm"`, absolute `workdir_root`,
`python` and `git` paths, and a `slurm` object containing `partition`, `mem` and
`time`. The parser accepts these three values as plain string tokens and rejects
a missing `mem`. Keep actual host and resource values in that private policy.
The remote root exists before launch. The supervisor submits and polls;
verification runs exclusively inside the allocated worker.

A cleanup-unconfirmed warning means removal was not confirmed; retain any
surviving stage. Inspect the reported job IDs, launch job name and `cleanup.json`
(or the adjacent `STAGE.cleanup.json` once removal starts);
a successful cancellation request alone is insufficient to establish termination.
Before deleting stage contents, cleanup persists its final evidence beside the
stage and closes the in-stage lock while retaining an external cleanup lock.
The stage directory is removed last; `cleanup: removed` reports confirmed absence.
Final cleanup evidence stays beside the removed stage until its exact receipt is
durable in coordinator state; acknowledgment then retires the auxiliary files.
Audit-publication errors leave the actual scheduler observation unchanged.
After an interrupted deletion, the coordinator's durable ordinary acknowledgment
and positive lifecycle state permit completion of an empty or absent stage.
Completion uses atomic `rmdir` under the external lock, imports no staged harness,
and runs no verifier or scheduler. Any remaining files stay subject to normal
cleanup guards; a partially deleted nonempty stage with no harness stays retained.
Before `rmdir`, completion checks the adjacent journal's stage, launch identity,
and digest against its saved receipt. An unreadable or mismatched journal leaves
the stage intact and reports cleanup unconfirmed. A lost cleanup response causes
retrieval to save the bound snapshot first, then check its digest and finish
within the same invocation. Removed status follows an actual absence check.
When a nonempty stage returns to normal cleanup, the coordinator saves retirement
of its old receipt pin before that cleanup can replace the journal. Empty-stage
validation preserves the existing pin on a mismatch.
External snapshots remain nested audit data, separate from fresh lifecycle
observations. Retrieval preserves their exact contents in coordinator state
before retirement, including after an interrupted final write. When the journal
is already gone, the recorded removal permits retiring its leftover lock; a
present mismatched journal stays intact.
Cleanup is conditional on terminal accounting for all launch jobs, finished
supervision and acknowledged claim evidence. Missing job IDs are recovered by
name from `squeue` and `sacct`; unavailable or empty discovery leaves the stage
intact. Restore access and retrieve the original launch with
`--verify-integration --retrieve-remote-evidence`. Retain unresolved stages and
receipts; the binding blocks replacement submissions and admission until its
evidence is resolved, and reruns retrieve the original launch.

Cleanup finishes scheduler observation with accounting by name followed by the
live queue, after every individual accounting/cancellation operation, and records
`scheduler_observed_at` in UTC. A live row, failed or ambiguous query, or newly
discovered ID lacking already-collected terminal accounting retains the stage.
New IDs are saved for the next bounded retrieval; the final queue observation
ends that cleanup pass's scheduler queries. An observed negative result resets the
coordinator's stale reconciliation/publication flags and acknowledgment;
a transport failure alone leaves them unchanged. Admission also blocks persisted
negative lifecycle evidence despite stale flags. Later positive observations can
reconcile the original immutable receipts. A positive cleanup observation in the
same invocation triggers one bounded re-ingestion and reconciliation under the
binding lock, followed by cleanup with the durable acknowledgment. That cleanup
again checks the scheduler; a negative result still revokes reconciliation.
A privileged requeue remains possible after the final observation;
`--no-requeue` and the worker-started guard protect against
ordinary restarts and receipt replacement, without an atomic scheduler fence.

## Drain the post-merge close intent

After receipt recording and successful advancement, `merge_unit.py` displays the current attempt's pending close key, `tracker` issue (or `no tracker declared`), and drain command. With a binding and key it invokes drain for that state directory and prints its summary, then runs audit and section. Missing configuration or sibling prints `Tracker drain: UNAVAILABLE (reason)`; drain and audit never change merge success. <!-- declaration: tracker.drain -->

Drain every relevant state directory before auditing; repeat `--state-dir` to cover several waves. <!-- declaration: tracker.drain -->

```bash
"$ORCH_PYTHON" "$HANIG_PROJECT_DIR/scripts/linear_sync.py" drain \
  --binding .hanig/linear-binding.json --state-dir "$STATE"
"$ORCH_PYTHON" "$HANIG_PROJECT_DIR/scripts/linear_sync.py" audit \
  --binding .hanig/linear-binding.json --out "$AUDIT"
```

The per-project host lock serializes local drainers. Each issue is reconciled once to its latest-ordered genuine state comment, including issues with receipts. Two-host races repair eventually after writes stop and listings catch up. Receipts are historical read-back observations; uncovered legacy history and comment editing/deletion limit coverage. No pending intent is not proof of delivery or unit closure, and tracker status supplies no coordinator authority. <!-- declaration: tracker.drain, tracker.authority -->

## Tracker and the hourly report

The tracker mirrors coordinator state, and GitHub's pull-request attachment does not perform the issue transition. <!-- declaration: tracker.authority -->


The DAG check is `linear_sync.py audit`. File and edit ad-hoc issues through `linear_sync.py issue`, declaring relations at filing or an independence reason; prose dependencies are not a substitute. Replay incomplete operations by their recorded operation ID. Plan units use plan needs and coordinator evidence; ad-hoc issues use Linear blocks edges. Prose candidates are advisory and a discrepancy supplies no coordinator authority. <!-- declaration: tracker.dag -->

The reporting order incorporates the still-open pull request 60 source material:

1. Running work: agents, pull requests, checks, failures, skips, corrections, and observation times.
2. Tracker: quote `linear_sync.py section --audit FILE` with the same inputs verbatim. <!-- declaration: report.three-parts -->
3. Ready work: compare the backlog to what landed and dispatch; otherwise state the exact blocker or saturation reason.

The order and the dispatch action are part of the decision surface rather than a pointer-only recommendation. Before the report is written, step-three dispatches reach the tracker or the report names their pending synchronization when tracker access is unavailable. <!-- declaration: report.three-parts -->

## Hourly reconciliation

The hourly loop includes this comparison, with every coordinator state directory for the repository and a window overlapping the preceding successful observation:

```bash
"$ORCH_PYTHON" "$HANIG_ORCHESTRATE_DIR/scripts/reconcile.py" \
  --state-dir "$STATE" --since 2026-09-26T00:00:00Z --json
```

The sweep examines every in-progress issue regardless of age and repeats after each dispatch, push, merge, and close. A dispatch immediately records the tracker event that moves every covered issue to in progress with its unit; the authorized operator applies it with linear_sync.py drain when tracker access is available, while an outage leaves pending synchronization without blocking unrelated dispatch. A stopped attempt that did not ship similarly records its state and preservation ref. The read-only reconciler defaults to the checkout origin and branch `main`; `--repo OWNER/REPO` (or a forge URL) and `--branch` select alternatives. The shorthand uses the host resolved by `gh repo view`; a failed resolution is unreadable input. Repeated `--state-dir` includes older runs. Its window is an inclusive timezone-bearing `--since`, the most recently merged `--limit N` PRs, or both. UNMEDIATED MERGE means no matching record accounts for the PR, repository, target and historical head/merge; the operator investigates bypass and reconciles evidence and tracker obligations. UNACKNOWLEDGED OBLIGATION means a matching source's outbox intent lacks a receipt; the operator resolves ambiguity by receiver read-back or deduplication before draining and attesting. MISMATCHED SOURCE means coordinator anchors name another repository or several; the operator selects the correct source, while the mismatched source contributes neither merge nor obligation coverage. Missing anchors are unreadable. Exit 0 means all sources were read without findings; exit 1 means findings; exit 2 means unreadable input and precludes a clean report. The command changes no forge, tracker or coordinator state. <!-- declaration: tracker.reconcile -->

Forge routing supports standard HTTP(S) and SSH remotes without explicit ports and keeps the observed HTTP(S) PR URL; ports are refused rather than silently discarded. Reconciliation requires a single-parent squash result; merge observations and the squash method remain attested. The actual merge parent's SHA supplies `--target-commit`, including when the target moved after the original observation. Head matching does not atomically freeze CI reruns or concurrent PR retargeting, and a one-parent result alone does not distinguish squash from a one-commit rebase by another operator. The same credential can bypass `merge_unit.py`. This detects bypass after the fact; it does not prevent it. Coverage is limited to supplied directories and the merged-PR window, so a direct push without a merged PR is outside the comparison. A pending request accounts for a lost response; cancelled or abandoned requests do not account for later merges. Historical reconciliation attests recording, not who executed the merge. A matching pending record cannot distinguish a lost response from a later direct merge of the same PR and head; this check establishes record presence, never execution attribution. The observation is not an atomic forge snapshot; a changed or unreadable page calls for a retry. <!-- declaration: limit.merge-command -->

The hook resolves a supported `gh` locator through a bounded `gh repo view` read, then reports counts after exact anchor matching for a single literal `git [-C DIR] push origin ...` without push options, or explicit `gh ... --repo OWNER/REPO` (`-R` also works). Other routing, compound commands, malformed inputs, missing anchors and differing hostname spellings produce repository-specific uncertainty. Hostname spelling is preserved in the comparison, and action detection is unchanged.

## Migrating a running project to a dedicated orchestration environment

Follow these four steps in order without disturbing live jobs or state: <!-- declaration: env.dedicated -->

1. Build the new dedicated orchestration env (stdlib-only `uv` venv): <!-- declaration: env.dedicated -->

```bash
PROJECT="my-project"
ORCH_ENV="$HOME/.local/state/$PROJECT/orch-venv"
uv venv --python 3.12 "$ORCH_ENV"
ORCH_PYTHON="$ORCH_ENV/bin/python"
"$ORCH_PYTHON" --version
```

2. Test read-only against a staged `plan.json` in a temp dir (`validate`, `status`, `orch_env.py check --strict`, and `hourly_facts.py` to temp dir): <!-- declaration: env.dedicated -->

```bash
set -eu
TEST_OUT="$(mktemp -d)"
cp "$PROJECT_DIR/plan.json" "$TEST_OUT/plan.json"
# Update $TEST_OUT/plan.json: set environments.orchestration (path=$ORCH_ENV, python="3.12", lock_file="none", build="uv venv --python 3.12 $ORCH_ENV") and declare/bind model-line envs (or environment="none" on non-Python code units)
HANIG_ORCH_PYTHON="$ORCH_PYTHON" "$ORCH_PYTHON" "$HANIG_SWARM_DIR/scripts/swarm.py" validate "$TEST_OUT/plan.json"
HANIG_ORCH_PYTHON="$ORCH_PYTHON" "$ORCH_PYTHON" "$HANIG_SWARM_DIR/scripts/swarm.py" status "$PROJECT_DIR/plan.json" --state-dir "$STATE"
PATH="$ORCH_ENV/bin:$PATH" HANIG_ORCH_PYTHON="$ORCH_PYTHON" "$ORCH_PYTHON" "$HANIG_ORCHESTRATE_DIR/scripts/orch_env.py" check --plan "$TEST_OUT/plan.json" --strict
HANIG_ORCH_PYTHON="$ORCH_PYTHON" "$ORCH_PYTHON" "$HANIG_ORCHESTRATE_DIR/scripts/hourly_facts.py" --state-root "$STATE_ROOT" --output-dir "$TEST_OUT" --repo "$REPO"
ls -la "$TEST_OUT" && rm -rf "$TEST_OUT"
```

3. Switch `plan.json`, cron/launchd, and helper scripts with timestamped backups first: <!-- declaration: env.dedicated -->

```bash
set -eu
BACKUP_DIR="$HOME/.local/state/$PROJECT/env-migration-backup-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp -L "$PROJECT_DIR/plan.json" "$BACKUP_DIR/plan.json.bak"
if crontab -l > "$BACKUP_DIR/crontab.bak" 2>"$BACKUP_DIR/crontab.err"; then
  rm -f "$BACKUP_DIR/crontab.err"
elif grep -qi "no crontab for" "$BACKUP_DIR/crontab.err"; then
  rm -f "$BACKUP_DIR/crontab.bak" "$BACKUP_DIR/crontab.err"
else
  cat "$BACKUP_DIR/crontab.err" >&2
  exit 1
fi
[ ! -d "$HOME/Library/LaunchAgents" ] || cp -a "$HOME/Library/LaunchAgents" "$BACKUP_DIR/LaunchAgents.bak"
[ ! -d "$PROJECT_DIR/scripts" ] || cp -a "$PROJECT_DIR/scripts" "$BACKUP_DIR/scripts.bak"
```

Update `plan.json`, cron lines, launchd plists, and helper wrappers to set `ORCH_PYTHON` and `HANIG_ORCH_PYTHON` and invoke `"$ORCH_PYTHON"` by absolute path (pass `--accept-plan-change` on the first `advance` because `environments` is in `plan_digest`). <!-- declaration: env.dedicated -->

4. Confirm the next `advance` tick and `hourly_facts.py` run are clean in the cron log, `swarm.py status`, and `latest.md` before removing old backups. <!-- declaration: env.dedicated -->
