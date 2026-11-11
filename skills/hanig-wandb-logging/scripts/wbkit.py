"""Stdlib helpers; caller supplies W&B."""
from __future__ import annotations
import argparse,dataclasses
from enum import Enum
import hashlib,datetime as _dt,importlib.metadata as _md,math,os,json,re,socket,subprocess,sys,types,warnings,weakref
from pathlib import Path
from typing import Mapping
__all__='JOB_TYPES VERDICTS provenance config_deltas run_name start_run define_metrics log_step finish lint_run'.split()
JOB_TYPES=('preprocess','train','finetune','eval','predict','sweep-trial','debug')
VERDICTS=('supported','refuted','inconclusive','broken')
METRIC_NAMESPACES=('train','val','eval','test','opt','perf','data','trainer')
STEP_METRIC='trainer/step'
_SL=re.compile('^[a-z0-9][a-z0-9-]{0,62}$')
_RN=re.compile('^[a-z]+-[a-z]+-\\d+$')
_PUBLIC_TOKEN=('pad','eos','bos','cls','sep','unk','mask','sos','decoder_start','special','stop','end','image','max')
_SEC=re.compile(r'(api[ _-]?key|'+''.join(r'(?<!'+p+r'[_-])'for p in _PUBLIC_TOKEN)+r'(?<!/)token(?=\.|$|[_-](?:value|secret|key|credential)(?:[._-]|$))|private[ _-]?key|secret[ _-]?(?:access[ _-]?)?key|secret(?![a-z])|authorization|passw(or)?d|credential)',re.I)
_AB=dict(zip('learning_rate lr weight_decay batch_size global_batch_size dropout num_layers n_layers hidden_dim d_model num_heads warmup_steps max_steps epochs max_epochs seed temperature mask_ratio grad_clip'.split(),'lr lr wd bs gbs do L L d d h wu steps ep ep s T mask clip'.split()))
_MS={}
def _metrics(run):
 q=id(run)
 if q not in _MS:
  try:
   ref=weakref.ref(run,lambda ref:_MS.pop(q,None))
  except TypeError:ref=lambda:run
  _MS[q]=(ref,{'summaries':{},'axis':STEP_METRIC,'namespaces':()})
 return _MS[q][1]
def _git(*args,cwd=None):
 try:
  out=subprocess.run(['git',*args],cwd=cwd,capture_output=True,text=True,timeout=10,check=True)
  return out.stdout.strip()or None
 except Exception:return None
def _pkg(name):
 try:
  return _md.version(name)
 except _md.PackageNotFoundError:return None
def _record(o):
 if dataclasses.is_dataclass(o)and not isinstance(o,type):return{f.name:getattr(o,f.name)for f in dataclasses.fields(o)}
 if isinstance(o,(argparse.Namespace,types.SimpleNamespace)):return vars(o)
 if isinstance(o,tuple)and hasattr(o,'_asdict'):return o._asdict()
 return o
def _secret_values(o):
 """Collect secret leaves."""
 found=set()
 def visit(v,secret=False):
  v=_record(v)
  if isinstance(v,Mapping):
   for k,x in v.items():visit(x,secret or bool(_SEC.search(str(k))))
  elif isinstance(v,(list,tuple)):
   for x in v:visit(x,secret)
  elif secret:
   if not isinstance(v,(str,int,float,bool,type(None))):
    x=_json(v)
    if x is not v:visit(x,True)
   else:
    text=str(v)
    if len(text)>=6:found.add(text)
 visit(o)
 return found
def _redact(o,known=()):
 """Mask secret-keyed config and known values; never scan free-text labels."""
 secrets=sorted(set(known)|_secret_values(o),key=lambda v:(-len(v),v))
 def clean(v):
  v=_record(v)
  if isinstance(v,Mapping):return{clean(k):'***'if _SEC.search(str(k))else clean(x)for k,x in v.items()}
  if isinstance(v,(list,tuple)):return[clean(x)for x in v]
  if isinstance(v,str):
   for secret in secrets:v=v.replace(secret,'***')
  return v
 return clean(o)
def _sent(run,o):return _redact(o,_metrics(run).get('secrets',()))
def _redact_argv(argv):
 """Command identity; option-shaped values before -- follow the flag rule."""
 flags=[]
 positional_count=0
 terminated=False
 for token in argv[1:]:
  numeric=False
  if token.startswith('-'):
   try:float(token);numeric=True
   except ValueError:pass
  if terminated or numeric:
   positional_count+=1
  elif token=='--':
   flags.append(token);terminated=True
  elif token.startswith('--'):
   flags.append(token.split('=',1)[0])
  elif token.startswith('-'):
   flags.append(token[:2])
  else:
   positional_count+=1
 return {
  'program':os.path.basename(argv[0])if argv else '',
  'flags':flags,
  'positional_count':positional_count,
 }
