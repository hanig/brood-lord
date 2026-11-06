# Delegation, evidence, and checked review facts

The initial unit owns its issue through the pull request rather than handing review triage back to the orchestrator. <!-- declaration: delegation.whole-loop -->

## Prompt construction

A complete delegated prompt contains:

- Objective and acceptance criteria. <!-- declaration: delegation.prompt -->
- Required source files and the facts already checked from them. <!-- declaration: delegation.prompt -->
- Repository constraints, scope, and deliberate exclusions. <!-- declaration: delegation.prompt -->
- Declared outputs and their location outside the repository when applicable. <!-- declaration: delegation.prompt -->
- Exact test command, mutation, review command, and delivered claims. <!-- declaration: delegation.prompt -->
- Push, pull-request, recovery, and coordinator completion protocol. <!-- declaration: delegation.prompt -->

Runner configuration remains structured plan data so the coordinator can validate and pass it as configuration. <!-- declaration: delegation.configuration -->

For both optional updates, use `swarm.py continue PLAN --state-dir DIR --unit OLD --new-id NEW --prompt-file FILE --head-ref RECOVERY_REF --repoint-dependents --tickets DRAFT` to preview a continuation. Omit either flag when its update is not needed. `--write` publishes it and updates the existing draft; repointing refuses dependents with recorded attempts. The draft copies the predecessor entry with `unit` and `continuation_of` adjusted; it does not file tracker issues or rewrite tracker relations. Plan and draft writes are sequential; after an interrupted draft write, rerun the same command to complete the draft update. <!-- declaration: delegation.continuation -->

A continuation names its recovery ref and completed evidence, while a retry receives a fresh attempt and repeats the unit. <!-- declaration: delegation.continuation, retry.boundary -->

## Detector ledger

Keep a private `detector-ledger.jsonl` beside the plan, one record per consequential finding, with lineage, attempt, round, detector, evidence, outcome and disposition. Detector is one of probe, property, review-r1, review-r2, review-r3, merge-verifier, ci or real-run. This observational ledger grants no authority. <!-- declaration: evidence.detector-ledger -->

## Checked review configuration

These observations track `skills/hanig-review-gate/reviewers.json` after the 2026-10-28 owner decision. <!-- declaration: evidence.checkable -->

`plan`: luna, kimi-k3.

`fast`: luna, haiku-5.5.

`standard`: terra, sonnet-5.5, kimi-k2.7-code.

`deep`: terra, sonnet-5.5, kimi-k2.7-code, sol, astra.

Owner decision 2026-10-28 replaces Luna with Terra in standard/deep; its price is unknown. Plan stays Luna/Kimi K3 (OpenAI/Moonshot). Escalation retains Luna and Haiku from fast, adds the standard seats, then Sol and Astra (2026-11-06: Sol now writes code; author exclusion applies). Declare authors with repeatable `--author PROVIDER/MODEL` for exclusion. Keep review identity stable; advance `--round N` with `--dispositions FILE` after round 1. <!-- declaration: review.panel-source -->

`committee`: deepseek-v4-pro, gemini-argon, sonnet-5.5.

`committee.py` picks the tie-breaker from reviewers.json by profile: the one enabled reviewer whose profile list is exactly `["tiebreak"]`. That is astra-tiebreak since 2026-11-06. Sol now writes code, and an author does not review or tiebreak its own work; sol-tiebreak is disabled. Planning is gemini-argon, muse-wonder and terra, with a tiebreak seat independent of its members.

Enabled price hints recorded per million tokens are luna input $0.2 and output $1.2; kimi-k2.7-code input $0.67 and output $3.4; sonnet-5.5 input $2.0 and output $10.0; deepseek-v4-pro input $0.87 and output $1.74; sol input $2.0 and output $10.0; and gemini-argon input $2.0 and output $10.0. Astra has no `_cost` record. Haiku pricing is unmeasured because models.json has no Anthropic price list; Kimi K3 also has no measured price hint. <!-- declaration: review.cost -->

Panel reporting names actual answers and absences instead of describing configured membership as completed review. <!-- declaration: review.honesty -->

## Checked effort measurement

The recorded 2026-09-19 note used three samples per cell on a real 120-line diff at a 16000 cap. GLM-5.3 returned zero characters after using the full reasoning budget in 3 of 3 high samples and 2 of 3 low samples, while null returned a 4581-character review costing $0.016 in 3 of 3. Kimi-K2.7-Code used about 12k completion tokens at every effort; high cost 83 percent more and returned slightly less answer. This supports null for those two reviewers, not a general claim that null is best for every model. GLM-5.3 left the roster on 2026-09-28; the measurement stays as the evidence for Kimi-K2.7-Code's null. <!-- declaration: review.effort -->
