# Framework integration

In project code, import wandb and pass it explicitly to wbkit. No framework
package is imported by wbkit. The project owns framework setup and checkpointing.

## Raw PyTorch or DDP

Call start_run on every rank; global RANK then SLURM_PROCID then LOCAL_RANK
select logging ownership. Nonzero ranks receive a disabled SDK run. Reduce
metrics across workers before rank-zero logging.

## Lightning and Hugging Face Trainer

Create the run on global rank zero after the framework establishes rank.
Attach the framework logger to that existing run. Use namespaced metrics and
log trainer/step from an optimizer-step callback for a common axis. If the
framework owns its axis, pass its metric to define_metrics; subsequent calls
retain explicit summary definitions and namespace bindings.

```python
# In caller-owned code, after run creation:
wbkit.define_metrics(run, summaries={"val/loss": "min", "val/score": "max"})
wbkit.log_step(run, optimizer_step, {"val/loss": loss})
```

## Accelerate

Create the run inside accelerator.is_main_process, then call wbkit.log_step
there with reduced metrics. This avoids a second tracker initialization.

## Sweeps

For SDK-created runs, call run_name and define_metrics on the existing run.
Sanitize custom config and notes with wbkit._redact before direct SDK writes.
Use job_type sweep-trial and a group naming the sweep question.

## Requeue and offline execution

Pass the same output_dir to start_run after requeue. It retains the first
launch config and id, and records each resume's provenance and count in summary.
Restore the training checkpoint independently; logging does not resume training. <!-- declaration: logging.conventions -->
Use WANDB_MODE=offline for smoke runs. A later caller-managed wandb sync uploads
local logs when authorized connectivity is available.
