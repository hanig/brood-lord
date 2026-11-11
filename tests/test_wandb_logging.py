"""Core regressions."""
from pathlib import Path
import argparse,hashlib,collections,importlib,importlib.util,itertools,dataclasses
from enum import Enum
import json,os,sys,subprocess,warnings
from tempfile import TemporaryDirectory as TD
import types,unittest
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1]/'skills/hanig-wandb-logging/scripts'
sys.path.insert(0,str(SCRIPTS))
import wbkit
PR='provenance'
TR='train'
RK='redacted_keys'
CFG='config'
AK='api_key'
H='CANARY'
S='synthetic-hidden'
T='SENTINEL'
CF='wandb_config.json'
BF='wandb_config.backup.json'
PF='wandb_provenance.json'
IDF='wandb_run_id'
RF='wandb_config.ready'
CG='wandb_config*.json'
AX='trainer/step'
V='val/loss'
def recipe(train):return{'arch':{},TR:train,'data':{}}
def shown(r):return repr(r.__dict__)
class Checks(unittest.TestCase):
 has=unittest.TestCase.assertIn
 kind=unittest.TestCase.assertIsInstance
 no=unittest.TestCase.assertFalse
 yes=unittest.TestCase.assertTrue
 le=unittest.TestCase.assertLessEqual
 warns=unittest.TestCase.assertWarnsRegex
 raises=unittest.TestCase.assertRaisesRegex
 eq=unittest.TestCase.assertEqual
 lacks=unittest.TestCase.assertNotIn
 def clean(s,r,x=H):s.lacks(x,shown(r))
class FakeRun:
 def __init__(s,**kw):s.__dict__.update(kw,id=kw.get('id')or 'abc123xy',config=kw.get(CFG)or{},summary={},disabled=kw.get('mode')=='disabled',url=None,definitions=[],logged=[])
 def define_metric(s,name,*,step_metric=None,summary=None):s.definitions.append((name,step_metric,summary))
 def log(s,metrics,**kw):s.logged.append(metrics)
 def finish(s,**kw):s.finished=True
class FakeWandb:
 __version__='0.31.0'
 def init(s,**kw):
  s.run=FakeRun(**kw)
  return s.run
def start(sdk=None,**kw):
 sdk=FakeWandb()if sdk is None else sdk
 args=dict(project='sample-train',family='sample',job_type=TR,model='sample',data='cells',hypothesis='A hypothesis.',expect='val/loss < 0.5',group='20261109-test',config={'arch':{'width':8},TR:{'seed':0},'data':{}})
 args.update(kw)
 with patch.object(wbkit,'_git',return_value=None):
  return wbkit.start_run(sdk,**args)
class Logging(Checks):
 def test_names(s):
  s.eq(wbkit.run_name('sample','cells',{'train.lr':0.0003},2),'sample-cells-lr3e-4-s2')
  s.eq(wbkit.config_deltas({TR:{'lr':2,'seed':1}},{TR:{'lr':1,'seed':1}}),{'train.lr':2})
  s.eq(wbkit.config_deltas({'lr':1},None),{})
  s.lacks(S,wbkit.run_name('sample','cells',{AK:S}))
  s.le(len(wbkit.run_name('x'*90,'cells')),64)
 def test_resume(s):
  with TD()as tmp:
   a=start(output_dir=tmp)
   b=start(output_dir=tmp)
   s.assertRegex(a.id,'^[a-z0-9]{8}$')
   s.eq(b.id,a.id)
   s.eq(a.config,b.config)
   s.eq(b.config[TR]['seed'],0)
   s.eq(b.summary['provenance/resume_count'],1)
   s.eq((Path(tmp)/IDF).read_text().strip(),a.id)
 def test_metrics(s):
  r=start(summaries={V:'min','val/score':'max'})
  s.has((V,AX,'min'),r.definitions)
  s.has(('val/score',AX,'max'),r.definitions)
  wbkit.log_step(r,3,{'train/loss':0.1})
  s.eq(r.logged[-1][AX],3)
  with patch.dict(os.environ,{'RANK':'1'}):
   d=start()
  s.yes(d.disabled)
  s.eq(wbkit.finish(d,'unused','inconclusive'),[])
 def test_lint(s):
  xs=wbkit.lint_run(name='happy-fox-2',notes='',group=None,job_type=None,config={},summary_keys=['loss'])
  for q in['name.random','notes.empty','group.missing','config.flat','metrics.unnamespaced']:
   s.yes(any((i.startswith(q+':')for i in xs)))
class CoreCases(Checks):
 def test_rank(s):
  for g,l in itertools.product(('0','4'),('0','1')):
   env={'SLURM_NTASKS':'8','SLURM_PROCID':g,'LOCAL_RANK':l}
   with s.subTest(env=env),patch.dict(os.environ,env,clear=True):
    s.eq(start().disabled,g!='0')
 def test_axis(s):
  r=start(summaries={'eval/loss':'min'})
  wbkit.log_step(r,5,{'extra/loss':.3})
  s.has(('extra/*',AX,None),r.definitions)
  wbkit.define_metrics(r,namespaces=('aux',))
  wbkit.define_metrics(r,step_metric='train/global_step')
  defs={n:(axis,summary)for n,axis,summary in r.definitions}
  s.eq(defs['train/*'][0],'train/global_step')
  s.eq(defs['aux/*'][0],'train/global_step')
  s.eq(defs['eval/loss'],('train/global_step','min'))
  wbkit.log_step(r,7,{'train/loss':0.2})
  s.eq(r.logged[-1]['train/global_step'],7)
  b=start()
  s.lacks('eval/loss',[d[0]for d in b.definitions])
 def test_family(s):
  for f in('Ab','A b'):
   s.eq(start(family=f).config['family'],f)
