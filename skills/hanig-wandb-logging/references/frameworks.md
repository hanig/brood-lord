# Frameworks

`import wandb; wbkit.start_run(wandb, **run_args)`.

- Torch/DDP: start per rank (RANK > SLURM_PROCID > LOCAL_RANK); reduce first.
  Nonzero ranks: disabled.
- Lightning/HF: start on global zero after rank setup; attach logger.
  Use namespaced metrics, trainer/step optimizer callbacks or framework axis
  via define_metrics (keeps summaries/namespaces).
- Accelerate: reduce; start/log in is_main_process.
- Sweeps: SDK run; run_name/define_metrics; wbkit._redact on direct writes;
  job_type=sweep-trial; group=question.

Requeue: reuse output_dir; restore training separately. <!-- declaration: logging.conventions -->
WANDB_MODE=offline; authorized sync.
