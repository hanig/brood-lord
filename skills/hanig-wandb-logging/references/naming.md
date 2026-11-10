# Naming and structure

Use a team entity and one project per metric schema. Recommend kebab-case;
underscore/period names pass through. Group one question under YYYYMMDD-question.

Names follow {model}-{data}-{deltas}-s{seed}, default bound 64 characters.
Only changed hyperparameters appear as deltas. <!-- declaration: logging.conventions -->
Names are labels; ids are identity.

```text
H: hypothesis or baseline purpose
Expect: val/loss < 0.5
Deltas vs base: train.lr=0.0003
Outcome (supported|refuted|inconclusive|broken): caller-supplied contract result
```

Expect quotes the predeclared predicate. Short hypotheses generate advice.

Nested family/arch/train/data/provenance maps to Arc Bench Training:
architecture.config, training.config with seed, training_data splits/mixtures,
run.commit and run.repository. Provenance includes Git/dirty state, command,
runtime versions, host and scheduler. Resolve Hydra/OmegaConf in the caller.

Use train/loss, val/loss, eval/loss, opt/lr, perf/samples_per_s,
perf/gpu_mem_gb and data/samples_seen. All namespaces use trainer/step.
Log the restored optimizer step with every call instead of SDK step=,
which can drop points after resume. Declare min/max summaries for best values.