class RecordCases(Checks):
 def test_records(s):
  @dataclasses.dataclass
  class Recipe:
   api_key:str
   width:int=8
  rs=(Recipe,argparse.Namespace,types.SimpleNamespace,collections.namedtuple('Record','api_key width'))
  ws=(lambda x:x,lambda x:[x],lambda x:{'recipe':x})
  for rc,w in itertools.product(rs,ws):
   c=recipe(w(rc(api_key=H,width=8)))
   base={TR:w(rc(api_key='old-value',width=8))}
   r=start(config=c,base_config=base)
   s.clean(r)
   s.has('8',repr(r.config))
 def test_fields(s):
  c={TR:{'max_tokens':1024,'tokens_per_batch':32,'tokenizer':'wordpiece'}}
  base={TR:{'max_tokens':512,'tokens_per_batch':16,'tokenizer':'character'}}
  r=start(config=c,base_config=base)
  s.eq(r.config[TR],c[TR])
  s.eq(wbkit.config_deltas(c,base),{'train.'+k:v for k,v in c[TR].items()})
  for k in('access_token',AK,'client_secret','password','credentials'):
   s.eq(wbkit._redact({k:H}),{})
 def test_rank(s):
  for p,l,n in itertools.product(('0','4'),('0','1'),(None,'8')):
   env={'SLURM_PROCID':p,'LOCAL_RANK':l}
   if n is not None:env['SLURM_NTASKS']=n
   with s.subTest(env=env),patch.dict(os.environ,env,clear=True):
    s.eq(start().disabled,p!='0')
@unittest.skipUnless(importlib.util.find_spec('wandb'),'wandb absent: offline integration needs caller-installed SDK')
class Offline(Checks):
 def test_offline(s):
  sdk=importlib.import_module('wandb')
  with TD()as tmp,patch.dict(os.environ,{'WANDB_MODE':'offline'}):
   r=start(sdk,output_dir=tmp,rank=0)
   rid,c=(r.id,dict(r.config))
   r.finish()
   r=start(sdk,output_dir=tmp,rank=0)
   s.eq(r.id,rid)
   s.eq(dict(r.config),c)
   s.eq(r.summary['provenance/resume_count'],1)
   path=Path(tmp)/'weights';path.write_bytes(b'weights')
   wbkit.log_dataset(sdk,r,'cells-filtered',path,summary='Offline data.',contents='Test bytes.',intended_use='Smoke test.')
   wbkit.log_model(sdk,r,path,step=1)
   wbkit.finish(r,'Offline check only.','inconclusive')
   d=start(sdk,rank=1)
   s.yes(d.disabled)
   d.finish()
class PartOne(Checks):
 def test_advice(s):
  for project in('state3_pretrain','sample.train'):
   with s.subTest(project=project),s.assertWarns(UserWarning):
    r=start(project=project,hypothesis='H0')
   s.eq(r.project,project)
   s.eq(r.notes.splitlines()[0],'H: H0')
   xs=wbkit.lint_run(name=r.name,notes=r.notes,group=r.group,job_type=r.job_type,config=r.config,summary_keys=[],project=project)
   s.yes(any((x.startswith('project.slug:')for x in xs)))
   s.yes(any((x.startswith('notes.short-hypothesis:')for x in xs)))
 def test_resumes(s):
  with TD()as tmp:
   a=start(output_dir=tmp)
   b=start(output_dir=tmp,config={TR:{'seed':9}})
   c=start(output_dir=tmp)
   s.eq(b.config,a.config)
   s.eq(c.config,a.config)
   s.eq(c.summary['provenance/resume_count'],2)
class RoundOne(Checks):
 def test_config(s):
  f=('token_count','token_embedding_dim','pad_token_id','mask_token_id','token_acc','token_loss','token_budget','token-count')
  c={TR:dict.fromkeys(f,8192)}
  r=start(config=c,base_config={TR:dict.fromkeys(f,4096)})
  s.eq(r.config[TR],c[TR])
  s.eq(wbkit.config_deltas(c,{TR:{}}),{'train.'+k:8192 for k in f})
  for k in f:s.has(k+'=8192',r.notes)
  s.eq(wbkit._redact({'token':T,'access_token':T}),{'token':T})
 def test_metrics(s):
  r=start()
  wbkit.log_step(r,5,{'train/token_acc':.91})
  s.eq(r.logged[-1]['train/token_acc'],.91)
  r.summary['val/token_loss']={'min':.1}
  wbkit.finish(r,'A recorded result.','inconclusive')
  s.eq(r.summary['val/token_loss'],{'min':.1})
 def test_labels(s):
  with TD()as tmp:
   base={TR:{'lr':.1,'seed':0}}
   a=start(output_dir=tmp,base_config=base,config={TR:{'lr':.2,'seed':1}})
   b=start(output_dir=tmp,base_config=base,config={TR:{'lr':.9,'seed':9}})
   s.eq(b.config,a.config)
   s.eq(b.name,a.name)
   s.eq(b.notes,a.notes)
 def test_partial(s):
  with TD()as tmp:
   p=Path(tmp)/PF
   p.write_text('{')
   (Path(tmp)/IDF).write_text('abc123xy\n')
   r=start(output_dir=tmp)
   s.eq(r.id,'abc123xy')
   s.eq(r.summary['provenance/resume_count'],1)
   s.kind(json.loads(p.read_text()),dict)
   s.has(b'{',[p.read_bytes()for p in Path(tmp).glob('*.corrupt-*')])
 def test_family(s):
  r=start(family='want',config={'family':'other',TR:{}})
  s.eq(r.config['family'],'want')
  s.eq(wbkit.config_deltas(r.config,{TR:{}}),{})
 def test_types(s):
  class Choice(Enum):
   A='chosen'
  class Scalar:
   def item(s):return 7
  class Array:
   def tolist(s):return[1,2]
  with TD()as tmp:
   c={TR:{'path':Path(tmp)/'data','choice':Choice.A,'number':Scalar(),'array':Array()}}
   r=start(output_dir=tmp,config=c)
   s.eq(r.config[TR],{'path':str(Path(tmp)/'data'),'choice':'chosen','number':7,'array':[1,2]})
   s.eq(start(output_dir=tmp,config=c).config,r.config)
 def test_verbatim(s):
  for t in('val/loss in [0.50,0.60]','--lrs=[1e-3,1e-4]','{"x":  1}'):
   with s.subTest(text=t):s.eq(wbkit._redact(t),t)
  t='val/loss in [0.50,0.60]'
  with patch.object(sys,'argv',['train.py','--lrs=[1e-3,1e-4]']):
   r=start(expect=t)
  s.has('Expect: '+t,r.notes)
  s.eq(r.config[PR]['command']['flags'],['--lrs'])
