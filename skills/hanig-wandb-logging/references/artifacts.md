# Artifacts: names, types, cards, metadata, lineage

Read this when logging datasets, checkpoints, predictions or eval results, or
when writing or fixing artifact descriptions.

## Types

Five, so the lineage graph and Registry filters stay legible:

| type | What | Typical producer |
|---|---|---|
| `dataset` | raw or processed data, splits | `preprocess` |
| `model` | checkpoints and final weights | `train`, `finetune` |
| `predictions` | per-example model outputs | `eval`, `predict` |
| `eval-results` | aggregated scores, per-subset tables | `eval` |
| `code` | a frozen config bundle or script snapshot, when git is not enough | any |

## Names

An artifact name is the logical asset. W&B versions it (`v0`, `v1`, ...), so
the name must not. <!-- declaration: logging.conventions -->

| Asset | Pattern | Example |
|---|---|---|
| dataset | `{source}-{processing}[-{split}]` | `tahoe100m-hvg2k-log1p`, `tahoe100m-hvg2k-val` |
| checkpoints of one run | `ckpt-{run_id}` | `ckpt-3k9x1qzt` |
| a model line you promote | `{family}-{variant}` | `state3-base` |
| predictions | `preds-{run_id}-{eval_set}` | `preds-3k9x1qzt-replogle-k562` |
| eval results | `evals-{run_id}-{benchmark}` | `evals-3k9x1qzt-perturb-bench` |

Lowercase, `[a-z0-9._-]`. Never `final`, `latest`, `best`, `new`, `v2` or a <!-- declaration: logging.conventions -->
date in the name: those are aliases or versions. A changed preprocessing recipe
is a new version of the same name if it is the same logical asset; a new name
only when consumers should not silently pick it up. <!-- declaration: logging.conventions -->

## The description card

The description renders as markdown in the UI and is the first thing a
consumer reads. Every artifact gets these fields (`wbkit.artifact_card` writes
them):

```
<One sentence: what this is, in domain terms.>

**Produced by:** [run name](url), job_type `preprocess`, commit `a1b2c3d4e5`
**Command:** `python prep.py --hvg 2000 --log1p`
**Inputs:** `arc-ml/state3-pretrain/tahoe100m-raw:v1`
**Contents:** 3 h5ad shards; 95.6M cells x 2,000 HVGs; X is log1p(CP10k);
  obs has cell_line, drug, dose_um, plate; genome GRCh38, Ensembl 110 gene ids.
**Intended use:** training and validation for state3 pretraining; split by
  plate, so do not re-split by cell.
**Caveats:** file:// reference on <storage-host>:/path/to/data; resolvable only where
  that path is mounted. 14 plates with failed QC removed (list in metadata).
```

What makes a card informative is what a consumer cannot infer: units and <!-- declaration: logging.conventions -->
transforms, identifier namespaces (gene id type, genome build), how the split
was made and what must not be re-split, what was filtered out, and where the <!-- declaration: logging.conventions -->
bytes physically live. Avoid restating the name.

For checkpoints, `wbkit.log_model` fills the card from the run (step, metrics
at save, file sizes). Add caveats when they exist: "EMA weights only", <!-- declaration: logging.conventions -->
"optimizer state not included", "trained with a data bug fixed in v4".

For a generated checkpoint name that violates these rules, wbkit uses a stable
`ckpt-id{digest}` name and records the original `run_id` in metadata. <!-- declaration: logging.conventions -->

## Metadata

Metadata is for machines: filters, sorting, and later export. Flat or shallow,
JSON-serializable, stable keys across versions.

- Datasets: `n_samples`, `n_features`, `splits` (counts per split),
  `source_uri`, `storage` (`upload`/`reference`), `n_files`, `total_bytes`,
  preprocessing params (`hvg`, `normalization`), `genome_build`, `gene_id_type`,
  `filtered` (what and how many).
- Models: `step`, `epoch`, `metrics` at save, `arch` (copied from config),
  `family`, `run_name`, `params_count`, `precision`, `parent_checkpoint`.
- Predictions and eval results: `eval_set`, `n_rows`, `columns`,
  `model` (the qualified model artifact ref), headline scores.

## Upload or reference

Upload when the bytes are small (under a few GB) or must be portable off the <!-- declaration: logging.conventions -->
cluster. Reference (`add_reference`) when the data lives on shared storage or
object storage: W&B records URIs and checksums, not bytes. `wbkit` switches to
a reference above 2 GiB.

- `s3://`, `gs://`, `az://` references resolve anywhere with credentials.
- `file://` references resolve only on hosts that mount the path. Say which <!-- declaration: logging.conventions -->
  cluster and path in the card's caveats.
- Checksumming millions of files is slow. Pass `checksum=False` only for <!-- declaration: logging.conventions -->
  immutable storage, and say so in the caveats.

## Aliases

- `latest` is automatic and means most recently logged. It is never "best". <!-- declaration: logging.conventions -->
- `best`, `step-{N}` for checkpoints (`wbkit.log_model` sets them).
- `candidate`, `staging`, `production` are lifecycle aliases in a Registry
  collection. Moving `production` changes what downstream consumers load:
  confirm first.

## Lineage

Every input goes through `run.use_artifact("{entity}/{project}/{name}:vN")`
(or `wbkit.use_input`, which also records it for the card). Reading a file
directly from disk leaves no edge, and the lineage graph then says the model
came from nowhere. Pin `:vN` in config; resolving an alias at run time is fine
only when the run's purpose is "evaluate whatever is in production", and the <!-- declaration: logging.conventions -->
resolved version is still recorded.

For a finetune, the parent checkpoint is an input too.

## Registry promotion

Link only versions that passed a stated check, into an existing collection <!-- declaration: logging.conventions -->
named for the durable task (`wandb-registry-model/state3-perturbation`).
`link_artifact` auto-creates a collection on a typo, so read exact names first.
Details of Registry paths and allowlists are in the official `wandb/skills`
`ARTIFACTS_AND_REGISTRY.md`.

## Fixing descriptions after the fact

Artifact descriptions and metadata are editable on a logged version:

```python
art = wandb.Api().artifact("arc-ml/state3-pretrain/tahoe100m-hvg2k:v3")
art.description = new_card
art.metadata = {**art.metadata, **extra}
art.save()
```

This changes shared state. Show the before/after and confirm before saving.
