# Naming

Use team entity/project per metric schema; kebab-case is advice.
Group YYYYMMDD-question. Default names: model-data-deltas-s{seed}, 64 chars.
Only changed hyperparameters appear as deltas. <!-- declaration: logging.conventions -->


```text
H: hypothesis or baseline purpose
Expect: val/loss < 0.5
Deltas vs base: train.lr=0.0003
Outcome (supported|refuted|inconclusive|broken): caller's contract result
```

Arc Bench: family/arch/train/data/provenance maps to architecture.config,
training.config/seed, training_data and run.commit/repository.
Provenance adds Git/dirty, versions and host/scheduler. Commands contain only <!-- declaration: logging.credentials -->
program basename, flag names and positional count. No argv digest is recorded. Long flags drop =values; short flags keep two characters, including
bundles. Lone - and -- stay. After the first --, tokens are counted and omitted;
negative tokens accepted by float() are counted and omitted. Option-shaped values
before -- follow the flag rule, without a caller parser.
Legacy argv strings and digest records become current-launch command metadata
with a warning.

Use train/loss, val/loss, eval/loss, opt/lr, perf/samples_per_s, perf/gpu_mem_gb
and data/samples_seen on trainer/step. Log restored optimizer steps, not SDK step= (may drop resumed points).
Declare min/max summaries.
