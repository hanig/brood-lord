"""Vendor into training code; caller supplies W&B. Stdlib-only logging helpers."""
from __future__ import annotations
import argparse
import dataclasses
import datetime as _dt
import importlib.metadata as _md
import os
import json
import re
import socket
import subprocess
import sys
import types
import warnings
import weakref
from pathlib import Path
from typing import Mapping
__all__ = ['JOB_TYPES', 'VERDICTS', 'provenance', 'config_deltas', 'run_name', 'start_run', 'define_metrics', 'log_step', 'finish', 'lint_run']
JOB_TYPES = ('preprocess', 'train', 'finetune', 'eval', 'predict', 'sweep-trial', 'debug')
VERDICTS = ('supported', 'refuted', 'inconclusive', 'broken')
METRIC_NAMESPACES = ('train', 'val', 'eval', 'test', 'opt', 'perf', 'data', 'trainer')
STEP_METRIC = 'trainer/step'
_SLUG_RE = re.compile('^[a-z0-9][a-z0-9-]{0,62}$')
_RANDOM_NAME_RE = re.compile('^[a-z]+-[a-z]+-\\d+$')
_SECRET_RE = re.compile('(api[_-]?key|token(?![a-z])|secret|passw(or)?d|credential)', re.I)
_ABBREV = {'learning_rate': 'lr', 'lr': 'lr', 'weight_decay': 'wd', 'batch_size': 'bs', 'global_batch_size': 'gbs', 'dropout': 'do', 'num_layers': 'L', 'n_layers': 'L', 'hidden_dim': 'd', 'd_model': 'd', 'num_heads': 'h', 'warmup_steps': 'wu', 'max_steps': 'steps', 'epochs': 'ep', 'max_epochs': 'ep', 'seed': 's', 'temperature': 'T', 'mask_ratio': 'mask', 'grad_clip': 'clip'}
_METRIC_STATE = {}

def _metric_state(run):
    key = id(run)
    if key not in _METRIC_STATE:
        try:
            ref = weakref.ref(run, lambda ref: _METRIC_STATE.pop(key, None))
        except TypeError:
            ref = lambda: run
        _METRIC_STATE[key] = (ref, {'summaries': {}, 'axis': STEP_METRIC, 'namespaces': ()})
    return _METRIC_STATE[key][1]

def _git(*args, cwd=None):
    try:
        out = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=True)
        return out.stdout.strip() or None
    except Exception:
        return None

def _pkg_version(name):
    try:
        return _md.version(name)
    except _md.PackageNotFoundError:
        return None

