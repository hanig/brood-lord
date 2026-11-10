"""wbkit: opinionated Weights & Biases helpers for informative runs and artifacts.

Vendor this single file into a training repository (for example as
``<package>/wbkit.py``). It imports only the standard library. The caller passes its ``wandb`` module.

What it enforces, so every run is findable and explainable months later:

* a run name built from the model, the data and the hyperparameters that differ
  from the group's base config, never a random ``adjective-noun-7``;
* notes that open with the hypothesis (``H:``) and close with the outcome;
* a nested config (``family``, ``arch``, ``train``, ``data``, ``provenance``) with
  git commit, dirty flag, command line, host and Slurm ids recorded;
* namespaced metric keys (``train/``, ``val/``, ``opt/``, ``perf/`` ...) on one
  shared step axis, with best-value summaries declared up front;
* artifacts whose description is a card (what, produced by, inputs, contents,
  intended use, caveats) and whose metadata is searchable;
* rank-zero-only logging and stable run ids across Slurm requeues.
"""

from __future__ import annotations

import datetime as _dt
import importlib.metadata as _md
import os
import re
import socket
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any, Iterable, Mapping


__all__ = [
    "JOB_TYPES",
    "ARTIFACT_TYPES",
    "VERDICTS",
    "provenance",
    "config_deltas",
    "run_name",
    "start_run",
    "define_metrics",
    "log_step",
    "use_input",
    "artifact_card",
    "log_dataset",
    "log_model",
    "log_predictions",
    "finish",
    "lint_run",
    "lint_artifact",
]

JOB_TYPES = ("preprocess", "train", "finetune", "eval", "predict", "sweep-trial", "debug")
ARTIFACT_TYPES = ("dataset", "model", "predictions", "eval-results", "code")
VERDICTS = ("supported", "refuted", "inconclusive", "broken")
METRIC_NAMESPACES = ("train", "val", "eval", "test", "opt", "perf", "data", "trainer")
STEP_METRIC = "trainer/step"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ARTIFACT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_RANDOM_NAME_RE = re.compile(r"^[a-z]+-[a-z]+-\d+$")
_SECRET_RE = re.compile(r"(api[_-]?key|token|secret|passw(or)?d|credential)", re.I)
_VERSION_RE = re.compile(r":v\d+$")
_VERSIONISH_NAME_RE = re.compile(
    r"(^|[-_.])(final|latest|best|new|old)([-_.]|$)|(^|[-_.])v\d+$|(^|[-_.])20\d{6}([-_.]|$)"
)
_ABBREV = {
    "learning_rate": "lr",
    "lr": "lr",
    "weight_decay": "wd",
    "batch_size": "bs",
    "global_batch_size": "gbs",
    "dropout": "do",
    "num_layers": "L",
    "n_layers": "L",
    "hidden_dim": "d",
    "d_model": "d",
    "num_heads": "h",
    "warmup_steps": "wu",
    "max_steps": "steps",
    "epochs": "ep",
    "max_epochs": "ep",
    "seed": "s",
    "temperature": "T",
    "mask_ratio": "mask",
    "grad_clip": "clip",
}
_INPUTS: dict[str, list[str]] = {}


# --------------------------------------------------------------------------- provenance


def _git(*args: str, cwd: str | None = None) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=True
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _pkg_version(name: str) -> str | None:
    try:
        return _md.version(name)
    except _md.PackageNotFoundError:
        return None


def _strip_url_creds(url: str | None) -> str | None:
    return re.sub(r"//[^/@]+@", "//", url) if url else url


def _redact_argv(argv: Iterable[str]) -> str:
    out, redact_next = [], False
    for tok in argv:
        if redact_next:
            out.append("***")
            redact_next = False
        elif _SECRET_RE.search(tok):
            if "=" in tok:
                out.append(tok.split("=", 1)[0] + "=***")
            else:
                out.append(tok)
                redact_next = tok.startswith("-")
        else:
            out.append(tok)
    return " ".join(out)


