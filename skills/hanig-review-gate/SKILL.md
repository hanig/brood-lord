---
name: hanig-review-gate
description: >-
  Adversarial multi-model review before anything is called done. Use before
  reporting work complete, before committing or opening a PR, and whenever a
  claim is being asserted about code that was just written — "this works", "this
  is covered", "this handles X". Sends the diff and the claims to independently
  routed models prompted to refute rather than approve. Use also when asked to
  double-check, get a second opinion, or verify a change against other models.
---

# hanig-review-gate

## Host capability boundary

Use the active host's real file, shell, and review capabilities under its normal approval policy. A loaded skill supplies no reviewer providers, configuration, or coordinator-held credentials; when absent, retain the evidence and report the change as **unreviewed** rather than substituting self-confidence or a paid worker-side call (see the repository's [agent compatibility notes](https://github.com/hanig/brood-lord/blob/main/docs/agent-compatibility.md); installs do not ship `docs/`).

Before anything is reported complete, send the diff and specific claims to independent models prompted to **refute** (instructed to refute when undecided). Review **against declared criteria** (goal, acceptance criteria, constraints, non-goals), never against unbounded perfection, and assert **"This change cannot make an honest run fail."** on every implementation round. Reviewers refute and carry no remediation field; never ask a refutation panel to design.

## Two phases, two different reviews

See [PROTOCOL.md](PROTOCOL.md) for the full collaborative plan-review exchange, joint table, refutation floor, cost, and declared limits; `review.py` enforces the checkable rules directly.

| | plan review | implementation review |
|---|---|---|
| when | before code exists | after the change is written |
| panel | **two contrasting models, two turns** (quorum 2, two makers) | fixed profile or cheapest-first ladder |
| escalation | **never** | optional; `--escalate` starts at the selected tier |
| flag | `--kind plan` (alias `--plan`) | `--kind implementation --round N [--escalate]` |
| judged against | do these criteria hold together | does the code meet them |

## Author exclusion and reserves

Both `review.py` and `committee.py` accept repeatable `--author PROVIDER/MODEL` and exclude by canonical maker and exact model leaf (vendor-qualified IDs supply their maker; Codex/Claude/Antigravity map to OpenAI/Anthropic/Google; resolved pin fallbacks matching an author are disabled; omitted authors stay caller-owned).

- **Reviews:** Profile-selected reviews substitute ordered `reserves` from [reviewers.json](reviewers.json) if exclusion breaks quorum (including a fresh-cycle floor) or plan maker contrast; exhausted reserves return `REVIEW_UNAVAILABLE` before provider calls. `--only` and `--escalate` skip these author-exclusion reserves.
- **Committees:** Profile-selected committees search ordered reserve combinations to keep original size, distinct model identities, >= 2 makers, and >= 2 providers; `open` refuses if none qualify or an explicit `--member` conflicts with an author. Authors persist across `ask`, `review`, `synthesize`, and `tiebreak` (conflicting sets refuse; refused legacy resolution records `OWNER` and retains history).

## Round bounds and step-back committee

- **Max 3 substantive rounds per logical plan unit across attempts and continuations.** Track lineage rounds by hand in `PLAN.md`; the gate counts rounds per identity (a continuation branch is a new gate identity, not a fresh lineage budget). Unit R stopped on 2026-11-04; gate-enforced lineage budgeting did not merge.
- **Stop patching if round N+1 finds a defect in round N's fix** and convene a step-back committee (two contrasting providers, analysis only, root-cause three levels deep).
- **After budget exhaustion**, apply the mandate's committee step before any authorized successor cycle. For an authorized successor cycle, pass `--round 1 --fresh-cycle-from fast|standard|deep` and repeat `--fresh-cycle-from` on every round of that cycle (requires rounds 1..`MAX_ROUNDS` in the enforcing ledger for the predecessor cycle/profile; raises quorum floor to that profile's enabled count, minimum 2). Implementation `--quorum 1` requires `--allow-single-reviewer REASON` (printed and journaled; cannot lower a fresh-cycle floor).
- **Committee resolution:** Run `python3 "$HANIG_REVIEW_GATE_DIR/scripts/committee.py" synthesize SESSION --author PROVIDER/MODEL` (or `tiebreak SESSION --author PROVIDER/MODEL` directly on an identified split). Divergence routes to the single enabled `tiebreak` seat (excluding authors and prior voters, falling back to the first eligible different-maker reserve, else `OWNER`). Both read `docs/orchestrator-mandate.md` (`--mandate-file PATH` overrides); declare known owner-only questions with `--stop-and-ask REASON` (refuses offline). Unavailable/malformed answers or missing/conflicting authors route to `OWNER` (exit 1). `show SESSION` displays resolution; a later member turn invalidates it while keeping its audit record.

