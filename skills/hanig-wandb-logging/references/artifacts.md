# Artifacts, cards and lineage

Logical names use lowercase [a-z0-9._-], at most 128 characters. Status tokens
final/latest/best/new/old, vN and date components are rejected. Use aliases for dates/status. <!-- declaration: logging.conventions -->
artifact_name(kind, *parts) builds dataset, model, predictions, eval-results and
code names, with an opaque stable digest for unsafe parts. <!-- declaration: logging.conventions -->

log_dataset(wandb, run, name, source, summary=..., contents=...,
intended_use=...) accepts files, directories and URI references.
log_model(wandb, run, path, step=...) adds run identity, architecture and metrics.
Cards contain summary, Produced by (link, job type, commit), Command (redacted
identity), Inputs, Contents, Intended use and Caveats. Explain units, transforms,
identifier namespaces, splits and filters. <!-- declaration: logging.conventions -->

Searchable metadata includes source_uri/storage/n_files/total_bytes, dataset
n_samples/n_features/splits/genome_build, model step/metrics/arch/family/run_name,
or prediction eval_set/n_rows/columns/model. Launch secret_keys apply to metadata;
model metrics are pruned before cards. No free-text scanning occurs. Never put credentials in URIs or text. <!-- declaration: logging.credentials -->

Local sources above 2 GiB default to references; reference=True/False overrides.
URI sources always use add_reference. file:// requires the same mounted storage;
object URIs require caller credentials. checksum=False fits immutable storage
only; record the caveat. Example: shared-storage:/path/to/data requires its mount. <!-- declaration: logging.conventions -->

use_input(run, "team/project/data-cells:v3") records the resolved qualified
version. Unpinned aliases warn. Atomic output_dir/wandb_inputs.json is bound to
the run id and reloaded on resume. Without output_dir, lineage lasts only for
the run object's lifetime. Direct disk reads create no edge. Parent checkpoints are inputs too. <!-- declaration: logging.conventions -->

latest means newest. Model aliases include step-N and caller-selected best.
sanity_checkpoint=True strips best even from explicit aliases; a Lightning
callback skips trainer.sanity_checking before updating its own best value.
post_hoc=True labels selection after results in caveats and metadata. No logging
helper judges convergence; Expect quotes the predicate declared before the run. <!-- declaration: logging.boundary -->

Registry promotions and later description/metadata edits change shared state.
Show exact before/after and confirm first; never delete versions as cleanup. <!-- declaration: approval.shared-state -->
