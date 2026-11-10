"""Stdlib logging helpers; caller supplies W&B."""
from __future__ import annotations
import argparse
import dataclasses
from enum import Enum
import hashlib
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
__all__='JOB_TYPES VERDICTS provenance config_deltas run_name start_run define_metrics log_step finish lint_run'.split()
JOB_TYPES=('preprocess','train','finetune','eval','predict','sweep-trial','debug')
VERDICTS=('supported','refuted','inconclusive','broken')
METRIC_NAMESPACES=('train','val','eval','test','opt','perf','data','trainer')
STEP_METRIC='trainer/step'
_SLUG_RE=re.compile('^[a-z0-9][a-z0-9-]{0,62}$')
_RANDOM_NAME_RE=re.compile('^[a-z]+-[a-z]+-\\d+$')
_SECRET_RE=re.compile('(api[_-]?key|token(?=\\.|$)|secret|passw(or)?d|credential)',re.I)
_ABBREV={'learning_rate':'lr','lr':'lr','weight_decay':'wd','batch_size':'bs','global_batch_size':'gbs','dropout':'do','num_layers':'L','n_layers':'L','hidden_dim':'d','d_model':'d','num_heads':'h','warmup_steps':'wu','max_steps':'steps','epochs':'ep','max_epochs':'ep','seed':'s','temperature':'T','mask_ratio':'mask','grad_clip':'clip'}
_METRIC_STATE={}

def _metric_state(run):
  key=id(run)
  if key not in _METRIC_STATE:
    try:
      ref=weakref.ref(run,lambda ref:_METRIC_STATE.pop(key,None))
    except TypeError:
      ref=lambda:run
    _METRIC_STATE[key]=(ref,{'summaries':{},'axis':STEP_METRIC,'namespaces':()})
  return _METRIC_STATE[key][1]

def _git(*args,cwd=None):
  try:
    out=subprocess.run(['git',*args],cwd=cwd,capture_output=True,text=True,timeout=10,check=True)
    return out.stdout.strip()or None
  except Exception:
    return None

def _pkg_version(name):
  try:
    return _md.version(name)
  except _md.PackageNotFoundError:
    return None

def _redact(obj):
  """Sanitize records, JSON and labeled secrets; unlabeled prose is caller-owned."""
  if dataclasses.is_dataclass(obj)and(not isinstance(obj,type)):
    obj={f.name:getattr(obj,f.name)for f in dataclasses.fields(obj)}
  elif isinstance(obj,(argparse.Namespace,types.SimpleNamespace)):
    obj=vars(obj)
  elif isinstance(obj,tuple)and hasattr(obj,'_asdict'):
    obj=obj._asdict()
  if isinstance(obj,Mapping):
    return{_redact(k):'***' if _SECRET_RE.search(str(k))else _redact(v)for k,v in obj.items()}
  if isinstance(obj,(list,tuple)):
    return[_redact(v)for v in obj]
  if isinstance(obj,str):
    dec=json.JSONDecoder()
    parts,pos=([],0)
    while pos<len(obj):
      if obj[pos]in '{[':
        try:
          v,size=dec.raw_decode(obj[pos:])
        except ValueError:
          pass
        else:
          clean=_redact(v)
          parts.append(obj[pos:pos+size]if clean==v else json.dumps(clean,ensure_ascii=False))
          pos+=size
          continue
      parts.append(obj[pos])
      pos+=1
    text=''.join(parts)
    labels=re.compile(r"[\"']?[\w./-]+[\"']?\s*[:=]\s*")
    out,pos=[],0
    for m in labels.finditer(text):
      if m.start()<pos:
        continue
      key=re.split(r'\s*[:=]',m.group())[0].strip('"\'')
      if not _SECRET_RE.search(key):
        continue
      end,quoted=m.end(),''
      if end<len(text)and text[end]in '"\'':
        quoted=text[end]
        end+=1
        while end<len(text)and text[end]!=quoted:
          end+=2 if text[end]=='\\' else 1
        end=min(end+1,len(text))
      elif end<len(text)and text[end]in '{[':
        try:
          _,size=dec.raw_decode(text[end:])
          end+=size
        except ValueError:
          end=len(text)
      else:
        v=re.match(r'\S+',text[end:])
        end+=v.end()if v else 0
      out.extend((text[pos:m.end()],quoted+'***'+quoted))
      pos=end
    out.append(text[pos:])
    return re.sub(r'([a-zA-Z][\w+.-]*://)[^/@\s]+@',r'\1',''.join(out))
  return obj

