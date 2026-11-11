# Artifacts, cards and lineage

Names use lowercase [a-z0-9._-], at most 128 characters. final/latest/best/new/old,
vN and date tokens are rejected; use aliases. artifact_name(kind, *parts) builds
safe names for dataset/model/predictions/eval-results/code, with digest fallback. <!-- declaration: logging.conventions -->

log_dataset(wandb, run, name, source, summary=..., contents=..., intended_use=...)
and log_model(wandb, run, path, step=...) create cards and searchable metadata.
Cards show Produced by, redacted Command, Inputs, Contents, Intended use and
Caveats. Explain units, identifiers, splits and filters. <!-- declaration: logging.conventions -->
Metadata includes storage/file sizes, dataset counts/splits, model step/metrics/
arch/family, or prediction eval_set/n_rows/model. Launch secret_keys prune
metadata and model card metrics. Never put credentials in URIs or free text. <!-- declaration: logging.credentials -->

Sources above 2 GiB default to references; reference=True/False overrides. URI
sources use add_reference; file:// needs its mount, object URIs need caller <!-- declaration: logging.conventions -->
credentials. checksum=False fits immutable storage only; record the caveat. <!-- declaration: logging.conventions -->
Example caveat: shared-storage:/path/to/data requires its mounted storage. <!-- declaration: logging.conventions -->

use_input(run, "team/project/data-cells:v3") records a resolved qualified version.
Aliases warn. Atomic output_dir/wandb_inputs.json binds lineage to the id and
reloads on resume; without output_dir, inputs last only for the run object. <!-- declaration: logging.conventions -->
Disk reads create no edge. Parent checkpoints are inputs too. <!-- declaration: logging.conventions -->

latest means newest. Models get step-N and caller-selected best aliases.
sanity_checkpoint=True strips best; callbacks skip trainer.sanity_checking
before updating best. post_hoc=True labels selection after results in caveats.
Logging never judges convergence. Expect quotes the predeclared predicate. <!-- declaration: logging.boundary -->

Registry promotions and later card edits change shared state. Show before/after
and confirm first; never delete versions as cleanup. <!-- declaration: approval.shared-state -->
