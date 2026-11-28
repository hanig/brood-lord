---
name: brood-hillclimb
description: >-
  Hill-climb a metric on a frozen harness: pre-register each hypothesis, answer
  seven benchmark questions, keep or revert by a paired A/B noise screen.
---

# brood-hillclimb

## Host capability boundary

`scripts/hillclimb.py` needs Python 3.9+ only and no network; it refuses state
inside a Git worktree. Without Python, report no verdict; never invent one.

## Invocation

Set `HANIG_HILLCLIMB_DIR` to the loaded `SKILL.md` directory and run
`python3 "$HANIG_HILLCLIMB_DIR/scripts/hillclimb.py" init`, then per attempt
`hypothesize` before measuring and `record` after; `--help` lists flags.
Runs are `[{"arm": "A", "value": 812.0}, ...]` in run order (A is baseline);
the checklist answers each question plus `"errors": N`.

## The benchmark questions

1. **Why not double?** Name the limiter.
2. **Was it tuned?** Run every side with production settings.
3. **Did it break limits?** Check arithmetic, bandwidth and Amdahl limits.
4. **Did it error?** Count failures and non-successes; check output correctness.
5. **Does it reproduce?** 5+ alternating A/B runs per side; median, range, rank-sum test.
6. **Does it matter?** Report the end-to-end path share.
7. **Did it even happen?** Confirm the work ran inside the timed region.

## The paired A/B noise screen

`keep` needs 5+ runs per arm, strict alternation, median gain `>= min_effect`
and `> max(A) - min(A)`, exact two-sided Mann-Whitney U `p < 0.05`, no
guardrail regression and zero errors. Missing inputs or over 60 runs give
`inconclusive`; other failures `revert`. `decision.tsv` is append-only; its
SHA-256 chain catches accidental edits, not tampering.
