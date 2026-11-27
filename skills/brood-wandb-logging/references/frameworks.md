# Frameworks

Caller: `import wandb; wbkit.start_run(wandb, **run_args)`.

- Torch/DDP: reduce first; start per rank (RANK > SLURM_PROCID > LOCAL_RANK); nonzero disabled.
- Lightning/HF: after rank setup, start on global zero; attach logger; namespaced
  metrics on trainer/step optimizer callbacks or define_metrics framework axis
  (retains summaries/namespaces).
- Accelerate: reduce; start/log in is_main_process.
- Sweeps: SDK run; run_name/define_metrics; wbkit._redact on direct writes;
  job_type=sweep-trial; group=question.

Requeue: reuse output_dir; restore training separately. <!-- declaration: logging.conventions -->
WANDB_MODE=offline; authorized sync.