class Atomic(Checks):
 def test_writes(s):
  real=Path.write_bytes
  for stop in range(5):
   with s.subTest(stop=stop),TD()as tmp:
    calls=[0]
    def partial(p,t,*args,**kw):
     n=calls[0]
     calls[0]+=1
     if n==stop:
      real(p,t[:1])
      raise InterruptedError('simulated preemption')
     return real(p,t,*args,**kw)
    with patch.object(Path,'write_bytes',partial),s.assertRaises(InterruptedError):
     start(output_dir=tmp)
    for name in(IDF,PF,CF,BF):
     p=Path(tmp)/name
     if p.exists():
      t=p.read_text()
      if name.endswith('.json'):s.kind(json.loads(t),dict)
      else:s.assertRegex(t,r'^[a-z0-9]{8}\n$')
    s.yes(start(output_dir=tmp).id)
class RoundTwo(Checks):
 def test_secretory(s):
  f=('secretory_cell_fraction','secreted_protein','secretome_size')
  c={'data':dict.fromkeys(f,.25)}
  r=start(config=c,base_config={'data':dict.fromkeys(f,.1)})
  s.eq(r.config['data'],c['data'])
  s.eq(wbkit.config_deltas(c,{'data':{}}),{'data.'+k:.25 for k in f})
  wbkit.log_step(r,1,{'data/'+k:.25 for k in f})
  for k in f:s.eq(r.logged[-1]['data/'+k],.25)
  for k in('secret','client_secret','secret_key'):
   s.eq(wbkit._redact({k:T}),{})
 def test_priority(s):
  with patch.dict(os.environ,{'SLURM_PROCID':'0','LOCAL_RANK':'1'},clear=True):
   s.no(start().disabled)
 def test_name(s):
  for legacy in(False,True):
   with TD()as tmp:
    a=start(output_dir=tmp,model='small',data='cells',config={'arch':{'model':'small'},'data':{'dataset':'cells'},TR:{'seed':0}})
    if legacy:
     for p in Path(tmp).glob(CG):
      v=json.loads(p.read_text());v.pop('name',None);p.write_text(json.dumps(v))
    b=start(output_dir=tmp,model='large',data='images',config={TR:{'seed':9}})
    s.eq(a.config,b.config)
    if not legacy:s.eq(a.name,b.name)
    s.lacks('large',b.name);s.lacks('images',b.name)
 def test_summaries(s):
  ops='min max mean last first copy none'.split()
  cases=[','.join(p)for n in(1,2)for p in itertools.product(ops,repeat=n)]+[None,'','MIN,MAX']
  for how in cases:
   r=start(summaries={V:how})
   s.has((V,AX,how),r.definitions)
  for how in('bad','min,bad'):
   with s.assertRaises(ValueError):start(summaries={V:how})
 def test_recovery(s):
  with TD()as tmp:
   a=start(output_dir=tmp,config={TR:{'lr':.1}})
   p=Path(tmp)/CF;raw=p.read_text();bad='{"cut":';p.write_text(bad)
   real=Path.write_bytes
   def cut(p,t):real(p,t[:1]);raise InterruptedError
   with patch.object(Path,'write_bytes',cut),s.assertRaises(InterruptedError):start(output_dir=tmp)
   s.eq(p.read_text(),bad)
   q=Path(tmp)/(p.name+'.corrupt-'+hashlib.sha256(bad.encode()).hexdigest())
   if q.exists():s.eq(q.read_bytes(),bad.encode())
   b=start(output_dir=tmp,config={TR:{'lr':.9}})
   s.eq(a.id,b.id);s.eq(a.config,b.config)
   s.eq(b.summary['provenance/resume_count'],1)
   s.eq(q.read_bytes(),bad.encode())
   q.write_bytes(b'?');p.write_text(bad);start(output_dir=tmp)
   s.eq(q.read_bytes(),bad.encode())
   for q in Path(tmp).glob('*backup*'):q.unlink()
   p.write_text('{');sdk=FakeWandb()
   with s.raises(ValueError,'recover'):start(sdk,output_dir=tmp)
   s.eq(p.read_text(),'{');s.no(hasattr(sdk,'run'))
   p.write_text(raw);(Path(tmp)/IDF).write_text('different')
   with s.raises(ValueError,'recover'):start(sdk,output_dir=tmp)
   s.no(hasattr(sdk,'run'))
 def test_empty(s):
  keys=('RANK','SLURM_PROCID','LOCAL_RANK')
  for vals in itertools.product((None,'','0','4'),repeat=3):
   env={k:v for k,v in zip(keys,vals)if v is not None}
   want=next((int(v)for v in vals if v),0)
   with s.subTest(env=env),patch.dict(os.environ,env,clear=True):
    s.eq(start().disabled,want!=0)
class LastReview(Checks):
 def test_array_base(s):
  class Array:
   def tolist(s):return[128,256]
   def __ne__(s,other):return s
   def __bool__(s):raise ValueError('ambiguous truth value')
  c=recipe({'layers':Array()});r=start(rank=0,config=c,base_config=c)
  s.eq(r.config[TR]['layers'],[128,256]);s.lacks('layers=',r.notes)
  s.eq(wbkit.config_deltas(c,c),{})
  s.eq(wbkit.config_deltas(c,recipe({'layers':[64]})),{'train.layers':[128,256]})
 def test_resume_contract(s):
  with TD()as d:
   a=start(rank=0,output_dir=d,name='small-cells',expect='val/loss < 0.5',config=recipe({'width':8}))
   for i in range(2):
    if i:(Path(d)/CF).unlink()
    b=start(rank=0,output_dir=d,name='large-images',hypothesis='A changed hypothesis.',expect='val/loss < 0.9',config=recipe({'width':64}))
    s.eq((a.id,a.config,a.name,a.notes),(b.id,b.config,b.name,b.notes))
    s.eq(b.summary['provenance/resume_count'],i+1)
 def test_missing_snapshot(s):
  for a,b in itertools.product((None,'{','[]'),repeat=2):
   if a is None and b is None:continue
   with s.subTest(primary=a,backup=b),TD()as d:
    start(rank=0,output_dir=d,config=recipe({'lr':.1}));p=Path(d)
    for f,v in zip((CF,BF),(a,b)):
     q=p/f
     if v is None:q.unlink()
     else:q.write_text(v)
    sdk=FakeWandb()
    with s.raises(ValueError,'recover'):start(sdk,rank=0,output_dir=d,config=recipe({'lr':.9}))
    s.no(hasattr(sdk,'run'))
    for f,v in zip((CF,BF),(a,b)):
     if v is not None:s.eq((p/f).read_text(),v)
 def test_infinite_delta(s):
  for v in(float('inf'),float('-inf'),float('nan')):
   r=start(rank=0,config=recipe({'grad_clip':v}),base_config=recipe({'grad_clip':1.}))
   s.has('clip',r.name);got=r.config[TR]['grad_clip'];s.kind(got,float)
   if v==v:s.eq(got,v)
   else:s.assertNotEqual(got,got)