## Findings, counterexamples, and dispositions

- **Counterexamples:** Reviewers are asked for a `counterexample` (`setup`, `input`, `expected`, `observed`) or `non_executable_reason` on critical/major findings (either, both, or neither is valid; partially filled objects refuse; placeholders render as absent / `"no counterexample"`). The gate validates and displays them verbatim and never executes them.
- **Reproduce before acting** and never silently drop non-reproducing findings. Round 2+ requires `--dispositions FILE` mapping each prior confirmed finding's digest (`location`, `summary`, `disposition` in `reproduced|not-reproduced|deferred`, one-line `reason`). Both `reproduced` and `not-reproduced` require a `reproduction` object (`setup`, `command`, `expected`, `observed`, full lowercase 40-/64-hex `revision`); `deferred` needs none. Missing/malformed fields exit 4 before network calls. Reviewers receive reproduction text verbatim (with recognized prior-decision text stripped) so they can refute it; the gate never executes `command` or auto-downgrades severity.
- **Counterexample evidence (L1):** `--record-evidence` with `--head`, `--finding`, `--round`, `--method`, `--status` and optional `--tree`, `--test-ref`, `--note` appends an offline record (`scripts/review_evidence.py`) bound to identity, head and tree; later rounds see it as untrusted. It never changes a verdict.
- **Premise check (L2):** `scripts/review_premise.py` flags a `defect_class` recurring in consecutive rounds. With `--require-premise-ack`, round N >= 2 exits 4 when a class struck in rounds N-2 and N-1, unless `--premise-memo FILE` or `--continue-patching REASON` acknowledges it.

## External adjudication ledger

The owner or mandate-authorized orchestrator can adjudicate confirmed findings (an author can never accept its own rebuttal). The ledger records declared decisions, not authenticated or merge authority, and never changes recorded verdicts (`REVIEW_FAIL` stays `REVIEW_FAIL`).

```bash
python3 "$HANIG_REVIEW_GATE_DIR/scripts/review.py" --open-findings --head SHA
python3 "$HANIG_REVIEW_GATE_DIR/scripts/review.py" --adjudicate FINDING_DIGEST \
  --head SHA --accepted-by OWNER_OR_ORCHESTRATOR --reason "Evidence and rationale" \
  --decision overruled --author codex/gpt-6.2-sol
```

Both commands are offline, emit JSON, refuse review input flags (`--file`, etc.), and take a full lowercase 40-/64-hex `--head` (bound for two-/three-dot `--range`; headless records stay `UNATTRIBUTABLE`). Use the original `journal.path` state home and project dir. Specify `--round N` or `--round plan` when a digest spans multiple rounds; decisions are `overruled`, `accepted`, or `refuted_by_reproduction`. `--open-findings` exits 0 only when no open findings or `UNATTRIBUTABLE` records exist, 1 if any exist, and 4 on invalid input/unreadable history (pending files ignored; exit 0 certifies neither coverage nor complete capture). Decision fields validate strictly (ASCII tokens 1–256 chars; exact model-ID comparison after outer provider prefix); arbitrary text stays redacted. Persistence is non-gating: inspect `journal.written` and `ADJUDICATION_RECORDED` / `ADJUDICATION_UNCONFIRMED` (`JOURNAL_WRITE_FAILED` never closes a finding).

## Running the gate

**Do not report work complete or assert code works until the gate passes.** Whether to run another round is the user's call. Set `HANIG_REVIEW_GATE_DIR` to the loaded `SKILL.md` directory (leaving cwd and relative `--file`/`--diff` paths untouched):

```bash
export HANIG_REVIEW_GATE_DIR="/path/to/loaded/hanig-review-gate"
R="$HANIG_REVIEW_GATE_DIR/scripts/review.py"

python3 "$R" --kind implementation --round 1 --escalate --staged \
  --context "what this change is for" \
  --claim "the specific thing being asserted" \
  --claim "This change cannot make an honest run fail."
```