def _redact(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: ("***" if _SECRET_RE.search(str(k)) else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def provenance(wandb, cwd: str | None = None) -> dict[str, Any]:
    """Where, from what code, and how this process was launched."""
    sha = _git("rev-parse", "HEAD", cwd=cwd)
    env = os.environ
    slurm_keys = (
        "SLURM_JOB_ID",
        "SLURM_ARRAY_JOB_ID",
        "SLURM_ARRAY_TASK_ID",
        "SLURM_JOB_PARTITION",
        "SLURM_JOB_NUM_NODES",
        "SLURM_NTASKS",
        "SLURM_RESTART_COUNT",
        "SLURM_CLUSTER_NAME",
    )
    slurm = {k[len("SLURM_"):].lower(): env[k] for k in slurm_keys if k in env}
    return {
        "git_commit": sha,
        "git_dirty": bool(_git("status", "--porcelain", cwd=cwd)) if sha else None,
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd),
        "git_remote": _strip_url_creds(_git("config", "--get", "remote.origin.url", cwd=cwd)),
        "command": _redact_argv(sys.argv),
        "cwd": os.getcwd(),
        "host": socket.gethostname(),
        "python": sys.version.split()[0],
        "torch": _pkg_version("torch"),
        "wandb": wandb.__version__,
        "cuda_visible_devices": env.get("CUDA_VISIBLE_DEVICES"),
        "slurm": slurm or None,
        "started_at": _utcnow(),
    }


# --------------------------------------------------------------------------- naming


def _flatten(d: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, Mapping):
            flat.update(_flatten(v, key + "."))
        else:
            flat[key] = v
    return flat


def config_deltas(config: Mapping[str, Any], base: Mapping[str, Any] | None) -> dict[str, Any]:
    """Flattened keys whose values differ from ``base`` (new keys included)."""
    if not base:
        return {}
    cur, ref = _flatten(_redact(config)), _flatten(_redact(base))
    skip = ("provenance.", "family.")
    return {
        k: v
        for k, v in cur.items()
        if not k.startswith(skip)
        and not _SECRET_RE.search(k)
        and (k not in ref or ref[k] != v)
    }


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "" if v else "off"
    if isinstance(v, float):
        if v != 0 and (abs(v) < 1e-2 or abs(v) >= 1e5):
            mant, exp = f"{v:.0e}".split("e")
            return f"{mant}e{int(exp)}"
        return f"{v:g}"
    return str(v)


def _slugify(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9.+-]+", "-", str(s)).strip("-")
    return re.sub(r"-{2,}", "-", s)


def run_name(
    model: str,
    data: str,
    deltas: Mapping[str, Any] | None = None,
    seed: int | None = None,
    max_len: int = 64,
) -> str:
    """``<model>-<data>-<delta...>-s<seed>``, e.g. ``state3-tahoe100m-lr3e-4-wd0.1-s0``."""
    parts = [_slugify(model), _slugify(data)]
    for key, val in _redact(deltas or {}).items():
        if _SECRET_RE.search(str(key)):
            continue
        leaf = key.rsplit(".", 1)[-1]
        if leaf == "seed":
            continue
        ab = _ABBREV.get(leaf, leaf.replace("_", "")[:10])
        parts.append(_slugify(f"{ab}{_fmt_value(val)}"))
    if seed is not None:
        parts.append(f"s{seed}")
    name = "-".join(p for p in parts if p)
    if len(name) > max_len:
        import hashlib

        digest = hashlib.sha1(name.encode()).hexdigest()[:6]
        name = name[: max_len - 7].rstrip("-") + "-" + digest
    return name


# --------------------------------------------------------------------------- runs


def _rank() -> int:
    for var in ("RANK", "LOCAL_RANK"):
        if var in os.environ:
            return int(os.environ[var])
    if int(os.environ.get("SLURM_NTASKS", "1")) > 1 and "SLURM_PROCID" in os.environ:
        return int(os.environ["SLURM_PROCID"])
    return 0


def _new_run_id() -> str:
    import secrets
    import string

    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def _check_slug(value: str, what: str) -> None:
    if not _SLUG_RE.match(value):
        raise ValueError(f"{what} {value!r} must be lowercase kebab-case, at most 63 chars")


def start_run(
    wandb,
    *,
    project: str,
    family: str,
    job_type: str,
    model: str,
    data: str,
    hypothesis: str,
    config: Mapping[str, Any],
    expect: str | None = None,
    base_config: Mapping[str, Any] | None = None,
    group: str | None = None,
    tags: Iterable[str] = (),
    summaries: Mapping[str, str] | None = None,
    entity: str | None = None,
    output_dir: str | os.PathLike | None = None,
    name: str | None = None,
    rank: int | None = None,
    **init_kwargs: Any,
):
    """Start an informative run. Non-zero ranks get a disabled run.

    ``config`` should be nested as ``{"arch": ..., "train": ..., "data": ...}``;
    ``family`` and ``provenance`` are added here. ``base_config`` is the group's
    reference config; keys that differ from it go into the run name.
    ``summaries`` maps metric keys to ``"min"`` or ``"max"`` so the runs table
    shows the best value instead of the last one.
    ``output_dir`` makes the run id survive a Slurm requeue: the id is written
    to ``<output_dir>/wandb_run_id`` and reused with ``resume="allow"``.
    """
    rank = _rank() if rank is None else rank
    if rank != 0:
        return wandb.init(mode="disabled")

    _check_slug(project, "project")
    _check_slug(family, "family")
    if job_type not in JOB_TYPES:
        raise ValueError(f"job_type {job_type!r} not in {JOB_TYPES}")
    if not hypothesis or len(hypothesis.strip()) < 10:
        raise ValueError("hypothesis is required: one sentence on why this run exists")
    if group is not None and len(group) > 128:
        raise ValueError("group must be at most 128 chars")

    flat = _flatten(config)
    seed = flat.get("train.seed", flat.get("seed"))
    deltas = config_deltas(config, base_config)
    name = name or run_name(model, data, deltas, seed)

    run_id, resuming = None, False
    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        id_file = out / "wandb_run_id"
        if id_file.exists():
            run_id, resuming = id_file.read_text().strip(), True
        else:
            run_id = _new_run_id()
            id_file.write_text(run_id + "\n")
        init_kwargs.setdefault("dir", str(out))

    prov = launch_prov = provenance(wandb)
    if output_dir is not None:
        import json

        prov_file = Path(output_dir) / "wandb_provenance.json"
        if resuming and prov_file.exists():
            launch_prov = json.loads(prov_file.read_text())
        else:
            prov_file.write_text(json.dumps(prov, indent=1) + "\n")
    tag_list = [t.lower() for t in tags]
    if prov["git_dirty"]:
        tag_list.append("dirty-tree")
    for t in tag_list:
        if len(t) > 64:
            raise ValueError(f"tag {t!r} longer than 64 chars")

    notes = f"H: {hypothesis.strip()}"
    if expect:
        notes += f"\nExpect: {expect.strip()}"
    if deltas:
        notes += "\nDeltas vs base: " + ", ".join(f"{k}={v}" for k, v in deltas.items())

    # Config keeps the first launch's provenance; a resume's goes into summary.
    full_config = _redact({"family": family, **config, "provenance": launch_prov})
    run = wandb.init(
        entity=entity,
        project=project,
        group=group,
        job_type=job_type,
        name=name,
        notes=notes,
        tags=sorted(set(tag_list)),
        config=full_config,
        allow_val_change=resuming,
        id=run_id,
        resume="allow" if run_id else None,
        **init_kwargs,
    )
    if resuming:
        run.summary["provenance/last_resume"] = prov
        run.summary["provenance/resume_count"] = int(run.summary.get("provenance/resume_count", 0)) + 1
    define_metrics(run, summaries=summaries)
    return run


def define_metrics(
    run,
    step_metric: str = STEP_METRIC,
    namespaces: Iterable[str] = METRIC_NAMESPACES,
    summaries: Mapping[str, str] | None = None,
) -> None:
    """Put every namespace on one step axis and declare best-value summaries."""
    run.define_metric(step_metric)
    for ns in namespaces:
        if f"{ns}/" != step_metric[: len(ns) + 1]:
            run.define_metric(f"{ns}/*", step_metric=step_metric)
    for key, how in (summaries or {}).items():
        if how not in ("min", "max", "mean", "last"):
            raise ValueError(f"summary for {key!r} must be min, max, mean or last")
        # wandb 0.31 dropped define_metric(goal=); summary="min"/"max" is enough.
        run.define_metric(key, step_metric=step_metric, summary=how)


_warned_keys: set[str] = set()


def log_step(run, step: int, metrics: Mapping[str, Any], commit: bool | None = None) -> None:
    """``run.log`` with the shared step key added and namespacing checked once per key."""
    for k in metrics:
        if "/" not in k and k not in _warned_keys:
            _warned_keys.add(k)
            warnings.warn(f"metric {k!r} has no namespace; use train/, val/, opt/, perf/ ...")
    run.log({**metrics, STEP_METRIC: step}, commit=commit)


# --------------------------------------------------------------------------- artifacts


def _qualified(art) -> str:
    base = art.name.split(":")[0]
    return f"{art.entity}/{art.project}/{base}:{art.version}"


def use_input(run, ref: str, type: str | None = None):
    """``run.use_artifact`` that records lineage and warns on unpinned refs."""
    if not _VERSION_RE.search(ref):
        warnings.warn(
            f"input {ref!r} is not pinned to :vN; the resolved version is recorded, "
            "but pin it in config for reproducibility"
        )
    art = run.use_artifact(ref, type=type)
    _INPUTS.setdefault(run.id, []).append(_qualified(art))
    return art


def artifact_card(
    *,
    summary: str,
    contents: str,
    intended_use: str,
    run=None,
    inputs: Iterable[str] | None = None,
    caveats: str | None = None,
) -> str:
    """Markdown description: what it is, who made it, from what, and how to use it."""
    lines = [summary.strip(), ""]
    if run is not None and getattr(run, "id", None):
        prov = dict(run.config.get("provenance") or {})
        sha = (prov.get("git_commit") or "unknown")[:10]
        dirty = " (dirty tree)" if prov.get("git_dirty") else ""
        url = getattr(run, "url", None)
        who = f"[{run.name}]({url})" if url else f"`{run.name}` ({run.id})"
        lines.append(f"**Produced by:** {who}, job_type `{run.job_type}`, commit `{sha}`{dirty}")
        if prov.get("command"):
            lines.append(f"**Command:** `{prov['command']}`")
    ins = list(inputs) if inputs is not None else (_INPUTS.get(getattr(run, "id", ""), []))
    lines.append("**Inputs:** " + (", ".join(f"`{i}`" for i in ins) if ins else "none recorded"))
    lines.append(f"**Contents:** {contents.strip()}")
    lines.append(f"**Intended use:** {intended_use.strip()}")
    lines.append(f"**Caveats:** {caveats.strip() if caveats else 'none known'}")
    return "\n".join(lines)


def _local_stats(path: Path) -> dict[str, Any]:
    if path.is_file():
        return {"n_files": 1, "total_bytes": path.stat().st_size}
    files = [p for p in path.rglob("*") if p.is_file()]
    return {"n_files": len(files), "total_bytes": sum(p.stat().st_size for p in files)}


def _check_artifact_name(name: str) -> None:
    if not _ARTIFACT_NAME_RE.match(name):
        raise ValueError(f"artifact name {name!r} must be lowercase [a-z0-9._-]")
    if _VERSIONISH_NAME_RE.search(name):
        raise ValueError(
            f"artifact name {name!r} encodes a version, date or status; "
            "W&B versions artifacts, so use aliases (best, candidate) instead"
        )


def _add_source(art, source: str | os.PathLike, reference: bool | None, checksum: bool) -> dict:
    src = str(source)
    if "://" in src:
        art.add_reference(src, checksum=checksum)
        return {"source_uri": src, "storage": "reference"}
    path = Path(src).resolve()
    stats = _local_stats(path)
    if reference is None:
        reference = stats["total_bytes"] > 2 * 1024**3
    if reference:
        art.add_reference(path.as_uri(), checksum=checksum)
        storage = "reference"
    elif path.is_dir():
        art.add_dir(str(path))
        storage = "upload"
    else:
        art.add_file(str(path))
        storage = "upload"
    return {"source_uri": path.as_uri(), "storage": storage, **stats}


def log_dataset(
    wandb,
    run,
    name: str,
    source: str | os.PathLike,
    *,
    summary: str,
    contents: str,
    intended_use: str,
    caveats: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    reference: bool | None = None,
    checksum: bool = True,
    aliases: Iterable[str] = (),
    type: str = "dataset",
):
    """Log a dataset (local path or s3://, gs://, file:// URI) with a card.

    Paths over 2 GiB become references by default: W&B stores checksums and
    the URI, not the bytes. A file:// reference only resolves on hosts that
    mount that path, so say where it lives in ``caveats``.
    """
    if getattr(run, "disabled", False):
        return None
    _check_artifact_name(name)
    if type not in ARTIFACT_TYPES:
        raise ValueError(f"type {type!r} not in {ARTIFACT_TYPES}")
    art = wandb.Artifact(name, type=type)
    md = _add_source(art, source, reference, checksum)
    md.update(metadata or {})
    art.description = artifact_card(
        summary=summary, contents=contents, intended_use=intended_use, run=run, caveats=caveats
    )
    art.metadata = md
    return run.log_artifact(art, aliases=list(aliases))


def log_model(
    wandb,
    run,
    path: str | os.PathLike,
    *,
    step: int,
    metrics: Mapping[str, Any] | None = None,
    is_best: bool = False,
    name: str | None = None,
    summary: str | None = None,
    intended_use: str = "Resume training, or evaluate with the arch config in metadata.",
    caveats: str | None = None,
    reference: bool | None = None,
    aliases: Iterable[str] = (),
):
    """Log a checkpoint as a ``model`` artifact with step, metrics and arch in metadata.

    The default name ``ckpt-<run_id>`` keeps every checkpoint of a run in one
    collection, versioned, with ``step-N`` and ``best`` aliases.
    """
    if getattr(run, "disabled", False):
        return None
    name = name or f"ckpt-{run.id}".lower()
    _check_artifact_name(name)
    metrics = dict(metrics or {})
    shown = ", ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}" for k, v in metrics.items())
    art = wandb.Artifact(name, type="model")
    md = _add_source(art, path, reference, checksum=True)
    md.update(
        {
            "step": step,
            "metrics": metrics,
            "family": run.config.get("family"),
            "arch": dict(run.config.get("arch") or {}),
            "run_name": run.name,
        }
    )
    art.metadata = md
    art.description = artifact_card(
        summary=summary or f"Checkpoint of `{run.name}` at step {step}.",
        contents=f"{md.get('n_files', '?')} file(s), {md.get('total_bytes', 0) / 1e6:.1f} MB. "
        + (f"Metrics at save: {shown}." if shown else "No metrics attached."),
        intended_use=intended_use,
        run=run,
        caveats=caveats,
    )
    al = [f"step-{step}", *aliases] + (["best"] if is_best else [])
    return run.log_artifact(art, aliases=al)