class CommandProvenance(Checks):
 """Exhaustive: 12 token shapes x 2 values; exact dictionaries, no digest."""
 def test_shapes(s):
  for value in ('VALUE_CANARY_A','VALUE_CANARY_B'):
   cases=(
    (['--k='+value],['--k'],0),
    (['--k',value],['--k'],1),
    (['-k'+value],['-k'],0),
    (['-k',value],['-k'],1),
    (['-xvf'],['-x'],0),
    (['-'],['-'],0),
    (['--'],['--'],0),
    ([value],[],1),
    (['--','--'+value],['--'],1),
    (['--shift','-1'],['--shift'],1),
    (['--shift','-0.5','-1e-3'],['--shift'],2),
    (['--config',json.dumps({AK:value})],['--config'],1),
   )
   for tokens,flags,count in cases:
    argv=['/private/project/train.py']+tokens
    with s.subTest(tokens=tokens),patch.object(sys,'argv',argv):
     want=dict(program='train.py',flags=flags,positional_count=count)
     got=wbkit._redact_argv(argv)
     s.eq(got,want)
     s.eq(start().config[PR]['command'],want)
     s.lacks(value,json.dumps(got));s.lacks('/private/project',json.dumps(got))
 def test_legacy_snapshot(s):
  # Exhaustive: 3 sources x 2 layouts.
  for source,command in itertools.product(
    ('primary','backup',PR),
    ('train.py -k LEGACY_VALUE_CANARY','train.py --k=LEGACY_VALUE_CANARY')):
   with s.subTest(source=source,command=command),TD()as d:
    a=start(output_dir=d);p=Path(d)
    if source==PR:
     f=p/PF;obj=json.loads(f.read_text());obj['command']=command
    else:
     f=p/(CF if source=='primary'else BF)
     obj=json.loads(f.read_text());obj[CFG][PR]['command']=command
     if source=='backup':(p/CF).unlink()
    f.write_text(json.dumps(obj))
    if source=='primary':(p/BF).write_text(json.dumps(obj))
    with s.warns(UserWarning,'legacy-command'):
     b=start(output_dir=d,config=recipe({'lr':9}))
    s.eq(a.id,b.id);s.eq(b.config[TR],a.config[TR])
    s.clean(b,'LEGACY_VALUE_CANARY')
    for f in p.glob('*.json'):s.lacks('LEGACY_VALUE_CANARY',f.read_text())
    s.kind(b.config[PR]['command'],dict)
 def test_combined(s):
  argv=['train.py','--k=SECRET','--k','SECRET','-kSECRET','-k','SECRET',
        '-xvf','-','--','positional','{"api_key":"SECRET"}']
  s.eq(wbkit._redact_argv(argv)['flags'],['--k','--k','-k','-k','-x','-','--'])
  s.eq(wbkit._redact_argv(argv)['positional_count'],4)
  s.lacks('SECRET',repr(wbkit._redact_argv(argv)))
  s.eq(wbkit._redact_argv([])['program'],'')
 def test_terminator(s):
  argv=['train.py','--','--CANARY']
  s.eq(wbkit._redact_argv(argv)['flags'],['--'])
  s.eq(wbkit._redact_argv(argv)['positional_count'],1)
 def test_numeric(s):
  for value in ('-1','-0.5','-1e-3'):
   with s.subTest(value=value):
    argv=['train.py','--shift',value]
    got=wbkit._redact_argv(argv)
    s.eq(got['flags'],['--shift']);s.eq(got['positional_count'],1)
 def test_option_shaped_limit(s):
  s.eq(wbkit._redact_argv(['train.py','--name','--CANARY'])['flags'],['--name','--CANARY'])
class ContinuationReview(Checks):
 def test_lost_snapshots(s):
  with TD()as d:
   a=start(rank=0,output_dir=d,config=recipe({'lr':.1}));a.finish()
   p=Path(d);good=(p/CF).read_text()
   for f in (CF,BF):(p/f).unlink()
   sdk=FakeWandb()
   with s.raises(ValueError,'recover'):
    start(sdk,rank=0,output_dir=d,config=recipe({'lr':.9}))
   s.no(hasattr(sdk,'run'))
   s.eq((p/IDF).read_text().strip(),a.id)
   (p/CF).write_text(good)
   b=start(rank=0,output_dir=d,config=recipe({'lr':.9}))
   s.eq((b.id,b.config),(a.id,a.config))
class AcceptedRecovery(Checks):
 def test_corrupt_refusal(s):
  for raw in (b'{',b'\xff\xfe'):
   with TD()as d:
    start(rank=0,output_dir=d);p=Path(d);sdk=FakeWandb()
    files=[p/CF,p/BF]
    for f in files:f.write_bytes(raw)
    before=[f.read_bytes()for f in files]
    with s.raises(ValueError,'recover'):start(sdk,rank=0,output_dir=d)
    s.eq([f.read_bytes()for f in files],before)
    s.no(hasattr(sdk,'run'))
 def test_unmarked_legacy(s):
  with TD()as d:
   a=start(rank=0,output_dir=d,config=recipe({'lr':.1}));p=Path(d)
   for f in (RF,CF,BF):(p/f).unlink()
   b=start(rank=0,output_dir=d,config=recipe({'lr':.9}))
   s.eq(b.id,a.id);s.eq(b.config[TR]['lr'],.9)