def _migrate_command(prov,current):
 if isinstance(prov,Mapping)and (isinstance(prov.get('command'),str)or isinstance(prov.get('command'),Mapping)and 'argv_sha256'in prov['command']):
  warnings.warn('provenance.legacy-command: replaced with current launch identity')
  return {**prov,'command':current}
 return prov
def _now():return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')
def provenance(wandb,cwd=None):
 sha=_git('rev-parse','HEAD',cwd=cwd)
 ev=os.environ
 ks='JOB_ID ARRAY_JOB_ID ARRAY_TASK_ID JOB_PARTITION JOB_NUM_NODES NTASKS RESTART_COUNT CLUSTER_NAME'.split()
 slurm={k.lower():ev['SLURM_'+k]for k in ks if 'SLURM_'+k in ev}
 return{'git_commit':sha,'git_dirty':bool(_git('status','--porcelain',cwd=cwd))if sha else None,'git_branch':_git('rev-parse','--abbrev-ref','HEAD',cwd=cwd),'git_remote':_redact(_git('config','--get','remote.origin.url',cwd=cwd)),'command':_redact_argv(sys.argv),'cwd':os.getcwd(),'host':socket.gethostname(),'python':sys.version.split()[0],'torch':_pkg('torch'),'wandb':wandb.__version__,'cuda_visible_devices':ev.get('CUDA_VISIBLE_DEVICES'),'slurm':slurm or None,'started_at':_now()}
def _flat(d,p=''):
 f={}
 for k,v in d.items():
  q=f'{p}{k}'
  if isinstance(v,Mapping):f.update(_flat(v,q+'.'))
  else:
   f[q]=v
 return f
def config_deltas(config,base):
 if not base:return{}
 cur,ref=(_flat(_norm(config)),_flat(_norm(base)))
 skip=('provenance.','family.')
 return{k:v for k,v in cur.items()if k not in('family','provenance')and not k.startswith(skip)and(not _SEC.search(k))and(k not in ref or ref[k]!=v)}
def _fmt(v):
 if isinstance(v,bool):return '' if v else 'off'
 if isinstance(v,float):
  if math.isfinite(v)and v!=0 and(abs(v)<0.01 or abs(v)>=100000.0):
   mant,exp=f'{v:.0e}'.split('e')
   return f'{mant}e{int(exp)}'
  return f'{v:g}'
 return _redact(str(v))
def _slugify(s):
 s=re.sub('[^A-Za-z0-9.+-]+','-',_redact(str(s))).strip('-')
 return re.sub('-{2,}','-',s)
def run_name(model,data,deltas=None,seed=None,max_len=64):
 known=_secret_values({'deltas':deltas,'seed':seed})
 ps=[_slugify(_redact(model,known)),_slugify(_redact(data,known))]
 for q,val in _redact(deltas or{},known).items():
  if _SEC.search(str(q)):
   continue
  leaf=q.rsplit('.',1)[-1]
  if leaf=='seed':
   continue
  ab=_AB.get(leaf,leaf.replace('_','')[:10])
  ps.append(_slugify(f'{ab}{_fmt(val)}'))
 if seed is not None:ps.append(_slugify(f's{_redact(seed)}'))
 name='-'.join((p for p in ps if p))
 if len(name)>max_len:
  digest=hashlib.sha1(name.encode()).hexdigest()[:6]
  name=name[:max_len-7].rstrip('-')+'-'+digest
 return name
def _rank():
 for q in('RANK','SLURM_PROCID','LOCAL_RANK'):
  v=os.environ.get(q,'').strip()
  if v:return int(v)
 return 0
def _id():
 import secrets
 import string
 alphabet=string.ascii_lowercase+string.digits
 return ''.join((secrets.choice(alphabet)for _ in range(8)))
def _slug(v,what):
 if not _SL.match(v):warnings.warn(f'{what}.slug: prefer kebab-case <=63 chars')
def _atomic(p,t):
 tmp=p.with_name(p.name+'.tmp')
 tmp.write_bytes(t.encode()if isinstance(t,str)else t)
 tmp.replace(p)