def _redact_argv(argv):
  out,hide=[],False
  for tok in argv:
    if hide:
      out.append('***')
      hide=False
    elif tok.startswith('-')and '=' in tok:
      key,v=tok.split('=',1)
      key,v=next(iter(_redact({key:v}).items()))
      out.append(key+'='+v)
    elif re.fullmatch(r'-+[\w-]+',tok)and _SECRET_RE.search(tok):
      out.append(_redact(tok))
      hide=True
    else:
      out.append(_redact(tok))
  return ' '.join(out)

def _utcnow():
  return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')

def provenance(wandb,cwd=None):
  sha=_git('rev-parse','HEAD',cwd=cwd)
  env=os.environ
  keys='JOB_ID ARRAY_JOB_ID ARRAY_TASK_ID JOB_PARTITION JOB_NUM_NODES NTASKS RESTART_COUNT CLUSTER_NAME'.split()
  slurm={k.lower():env['SLURM_'+k]for k in keys if 'SLURM_'+k in env}
  return{'git_commit':sha,'git_dirty':bool(_git('status','--porcelain',cwd=cwd))if sha else None,'git_branch':_git('rev-parse','--abbrev-ref','HEAD',cwd=cwd),'git_remote':_redact(_git('config','--get','remote.origin.url',cwd=cwd)),'command':_redact_argv(sys.argv),'cwd':os.getcwd(),'host':socket.gethostname(),'python':sys.version.split()[0],'torch':_pkg_version('torch'),'wandb':wandb.__version__,'cuda_visible_devices':env.get('CUDA_VISIBLE_DEVICES'),'slurm':slurm or None,'started_at':_utcnow()}

def _flatten(d,prefix=''):
  flat={}
  for k,v in d.items():
    key=f'{prefix}{k}'
    if isinstance(v,Mapping):
      flat.update(_flatten(v,key+'.'))
    else:
      flat[key]=v
  return flat

def config_deltas(config,base):
  if not base:
    return{}
  cur,ref=(_flatten(_redact(config)),_flatten(_redact(base)))
  skip=('provenance.','family.')
  return{k:v for k,v in cur.items()if k not in('family','provenance')and not k.startswith(skip)and(not _SECRET_RE.search(k))and(k not in ref or ref[k]!=v)}

def _fmt_value(v):
  if isinstance(v,bool):
    return '' if v else 'off'
  if isinstance(v,float):
    if v!=0 and(abs(v)<0.01 or abs(v)>=100000.0):
      mant,exp=f'{v:.0e}'.split('e')
      return f'{mant}e{int(exp)}'
    return f'{v:g}'
  return _redact(str(v))

def _slugify(s):
  s=re.sub('[^A-Za-z0-9.+-]+','-',_redact(str(s))).strip('-')
  return re.sub('-{2,}','-',s)

def run_name(model,data,deltas=None,seed=None,max_len=64):
  parts=[_slugify(_redact(model)),_slugify(_redact(data))]
  for key,val in _redact(deltas or{}).items():
    if _SECRET_RE.search(str(key)):
      continue
    leaf=key.rsplit('.',1)[-1]
    if leaf=='seed':
      continue
    ab=_ABBREV.get(leaf,leaf.replace('_','')[:10])
    parts.append(_slugify(f'{ab}{_fmt_value(val)}'))
  if seed is not None:
    parts.append(_slugify(f's{_redact(seed)}'))
  name='-'.join((p for p in parts if p))
  if len(name)>max_len:
    digest=hashlib.sha1(name.encode()).hexdigest()[:6]
    name=name[:max_len-7].rstrip('-')+'-'+digest
  return name

def _rank():
  if 'RANK' in os.environ:
    return int(os.environ['RANK'])
  if 'SLURM_PROCID' in os.environ:
    rank=int(os.environ['SLURM_PROCID'])
    if rank!=0:
      return rank
  return int(os.environ.get('LOCAL_RANK','0'))