class ContinuationTwo(Checks):
 def test_metric_token(s):
  for ns in wbkit.METRIC_NAMESPACES:
   q=ns+'/token';expect=q+' == 0.5'
   r=start(rank=0,expect=expect)
   wbkit.log_step(r,1,{q:.5})
   s.eq(r.logged[-1][q],.5);s.has('Expect: '+expect,r.notes)
 def test_filename_bytes(s):
  with TD()as d:
   raw=os.fsencode(d)+b'/data-\xff'
   s.eq(os.fsencode(os.fsdecode(raw)),raw)
   argv=['train.py',os.fsdecode(raw)]
   with patch.object(sys,'argv',argv):
    got=start(rank=0).config[PR]['command']
   s.eq(got,dict(program='train.py',flags=[],positional_count=1))
 def test_long_string(s):
  # Bound: 1M characters, 10s child.
  cmd=[sys.executable,'-B','-c',"from tests.test_wandb_logging import start; r=start(rank=0,config={'data':{'sequence':'A'*1000000}}); assert r.config['data']['sequence']=='A'*1000000"]
  s.eq(subprocess.run(cmd,capture_output=True,timeout=10).returncode,0)
 def test_mismatch_warning(s):
  for old,new in ((.1,.9),(1.,1),(1,True),([1.],[1]),({'a':1.},{'a':1})):
   with s.subTest(old=old,new=new),TD()as d:
    start(rank=0,output_dir=d,config=recipe({'lr':old}))
    training=recipe({'lr':new})
    with warnings.catch_warnings(record=True)as found:
     warnings.simplefilter('always');r=start(rank=0,output_dir=d,config=training)
    s.eq(training[TR]['lr'],new)
    s.eq(json.dumps(r.config[TR]['lr']),json.dumps(old))
    s.yes(any('config-mismatch'in str(w.message)for w in found))
class KnownValueSanitizer(Checks):
 def test_persisted_digest_migration(s):
  # Exhaustive: 3 sources.
  for source in (PF,CF,BF):
   with s.subTest(source=source),TD()as d:
    a=start(rank=0,output_dir=d);p=Path(d)/source;v=json.loads(p.read_text())
    prov=v if source==PF else v[CFG][PR]
    prov['command']['argv_sha256']='old-digest'
    p.write_text(json.dumps(v))
    if source==CF:(Path(d)/BF).write_text(json.dumps(v))
    if source.endswith('backup.json'):(Path(d)/CF).unlink()
    with s.warns(UserWarning,'legacy-command'):b=start(rank=0,output_dir=d)
    s.eq(a.id,b.id);s.lacks('argv_sha256',repr(b.config))
    for f in Path(d).glob('*.json'):s.lacks('argv_sha256',f.read_text())
class ExactRedaction(Checks):
 # Exhaustive: 20 defaults x 3 spellings x 2 depths x 2 sources x 3 shapes.
 WORDS=(AK,'wandb_api_key','password','passwd','passphrase','secret',
        'secret_key','client_secret','secret_access_key','aws_secret_access_key',
        'access_token','refresh_token','auth_token','bearer_token','session_token',
        'hf_token','github_token','private_key','encryption_key','credentials')
 def test_defaults(s):
  s.eq(wbkit.DEFAULT_SECRET_KEYS,frozenset(k.replace('_','')for k in s.WORDS))
  for word,spelling,depth,source,shape in itertools.product(s.WORDS,range(3),(1,2),(CFG,'base_config'),range(3)):
   parts=word.split('_')
   k=(word,parts[0]+''.join(p.title()for p in parts[1:]),word.replace('_','-'))[spelling]
   val=(S,{'nested':S},[S])[shape]
   cfg={k:val}if depth==1 else {TR:{k:val}}
   path=k if depth==1 else 'train.'+k
   with s.subTest(key=k,depth=depth,source=source,shape=shape),TD()as d:
    r=start(rank=0,output_dir=d,**{source:cfg})
    s.clean(r,S)
    s.has(path,r.config[PR][RK])
    for p in Path(d).glob('*.json'):s.lacks(S,p.read_text())
    s.lacks(S,repr(wbkit.config_deltas(cfg,{},secret_keys=())))
 def test_custom(s):
  for k in ('ProjectKey','project_key','project-key','project.key','project key'):
   r=start(rank=0,config=recipe({k:S,'token':7}),secret_keys=['project_key'],base_config=recipe({}))
   s.eq(r.config[TR],{'token':7})
   s.eq(r.config[PR][RK],['train.'+k])
 def test_iterable(s):
  with TD()as d:
   for p in (CFG,'base_config'):
    r=start(rank=0,output_dir=d,secret_keys=iter(['custom']),**{p:recipe({'custom':S})})
    s.has('train.custom',r.config[PR][RK])
   for p in Path(d).glob(CG):
    v=json.loads(p.read_text());v[CFG][TR]['custom']=S;p.write_text(json.dumps(v))
   s.clean(start(rank=0,output_dir=d,secret_keys=iter(['custom'])),S)
  s.lacks(S,wbkit.run_name('m','d',{TR:{'custom':S}},secret_keys=iter(['custom'])))
 def test_public(s):
  # Exhaustive: 9 public keys; values equal a removed secret on purpose.
  keys=('token','max_tokens','pad_token','tokenizer','val/token','secretory',
        'assistant_token','passphrase_len','train_config')
  cfg=recipe(dict.fromkeys(keys,S));cfg[TR][AK]=S
  r=start(rank=0,config=cfg,base_config=recipe({}),name=S,hypothesis=S,
          expect=S,tags=[S],settings={'value':S},entity=S,group=S)
  s.eq(r.config[TR],dict.fromkeys(keys,S))
  s.eq(r.name,S);s.eq(r.notes.splitlines()[:2],['H: '+S,'Expect: '+S])
  s.eq(r.tags,[S]);s.eq(r.settings,{'value':S});s.eq(r.entity,S);s.eq(r.group,S)
  for k in keys:s.has(k+'=',r.notes)
  wbkit.log_step(r,1,{'val/password_accuracy':S,'val/token':.5})
  s.eq(r.logged[-1]['val/password_accuracy'],S)
  r.summary['password']=S
  wbkit.finish(r,S,'supported')
  s.eq(r.summary['password'],S);s.eq(r.summary['verdict'],'supported')
  s.has('Outcome (supported): '+S,r.notes)
 def test_normalization(s):
  for k in ('SecretAccessKey','secret_access_key','secret-access-key','secret.access.key','secret access key'):
   s.eq(wbkit._redact({k:S,'secretory':S}),{'secretory':S})
 def test_remote(s):
  # Exhaustive: 10 URL shapes, safe synthetic values only.
  cases=(
   ('https://user:CANARY@example.org:443/a.git?token=CANARY#CANARY','https://example.org:443/a.git'),
   ('ssh://user:CANARY@example.org/a.git','ssh://example.org/a.git'),
   ('user@example.org:a.git','example.org:a.git'),
   ('user:CANARY@example.org:a.git',None),
   ('example.org:a.git?token=CANARY#CANARY','example.org:a.git'),
   ('https://example.org/a?token=CANARY','https://example.org/a'),
   ('ssh://user@[::1]:22/a','ssh://[::1]:22/a'),
   ('not a URL',None),('https://[broken/a',None),('https://example.org:bad/a',None))
  for (raw,want),source in itertools.product(cases,(None,PF,CF,BF)):
   with s.subTest(raw=raw),TD()as d:
    def git(*args,**kw):return raw if args[:2]==(CFG,'--get')else None
    with patch.object(wbkit,'_git',side_effect=git):
     r=wbkit.start_run(FakeWandb(),project='sample',family='sample',job_type=TR,model='m',data='d',hypothesis='A hypothesis.',config=recipe({}),output_dir=d,rank=0)
    if source:
     p=Path(d)/source;v=json.loads(p.read_text())
     prov=v if source==PF else v[CFG][PR]
     prov['git_remote']=raw;p.write_text(json.dumps(v))
     if source==CF:(Path(d)/BF).write_text(json.dumps(v))
     if source.endswith('backup.json'):(Path(d)/CF).unlink()
     r=start(rank=0,output_dir=d)
    s.eq(r.config[PR]['git_remote'],want)
    for p in Path(d).glob('*.json'):s.lacks('CANARY',p.read_text())
 def test_sdk_arguments(s):
  class Strict(FakeWandb):
   def init(self,*,config=None,**kw):
    s.has('allow_val_change',kw)
    return super().init(config=config,**kw)
  r=start(Strict(),rank=0,config=recipe({AK:CFG,'default_token':CFG }))
  s.eq(r.config[TR],{'default_token':CFG })
