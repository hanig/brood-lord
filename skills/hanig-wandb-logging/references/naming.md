# Naming

Team entity/project follows metric schema; kebab-case is advice.
Group: YYYYMMDD-question; names: model-data-deltas-s{seed}, 64 chars.
Deltas are changed hyperparameters. <!-- declaration: logging.conventions -->


```text
H: hypothesis or baseline purpose
Expect: val/loss < 0.5
Deltas vs base: train.lr=0.0003
Outcome (supported|refuted|inconclusive|broken): caller's contract result
```

Arc Bench family/arch/train/data/provenance maps to architecture.config,
training.config/seed, training_data and run.commit/repository.
Provenance: Git/dirty, versions, host/scheduler. <!-- declaration: logging.credentials -->
Commands: program basename, flags, positional count; no digest. Long flags drop
=values; short/bundled flags keep two characters. Keep lone -/--; count/omit
post-- and negative float() tokens. Option-shaped values before -- are flags.
Legacy command strings/digests become current metadata with a warning.

Use train/loss, val/loss, eval/loss, opt/lr, perf/samples_per_s,
perf/gpu_mem_gb, data/samples_seen on trainer/step; declare min/max summaries.
Log restored optimizer steps; SDK step= may drop resumed points.
