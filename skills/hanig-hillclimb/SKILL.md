---
name: hanig-hillclimb
description: >-
  Hill-climb a performance metric against a frozen harness: pre-register one
  hypothesis at a time, answer the seven benchmark questions, and keep or
  revert each change by a paired A/B noise screen logged to an append-only ledger.
disable-model-invocation: true
---

# hanig-hillclimb

Use this when a change is meant to move a measured number (throughput,
latency, loss, memory) and each attempt must end in an auditable keep, revert
or inconclusive verdict. The harness is frozen before the first hypothesis, so
no attempt can win by changing what is measured.

## Host capability boundary

`scripts/hillclimb.py` needs only Python 3.9+ and the standard library. It
runs no benchmark, reads no credential and reaches no network: the caller runs
the harness and hands over the runs as JSON. Its state directory must sit
outside every Git worktree (the script walks parents for `.git` and refuses).
If the host cannot run Python, record runs by hand and report that no ledger
verdict exists; never invent one.

## Invocation

Set `HANIG_HILLCLIMB_DIR` to the directory containing the `SKILL.md` instance
actually loaded, and keep the state directory outside the repository:

```sh
HC="$HANIG_HILLCLIMB_DIR/scripts/hillclimb.py"
python3 "$HC" init --state-dir "$STATE" --harness bench.py bench.cfg \
  --metric qps --direction higher --min-effect 50 --guardrail 'p99_ms:<=:40'
python3 "$HC" hypothesize --state-dir "$STATE" --id h1 \
  --hypothesis "batch fsync per 64 rows" --expected-limiter "fsync per row"
python3 "$HC" record --state-dir "$STATE" --id h1 --runs-json runs.json \
  --checklist-json checklist.json --guardrails-json guardrails.json
python3 "$HC" verify-harness-frozen --state-dir "$STATE"
python3 "$HC" verify-log --state-dir "$STATE"
python3 "$HC" summary --state-dir "$STATE" --json
```

`init` stores a SHA-256 digest of every harness file in `harness.lock.json`
and starts `decision.tsv`. `hypothesize` and `record` refuse (exit 2) on any
harness drift, deletion or unreadable file. Only one hypothesis may be open;
`hypothesize` writes it before any measurement, so the expected limiter and
`--min-effect` cannot be chosen after seeing the result. IDs are never reused.

## Inputs

`runs.json` lists the runs in execution order, arm A the baseline and arm B
the candidate: `[{"arm": "A", "value": 812.0}, {"arm": "B", "value": 901.5}, ...]`.
A run may carry `"errors": N`. `guardrails.json` maps each declared guardrail
to the candidate's observed value. `checklist.json` maps each question title
below to a non-empty answer drawn from run evidence, plus `"errors": N`, the
failure count from question 4.

## The benchmark questions

From the pstack benchmark checklist; answer each before `record`:

1. **Why not double?** Name the limiter.
2. **Was it tuned?** Run every side with production settings.
3. **Did it break limits?** Check arithmetic, bandwidth and Amdahl limits.
4. **Did it error?** Count failures and non-successes; check outputs are correct.
5. **Does it reproduce?** At least 5 alternating A/B runs per side, median and range, rank-sum test when close.
6. **Does it matter?** Report the share of the end-to-end path.
7. **Did it even happen?** Confirm the work ran inside the timed region.

## The paired A/B noise screen

`keep` requires all of:

1. at least 5 runs in each arm;
2. strict alternation (`A, B, A, B, ...` or `B, A, B, A, ...`);
3. median improvement in the declared direction `>= min_effect`;
4. median improvement `>` the baseline range `max(A) - min(A)`;
5. exact two-sided Mann-Whitney U (rank-sum) permutation `p < 0.05`, midranks for ties;
6. no guardrail regression and zero errors across checklist and runs.

Too few runs, broken alternation, a missing error count or guardrail value, or
more than 60 total runs (the exact-enumeration bound) give `inconclusive`. A
valid screen that fails conditions 3-6 gives `revert`. The screen says the gap
cleared run-to-run noise on this host; it does not prove the mechanism. Report
the verdict with the median, range, run count and the limiter from question 1.

## Ledger

`decision.tsv` opens with `schema="hanig_hillclimb_decision.v1"` and is
appended with `O_APPEND`; earlier bytes never change. Each row carries a
SHA-256 chain over the previous row, and `state.json` records the digest of the
last written prefix. `verify-log` exits 1 on an edited header or row, or a
truncated tail. This is accidental-edit evidence, not cryptographic
tamper-proofing: a same-UID writer can recompute both.