class RoundOneRepairs(Checks):
 def test_generated_provenance(s):
  for key in (PR,'family'):
   with TD()as d:
    for i in range(2):
     r=start(rank=0,output_dir=d,config=recipe({key:H}),secret_keys=[key])
     s.eq(r.config[PR][RK],['train.'+key]);s.clean(r)
     s.eq(r.config['family'],'sample')
 def test_new_secret_on_resume(s):
  with TD()as d:
   c=recipe({'project_key':H})
   a=start(rank=0,output_dir=d,config=c,base_config=recipe({}))
   with warnings.catch_warnings(record=True)as found:
    warnings.simplefilter('always')
    r=start(rank=0,output_dir=d,config=c,base_config=recipe({}),secret_keys=['project_key'])
   s.eq((r.id,r.name,r.notes),(a.id,a.name,a.notes))
   s.has(H,r.name);s.has(H,r.notes)
   s.lacks('project_key',r.config[TR])
   s.has('train.project_key',r.config[PR][RK])
   alerts=[str(w.message)for w in found if 'resume.new-secret'in str(w.message)]
   s.eq(len(alerts),1)
   for text in ('train.project_key','first launch already sent its value','new output_dir','complete secret_keys'):
    s.has(text,alerts[0])
   s.lacks(H,alerts[0])
   for p in Path(d).glob(CG):
    saved=json.loads(p.read_text())
    s.eq((saved['name'],saved['notes']),(a.name,a.notes))
    s.lacks('project_key',saved[CFG][TR])
   with warnings.catch_warnings(record=True)as found:
    warnings.simplefilter('always')
    b=start(rank=0,output_dir=d,config=c,secret_keys=['project_key'])
   s.eq((b.name,b.notes),(a.name,a.notes))
   s.no(any('resume.new-secret'in str(w.message)for w in found))
class ObjectContract(Checks):
 def test_structured_secret(s):
  @dataclasses.dataclass
  class D:
   api_key:str=S
   width:int=8
  # Exhaustive: 3 containers x 3 nesting shapes x 2 sources.
  for cls,shape,source in itertools.product(
      (D,argparse.Namespace,types.SimpleNamespace),range(3),(CFG,'base_config')):
   obj=cls(api_key=S,width=8)
   value=(obj,{'nested':obj},[{'nested':obj}])[shape]
   path=('train.api_key','train.nested.api_key','train.0.nested.api_key')[shape]
   with s.subTest(cls=cls,shape=shape,source=source),TD()as d:
    args={source:recipe(value)}
    if source==CFG:args['base_config']=recipe({})
    r=start(rank=0,output_dir=d,**args)
    s.clean(r,S)
    s.has(path,r.config[PR][RK])
    for p in Path(d).glob('*.json'):s.lacks(S,p.read_text())
    if source==CFG:
     expected=({'width':8},{'nested':{'width':8}},[{'nested':{'width':8}}])[shape]
     s.eq(r.config[TR],expected)
    s.eq(obj.api_key,S)
 def test_arbitrary_object(s):
  class M:
   api_key=S
   width=8
   def __str__(self):return "api_key='"+self.api_key+"' width=8"
   def __deepcopy__(self,memo):raise AssertionError('must not deep-copy')
  @dataclasses.dataclass
  class D:
   child:object
   password:str=S
  obj=M()
  s.assertIs(wbkit._record(obj),obj)
  s.assertIs(wbkit._record(D(obj))['child'],obj)
  with TD()as d:
   r=start(rank=0,output_dir=d,config=recipe(D(obj)))
   s.eq(r.config[TR],{'child':str(obj)})
   s.eq(r.config[PR][RK],['train.password'])
   for p in Path(d).glob(CG):
    s.eq(json.loads(p.read_text())[CFG][TR],{'child':str(obj)})