def _load(p):
 if not p.exists():return{}
 try:
  v=json.loads(p.read_text())
  if isinstance(v,dict):return v
 except ValueError:
  pass
 data=p.read_bytes()
 bak=p.with_name(p.name+'.corrupt-'+hashlib.sha256(data).hexdigest())
 if not bak.exists()or bak.read_bytes()!=data:_atomic(bak,data)
 print('[wbkit] recovered metadata; bytes kept',file=sys.stderr)
 return{}
def _json(v):
 if isinstance(v,Enum):return v.value
 for method in('tolist','item'):
  convert=getattr(v,method,None)
  if callable(convert):
   c=convert()
   if c is not v:return c
 return str(v)
def _norm(v):return _redact(json.loads(json.dumps(_redact(v),default=_json)))
def start_run(wandb,*,project,family,job_type,model,data,hypothesis,config,expect=None,base_config=None,group=None,tags=(),summaries=None,entity=None,output_dir=None,name=None,rank=None,**kw):
 rank=_rank()if rank is None else rank
 if rank!=0:return wandb.init(mode='disabled')
 _slug(project,'project')
 if not isinstance(family,str)or not family:warnings.warn('family.empty: recommend a family')
 if job_type not in JOB_TYPES:warnings.warn('job_type.nonstandard: use JOB_TYPES')
 if not hypothesis or len(hypothesis.strip())<10:warnings.warn('notes.short-hypothesis: recommend 10+ chars')
 if group is not None and len(group)>128:warnings.warn('group.long: recommend <=128 chars')
 known=_secret_values(config)
 prov=_redact(provenance(wandb),known)
 rid,again,saved=None,False,{}
 if output_dir is not None:
  out=Path(output_dir)
  out.mkdir(parents=True,exist_ok=True)
  sf=out/'wandb_config.json'
  bf=out/'wandb_config.backup.json'
  saved=_load(sf)
  if not isinstance(saved.get('config'),Mapping):
   saved=_load(bf)
   if(sf.exists()or bf.exists()or(out/'wandb_config.ready').exists())and not isinstance(saved.get('config'),Mapping):raise ValueError('recover wandb_config.json')
  idf=out/'wandb_run_id'
  rid=idf.read_text().strip()if idf.exists()else None
  rid=rid or saved.get('id')
  if saved and saved.get('id')!=rid:raise ValueError('recover matching run state')
  again=bool(rid)
  rid=rid or _id()
  _atomic(idf,rid+'\n')
  kw.setdefault('dir',str(out))
  pf=out/'wandb_provenance.json'
  p0=_load(pf)if again else{}
  if not p0:p0=(saved.get('config')or{}).get('provenance')or prov
  p0=_migrate_command(p0,prov['command'])
  _atomic(pf,json.dumps(_redact(p0,known))+'\n')
 else:
  p0=prov
 cfg=_redact({**config,'family':family,'provenance':p0})
 if again and isinstance(saved.get('config'),Mapping):
  cfg=_redact(saved['config'])
  old={k:v for k,v in cfg.items()if k!='provenance'}
  current={k:v for k,v in _norm({**config,'family':family}).items()if k!='provenance'}
  if current!=old:warnings.warn('resume.config-mismatch: saved logging config retained; restore caller training config separately')
 if isinstance(cfg.get('provenance'),Mapping):
  cfg['provenance']=_migrate_command(cfg['provenance'],prov['command'])
 cfg=_redact(_norm(cfg),known)
 rc=int(saved.get('resume_count',0))+1 if again else 0
 f=_flat(cfg)
 seed=f.get('train.seed',f.get('seed'))
 deltas=config_deltas(cfg,base_config)
 name=saved.get('name')or name
 if not name:
  if again and saved:
   warnings.warn('resume.legacy-name: set name')
   model,data='resume',rid
  name=run_name(_redact(model,known),_redact(data,known),deltas,seed)
 name=_redact(name,known)
 notes=f"H: {(hypothesis or '').strip()}"
 if expect:
  notes+=f'\nExpect: {expect.strip()}'
 if deltas:
  notes+='\nDeltas vs base: '+', '.join(f'{k}={json.dumps(v)}' for k,v in deltas.items())
 if again and saved and 'notes'not in saved:warnings.warn('resume.legacy-notes: supply original hypothesis/expect')
 notes=_redact(saved.get('notes')or notes,known)
 tag_list=[t.lower()for t in tags]
 if prov['git_dirty']:tag_list.append('dirty-tree')
 for t in tag_list:
  if len(t)>64:warnings.warn('tag.long: recommend <=64 chars')
 if output_dir is not None:
  rec=json.dumps({'id':rid,'config':cfg,'resume_count':rc,'name':name,'notes':notes})+'\n'
  _atomic(out/'wandb_config.json',rec)
  _atomic(out/'wandb_config.backup.json',rec)
  _atomic(out/'wandb_config.ready',rid+'\n')
 args=_redact(dict(entity=entity,project=project,group=group,job_type=job_type,name=name,notes=notes,tags=sorted(set(tag_list)),config=cfg,allow_val_change=again,id=rid,resume='allow' if rid else None,**kw),known)
 run=wandb.init(**args)
 _metrics(run)['secrets']=known
 if again:
  run.summary['provenance/last_resume']=prov
  run.summary['provenance/resume_count']=rc
 define_metrics(run,summaries=summaries)
 return run
