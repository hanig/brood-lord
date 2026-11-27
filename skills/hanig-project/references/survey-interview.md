# Survey and interview evidence

The decision surface is `../SKILL.md`. This reference records why the survey
and interview declarations exist and gives examples without becoming a second
source of behavior. <!-- declaration: placement.reference-elaboration -->

## Why the survey runs first

Reading the survey before speaking is what keeps the interview to judgment.
The walk, the partition facts and the repository state below are all things
the host can answer, so asking about them spends the owner's attention on <!-- declaration: survey.read-before-ask -->
something inspection already settled. <!-- declaration: survey.read-before-ask -->

## Repository walk states

The survey tree walk runs in a child process with a real deadline. A check in
the walking loop cannot interrupt `os.scandir` while `opendir()` is blocked on <!-- declaration: placement.reference-elaboration -->
a stale NFS handle or dead automount.

`repo.walk` therefore records `complete`, `truncated`, `stuck`, or `unknown`.
Truncation says a declared cap stopped a responsive tree; stuck says a
directory stopped answering and records `stuck_at`. Counts from either case
must be presented as floors rather than totals. <!-- declaration: survey.incomplete-walk -->

## Partition facts

The survey records `allow_accounts`, `deny_accounts`, partition `qos`,
`qos_grptres`, and `max_mem_per_cpu_mb`. Each permission or limit field carries <!-- declaration: survey.partition-state -->
`set`, `unrestricted`, or `unknown`; unknown means the query did not answer, so
it must be reported rather than planned around as unrestricted. <!-- declaration: survey.partition-state -->

Two observed failures motivated these fields. A 736-CPU partition had 202 idle
CPUs while an unseen `QOSGrpCpuLimit` blocked the selected account. Separately,
700 GB under `MaxMemPerCPU=5120` charged 140 CPUs rather than the requested 32.

When the per-CPU memory limit is set, the charged count is <!-- declaration: cluster.memory-charging -->
`ceil(mem_mb / max_mem_per_cpu_mb)`. The interview must quote that charged CPU <!-- declaration: cluster.memory-charging -->
count, recommend shrinking per-job memory until that charged count fits the
account, and ask whether the resulting footprint fits both budget and queue. <!-- declaration: cluster.memory-charging -->

Slurm can print `DenyAccounts` instead of `AllowAccounts`. Reading only the <!-- declaration: cluster.account-allowance -->
allow side makes an expressly denied partition look open. After reading both, <!-- declaration: cluster.account-allowance -->
ask the owner whether CPU-only work may use an exclusively allowed idle <!-- declaration: cluster.account-allowance -->
partition and recommend yes when the shared queue is capped. A named denial is <!-- declaration: cluster.account-allowance -->
not a question: report the closed route and move on. Report an unknown allow or <!-- declaration: cluster.account-allowance -->
deny state instead of asking around it or making a recommendation. <!-- declaration: cluster.account-allowance -->

The surveyed `qos_grptres` describes partition QoS only. Account or association <!-- declaration: cluster.qos-scope -->
QoS can carry another `GrpTRES` that the survey does not see, so unrestricted <!-- declaration: cluster.qos-scope -->
at the partition level must not be presented as a promise that submission will <!-- declaration: cluster.qos-scope -->
run. <!-- declaration: cluster.qos-scope -->

Validation refuses a partition the surveyed cluster does not have and refuses <!-- declaration: plan.scheduler-route -->
known allow or deny mismatches. It cannot validate account-name existence and <!-- declaration: plan.scheduler-route -->
stays silent when account data is unknown, so report that unknown state; a typo <!-- declaration: plan.scheduler-route -->
can survive validation and fail only at submission. <!-- declaration: plan.scheduler-route -->

## Repository destination examples

The survey already reports repository, remote, and branch state. A repository
with a remote is adopted without another question, and the adopted remote and
branch are stated in one line. A repository without a
remote gets one push-destination question with local work recommended. With no
repository, ask whether source must outlive the attempt and recommend nowhere. <!-- declaration: repository.destination -->

The deciding distinction is source versus data. A manifest, checkpoint, or TSV
is compute data even when code produced it. Swarm output must not be committed; <!-- declaration: repository.source-data -->
shared publication uses promotion and a named approver. <!-- declaration: repository.source-data -->

## Dispatch-complete interview

The interview is complete when the plan can run, not when questions run out.
One prior plan was validated and filed as five issues before anyone requested
the corpus subpath and glob that its command needed. <!-- declaration: interview.dispatch-complete -->

Run the schema before ending the interview and settle every required input, <!-- declaration: interview.dispatch-complete -->
partition, account, promotion destination and approver, code provider and mode,
target branch, runtime verification, and retry exposure. <!-- declaration: interview.dispatch-complete -->