class FinalRoundCounterexamples(Checks):
 def test_enum_key(s):
  class Key(str,Enum):
   PASSWORD='password'
  with TD()as d:
   a=start(rank=0,output_dir=d,config=recipe({Key.PASSWORD:S}))
   b=start(rank=0,output_dir=d,config=recipe({Key.PASSWORD:S}))
   s.clean(a,S)
   s.eq(a.config,b.config)
   for p in Path(d).glob(CG):s.lacks(S,p.read_text())
 def test_deque_entry(s):
  with TD()as d:
   r=start(rank=0,output_dir=d,config=recipe({'sched':collections.deque([{AK:S}])}))
   s.clean(r,S)
   s.has('train.sched.0.api_key',r.config[PR][RK])
   for p in Path(d).glob(CG):s.lacks(S,p.read_text())
 def test_unnamespaced_axis(s):
  r=start(rank=0)
  with s.assertWarns(UserWarning):wbkit.log_step(r,10000,{'loss':.2})
  s.has(('loss',AX,None),r.definitions)
 def test_delta_types(s):
  # Exhaustive 6 x 6 type/value pairs, at two mapping depths.
  values=(True,False,1,0,1.0,0.0)
  for a,b,nested in itertools.product(values,values,(False,True)):
   wrap=lambda x:{'warmup':{'value':x}if nested else x}
   key='train.warmup'+('.value'if nested else '')
   expected={}if type(a)is type(b)and a==b else {key:a}
   s.eq(wbkit.config_deltas(recipe(wrap(a)),recipe(wrap(b))),expected)
 def test_small_name_bound(s):
  # Exhaustive max_len 1..96 and invalid -2..0.
  for n in range(1,97):
   s.le(len(wbkit.run_name('abcdefgh','ijklmn',{},max_len=n)),n)
  for n in range(-2,1):
   with s.assertRaises(ValueError):wbkit.run_name('x','y',max_len=n)
 def test_mapping_keys(s):
  class Text(Enum):PASSWORD='password';WIDTH='width'
  class Number(Enum):password=1
  class Plain:
   def __str__(s):return 'password'
  # Exhaustive five key types x two config sources.
  for key,base in itertools.product((Text.PASSWORD,Number.password,Plain(),12,False),(False,True)):
   field='password'if not isinstance(key,(int,bool))else str(key)
   cfg=recipe({key:S,'width':8})
   with TD()as d:
    kwargs=dict(config=recipe({})if base else cfg,base_config=cfg if base else None,output_dir=d)
    a=start(**kwargs);b=start(**kwargs)
    s.eq(a.config,b.config)
    if field=='password':
     s.clean(a,S)
     s.has('train.password',a.config[PR][RK])
    elif not base:s.eq(a.config[TR][field],S)
    else:s.eq(wbkit._redact({key:S}),{field:S})
    for p in Path(d).glob(CG):
     s.eq(json.loads(p.read_text())[CFG],a.config)
  s.eq(wbkit._redact({Text.WIDTH:8}),{'width':8})
 def test_containers(s):
  @dataclasses.dataclass(frozen=True)
  class Record:
   api_key:str=S
   width:int=8
  # Exhaustive six containers x four record/mapping kinds x two sources.
  containers=(list,tuple,collections.deque,set,frozenset,lambda xs:collections.UserDict({'record':xs[0]}))
  records=(lambda:{AK:S,'width':8},Record,lambda:argparse.Namespace(api_key=S,width=8),lambda:types.SimpleNamespace(api_key=S,width=8))
  for container,record,base in itertools.product(containers,records,(False,True)):
   item=record()
   try:v=container([item])
   except TypeError:continue
   c=recipe({'nested':v})
   with TD()as d:
    r=start(config=c,base_config=c if base else None,output_dir=d)
    b=start(config=c,base_config=c if base else None,output_dir=d)
    s.eq(r.config,b.config);s.clean(r,S)
    s.yes(any(q.endswith('.api_key')for q in r.config[PR][RK]))
    for p in Path(d).glob(CG):s.lacks(S,p.read_text())
  for cls in(set,frozenset):
   s.eq(wbkit._redact(cls((3,1,2))),[1,2,3])
   v=cls((1,'a'))
   s.eq(wbkit._redact(v),list(v))