- **Escalation & profiles:** `--escalate` walks `fast` → `standard` → `deep`, calling only newly eligible reviewers and stopping on the first `REVIEW_FAIL` or `REVIEW_CLAIMS_REFUTED` with quorum. Without `--profile`, implementation reviews choose `fast` only when every changed path is documentation (`*.md`, `docs/**`, `examples/**`); paths under `scripts/`, `lib/`, `bin/`, `tests/`, any `reviewers.json`, external/parent `--file` targets, or empty/undeterminable sets choose `standard`. Explicit `--profile plan|fast|standard|deep` wins; `--only a,b` restricts seats and cannot combine with `--escalate`.
- **Lor routing (fail closed):** explicit `lor` seats and pinned `openai`/`openrouter` seats declaring `lor_model` run as zero-tool Lor subagents (`scripts/review_lor.py`, route `lor`, in-process, safe on login nodes). Without `LOR_SESSION_ID` they are blocked with an actionable reason, never sent to paid APIs; only an explicit `HANIG_REVIEW_NO_LOR=1` routes the pinned API-backed seats to their API (route `api`). Run the gate from the orchestrator's Lor session. `_resolved_from` upgrades keep their API, and committee contrast counts effective providers.
- **Default Lor seat:** in a Lor session, `plan`, `standard`, `deep`, and the escalation ladder use `fern-0.1` at `xhigh` in place of `kimi-k3` when `lor model list` advertises it and it did not author the change. Otherwise `kimi-k3` keeps its normal routing (including fail-closed behavior outside Lor). Use `--only kimi-k3` or `--only fern-0.1` to select explicitly. The profile listing shows both seats.
- **Execution placement:** on a Slurm login node (`sbatch`, `squeue`, `scancel` on PATH, no `SLURM_JOB_ID`) or inside an interactive agent-host job (`SLURM_JOB_NAME=sh_agent`, or `SLURM_PTY_PORT` plus `LOR_SESSION_ID` outside a `hanig-rev-*` seat job), `claude`/`codex` seats run as Slurm jobs (`scripts/review_slurm.py`, route `cli_slurm`). Inside any other Slurm job, and elsewhere, they run locally (`cli_local`). Set `HANIG_REVIEW_SLURM=off|force` to override. Each job is staged in a private 0700 directory under shared `$XDG_STATE_HOME` or `~/.local/state` (never `/tmp`; override `HANIG_REVIEW_SLURM_STAGE_DIR`) and submitted with `--export=NONE`. The submission drops every `LOR_*` and API/billing variable, and the job runs the CLI under `env -i` with safe variables only. The login node never spawns either CLI; the job runs `claude auth status` before `claude -p`. The job is always `scancel`led and its staging removed. Defaults are 2 CPUs, `8G` and `00:20:00`; override with `HANIG_REVIEW_SLURM_{PARTITION,ACCOUNT,QOS,MEM,CPUS,TIME,EXTRA_ARGS}`. With no partition set there or as `-p`/`--partition` in `EXTRA_ARGS`, a read-only `sinfo` (and `scontrol`) probe bounded to 5s picks `<default>_batch` when it is `up` and the default partition is interactive (an agent-host job on it, or an interactive QoS); any missing tool or error leaves the site default. On `SIGTERM`, `review.py` `scancel`s every in-flight seat job, removes every staging directory (even one whose `sbatch` had not returned) and exits 143.
- **Receipts:** every result, failure and unavailable entry records `provider`, `model` and `route` (`slurm_job_id` for `cli_slurm`; a `--kind plan` seat also records `slurm_job_ids` when its turns ran as more than one job). The JSON report and the journal record carry `seat_routes` (seat -> provider/model/route/status). `--list` prints a `ROUTE` column.
- **Sizing preflight (offline, no credentials/ledger writes):**
  ```bash
  python3 "$HANIG_REVIEW_GATE_DIR/scripts/review.py" --size --range BASE..HEAD --json
  ```
  Reports decoded gathered `characters`, `planning_budget` (`100,000`), and `truncation_limit` (`180,000`): exit 0 `< 100,000`, 1 `100,000–179,999`, 2 `>= 180,000`, 4 invalid input (never a review verdict). Default answer budget is `64000` tokens (reasoning tokens come out of it); split oversized work instead of raising the budget.