def _redact(obj):
    """Sanitize records, JSON and labeled secrets; unlabeled prose is caller-owned."""
    if dataclasses.is_dataclass(obj) and (not isinstance(obj, type)):
        obj = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    elif isinstance(obj, (argparse.Namespace, types.SimpleNamespace)):
        obj = vars(obj)
    elif isinstance(obj, tuple) and hasattr(obj, '_asdict'):
        obj = obj._asdict()
    if isinstance(obj, Mapping):
        return {k: '***' if _SECRET_RE.search(str(k)) else _redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        decoder = json.JSONDecoder()
        parts, pos = ([], 0)
        while pos < len(obj):
            if obj[pos] in '{[':
                try:
                    value, size = decoder.raw_decode(obj[pos:])
                except ValueError:
                    pass
                else:
                    parts.append(json.dumps(_redact(value), ensure_ascii=False))
                    pos += size
                    continue
            parts.append(obj[pos])
            pos += 1
        text = ''.join(parts)
        key = '[\\w.-]*(?:api[_-]?key|token(?![a-z])|secret|passw(?:or)?d|credential)[\\w.-]*'
        pattern = '(?i)([\\"\']?' + key + '[\\"\']?\\s*[:=]\\s*)(?:\\"[^\\"]*\\"|\'[^\']*\'|[^\\s,;})]+)'
        text = re.sub(pattern, lambda m: m.group(1) + '"***"', text)
        return re.sub('//[^/@\\s]+@', '//', text)
    return obj

def _redact_argv(argv):
    out, redact_next = ([], False)
    for tok in argv:
        if redact_next:
            out.append('***')
            redact_next = False
        elif tok.startswith('-') and '=' not in tok and _SECRET_RE.search(tok):
            out.append(tok)
            redact_next = True
        else:
            out.append(_redact(tok))
    return ' '.join(out)

def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')

def provenance(wandb, cwd=None):
    """Where, from what code, and how this process was launched."""
    sha = _git('rev-parse', 'HEAD', cwd=cwd)
    env = os.environ
    slurm_keys = ('SLURM_JOB_ID', 'SLURM_ARRAY_JOB_ID', 'SLURM_ARRAY_TASK_ID', 'SLURM_JOB_PARTITION', 'SLURM_JOB_NUM_NODES', 'SLURM_NTASKS', 'SLURM_RESTART_COUNT', 'SLURM_CLUSTER_NAME')
    slurm = {k[len('SLURM_'):].lower(): env[k] for k in slurm_keys if k in env}
    return {'git_commit': sha, 'git_dirty': bool(_git('status', '--porcelain', cwd=cwd)) if sha else None, 'git_branch': _git('rev-parse', '--abbrev-ref', 'HEAD', cwd=cwd), 'git_remote': _redact(_git('config', '--get', 'remote.origin.url', cwd=cwd)), 'command': _redact_argv(sys.argv), 'cwd': os.getcwd(), 'host': socket.gethostname(), 'python': sys.version.split()[0], 'torch': _pkg_version('torch'), 'wandb': wandb.__version__, 'cuda_visible_devices': env.get('CUDA_VISIBLE_DEVICES'), 'slurm': slurm or None, 'started_at': _utcnow()}

def _flatten(d, prefix=''):
    flat: dict[str, Any] = {}
    for k, v in d.items():
        key = f'{prefix}{k}'
        if isinstance(v, Mapping):
            flat.update(_flatten(v, key + '.'))
        else:
            flat[key] = v
    return flat

def config_deltas(config, base):
    """Flattened keys whose values differ from ``base`` (new keys included)."""
    if not base:
        return {}
    cur, ref = (_flatten(_redact(config)), _flatten(_redact(base)))
    skip = ('provenance.', 'family.')
    return {k: v for k, v in cur.items() if not k.startswith(skip) and (not _SECRET_RE.search(k)) and (k not in ref or ref[k] != v)}

def _fmt_value(v):
    if isinstance(v, bool):
        return '' if v else 'off'
    if isinstance(v, float):
        if v != 0 and (abs(v) < 0.01 or abs(v) >= 100000.0):
            mant, exp = f'{v:.0e}'.split('e')
            return f'{mant}e{int(exp)}'
        return f'{v:g}'
    return str(v)

def _slugify(s):
    s = re.sub('[^A-Za-z0-9.+-]+', '-', str(s)).strip('-')
    return re.sub('-{2,}', '-', s)

def run_name(model, data, deltas=None, seed=None, max_len=64):
    """Model-data-deltas-seed label, with a hash when truncated."""
    parts = [_slugify(_redact(model)), _slugify(_redact(data))]
    for key, val in _redact(deltas or {}).items():
        if _SECRET_RE.search(str(key)):
            continue
        leaf = key.rsplit('.', 1)[-1]
        if leaf == 'seed':
            continue
        ab = _ABBREV.get(leaf, leaf.replace('_', '')[:10])
        parts.append(_slugify(f'{ab}{_fmt_value(val)}'))
    if seed is not None:
        parts.append(_slugify(f's{_redact(seed)}'))
    name = '-'.join((p for p in parts if p))
    if len(name) > max_len:
        import hashlib
        digest = hashlib.sha1(name.encode()).hexdigest()[:6]
        name = name[:max_len - 7].rstrip('-') + '-' + digest
    return name

def _rank():
    if 'RANK' in os.environ:
        return int(os.environ['RANK'])
    if 'SLURM_PROCID' in os.environ:
        rank = int(os.environ['SLURM_PROCID'])
        if rank != 0:
            return rank
    return int(os.environ.get('LOCAL_RANK', '0'))

def _new_run_id():
    import secrets
    import string
    alphabet = string.ascii_lowercase + string.digits
    return ''.join((secrets.choice(alphabet) for _ in range(8)))

def _check_slug(value, what):
    if not _SLUG_RE.match(value):
        warnings.warn(f'{what}.slug: recommend kebab-case, 63 chars')

def start_run(wandb, *, project, family, job_type, model, data, hypothesis, config, expect=None, base_config=None, group=None, tags=(), summaries=None, entity=None, output_dir=None, name=None, rank=None, **init_kwargs):
    """Start with base-config deltas, summary rules and a persistent output_dir ID."""
    rank = _rank() if rank is None else rank
    if rank != 0:
        return wandb.init(mode='disabled')
    _check_slug(project, 'project')
    if not isinstance(family, str) or not family:
        warnings.warn('family.empty: a non-empty family is recommended')
    if job_type not in JOB_TYPES:
        warnings.warn('job_type.nonstandard: use a conventional job type')
    if not hypothesis or len(hypothesis.strip()) < 10:
        warnings.warn('notes.short-hypothesis: recommend 10+ characters')
    if group is not None and len(group) > 128:
        warnings.warn('group.long: at most 128 chars is recommended')
    flat = _flatten(config)
    seed = flat.get('train.seed', flat.get('seed'))
    deltas = config_deltas(config, base_config)
    name = name or run_name(model, data, deltas, seed)
    run_id, resuming = (None, False)
    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        id_file = out / 'wandb_run_id'
        if id_file.exists():
            run_id, resuming = (id_file.read_text().strip(), True)
        else:
            run_id = _new_run_id()
            id_file.write_text(run_id + '\n')
        init_kwargs.setdefault('dir', str(out))
    prov = launch_prov = _redact(provenance(wandb))
    if output_dir is not None:
        import json
        prov_file = Path(output_dir) / 'wandb_provenance.json'
        if resuming and prov_file.exists():
            launch_prov = json.loads(prov_file.read_text())
        else:
            prov_file.write_text(json.dumps(prov, indent=1) + '\n')
    tag_list = [t.lower() for t in tags]
    if prov['git_dirty']:
        tag_list.append('dirty-tree')
    for t in tag_list:
        if len(t) > 64:
            warnings.warn('tag.long: at most 64 chars is recommended')
    notes = f"H: {(hypothesis or '').strip()}"
    if expect:
        notes += f'\nExpect: {expect.strip()}'
    if deltas:
        notes += '\nDeltas vs base: ' + ', '.join((f'{k}={v}' for k, v in deltas.items()))
    full_config = _redact({'family': family, **config, 'provenance': launch_prov})
    resume_count = 0
    if output_dir is not None:
        state_file = Path(output_dir) / 'wandb_config.json'
        if resuming and state_file.exists():
            saved = json.loads(state_file.read_text())
            full_config = _redact(saved['config'])
            resume_count = saved.get('resume_count', 0) + 1
        state = {'config': full_config, 'resume_count': resume_count}
        temporary = state_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, indent=1) + '\n')
        temporary.replace(state_file)
    payload = _redact(dict(entity=entity, project=project, group=group, job_type=job_type, name=name, notes=notes, tags=sorted(set(tag_list)), config=full_config, allow_val_change=resuming, id=run_id, resume='allow' if run_id else None, **init_kwargs))
    run = wandb.init(**payload)
    if resuming:
        run.summary['provenance/last_resume'] = _redact(prov)
        run.summary['provenance/resume_count'] = resume_count
    define_metrics(run, summaries=summaries)
    return run

