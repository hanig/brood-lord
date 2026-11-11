# Naming

Use a team entity and a project per metric schema. Kebab-case is advice;
underscore/period names pass. Group one question under YYYYMMDD-question.
Model-data-deltas-s{seed} labels default to 64 characters; ids carry identity.
Only changed hyperparameters appear as deltas. <!-- declaration: logging.conventions -->

The sanitizer masks labeled credentials and token-value fields in prose.
Tokenizer fields such as pad_token and eos_token remain public configuration.

```text
H: hypothesis or baseline purpose
Expect: val/loss < 0.5
Deltas vs base: train.lr=0.0003
Outcome (supported|refuted|inconclusive|broken): caller's contract result
```

Family/arch/train/data/provenance matches Arc Bench Training architecture.config,
training.config/seed, training_data splits/mixtures and run.commit/repository.
Provenance adds Git/dirty, versions and host/scheduler. Commands contain only <!-- declaration: logging.credentials -->
program basename, flag names, positional count and a SHA-256 digest of NUL-joined
UTF-8 argv. Long flags drop =values; short flags keep two characters, including
bundles. Lone - and -- stay. After the first --, tokens are counted and omitted;
negative tokens accepted by float() are counted and omitted. Option-shaped values
before -- follow the flag rule, since no caller option parser is available.
Legacy argv strings become current-launch command metadata with a warning.
Resolve Hydra/OmegaConf.

Use train/loss, val/loss, eval/loss, opt/lr, perf/samples_per_s, perf/gpu_mem_gb
and data/samples_seen on trainer/step. Log the restored optimizer step instead
of SDK step=, which can drop points after resume. Declare min/max summaries.