def log_predictions(
    wandb,
    run,
    name: str,
    table: "wandb.Table",
    *,
    eval_set: str,
    summary: str,
    intended_use: str = "Error analysis and comparison against other runs on the same eval set.",
    caveats: str | None = None,
    metadata: Mapping[str, Any] | None = None,
):
    """Log a predictions table as its own artifact so it can be joined across runs."""
    if getattr(run, "disabled", False):
        return None
    _check_artifact_name(name)
    art = wandb.Artifact(name, type="predictions")
    art.add(table, "predictions")
    cols = ", ".join(table.columns)
    art.metadata = {"eval_set": eval_set, "n_rows": len(table.data), "columns": table.columns, **(metadata or {})}
    art.description = artifact_card(
        summary=summary,
        contents=f"Table `predictions` with {len(table.data)} rows; columns: {cols}.",
        intended_use=intended_use,
        run=run,
        caveats=caveats,
    )
    return run.log_artifact(art)


# --------------------------------------------------------------------------- finishing and lint


def finish(run, outcome: str, verdict: str, exit_code: int | None = None) -> list[str]:
    """Record the outcome against the hypothesis, lint the run, then finish it."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict {verdict!r} not in {VERDICTS}")
    if getattr(run, "disabled", False) or not getattr(run, "id", None):
        return []
    run.notes = (run.notes or "") + f"\nOutcome ({verdict}): {outcome.strip()}"
    run.summary["verdict"] = verdict
    if verdict == "broken":
        run.tags = tuple(sorted(set(run.tags or ()) | {"broken"}))
    issues = lint_run(
        name=run.name,
        notes=run.notes,
        group=run.group,
        job_type=run.job_type,
        config=dict(run.config),
        summary_keys=list(run.summary.keys()),
        tags=list(run.tags or ()),
    )
    for issue in issues:
        print(f"[wbkit] {issue}", file=sys.stderr)
    run.finish(exit_code=exit_code)
    return issues


def lint_run(
    *,
    name: str | None,
    notes: str | None,
    group: str | None,
    job_type: str | None,
    config: Mapping[str, Any],
    summary_keys: Iterable[str],
    tags: Iterable[str] = (),
    state: str | None = None,
) -> list[str]:
    """Rules shared by ``finish`` and the project auditor.

    Each issue is ``"<rule-code>: <advice>"``; the code is stable so the auditor
    can group by it.
    """
    issues = []
    if not name or _RANDOM_NAME_RE.match(name):
        issues.append("name.random: W&B default name; use model-data-deltas-seed")
    if not notes or not notes.strip():
        issues.append("notes.empty: open notes with 'H: <hypothesis>'")
    elif "H:" not in notes:
        issues.append("notes.no-hypothesis: no 'H:' line")
    if state == "finished" and notes and "Outcome" not in notes:
        issues.append("notes.no-outcome: finished run has no 'Outcome' line")
    if not group:
        issues.append("group.missing: group the runs that answer one question")
    if job_type not in JOB_TYPES:
        issues.append(f"job_type.nonstandard: use one of {', '.join(JOB_TYPES)}")
    prov = config.get("provenance") or {}
    if isinstance(prov, Mapping) and isinstance(prov.get("value"), Mapping):
        prov = prov["value"]  # older public-API config shape wraps values
    if not prov.get("git_commit"):
        issues.append("config.no-commit: provenance.git_commit missing")
    elif prov.get("git_dirty"):
        issues.append("config.dirty-tree: launched from uncommitted code")
    missing = [s for s in ("arch", "train", "data") if s not in config]
    if missing:
        issues.append(f"config.flat: no {'/'.join(missing)} section(s)")
    bare = sorted(
        k for k in summary_keys if not k.startswith("_") and "/" not in k and k != "verdict"
    )
    if bare:
        issues.append(f"metrics.unnamespaced: {', '.join(bare[:8])}")
    return issues


def lint_artifact(*, name: str, type: str, description: str | None, metadata: Mapping | None) -> list[str]:
    """Rules for one artifact collection, judged on its newest version."""
    if type.startswith("wandb-") or type in ("run_table", "job"):
        return []
    issues = []
    desc = (description or "").strip()
    if len(desc) < 40:
        issues.append("artifact.no-description: description missing or under 40 chars")
    else:
        absent = [f for f in ("Produced by", "Contents", "Intended use") if f not in desc]
        if absent:
            issues.append(f"artifact.thin-card: description lacks {', '.join(absent)}")
    if not metadata:
        issues.append("artifact.no-metadata: metadata empty")
    if _VERSIONISH_NAME_RE.search(name):
        issues.append("artifact.versioned-name: name encodes a version, date or status")
    if type not in ARTIFACT_TYPES:
        issues.append(f"artifact.nonstandard-type: {type!r}")
    return issues
