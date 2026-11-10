# Frameworks

- PyTorch/DDP: call start_run per rank (RANK, SLURM_PROCID, LOCAL_RANK priority).
  Nonzero ranks get disabled runs. Reduce metrics before rank-zero logging.
- Lightning/HF: initialize after rank setup on global zero; attach the logger
  to that run. Log trainer/step in an optimizer callback, or pass the framework's
  axis to define_metrics; rebinding retains summaries/namespaces.
- Accelerate: create/log inside is_main_process with reduced metrics.
- Sweeps: use the existing SDK run, run_name/define_metrics and sanitized direct
  writes via wbkit._redact. Use job_type sweep-trial and a question group.

Reuse output_dir on requeue. Logging does not resume training. <!-- declaration: logging.conventions -->
Smoke-test with WANDB_MODE=offline; sync later with authorized access.