Every question must concern judgment that inspection cannot settle and carry a <!-- declaration: interview.judgment-only -->
recommended answer. The topics are the ones the declaration itself lists, and
that list is a floor rather than an exhaustive set; it is deliberately not <!-- declaration: interview.judgment-only -->
copied here. A second copy is what let the reporting cadence leave one surface <!-- declaration: interview.judgment-only -->
while surviving in another. <!-- declaration: interview.judgment-only -->

## Reporting cadence and escalation

Cadence and escalation are two answers, not one. Cadence is how often a
healthy run reports. Escalation is what reaches the owner immediately
regardless of cadence: a NEEDS_HUMAN unit, an exhausted budget, a blocked
merge, a conflict a machine must not settle. <!-- declaration: interview.reporting-cadence -->
"Only at the end" is a legitimate cadence and is never a legitimate <!-- declaration: interview.reporting-cadence -->
escalation policy. <!-- declaration: interview.reporting-cadence -->

Both answers belong in the plan as a top-level `reporting` block rather than
in a session's memory, because a session dies and its successor inherits the
plan and not the conversation. The block sits outside `plan_digest`, so
changing cadence mid-run does not invalidate recorded attempts. <!-- declaration: interview.reporting-cadence -->

Nothing enforces cadence; the orchestrator honours it. Enforcing it is
ARC-691's clock rather than a sentence in a skill. <!-- declaration: interview.reporting-cadence -->

## Orchestration environment

`survey.py` records `machine.python_env` (conda/venv/system, `shared_risk`; unactivated custom-named conda prefixes without active `CONDA_DEFAULT_ENV=base` or standard conda root names report their directory basename) and repo lock files (`requirements*.lock`, `uv.lock`, collected within the bounded `MAX_DEPTH` walk up to 200 files). Ask once where orchestration's dedicated stdlib-only env lives, recommending `~/.local/state/PROJECT/orch-venv` (`uv`, Python 3.12), and record `environments.orchestration` in `plan.json` so cron and helpers never use shared personal envs. <!-- declaration: interview.orchestration-env -->

## Coding backend and model

The coding backend is one owner decision taken once, near the start, because
every backend drives the same coordinator-owned Git worktree and they differ
in who runs the agent and what it costs. `lor` starts the agent as a native
subagent of the coordinator's own Lor session with `lor session start` and
suits Lor-served models. `claude`, `codex` and `pi` run headless turns of that
CLI with no daemon and suit models served through a Claude or Codex
subscription login, or through `pi`. Paseo is retired and is not offered. <!-- declaration: interview.code-agent -->

Inspection settles what is available, so read it before asking. The survey's
`machine.tools` lists which of `lor`, `claude`, `codex`, `pi` and `opencode`
the host has. When `LOR_SESSION_ID` is set, `lor model list` names the
canonical model ids the session can serve; offer those for
`lor/CANONICAL_MODEL_ID`. A planning session without `LOR_SESSION_ID` or `lor` on PATH cannot vouch for a Lor <!-- declaration: interview.code-agent -->
backend, because Lor dispatch is refused before any attempt exists when the <!-- declaration: interview.code-agent -->
coordinator runs outside a Lor session; say so and recommend a direct CLI the
survey found unless the owner will run the coordinator from a Lor session. <!-- declaration: interview.code-agent -->

Ask for the model and the effort together, then record one top-level object
in `plan.json`, for example:

```json
{"code_agent": {"provider": "lor/CANONICAL_MODEL_ID", "thinking": "high"}}
```

```json
{"code_agent": {"backend": "claude", "model": "MODEL", "thinking": "high"}}
```

`pi` takes model `PROVIDER/MODEL`; provider `openrouter` reads
`OPENROUTER_API_KEY` on the coordinator side. These backends take no
mode, and a restrictive mode is refused at validation. `code_agent` applies to code <!-- declaration: interview.code-agent -->
units that pin no `provider` or `model`, so a unit that pins its own agent
keeps it. Leaving `code_agent` out keeps the `hanig-swarm` routing default, which is the legacy Paseo route. <!-- declaration: interview.code-agent -->

The answer is a starting point, not a commitment. `code_agent` is part of
`plan_digest`, so changing the backend later goes through `swarm.py set-agent`,
which writes coordinator state and leaves `plan.json` and `plan_digest`
untouched; attempts already running keep the backend they started with. The
switching workflow belongs to `hanig-orchestrate`. <!-- declaration: interview.code-agent -->

For a half-finished repository, the initial draft must be based on its project <!-- declaration: adoption.context -->
documents, architecture decisions, recent commits, and outputs already on
disk. <!-- declaration: adoption.context -->

Before filing that draft, completed work must be excluded and an artifact-free <!-- declaration: adoption.remaining-work -->
TODO must not be promoted into a unit merely because it exists in prose. <!-- declaration: adoption.remaining-work -->