def _new_run_id():
  import secrets
  import string
  alphabet=string.ascii_lowercase+string.digits
  return ''.join((secrets.choice(alphabet)for _ in range(8)))

def _check_slug(v,what):
  if not _SLUG_RE.match(v):
    warnings.warn(f'{what}.slug: recommend kebab-case, 63 chars')

def _atomic(path,text):
  tmp=path.with_name(path.name+'.tmp')
  tmp.write_text(text)
  tmp.replace(path)

def _load(path):
  if not path.exists():
    return{}
  try:
    v=json.loads(path.read_text())
    if isinstance(v,dict):
      return v
  except ValueError:
    pass
  data=path.read_bytes()
  backup=path.with_name(path.name+'.corrupt-'+hashlib.sha256(data).hexdigest())
  if not backup.exists():
    backup.write_bytes(data)
  print('[wbkit] recovered incomplete logging metadata; original bytes retained',file=sys.stderr)
  return{}

def _json_default(v):
  if isinstance(v,Enum):
    return v.value
  for method in('tolist','item'):
    convert=getattr(v,method,None)
    if callable(convert):
      clean=convert()
      if clean is not v:
        return clean
  return str(v)

def start_run(wandb,*,project,family,job_type,model,data,hypothesis,config,expect=None,base_config=None,group=None,tags=(),summaries=None,entity=None,output_dir=None,name=None,rank=None,**init_kwargs):
  rank=_rank()if rank is None else rank
  if rank!=0:
    return wandb.init(mode='disabled')
  _check_slug(project,'project')
  if not isinstance(family,str)or not family:
    warnings.warn('family.empty: a non-empty family is recommended')
  if job_type not in JOB_TYPES:
    warnings.warn('job_type.nonstandard: use a conventional job type')
  if not hypothesis or len(hypothesis.strip())<10:
    warnings.warn('notes.short-hypothesis: recommend 10+ characters')
  if group is not None and len(group)>128:
    warnings.warn('group.long: at most 128 chars is recommended')
  prov=_redact(provenance(wandb))
  rid,resuming,saved=None,False,{}
  if output_dir is not None:
    out=Path(output_dir)
    out.mkdir(parents=True,exist_ok=True)
    saved=_load(out/'wandb_config.json')
    id_file=out/'wandb_run_id'
    rid=id_file.read_text().strip()if id_file.exists()else None
    rid=rid or saved.get('id')
    resuming=bool(rid)
    rid=rid or _new_run_id()
    _atomic(id_file,rid+'\n')
    init_kwargs.setdefault('dir',str(out))
    prov_file=out/'wandb_provenance.json'
    initial=_load(prov_file)if resuming else{}
    if not initial:
      initial=(saved.get('config')or{}).get('provenance')or prov
    _atomic(prov_file,json.dumps(_redact(initial))+'\n')
  else:
    initial=prov
  cfg=_redact({**config,'family':family,'provenance':initial})
  if resuming and isinstance(saved.get('config'),Mapping):
    cfg=_redact(saved['config'])
  cfg=_redact(json.loads(json.dumps(cfg,default=_json_default)))
  resumes=int(saved.get('resume_count',0))+1 if resuming else 0
  flat=_flatten(cfg)
  seed=flat.get('train.seed',flat.get('seed'))
  deltas=config_deltas(cfg,base_config)
  name=name or run_name(model,data,deltas,seed)
  notes=f"H: {(hypothesis or '').strip()}"
  if expect:
    notes+=f'\nExpect: {expect.strip()}'
  if deltas:
    notes+='\nDeltas vs base: '+', '.join(f'{k}={v}' for k,v in deltas.items())
  tag_list=[t.lower()for t in tags]
  if prov['git_dirty']:
    tag_list.append('dirty-tree')
  for t in tag_list:
    if len(t)>64:
      warnings.warn('tag.long: at most 64 chars is recommended')
  if output_dir is not None:
    _atomic(out/'wandb_config.json',json.dumps({'id':rid,'config':cfg,'resume_count':resumes})+'\n')
  payload=_redact(dict(entity=entity,project=project,group=group,job_type=job_type,name=name,notes=notes,tags=sorted(set(tag_list)),config=cfg,allow_val_change=resuming,id=rid,resume='allow' if rid else None,**init_kwargs))
  run=wandb.init(**payload)
  if resuming:
    run.summary['provenance/last_resume']=prov
    run.summary['provenance/resume_count']=resumes
  define_metrics(run,summaries=summaries)
  return run