def define_metrics(run, step_metric=STEP_METRIC, namespaces=METRIC_NAMESPACES, summaries=None):
    """Put every namespace on one step axis and declare best-value summaries."""
    state = _metric_state(run)
    definitions = {**state['summaries'], **(summaries or {})}
    namespaces = tuple(dict.fromkeys((*state['namespaces'], *namespaces)))
    run.define_metric(_redact(step_metric))
    for ns in namespaces:
        run.define_metric(_redact(f'{ns}/*'), step_metric=_redact(step_metric))
    for key, how in definitions.items():
        if how not in ('min', 'max', 'mean', 'last'):
            raise ValueError(f'summary for {key!r} must be min, max, mean or last')
        run.define_metric(_redact(key), step_metric=_redact(step_metric), summary=how)
    state.update(summaries=definitions, axis=step_metric, namespaces=namespaces)
_warned_keys: set[str] = set()

def log_step(run, step, metrics, commit=None):
    """Log on the shared axis; warn once per bare metric."""
    for k in metrics:
        if '/' not in k and k not in _warned_keys:
            _warned_keys.add(k)
            warnings.warn(f'metric {k!r} needs a namespace')
    run.log(_redact({**metrics, _metric_state(run)['axis']: step}), commit=commit)

def finish(run, outcome, verdict, exit_code=None):
    """Record the outcome against the hypothesis, lint the run, then finish it."""
    if verdict not in VERDICTS:
        raise ValueError(f'verdict {verdict!r} not in {VERDICTS}')
    if getattr(run, 'disabled', False) or not getattr(run, 'id', None):
        return []
    run.notes = _redact((run.notes or '') + f'\nOutcome ({verdict}): {outcome.strip()}')
    for key in list(run.summary.keys()):
        run.summary[key] = _redact({key: run.summary[key]})[key]
    run.summary['verdict'] = verdict
    if verdict == 'broken':
        run.tags = tuple(sorted(set(run.tags or ()) | {'broken'}))
    issues = lint_run(name=run.name, notes=run.notes, group=run.group, job_type=run.job_type, config=dict(run.config), summary_keys=list(run.summary.keys()), tags=list(run.tags or ()))
    for issue in issues:
        print(f'[wbkit] {issue}', file=sys.stderr)
    run.finish(exit_code=exit_code)
    _METRIC_STATE.pop(id(run), None)
    return issues

