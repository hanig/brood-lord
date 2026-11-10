# Naming and structure

Use a team entity and one project for runs with a shared metric schema.
Kebab-case projects are recommended; underscore and period names pass through.
Group runs answering one question under a stable `YYYYMMDD-question` group.

Names follow `{model}-{data}-{deltas}-s{seed}`, at most 64 characters by default.
Only changed hyperparameters from the base config appear in deltas. <!-- declaration: logging.conventions -->
Use role tags such as baseline, ablation, debug and broken. Keep numeric
hyperparameters in config rather than tags. Run names are labels; ids are identity.

Notes follow this template. The Expect line quotes the predeclared predicate.

```text
H: one sentence stating the hypothesis or baseline purpose
Expect: val/loss < 0.5
Deltas vs base: train.lr=0.0003
Outcome (supported|refuted|inconclusive|broken): caller-supplied contract result
```

A full sentence is recommended; shorter notes produce advice, not exceptions.

```python
config = {
    "family": "sample",
    "arch": {"width": 512, "layers": 12},
    "train": {"lr": 0.0003, "seed": 0, "max_steps": 40000},
    "data": {"train": "cells-train", "val": "cells-val", "mixture": {"cells": 1.0}},
    "provenance": {"git_commit": "...", "git_remote": "...", "command": "..."}
}
```

This matches the Arc Bench Training contract: architecture.config,
training.config including seed, training_data splits and mixture shares,
run.commit and run.repository. wbkit adds family and launch provenance with
Git commit/dirty state, command, runtime versions, host and scheduler identity.
Resolve Hydra/OmegaConf containers in the caller before passing them in.

Metrics use lowercase namespaced keys: train/loss, val/loss, eval/loss,
opt/lr, perf/samples_per_s, perf/gpu_mem_gb and data/samples_seen.
All namespaces use trainer/step. Log the restored optimizer step with each
call instead of SDK step=, which can drop points after resume.
Declare min/max summaries up front for the best value rather than the last.