def define_metrics(run,step_metric=STEP_METRIC,namespaces=METRIC_NAMESPACES,summaries=None):
  state=_metric_state(run)
  defs={**state['summaries'],**(summaries or{})}
  namespaces=tuple(dict.fromkeys((*state['namespaces'],*namespaces)))
  run.define_metric(_redact(step_metric))
  for ns in namespaces:
    run.define_metric(_redact(f'{ns}/*'),step_metric=_redact(step_metric))
  for key,how in defs.items():
    if how not in('min','max','mean','last'):
      raise ValueError(f'summary for {key!r} must be min, max, mean or last')
    run.define_metric(_redact(key),step_metric=_redact(step_metric),summary=how)
  state.update(summaries=defs,axis=step_metric,namespaces=namespaces)
_warned_keys:set[str]=set()

def log_step(run,step,metrics,commit=None):
  for k in metrics:
    if '/' not in k and k not in _warned_keys:
      _warned_keys.add(k)
      warnings.warn(f'metric {_redact(k)!r} needs a namespace')
  run.log(_redact({**metrics,_metric_state(run)['axis']:step}),commit=commit)

def finish(run,outcome,verdict,exit_code=None):
  if verdict not in VERDICTS:
    raise ValueError(f'verdict {verdict!r} not in {VERDICTS}')
  if getattr(run,'disabled',False)or not getattr(run,'id',None):
    return[]
  run.notes=_redact((run.notes or '')+f'\nOutcome ({verdict}): {outcome.strip()}')
  summary=_redact(dict(run.summary))
  for key in list(run.summary.keys()):
    if key not in summary:
      del run.summary[key]
  run.summary.update(summary)
  run.summary['verdict']=verdict
  if verdict=='broken':
    run.tags=tuple(sorted(set(run.tags or())|{'broken'}))
  issues=lint_run(name=run.name,notes=run.notes,group=run.group,job_type=run.job_type,config=dict(run.config),summary_keys=list(run.summary.keys()),tags=list(run.tags or()))
  for issue in issues:
    print(f'[wbkit] {issue}',file=sys.stderr)
  run.finish(exit_code=exit_code)
  _METRIC_STATE.pop(id(run),None)
  return issues

def lint_run(*,name,notes,group,job_type,config,summary_keys,tags=(),state=None,project=None):
  issues=[]
  if project and(not _SLUG_RE.fullmatch(project)):
    issues.append('project.slug: kebab-case is recommended')
  if notes and notes.startswith('H:')and(len(notes.splitlines()[0][2:].strip())<10):
    issues.append('notes.short-hypothesis: recommend a sentence')
  if not name or _RANDOM_NAME_RE.match(name):
    issues.append('name.random: use model-data-deltas-seed')
  if not notes or not notes.strip():
    issues.append("notes.empty: add H: hypothesis")
  elif 'H:' not in notes:
    issues.append("notes.no-hypothesis: no 'H:' line")
  if state=='finished' and notes and('Outcome' not in notes):
    issues.append("notes.no-outcome: add Outcome line")
  if not group:
    issues.append('group.missing: group one question')
  if job_type not in JOB_TYPES:
    issues.append(f"job_type.nonstandard: use one of {', '.join(JOB_TYPES)}")
  prov=config.get('provenance')or{}
  if isinstance(prov,Mapping)and isinstance(prov.get('value'),Mapping):
    prov=prov['value']
  if not isinstance(prov,Mapping):
    prov={}
  if not prov.get('git_commit'):
    issues.append('config.no-commit: missing git_commit')
  elif prov.get('git_dirty'):
    issues.append('config.dirty-tree: uncommitted launch')
  missing=[s for s in('arch','train','data')if s not in config]
  if missing:
    issues.append(f"config.flat: no {'/'.join(missing)} section(s)")
  bare=sorted((k for k in summary_keys if not k.startswith('_')and '/' not in k and(k!='verdict')))
  if bare:
    issues.append(f"metrics.unnamespaced: {', '.join(bare[:8])}")
  return issues
