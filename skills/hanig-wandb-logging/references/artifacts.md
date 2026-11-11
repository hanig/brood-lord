# Artifacts

artifact_name(kind, *parts) builds logical dataset/model/predictions/eval-results/code names with safe digest fallback. <!-- declaration: logging.conventions -->
Names: lowercase [a-z0-9._-], 128 chars max; no status, vN or dates. Use aliases. <!-- declaration: logging.conventions -->
log_dataset(wandb,run,name,source,summary=...,contents=...,intended_use=...) and log_model(wandb,run,path,step=...) create cards/metadata. <!-- declaration: logging.conventions -->
Cards: Produced by, redacted Command, Inputs, Contents, Intended use, Caveats. Give units/splits. <!-- declaration: logging.conventions -->
Metadata: storage/sizes, dataset counts/splits, model step/metrics/arch/family, prediction eval_set/n_rows/model. Launch secret_keys prune metadata/card metrics; no credentials in URIs/text. <!-- declaration: logging.credentials -->
Local sources above 2 GiB default to references; reference=True/False overrides. URIs use add_reference. file:// needs its mount; object URIs need credentials. checksum=False fits immutable storage; record caveats such as shared-storage:/path/to/data requires its mount. <!-- declaration: logging.conventions -->
use_input(run,"team/project/data-cells:v3") resolves versions, warns on aliases, atomically saves output_dir/wandb_inputs.json bound to id, and reloads on resume. Without output_dir, inputs last for the run object only. Disk reads create no edge; parent checkpoints are inputs. <!-- declaration: logging.conventions -->
latest means newest; models get step-N and caller-selected best. <!-- declaration: logging.conventions -->
sanity_checkpoint=True strips best; callbacks skip trainer.sanity_checking. post_hoc=True labels choices after results. Logging never judges convergence; Expect quotes the predeclared predicate. <!-- declaration: logging.boundary -->
Registry promotions/card edits change shared state: show before/after and confirm. Never delete versions as cleanup. <!-- declaration: approval.shared-state -->
