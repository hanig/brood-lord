# Framework integration

Import wandb in project code; pass it to wbkit.

## PyTorch / DDP

Call start_run on every rank. Global RANK then SLURM_PROCID then LOCAL_RANK
select ownership; nonzero ranks get disabled runs. Reduce metrics before logging.

## Lightning / Hugging Face Trainer

Create the run after rank initialization, on global rank zero. Attach the logger
to it. Log trainer/step from an optimizer callback. For a framework-owned axis,
pass it to define_metrics; rebinding retains summaries and namespaces.

## Accelerate / sweeps

Accelerate: log reduced metrics in accelerator.is_main_process. For sweeps,
apply run_name/define_metrics to the existing run; sanitize direct writes with
wbkit._redact. Use job_type sweep-trial and a group for the question.

## Requeues

Reuse output_dir for the same logical run. Restore the checkpoint separately;
logging does not resume training. <!-- declaration: logging.conventions -->
Use WANDB_MODE=offline for smoke checks, then caller-managed wandb sync
when authorized connectivity is available.
