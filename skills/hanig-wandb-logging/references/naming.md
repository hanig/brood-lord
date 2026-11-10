# Naming and structure

## Contents

1. Entity and project
2. Group and job_type
3. Run names and ids
4. Tags
5. Notes
6. Config layout
7. Metric keys
8. Tables and media

## 1. Entity and project

Use a team entity for anything other people will read. Registry links and team
visibility both require it; personal-entity runs must be re-logged to move. <!-- declaration: logging.conventions -->

A project groups runs sharing metric schema and plot axes in one table/workspace.

| Good | Why |
|---|---|
| `state3-pretrain` | one model family, one objective |
| `ecclip-binding` | one research program |
| `perturb-eval` | evals of many models on one benchmark, shared metrics |

| Bad | Problem |
|---|---|
| `person-experiments` | per person; mixes metric schemas |
| `state3-nov2026` | dates belong in groups |
| `state3-lr-sweep` | a sweep is a group, not a project |
| `test`, `debug`, `my-project` | unfindable; use `job_type="debug"` instead |

Split a project only when metric schemas diverge or access must differ. <!-- declaration: logging.conventions -->
Segment inside a project with group, job_type, tags and config.

## 2. Group and job_type

A group is one question: ablation, seed replicates, sweep, or one pipeline invocation.

Format: `YYYYMMDD-{slug}`, e.g. `20261109-mask-ratio-ablation`. Fix the date
when posing the question; later reruns stay in the same group.

`job_type` vocabulary (the workspace can then split panels by it):

| job_type | Use |
|---|---|
| `preprocess` | builds a dataset artifact |
| `train` | trains from scratch |
| `finetune` | starts from a parent checkpoint (record it as an input) |
| `eval` | scores a fixed model on a fixed eval set |
| `predict` | writes predictions without scoring |
| `sweep-trial` | one trial of a W&B or Optuna sweep |
| `debug` | smoke tests; filter these out of every real view |

## 3. Run names and ids

`{model}-{data}-{deltas}-s{seed}`, at most 64 characters.

- `model`: architecture or family short name (`state3`, `esm2-650m`).
- `data`: training data short name (`tahoe100m`, `replogle-k562`).
- `deltas`: only the hyperparameters that differ from the group's base config, <!-- declaration: logging.conventions -->
  abbreviated (`lr3e-4`, `wd0.1`, `mask0.3`, `L24`). A base-config run has none.
- `s{seed}`: always, so seed replicates are distinguishable at a glance. <!-- declaration: logging.conventions -->

Examples: `state3-tahoe100m-s0` (baseline), `state3-tahoe100m-mask0.3-s1`,
`esm2-650m-ecclip-lr1e-5-ep20-s0`.

Names are labels; the run id is identity. Keep W&B's generated id unless the
run must resume under a new Slurm job, in which case persist it to <!-- declaration: logging.conventions -->
`{output_dir}/wandb_run_id` (what `wbkit.start_run(wandb, output_dir=...)` does) and
pass `resume="allow"`.

## 4. Tags

Use a small controlled vocabulary for filtering.

- Role: `baseline`, `ablation`, `candidate`, `debug`, `broken`, `dirty-tree`.
- Facets with a prefix: `data:{name}`, `arch:{name}`, `cluster:{name}`.

Do not tag with values already in config (lr, batch size): filter on config. <!-- declaration: logging.conventions -->
Tag `broken` instead of deleting a failed run; failures are information.

## 5. Notes

Notes are the only field that says why the run exists. Structure: <!-- declaration: logging.conventions -->

```
H: <one-sentence hypothesis or purpose>
Expect: <the metric and direction or threshold that would support it>
Deltas vs base: <auto-filled by wbkit>
Outcome (<supported|refuted|inconclusive|broken>): <one or two sentences with numbers>
```

For a baseline the hypothesis is the purpose: "H: reference run for the
mask-ratio ablation at the current default config."

`summary.verdict` mirrors the outcome so it can be filtered and sorted.

## 6. Config layout

```python
{
  "family": "state3",                       # added by wbkit
  "arch":  {"d_model": 512, "n_layers": 12, "n_heads": 8, ...},
  "train": {"lr": 3e-4, "wd": 0.1, "batch_size": 256, "max_steps": 40000,
            "seed": 0, "precision": "bf16-mixed", ...},
  "data":  {"train": "arc-ml/state3-pretrain/tahoe100m-hvg2k:v3",
            "val":   "arc-ml/state3-pretrain/tahoe100m-hvg2k-val:v3",
            "mixture": {"arc_ccl": 0.27, "tahoe": 0.73}},
  "provenance": {"git_commit": ..., "git_dirty": ..., "command": ...,
                 "host": ..., "slurm": {...}, ...}   # added by wbkit
}
```

This matches the Arc Bench Training contract: `architecture.config`,
`training.config` including seed, `training_data` splits/mixture shares,
`run.commit` and `run.repository`, making later export mechanical.

Hydra or OmegaConf users: `OmegaConf.to_container(cfg, resolve=True)` then map
groups onto these sections; do not log the unresolved config. <!-- declaration: logging.conventions -->

Filter on nested keys with dots: `config.train.lr`, `config.arch.n_layers`.

## 7. Metric keys

`{namespace}/{name}[_{unit}]`, lowercase snake after the slash.

| Namespace | Contents |
|---|---|
| `train/` | per-step training loss and auxiliary losses |
| `val/` | periodic validation on held-out data |
| `eval/` | HF Trainer's validation prefix; accept it in HF projects rather than remap |
| `test/` | final held-out test, logged once |
| `opt/` | `lr`, `grad_norm`, `loss_scale`, `weight_norm` |
| `perf/` | `step_time_s`, `samples_per_s`, `tokens_per_s`, `gpu_mem_gb`, `mfu` |
| `data/` | `epoch`, `samples_seen`, per-source sampling counts |
| `trainer/` | `step` (the shared x-axis) |

All namespaces plot against `trainer/step` via `define_metric("val/*",
step_metric="trainer/step")`. Log `trainer/step` with every call instead of
passing `step=`, which W&B requires to be monotonic per process and silently <!-- declaration: logging.conventions -->
drops on violation after a resume.

Declare best-value summaries: `define_metric("val/loss", summary="min")`.
Otherwise the table shows the last value, potentially the worst after overfitting.

Per-subset metrics: `val/pearson_delta/k562`, `val/pearson_delta/hepg2`. Panels
group on the prefix.

## 8. Tables and media

Log a small, fixed sample of predictions as a `wandb.Table` each eval (same
row ids every time, so rows can be compared across steps). Log full predictions
once, at the end, as a `predictions` artifact (`wbkit.log_predictions`) so they
can be joined across runs. Images and plots: `val/umap`, `val/confusion`,
under the same namespace as their metric.
