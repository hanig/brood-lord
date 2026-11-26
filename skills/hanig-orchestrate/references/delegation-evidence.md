# Delegation, evidence, and checked review facts

The initial unit owns its issue's implementation through a tested, mutation-checked, pushed commit with `evidence.md` and `test-output.txt`, and never runs `review.py` or `committee.py` on its own diff: the author is not its own reviewer, and the gate runs where the orchestrator runs. The orchestrator runs `review.py` and `committee.py` from its own Lor session (`LOR_SESSION_ID` present) on the pushed head, adjudicates, and opens and merges the pull request; on `REVIEW_FAIL` it sends the findings back or dispatches a repair continuation, in which the worker reproduces each finding, repairs it and records dispositions. <!-- declaration: delegation.whole-loop -->

## Prompt construction

A complete delegated prompt contains:

- Objective and acceptance criteria. <!-- declaration: delegation.prompt -->
- Required source files and the facts already checked from them. <!-- declaration: delegation.prompt -->
- Repository constraints, scope, and deliberate exclusions. <!-- declaration: delegation.prompt -->
- Declared outputs and their location outside the repository when applicable. <!-- declaration: delegation.prompt -->
- Exact test command, mutation, and the delivered claims the orchestrator's review will assert; no instruction to run `review.py` or `committee.py` on the worker's own diff. <!-- declaration: delegation.prompt -->
- Push, evidence hand-off (`evidence.md` and `test-output.txt`), recovery, and coordinator completion protocol; the orchestrator, not the worker, opens the pull request. <!-- declaration: delegation.prompt -->

Runner configuration remains structured plan data so the coordinator can validate and pass it as configuration. <!-- declaration: delegation.configuration -->

For both optional updates, use `swarm.py continue PLAN --state-dir DIR --unit OLD --new-id NEW --prompt-file FILE --head-ref RECOVERY_REF --repoint-dependents --tickets DRAFT` to preview a continuation. Omit either flag when its update is not needed. `--write` publishes it and updates the existing draft; repointing refuses dependents with recorded attempts. The draft copies the predecessor entry with `unit` and `continuation_of` adjusted; it does not file tracker issues or rewrite tracker relations. Plan and draft writes are sequential; after an interrupted draft write, rerun the same command to complete the draft update. <!-- declaration: delegation.continuation -->

A continuation names its recovery ref and completed evidence, while a retry receives a fresh attempt and repeats the unit. <!-- declaration: delegation.continuation, retry.boundary -->

## Detector ledger

Keep a private `detector-ledger.jsonl` beside the plan, one record per consequential finding, with lineage, attempt, round, detector, evidence, outcome and disposition. Detector is one of probe, property, review-r1, review-r2, review-r3, merge-verifier, ci or real-run. This observational ledger grants no authority. <!-- declaration: evidence.detector-ledger -->

## Checked review configuration

These observations track `skills/hanig-review-gate/reviewers.json` after the 2026-11-26 owner decision. <!-- declaration: evidence.checkable -->

`plan`: luna, kimi-k3.

`fast`: luna, haiku-5.5.

`standard`: terra, sonnet-5.5, kimi-k3.

`deep`: terra, sonnet-5.5, kimi-k3, sol, astra.

Owner decision 2026-10-28 replaces Luna with Terra in standard/deep; its price is unknown. Owner decision 2026-11-26 replaces kimi-k2.7-code (disabled) with Lor-served kimi-k3 in standard/deep, so the default profiles route only through Lor or the Claude/Codex subscriptions. Plan stays Luna/Kimi K3 (OpenAI/Moonshot). Escalation retains Luna and Haiku from fast, adds the standard seats, then Sol and Astra (2026-11-06: Sol now writes code; author exclusion applies). Declare authors with repeatable `--author PROVIDER/MODEL` for exclusion. Keep review identity stable; advance `--round N` with `--dispositions FILE` after round 1. <!-- declaration: review.panel-source -->

`committee`: deepseek-v4-pro, gemini-neon, sonnet-5.5.

`committee.py` starts with the one enabled reviewer whose profile list is exactly `["tiebreak"]`. It excludes declared authors and prior voters by canonical model maker and leaf. If the primary is excluded, it checks the ordered tiebreak reserves for an eligible model from a different maker than the primary. If none qualifies, the split goes to `OWNER`. See `reviewers.json` for current profiles and reserves.

Enabled price hints recorded per million tokens are luna input $0.2 and output $1.2; sonnet-5.5 input $2.0 and output $10.0; deepseek-v4-pro input $0.87 and output $1.74; sol input $2.0 and output $10.0; and gemini-neon input $3.0 and output $15.0. Astra has no `_cost` record. Haiku pricing is unmeasured because models.json has no Anthropic price list; Kimi K3 also has no measured price hint. <!-- declaration: review.cost -->

Panel reporting names actual answers and absences instead of describing configured membership as completed review. <!-- declaration: review.honesty -->

## Checked effort measurement

The recorded 2026-09-19 note used three samples per cell on a real 120-line diff at a 16000 cap. GLM-5.3 returned zero characters after using the full reasoning budget in 3 of 3 high samples and 2 of 3 low samples, while null returned a 4581-character review costing $0.016 in 3 of 3. Kimi-K2.7-Code used about 12k completion tokens at every effort; high cost 83 percent more and returned slightly less answer. This supports null for those two reviewers, not a general claim that null is best for every model. GLM-5.3 left the roster on 2026-09-28; the measurement stays as the evidence for Kimi-K2.7-Code's null. <!-- declaration: review.effort -->
