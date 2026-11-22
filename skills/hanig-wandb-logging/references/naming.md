# Naming

Team entity/project follows metric schema; kebab-case is advice.
Group: YYYYMMDD-question; names: model-data-deltas-s{seed}, 64 chars.
Deltas are changed hyperparameters. <!-- declaration: logging.conventions -->


```text
H: hypothesis or baseline purpose
Expect: val/loss < 0.5
Deltas vs base: train.lr=0.0003
Outcome (supported|refuted|inconclusive|broken): caller's contract result
```

Arc Bench family/arch/train/data/provenance maps to architecture.config,
training.config/seed, training_data and run.commit/repository.
Provenance: Git/dirty, versions, host/scheduler. <!-- declaration: logging.credentials -->
Commands: program basename, flags, positional count; no digest. Long flags drop =values; short/bundled flags keep two characters. Keep lone -/--; count/omit
post-- and negative float() tokens. Option-shaped values before -- are flags.
Legacy command strings/digests become current metadata with a warning.

Use train/loss, val/loss, eval/loss, opt/lr, perf/samples_per_s,
perf/gpu_mem_gb, data/samples_seen on trainer/step; declare min/max summaries.
Log restored optimizer steps; SDK step= may drop resumed points.

## Normalization and schema envelope

Key normalization applies `lower()` to leaf keys and strips `_`, `-`, `.`, and spaces.

```text
DEFAULT_SECRET_KEYS:
apikey wandbapikey password passwd passphrase secret secretkey clientsecret
secretaccesskey awssecretaccesskey accesstoken refreshtoken authtoken bearertoken
sessiontoken hftoken githubtoken privatekey encryptionkey credentials

RESERVED_KEYS:
provenance family model data seed name notes group gitcommit gitdirty gitbranch
gitremote command program flags positionalcount cwd host python torch wandb
cudavisibledevices slurm startedat redactedkeys jobid arrayjobid arraytaskid
jobpartition jobnumnodes ntasks restartcount clustername
```

Dataclass fields (without deepcopy), `argparse.Namespace`, and `SimpleNamespace` recurse as mappings; other objects serialize unchanged. Launch and resume keys convert to text (`Enum` string value/name, else `str(key)`). Every `Mapping`, `list`, `tuple`, `deque`, `set`, `frozenset`, and converted record recurses; deques and sets become lists (sets sorted when orderable, else iteration order). Newly secret resume keys are pruned with one path/earlier-exposure warning while earlier labels stay intact.

Metrics bind to `trainer/step` or the configured axis; `config_deltas` compares identical types and equal values (floats by `==` or both NaN), warning once per path per launch on collisions (last entry stays) and once per path across `config` and `base_config`. In-memory set signatures use a private sentinel distinct from caller lists and compare members by value (including signed zero) independent of order. Explicit names accept W&B letters/digits/`-`/`_`/`.` up to 128 chars; dates, versions, and whole-token status words warn (`old-faithful-geyser` triggers no warning). `run_name` accepts integer `max_len >= 1` (`ValueError` below 1); `None` selects a generated artifact name, while an empty string raises an error. Unknown remote card sizes report `size unknown`.