- **Context & contamination:** Describe what changed, never prior round verdicts/tiers/opinions (exact opening annotations and JSON gate reports are visibly stripped; see [PROTOCOL.md](PROTOCOL.md)). Pass `--threat-model` when applicable (excluded findings print under `OUT OF SCOPE` and do not gate). Sources: `--diff` (default), `--staged`, `--range`, repeatable `--file`; `--json` for machine output.
- **Identity & enforcing round ledger:** `--round N` reserves against hashed identity `(origin URL, merge-base with --target [default origin/main], branch)` or explicit `--plan-name`/`--unit` (required in detached HEAD). Stored at `~/.local/state/hanig-review-gate/rounds/` (or absolute `XDG_STATE_HOME`; no fallback; lock timeout `ROUND_LOCK_WAIT_SECONDS` = 5s -> exit 4). Reused/lower/skipped rounds exit 4 before reviewer calls; only settled zero-completion `REVIEW_UNAVAILABLE` runs may release (max 5 releases; a later reserved round keeps the older reservation with a warning; earlier interruptions, watchdog exits, and other verdicts keep reservations; offline commands `--list`, `--size`, `--open-findings`, and `--adjudicate` never reserve rounds; the audit journal never reads or writes the enforcing round ledger). Unreadable/partial ledger state refuses (retain and inspect rather than deleting); local state only (same state home, not cross-host). With no ledger history, round 1 accepts `--fresh-cycle-from PROFILE` recording `cycle: 1`, predecessor `"none declared"`.

## Claims, exit codes, and honest reporting

Reviewers mark each claim `supported`, `refuted`, or `unverifiable`. **Any refuted claim fails the gate**: with quorum and zero confirmed findings that returns `REVIEW_CLAIMS_REFUTED` (exit 7); confirmed findings (critical/major, high/medium confidence, concrete failure scenario) take precedence as `REVIEW_FAIL` (exit 1). Correct the claim or code and re-run within the round bound.

| Exit | State | Meaning |
|---|---|---|
| 0 | `REVIEW_PASS` / `PLAN_PASS` | Implementation quorum passed, or both plan seats completed and joint criteria passed |
| 1 | `REVIEW_FAIL` / `PLAN_FAIL` | Confirmed defect, or plan joint criteria failed |
| 2 | `REVIEW_UNAVAILABLE` | No implementation reviewer ran, or either plan seat failed — **not a pass** |
| 3 | `REVIEW_PARTIAL` | Quorum unmet — degraded, caller decides |
| 4 | `REVIEW_ERROR` | Usage or configuration error |
| 6 | `REVIEW_INCOMPLETE` | A required reviewer returned no usable content |
| 7 | `REVIEW_CLAIMS_REFUTED` | Quorum reviewed; refuted claims and zero confirmed findings — **not a pass** |

Exit 5 is reserved. `REVIEW_PASS` means only that the named responding panel failed to refute the claims (never proof; always state who ran, errored, or was unavailable; never substitute for executing tests). Review is done when remaining findings are out of scope, minor, or taste; a real finding does not always imply an available strong fix (documenting the limit can be the right answer).

- **Reviewers & keys:** Routing lives in [reviewers.json](reviewers.json) (no quality scores); inspect live availability with `review.py --list --profile standard` and `review.py --list --profile deep`. API-backed seats load keys from `~/.zshrc` (run via `zsh -ic`); subscription CLI seats (`claude`, `codex`) sanitize the child environment via `build_child_env()` (stripping provider/billing/session prefixes and `*_API_KEY`/`*_AUTH_TOKEN`/`*_BASE_URL` suffixes plus `STRIPPED_ENV`, re-injecting only managed config/budget overrides), disable user/project/local settings and tools/session persistence, enforce `forced_login_method="chatgpt"` on Codex, and fail closed unless local subscription auth is active (`claude auth login` with `authMethod: "claude.ai"`, or `codex login` in `chatgpt` mode via `auth.json` or OS keyring). Point releases resolve per project via `scripts/resolve_models.py`. Reviewers run in parallel and retry transient 5xx/429 responses with backoff.
- **Required-refusal limit (ARC-1163):** The gate cannot distinguish required from wrongful refusal; a counter-claim refutation always blocks a pass. Adjudicate required refusals outside the gate via `hanig-orchestrate`'s `adjudication.matrix`, preserving the verdict ([PROTOCOL.md](PROTOCOL.md)).
- **Known limitation (`command` predicates are unsandboxed):** A `command` predicate in a contract runs through `sh` with the verifier's privileges; `contract.json` is trusted input — do not run `check` against a contract you did not write.