def lint_run(*, name, notes, group, job_type, config, summary_keys, tags=(), state=None, project=None):
    """Return rule-code: advice issues."""
    issues = []
    if project and (not _SLUG_RE.fullmatch(project)):
        issues.append('project.slug: kebab-case is recommended')
    if notes and notes.startswith('H:') and (len(notes.splitlines()[0][2:].strip()) < 10):
        issues.append('notes.short-hypothesis: recommend a sentence')
    if not name or _RANDOM_NAME_RE.match(name):
        issues.append('name.random: use model-data-deltas-seed')
    if not notes or not notes.strip():
        issues.append("notes.empty: add H: hypothesis")
    elif 'H:' not in notes:
        issues.append("notes.no-hypothesis: no 'H:' line")
    if state == 'finished' and notes and ('Outcome' not in notes):
        issues.append("notes.no-outcome: add Outcome line")
    if not group:
        issues.append('group.missing: group one question')
    if job_type not in JOB_TYPES:
        issues.append(f"job_type.nonstandard: use one of {', '.join(JOB_TYPES)}")
    prov = config.get('provenance') or {}
    if isinstance(prov, Mapping) and isinstance(prov.get('value'), Mapping):
        prov = prov['value']
    if not isinstance(prov, Mapping):
        prov = {}
    if not prov.get('git_commit'):
        issues.append('config.no-commit: missing git_commit')
    elif prov.get('git_dirty'):
        issues.append('config.dirty-tree: uncommitted launch')
    missing = [s for s in ('arch', 'train', 'data') if s not in config]
    if missing:
        issues.append(f"config.flat: no {'/'.join(missing)} section(s)")
    bare = sorted((k for k in summary_keys if not k.startswith('_') and '/' not in k and (k != 'verdict')))
    if bare:
        issues.append(f"metrics.unnamespaced: {', '.join(bare[:8])}")
    return issues