def define_metrics(run,step_metric=STEP_METRIC,namespaces=METRIC_NAMESPACES,summaries=None):
 state=_metrics(run)
 defs={**state['summaries'],**(summaries or{})}
 namespaces=tuple(dict.fromkeys((*state['namespaces'],*namespaces)))
 run.define_metric(_sent(run,step_metric))
 for ns in namespaces:
  run.define_metric(_sent(run,f'{ns}/*'),step_metric=_sent(run,step_metric))
 for q,how in defs.items():
  if how and not set(how.lower().split(','))<=set('min max mean last first copy none'.split()):raise ValueError('invalid summary')
  run.define_metric(_sent(run,q),step_metric=_sent(run,step_metric),summary=how)
 state.update(summaries=defs,axis=step_metric,namespaces=namespaces)
_warned:set[str]=set()
def log_step(run,step,metrics,commit=None):
 for k in metrics:
  if '/' not in k and k not in _warned:
   _warned.add(k)
   warnings.warn(f'metric {_sent(run,k)!r} needs a namespace')
 run.log(_sent(run,{**metrics,_metrics(run)['axis']:step}),commit=commit)
def finish(run,outcome,verdict,exit_code=None):
 if verdict not in VERDICTS:raise ValueError(f'verdict {verdict!r} not in {VERDICTS}')
 if getattr(run,'disabled',False)or not getattr(run,'id',None):return[]
 run.notes=_sent(run,(run.notes or '')+f'\nOutcome ({verdict}): {outcome.strip()}')
 sm=_sent(run,dict(run.summary))
 for q in list(run.summary.keys()):
  if q not in sm:
   del run.summary[q]
 run.summary.update(sm)
 run.summary['verdict']=verdict
 if verdict=='broken':run.tags=tuple(sorted(set(run.tags or())|{'broken'}))
 xs=lint_run(name=run.name,notes=run.notes,group=run.group,job_type=run.job_type,config=dict(run.config),summary_keys=list(run.summary.keys()),tags=list(run.tags or()))
 for err in xs:
  print(f'[wbkit] {err}',file=sys.stderr)
 run.finish(exit_code=exit_code)
 _MS.pop(id(run),None)
 return xs
def lint_run(*,name,notes,group,job_type,config,summary_keys,tags=(),state=None,project=None):
 xs=[]
 if project and(not _SL.fullmatch(project)):xs.append('project.slug: kebab-case is recommended')
 if notes and notes.startswith('H:')and(len(notes.splitlines()[0][2:].strip())<10):xs.append('notes.short-hypothesis: add a sentence')
 if not name or _RN.match(name):xs.append('name.random: model-data-deltas-seed')
 if not notes or not notes.strip():xs.append("notes.empty: add H: hypothesis")
 elif 'H:' not in notes:xs.append("notes.no-hypothesis: add H:")
 if state=='finished' and notes and('Outcome' not in notes):xs.append("notes.no-outcome: add Outcome")
 if not group:xs.append('group.missing: add a group')
 if job_type not in JOB_TYPES:xs.append(f"job_type.nonstandard: use one of {', '.join(JOB_TYPES)}")
 prov=config.get('provenance')or{}
 if isinstance(prov,Mapping)and isinstance(prov.get('value'),Mapping):prov=prov['value']
 if not isinstance(prov,Mapping):prov={}
 if not prov.get('git_commit'):xs.append('config.no-commit: missing git_commit')
 elif prov.get('git_dirty'):xs.append('config.dirty-tree: uncommitted launch')
 miss=[s for s in('arch','train','data')if s not in config]
 if miss:xs.append(f"config.flat: no {'/'.join(miss)} section(s)")
 b=sorted((k for k in summary_keys if not k.startswith('_')and '/' not in k and(k!='verdict')))
 if b:xs.append(f"metrics.unnamespaced: {', '.join(b[:8])}")
 return xs
