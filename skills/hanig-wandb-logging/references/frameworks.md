# Framework integration

In your own training code, `import wandb` and pass it explicitly to wbkit.

Read the section for the framework the code uses. The pattern is the same
everywhere: create the run yourself with `wbkit.start_run`, let the framework
attach to the existing run, and turn off the framework's own model upload so
checkpoints go through `wbkit.log_model` with a card.

## Raw PyTorch (single process or DDP)

`wbkit.start_run` returns a disabled run on nonzero global ranks, falling back
to `SLURM_PROCID` and then `LOCAL_RANK`, so every rank can call it.
Reduce metrics across ranks before logging; log on rank 0 only (the disabled <!-- declaration: logging.conventions -->
run makes `log_step` a no-op elsewhere).

## PyTorch Lightning

Lightning's `WandbLogger` reuses `wandb.run` when one exists.

```python
run = wbkit.start_run(wandb, ...)                    # rank 0 only, see note
logger = WandbLogger(log_model=False)         # attaches to run; no auto upload
trainer = L.Trainer(logger=logger, callbacks=[WbkitCheckpoint(run)], ...)
```

Lightning logs with its own `trainer/global_step`. Either pass
`step_metric="trainer/global_step"` to `wbkit.define_metrics`, or keep
`trainer/step` and log it from a callback. Metric names come from
`self.log("train/loss", ...)` in the LightningModule; namespace them there.

Rank note: under Lightning, the process rank is known only after the Trainer <!-- declaration: logging.conventions -->
starts. Use `rank_zero_only` or create the run in `setup()` on global rank 0.

Lightning's [save_checkpoint](https://lightning.ai/docs/pytorch/stable/optional-api/api/lightning.pytorch.trainer.trainer.Trainer) requires all ranks for distributed checkpointing. Broadcast the decision; only artifact logging is rank zero. <!-- declaration: logging.conventions -->

```python
class WbkitCheckpoint(L.Callback):
    def __init__(self, run, monitor="val/loss", mode="min"):
        self.run, self.monitor, self.mode, self.best = run, monitor, mode, None
    def on_validation_end(self, trainer, pl_module):
        val = trainer.callback_metrics.get(self.monitor)
        better = False
        if trainer.is_global_zero and val is not None:
            val = float(val)
            better = self.best is None or (val < self.best if self.mode == "min" else val > self.best)
        if trainer.strategy.broadcast(better):
            path = f"{trainer.default_root_dir}/best.ckpt"
            trainer.save_checkpoint(path)
            if trainer.is_global_zero:
                self.best = val
                wbkit.log_model(wandb, self.run, path, step=trainer.global_step,
                                metrics={self.monitor: val}, is_best=True)
```

## Hugging Face Trainer

`WandbCallback` only calls `wandb.init` when `wandb.run is None`, so start the <!-- declaration: logging.conventions -->
run first.

```python
os.environ["WANDB_LOG_MODEL"] = "false"       # no anonymous checkpoint uploads
run = wbkit.start_run(wandb, ..., summaries={"eval/loss": "min"})
wbkit.define_metrics(run, step_metric="train/global_step")
args = TrainingArguments(report_to=["wandb"], run_name=run.name, ...)
```

HF logs `train/*` and `eval/*` against `train/global_step`; use that as the
step metric rather than remapping keys. Log the final model with
`wbkit.log_model` after `trainer.train()`.

## Accelerate

```python
accelerator = Accelerator(log_with="wandb")
if accelerator.is_main_process:
    run = wbkit.start_run(wandb, ...)
accelerator.init_trackers(project, config=None)   # attaches to the existing run
```

Or skip `init_trackers` and call `wbkit.log_step` under
`if accelerator.is_main_process`.

## Sweeps

`wandb.agent` creates the run, so `wbkit.start_run` must not. Inside the <!-- declaration: logging.conventions -->
trial function, after `wandb.init()` returns:

```python
run = wandb.init()                                   # sweep supplies config
run.name = wbkit.run_name(model, data, dict(run.config), seed=run.config.get("seed"))
run.notes = f"H: sweep trial for {sweep_question}"
run.config.update({"provenance": wbkit.provenance(wandb)}, allow_val_change=True)
wbkit.define_metrics(run, summaries={"val/loss": "min"})
```

Use `job_type="sweep-trial"` and make the sweep's group the question being
swept.

## Slurm requeue and preemption

Pass `output_dir` to `start_run`: the run id is persisted to
`{output_dir}/wandb_run_id` and reused with `resume="allow"`, so a requeued job
continues the same run. Resume count and the new provenance go into
`summary.provenance/*`. Log `trainer/step` from the restored checkpoint step
so curves continue without gaps or duplicates.

On compute nodes without internet, set `WANDB_MODE=offline` and sync from a
login node with `wandb sync {output_dir}/wandb/offline-run-*`.