class PartTwo(Checks):
 def test_unorderable_set(s):
  from decimal import Decimal
  value={Decimal('NaN'),Decimal('1')}
  s.eq(wbkit._redact(value),[str(x)for x in value])
  s.eq(start(config=recipe({'values':value})).config[TR]['values'],[str(x)for x in value])
 def test_conversion(s):
  calls=[]
  class Dtype:
   @classmethod
   def tolist(cls):calls.append('tolist');return [1]
   @classmethod
   def item(cls):calls.append('item');return 1
  class NeedsArg:
   def item(self,arg):return arg
  class Broken:
   def tolist(self):raise RuntimeError('broken')
  for v in (Dtype,NeedsArg(),Broken()):
   s.eq(wbkit._json(v),str(v))
   s.eq(start(config=recipe({'x':v})).config[TR]['x'],str(v))
  s.eq(calls,[])
 def test_secret_keys_form(s):
  for v in ('api_token',b'api_token'):
   sdk=FakeWandb()
   with s.raises(ValueError,r'secret_keys=\("name",\)'):start(sdk,secret_keys=v)
   s.no(hasattr(sdk,'run'))
 def test_collisions(s):
  with warnings.catch_warnings(record=True)as ws:
   warnings.simplefilter('always')
   r=start(config=recipe({1:'old','1':'kept',2:'old','2':'new'}),base_config=recipe({'width':8}))
  messages=[str(w.message)for w in ws if 'key-collision' in str(w.message)]
  s.eq(messages,['config.key-collision: train.1','config.key-collision: train.2'])
  s.eq(r.config[TR],{'1':'kept','2':'new'})
 def test_container_types(s):
  # Exhaustive: four container types x four types, then resume each pair.
  for a,b in itertools.product((list,tuple,set,frozenset),repeat=2):
   x,y=recipe({'values':a([1,2])}),recipe({'values':b([1,2])})
   with s.subTest(a=a,b=b),TD()as d:
    s.eq(wbkit.config_deltas(x,y),{}if a is b else {'train.values':[1,2]})
    start(config=x,output_dir=d)
    with warnings.catch_warnings(record=True)as ws:
     warnings.simplefilter('always');r=start(config=y,output_dir=d)
    mismatch=any('resume.config-mismatch' in str(w.message) for w in ws)
    s.eq(mismatch,a is not b)
    s.eq(r.config[TR]['values'],[1,2])
 def test_mixed_set_resume(s):
  class OrderedSet(set):
   def __init__(self,reverse):super().__init__((1,'a'));self.reverse=reverse
   def __iter__(self):return iter(sorted(super().__iter__(),key=str,reverse=self.reverse))
  with TD()as d:
   start(config=recipe({'values':OrderedSet(False)}),output_dir=d)
   with warnings.catch_warnings(record=True)as ws:
    warnings.simplefilter('always');start(config=recipe({'values':OrderedSet(True)}),output_dir=d)
   s.no(any('resume.config-mismatch' in str(w.message)for w in ws))
  with TD()as d:
   start(config=recipe({'values':{1,'a'}}),output_dir=d)
   with warnings.catch_warnings(record=True)as ws:
    warnings.simplefilter('always');start(config=recipe({'values':{'a',1}}),output_dir=d)
   s.no(any('resume.config-mismatch' in str(w.message)for w in ws))
 def test_snapshot_disagreement(s):
  # Exhaustive two disagreement fields, primary and backup remain byte exact.
  for field in ('id','config'):
   with TD()as d:
    start(output_dir=d);p=Path(d);obj=json.loads((p/CF).read_text())
    obj[field]='another'if field=='id'else recipe({'lr':9})
    (p/CF).write_text(json.dumps(obj));before=[(p/f).read_bytes()for f in (CF,BF)]
    sdk=FakeWandb()
    with s.raises(ValueError,'disagree'):start(sdk,output_dir=d)
    s.no(hasattr(sdk,'run'));s.eq([(p/f).read_bytes()for f in (CF,BF)],before)
 def test_archive_secret(s):
  # Exhaustive valid-but-invalid snapshot and truncated JSON; custom/default key.
  for key,raw in itertools.product(('custom_key','password'),('list','truncated')):
   with TD()as d:
    start(config=recipe({key:S}),output_dir=d);p=Path(d)
    value=json.dumps([{key:S}])if raw=='list'else '{"'+key+'":"'+S+'",'
    (p/CF).write_text(value)
    r=start(config=recipe({}),secret_keys=(key,),output_dir=d)
    archives=list(p.glob('*.corrupt-*'));s.yes(archives)
    for f in archives:s.lacks(S,f.read_text())
    for f in (CF,BF):s.lacks(S,(p/f).read_text())
    s.lacks(S,repr(r.config))
 def test_resume_none(s):
  with TD()as d:
   a=start(output_dir=d)
   with warnings.catch_warnings(record=True)as ws:
    warnings.simplefilter('always');b=start(output_dir=d,config=None)
   s.eq(b.config,a.config);s.no(any('config-mismatch' in str(w.message)for w in ws))
 def test_builders(s):
  # Exhaustive 5 kinds x 9 token shapes x 2 suffixes, includes date-shaped ids.
  for kind,token,suffix in itertools.product(wbkit.ARTIFACT_TYPES,('20261109','v2','final','latest','best','cells','with spaces','A'*200,''),('val','20261110')):
   name=wbkit.artifact_name(kind,token,suffix)
   wbkit._check_artifact_name(name)
   s.no(wbkit._VERSIONISH_NAME_RE.search(name))
  for name in ('final','latest','v2','20261109','cells-final','cells-20261109-val'):
   with s.assertRaises(ValueError):wbkit._check_artifact_name(name)
   s.has('artifact.versioned-name',[x.split(':')[0]for x in wbkit.lint_artifact(name=name,type='dataset',description='',metadata={})])
 def test_artifacts(s):
  with TD()as d:
   path=Path(d)/'weights';path.write_bytes(b'weights')
   sdk=ArtifactSDK();r=start(sdk,output_dir=d,secret_keys=('custom_key',))
   ds=wbkit.log_dataset(sdk,r,'cells-filtered',path,summary='Cells.',contents='Counts.',intended_use='Train.',metadata={'n_samples':2,'custom_key':S})
   s.eq(ds.metadata['n_samples'],2);s.lacks(S,repr(ds.metadata))
   for field in ('Produced by','Inputs','Contents','Intended use','Caveats'):s.has(field,ds.description)
   ck=wbkit.log_model(sdk,r,path,step=0,is_best=True,sanity_checkpoint=True,aliases=['best','candidate'],post_hoc=True,metrics={'custom_key':S,'val/loss':.2})
   s.lacks('best',ck.aliases);s.has('candidate',ck.aliases)
   s.has('Post hoc',ck.description);s.has('Sanity checkpoint',ck.description)
   s.lacks(S,ck.description);s.lacks(S,repr(ck.metadata))
   ck=wbkit.log_model(sdk,r,path,step=1,is_best=True)
   s.has('best',ck.aliases);s.has('step-1',ck.aliases)
   s.eq(wbkit.lint_artifact(name=ck.name,type=ck.type,description=ck.description,metadata=ck.metadata),[])
   ds=wbkit.log_dataset(sdk,r,'cells-reference','s3://example/data',summary='Cells.',contents='Counts.',intended_use='Train.')
   s.eq(ds.metadata['storage'],'reference')
 def test_lineage_restart(s):
  with TD()as d:
   r=start(ArtifactSDK(),output_dir=d)
   wbkit.use_input(r,'team/project/cells:v3')
   s.has('team/project/cells:v3',wbkit.artifact_card(summary='Cells',contents='x',intended_use='x',run=r))
   # A fresh interpreter cannot inherit any in-memory registry.
   cmd=[sys.executable,'-B','-c',"from tests.test_wandb_logging import *; r=start(ArtifactSDK(),output_dir=sys.argv[1]); print(wbkit.artifact_card(summary='x',contents='x',intended_use='x',run=r))",d]
   child=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
   s.eq(child.returncode,0,child.stderr);s.has('team/project/cells:v3',child.stdout)
   s.eq(json.loads((Path(d)/'wandb_inputs.json').read_text())['id'],r.id)
class FakeArtifact:
 def __init__(s,name,type):s.name=name;s.type=type;s.metadata={};s.description='';s.sources=[]
 def add_file(s,path):s.sources.append(('file',path))
 def add_dir(s,path):s.sources.append(('dir',path))
 def add_reference(s,uri,**kw):s.sources.append(('reference',uri))
class ArtifactSDK(FakeWandb):
 Artifact=FakeArtifact
 def init(s,**kw):
  r=super().init(**kw)
  def log(art,aliases=()):art.aliases=list(aliases);return art
  r.log_artifact=log
  r.use_artifact=lambda ref,type=None:types.SimpleNamespace(entity='team',project='project',name='cells:v3',version='v3')
  return r

if __name__=='__main__':
 unittest.main()
