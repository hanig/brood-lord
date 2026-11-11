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
H='CANARY'
S='synthetic-hidden'
T='SENTINEL'
AX='trainer/step'
V='val/loss'
def recipe(train):return{'arch':{},'train':train,'data':{}}
def shown(r):return repr(r.__dict__)
class Checks(unittest.TestCase):
 eq=unittest.TestCase.assertEqual
 lacks=unittest.TestCase.assertNotIn
 def clean(s,r,x=H):s.lacks(x,shown(r))
class FakeRun:
 def __init__(s,**kw):s.__dict__.update(kw,id=kw.get('id')or 'abc123xy',config=kw.get('config')or{},summary={},disabled=kw.get('mode')=='disabled',url=None,definitions=[],logged=[])
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
 args=dict(project='sample-train',family='sample',job_type='train',model='sample',data='cells',hypothesis='A hypothesis.',expect='val/loss < 0.5',group='20261109-test',config={'arch':{'width':8},'train':{'seed':0},'data':{}})
 args.update(kw)
 with patch.object(wbkit,'_git',return_value=None):
  return wbkit.start_run(sdk,**args)
class Logging(Checks):
 def test_names(s):
  s.eq(wbkit.run_name('sample','cells',{'train.lr':0.0003},2),'sample-cells-lr3e-4-s2')
  s.eq(wbkit.config_deltas({'train':{'lr':2,'seed':1}},{'train':{'lr':1,'seed':1}}),{'train.lr':2})
  s.eq(wbkit.config_deltas({'lr':1},None),{})
  s.lacks(S,wbkit.run_name('sample','cells',{'api_key':S}))
  s.assertLessEqual(len(wbkit.run_name('x'*90,'cells')),64)
 def test_resume(s):
  with TD()as tmp:
   a=start(output_dir=tmp)
   b=start(output_dir=tmp)
   s.assertRegex(a.id,'^[a-z0-9]{8}$')
   s.eq(b.id,a.id)
   s.eq(a.config,b.config)
   s.eq(b.config['train']['seed'],0)
   s.eq(b.summary['provenance/resume_count'],1)
   s.eq((Path(tmp)/'wandb_run_id').read_text().strip(),a.id)
 def test_metrics(s):
  r=start(summaries={V:'min','val/score':'max'})
  s.assertIn((V,AX,'min'),r.definitions)
  s.assertIn(('val/score',AX,'max'),r.definitions)
  wbkit.log_step(r,3,{'train/loss':0.1})
  s.eq(r.logged[-1][AX],3)
  with patch.dict(os.environ,{'RANK':'1'}):
   d=start()
  s.assertTrue(d.disabled)
  s.eq(wbkit.finish(d,'unused','inconclusive'),[])
 def test_lint(s):
  xs=wbkit.lint_run(name='happy-fox-2',notes='',group=None,job_type=None,config={},summary_keys=['loss'])
  for q in['name.random','notes.empty','group.missing','config.flat','metrics.unnamespaced']:
   s.assertTrue(any((i.startswith(q+':')for i in xs)))
class CoreCases(Checks):
 def test_rank(s):
  for g,l in itertools.product(('0','4'),('0','1')):
   env={'SLURM_NTASKS':'8','SLURM_PROCID':g,'LOCAL_RANK':l}
   with s.subTest(env=env),patch.dict(os.environ,env,clear=True):
    s.eq(start().disabled,g!='0')
 def test_axis(s):
  r=start(summaries={'eval/loss':'min'})
  wbkit.log_step(r,5,{'extra/loss':.3})
  s.assertIn(('extra/*',AX,None),r.definitions)
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
   base={'train':w(rc(api_key='old-value',width=8))}
   r=start(config=c,base_config=base)
   s.clean(r)
   s.assertIn('8',repr(r.config))
 def test_fields(s):
  c={'train':{'max_tokens':1024,'tokens_per_batch':32,'tokenizer':'wordpiece'}}
  base={'train':{'max_tokens':512,'tokens_per_batch':16,'tokenizer':'character'}}
  r=start(config=c,base_config=base)
  s.eq(r.config['train'],c['train'])
  s.eq(wbkit.config_deltas(c,base),{'train.'+k:v for k,v in c['train'].items()})
  for k in('access_token','api_key','client_secret','password','credentials'):
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
   wbkit.finish(r,'Offline check only.','inconclusive')
   d=start(sdk,rank=1)
   s.assertTrue(d.disabled)
   d.finish()
class PartOne(Checks):
 def test_advice(s):
  for project in('state3_pretrain','sample.train'):
   with s.subTest(project=project),s.assertWarns(UserWarning):
    r=start(project=project,hypothesis='H0')
   s.eq(r.project,project)
   s.eq(r.notes.splitlines()[0],'H: H0')
   xs=wbkit.lint_run(name=r.name,notes=r.notes,group=r.group,job_type=r.job_type,config=r.config,summary_keys=[],project=project)
   s.assertTrue(any((x.startswith('project.slug:')for x in xs)))
   s.assertTrue(any((x.startswith('notes.short-hypothesis:')for x in xs)))
 def test_resumes(s):
  with TD()as tmp:
   a=start(output_dir=tmp)
   b=start(output_dir=tmp,config={'train':{'seed':9}})
   c=start(output_dir=tmp)
   s.eq(b.config,a.config)
   s.eq(c.config,a.config)
   s.eq(c.summary['provenance/resume_count'],2)
class RoundOne(Checks):
 def test_config(s):
  f=('token_count','token_embedding_dim','pad_token_id','mask_token_id','token_acc','token_loss','token_budget','token-count')
  c={'train':dict.fromkeys(f,8192)}
  r=start(config=c,base_config={'train':dict.fromkeys(f,4096)})
  s.eq(r.config['train'],c['train'])
  s.eq(wbkit.config_deltas(c,{'train':{}}),{'train.'+k:8192 for k in f})
  for k in f:s.assertIn(k+'=8192',r.notes)
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
   base={'train':{'lr':.1,'seed':0}}
   a=start(output_dir=tmp,base_config=base,config={'train':{'lr':.2,'seed':1}})
   b=start(output_dir=tmp,base_config=base,config={'train':{'lr':.9,'seed':9}})
   s.eq(b.config,a.config)
   s.eq(b.name,a.name)
   s.eq(b.notes,a.notes)
 def test_partial(s):
  with TD()as tmp:
   p=Path(tmp)/'wandb_provenance.json'
   p.write_text('{')
   (Path(tmp)/'wandb_run_id').write_text('abc123xy\n')
   r=start(output_dir=tmp)
   s.eq(r.id,'abc123xy')
   s.eq(r.summary['provenance/resume_count'],1)
   s.assertIsInstance(json.loads(p.read_text()),dict)
   s.assertIn(b'{',[p.read_bytes()for p in Path(tmp).glob('*.corrupt-*')])
 def test_family(s):
  r=start(family='want',config={'family':'other','train':{}})
  s.eq(r.config['family'],'want')
  s.eq(wbkit.config_deltas(r.config,{'train':{}}),{})
 def test_types(s):
  class Choice(Enum):
   A='chosen'
  class Scalar:
   def item(s):return 7
  class Array:
   def tolist(s):return[1,2]
  with TD()as tmp:
   c={'train':{'path':Path(tmp)/'data','choice':Choice.A,'number':Scalar(),'array':Array()}}
   r=start(output_dir=tmp,config=c)
   s.eq(r.config['train'],{'path':str(Path(tmp)/'data'),'choice':'chosen','number':7,'array':[1,2]})
   s.eq(start(output_dir=tmp,config=c).config,r.config)
 def test_verbatim(s):
  for t in('val/loss in [0.50,0.60]','--lrs=[1e-3,1e-4]','{"x":  1}'):
   with s.subTest(text=t):s.eq(wbkit._redact(t),t)
  t='val/loss in [0.50,0.60]'
  with patch.object(sys,'argv',['train.py','--lrs=[1e-3,1e-4]']):
   r=start(expect=t)
  s.assertIn('Expect: '+t,r.notes)
  s.eq(r.config['provenance']['command']['flags'],['--lrs'])
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
    for name in('wandb_run_id','wandb_provenance.json','wandb_config.json','wandb_config.backup.json'):
     p=Path(tmp)/name
     if p.exists():
      t=p.read_text()
      if name.endswith('.json'):s.assertIsInstance(json.loads(t),dict)
      else:s.assertRegex(t,r'^[a-z0-9]{8}\n$')
    s.assertTrue(start(output_dir=tmp).id)
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
   s.assertFalse(start().disabled)
 def test_name(s):
  for legacy in(False,True):
   with TD()as tmp:
    a=start(output_dir=tmp,model='small',data='cells',config={'arch':{'model':'small'},'data':{'dataset':'cells'},'train':{'seed':0}})
    if legacy:
     for p in Path(tmp).glob('wandb_config*.json'):
      v=json.loads(p.read_text());v.pop('name',None);p.write_text(json.dumps(v))
    b=start(output_dir=tmp,model='large',data='images',config={'train':{'seed':9}})
    s.eq(a.config,b.config)
    if not legacy:s.eq(a.name,b.name)
    s.lacks('large',b.name);s.lacks('images',b.name)
 def test_summaries(s):
  ops='min max mean last first copy none'.split()
  cases=[','.join(p)for n in(1,2)for p in itertools.product(ops,repeat=n)]+[None,'','MIN,MAX']
  for how in cases:
   r=start(summaries={V:how})
   s.assertIn((V,AX,how),r.definitions)
  for how in('bad','min,bad'):
   with s.assertRaises(ValueError):start(summaries={V:how})
 def test_recovery(s):
  with TD()as tmp:
   a=start(output_dir=tmp,config={'train':{'lr':.1}})
   p=Path(tmp)/'wandb_config.json';raw=p.read_text();bad='{"cut":';p.write_text(bad)
   real=Path.write_bytes
   def cut(p,t):real(p,t[:1]);raise InterruptedError
   with patch.object(Path,'write_bytes',cut),s.assertRaises(InterruptedError):start(output_dir=tmp)
   s.eq(p.read_text(),bad)
   q=Path(tmp)/(p.name+'.corrupt-'+hashlib.sha256(bad.encode()).hexdigest())
   if q.exists():s.eq(q.read_bytes(),bad.encode())
   b=start(output_dir=tmp,config={'train':{'lr':.9}})
   s.eq(a.id,b.id);s.eq(a.config,b.config)
   s.eq(b.summary['provenance/resume_count'],1)
   s.eq(q.read_bytes(),bad.encode())
   q.write_bytes(b'?');p.write_text(bad);start(output_dir=tmp)
   s.eq(q.read_bytes(),bad.encode())
   for q in Path(tmp).glob('*backup*'):q.unlink()
   p.write_text('{');sdk=FakeWandb()
   with s.assertRaisesRegex(ValueError,'recover'):start(sdk,output_dir=tmp)
   s.eq(p.read_text(),'{');s.assertFalse(hasattr(sdk,'run'))
   p.write_text(raw);(Path(tmp)/'wandb_run_id').write_text('different')
   with s.assertRaisesRegex(ValueError,'recover'):start(sdk,output_dir=tmp)
   s.assertFalse(hasattr(sdk,'run'))
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
  s.eq(r.config['train']['layers'],[128,256]);s.lacks('layers=',r.notes)
  s.eq(wbkit.config_deltas(c,c),{})
  s.eq(wbkit.config_deltas(c,recipe({'layers':[64]})),{'train.layers':[128,256]})
 def test_resume_contract(s):
  with TD()as d:
   a=start(rank=0,output_dir=d,name='small-cells',expect='val/loss < 0.5',config=recipe({'width':8}))
   for i in range(2):
    if i:(Path(d)/'wandb_config.json').unlink()
    b=start(rank=0,output_dir=d,name='large-images',hypothesis='A changed hypothesis.',expect='val/loss < 0.9',config=recipe({'width':64}))
    s.eq((a.id,a.config,a.name,a.notes),(b.id,b.config,b.name,b.notes))
    s.eq(b.summary['provenance/resume_count'],i+1)
 def test_missing_snapshot(s):
  for a,b in itertools.product((None,'{','[]'),repeat=2):
   if a is None and b is None:continue
   with s.subTest(primary=a,backup=b),TD()as d:
    start(rank=0,output_dir=d,config=recipe({'lr':.1}));p=Path(d)
    for f,v in zip(('wandb_config.json','wandb_config.backup.json'),(a,b)):
     q=p/f
     if v is None:q.unlink()
     else:q.write_text(v)
    sdk=FakeWandb()
    with s.assertRaisesRegex(ValueError,'recover'):start(sdk,rank=0,output_dir=d,config=recipe({'lr':.9}))
    s.assertFalse(hasattr(sdk,'run'))
    for f,v in zip(('wandb_config.json','wandb_config.backup.json'),(a,b)):
     if v is not None:s.eq((p/f).read_text(),v)
 def test_infinite_delta(s):
  for v in(float('inf'),float('-inf'),float('nan')):
   r=start(rank=0,config=recipe({'grad_clip':v}),base_config=recipe({'grad_clip':1.}))
   s.assertIn('clip',r.name);got=r.config['train']['grad_clip'];s.assertIsInstance(got,float)
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
    (['--config',json.dumps({'api_key':value})],['--config'],1),
   )
   for tokens,flags,count in cases:
    argv=['/private/project/train.py']+tokens
    with s.subTest(tokens=tokens),patch.object(sys,'argv',argv):
     want=dict(program='train.py',flags=flags,positional_count=count)
     got=wbkit._redact_argv(argv)
     s.eq(got,want)
     s.eq(start().config['provenance']['command'],want)
     s.lacks(value,json.dumps(got));s.lacks('/private/project',json.dumps(got))
 def test_legacy_snapshot(s):
  # Exhaustive: 3 sources x 2 layouts.
  for source,command in itertools.product(
    ('primary','backup','provenance'),
    ('train.py -k LEGACY_VALUE_CANARY','train.py --k=LEGACY_VALUE_CANARY')):
   with s.subTest(source=source,command=command),TD()as d:
    a=start(output_dir=d);p=Path(d)
    if source=='provenance':
     f=p/'wandb_provenance.json';obj=json.loads(f.read_text());obj['command']=command
    else:
     f=p/('wandb_config.json'if source=='primary'else 'wandb_config.backup.json')
     obj=json.loads(f.read_text());obj['config']['provenance']['command']=command
     if source=='backup':(p/'wandb_config.json').unlink()
    f.write_text(json.dumps(obj))
    with s.assertWarnsRegex(UserWarning,'legacy-command'):
     b=start(output_dir=d,config=recipe({'lr':9}))
    s.eq(a.id,b.id);s.eq(b.config['train'],a.config['train'])
    s.clean(b,'LEGACY_VALUE_CANARY')
    for f in p.glob('*.json'):s.lacks('LEGACY_VALUE_CANARY',f.read_text())
    s.assertIsInstance(b.config['provenance']['command'],dict)
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
   p=Path(d);good=(p/'wandb_config.json').read_text()
   for f in ('wandb_config.json','wandb_config.backup.json'):(p/f).unlink()
   sdk=FakeWandb()
   with s.assertRaisesRegex(ValueError,'recover'):
    start(sdk,rank=0,output_dir=d,config=recipe({'lr':.9}))
   s.assertFalse(hasattr(sdk,'run'))
   s.eq((p/'wandb_run_id').read_text().strip(),a.id)
   (p/'wandb_config.json').write_text(good)
   b=start(rank=0,output_dir=d,config=recipe({'lr':.9}))
   s.eq((b.id,b.config),(a.id,a.config))
class AcceptedRecovery(Checks):
 def test_corrupt_refusal(s):
  for raw in (b'{',b'\xff\xfe'):
   with TD()as d:
    start(rank=0,output_dir=d);p=Path(d);sdk=FakeWandb()
    files=[p/'wandb_config.json',p/'wandb_config.backup.json']
    for f in files:f.write_bytes(raw)
    before=[f.read_bytes()for f in files]
    with s.assertRaisesRegex(ValueError,'recover'):start(sdk,rank=0,output_dir=d)
    s.eq([f.read_bytes()for f in files],before)
    s.assertFalse(hasattr(sdk,'run'))
 def test_unmarked_legacy(s):
  with TD()as d:
   a=start(rank=0,output_dir=d,config=recipe({'lr':.1}));p=Path(d)
   for f in ('wandb_config.ready','wandb_config.json','wandb_config.backup.json'):(p/f).unlink()
   b=start(rank=0,output_dir=d,config=recipe({'lr':.9}))
   s.eq(b.id,a.id);s.eq(b.config['train']['lr'],.9)
class ContinuationTwo(Checks):
 def test_metric_token(s):
  for ns in wbkit.METRIC_NAMESPACES:
   q=ns+'/token';expect=q+' == 0.5'
   r=start(rank=0,expect=expect)
   wbkit.log_step(r,1,{q:.5})
   s.eq(r.logged[-1][q],.5);s.assertIn('Expect: '+expect,r.notes)
 def test_filename_bytes(s):
  with TD()as d:
   raw=os.fsencode(d)+b'/data-\xff'
   s.eq(os.fsencode(os.fsdecode(raw)),raw)
   argv=['train.py',os.fsdecode(raw)]
   with patch.object(sys,'argv',argv):
    got=start(rank=0).config['provenance']['command']
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
    s.eq(training['train']['lr'],new)
    s.eq(json.dumps(r.config['train']['lr']),json.dumps(old))
    s.assertTrue(any('config-mismatch'in str(w.message)for w in found))
class KnownValueSanitizer(Checks):

 def test_persisted_digest_migration(s):
  # Exhaustive: 3 sources.
  for source in ('wandb_provenance.json','wandb_config.json','wandb_config.backup.json'):
   with s.subTest(source=source),TD()as d:
    a=start(rank=0,output_dir=d);p=Path(d)/source;v=json.loads(p.read_text())
    prov=v if source=='wandb_provenance.json'else v['config']['provenance']
    prov['command']['argv_sha256']='old-digest'
    p.write_text(json.dumps(v))
    if source.endswith('backup.json'):(Path(d)/'wandb_config.json').unlink()
    with s.assertWarnsRegex(UserWarning,'legacy-command'):b=start(rank=0,output_dir=d)
    s.eq(a.id,b.id);s.lacks('argv_sha256',repr(b.config))
    for f in Path(d).glob('*.json'):s.lacks('argv_sha256',f.read_text())
class ExactRedaction(Checks):
 # Exhaustive: 20 defaults x 3 spellings x 2 depths x 2 sources x 3 shapes.
 WORDS=('api_key','wandb_api_key','password','passwd','passphrase','secret',
        'secret_key','client_secret','secret_access_key','aws_secret_access_key',
        'access_token','refresh_token','auth_token','bearer_token','session_token',
        'hf_token','github_token','private_key','encryption_key','credentials')
 def test_defaults(s):
  s.eq(wbkit.DEFAULT_SECRET_KEYS,frozenset(k.replace('_','')for k in s.WORDS))
  for word,spelling,depth,source,shape in itertools.product(s.WORDS,range(3),(1,2),('config','base_config'),range(3)):
   parts=word.split('_')
   k=(word,parts[0]+''.join(p.title()for p in parts[1:]),word.replace('_','-'))[spelling]
   val=(S,{'nested':S},[S])[shape]
   cfg={k:val}if depth==1 else {'train':{k:val}}
   path=k if depth==1 else 'train.'+k
   with s.subTest(key=k,depth=depth,source=source,shape=shape),TD()as d:
    r=start(rank=0,output_dir=d,**{source:cfg})
    s.clean(r,S)
    s.assertIn(path,r.config['provenance']['redacted_keys'])
    for p in Path(d).glob('*.json'):s.lacks(S,p.read_text())
    s.lacks(S,repr(wbkit.config_deltas(cfg,{},secret_keys=())))
 def test_custom(s):
  for k in ('ProjectKey','project_key','project-key','project.key','project key'):
   r=start(rank=0,config=recipe({k:S,'token':7}),secret_keys=['project_key'],base_config=recipe({}))
   s.eq(r.config['train'],{'token':7})
   s.eq(r.config['provenance']['redacted_keys'],['train.'+k])
 def test_iterable(s):
  with TD()as d:
   for p in ('config','base_config'):
    r=start(rank=0,output_dir=d,secret_keys=iter(['custom']),**{p:recipe({'custom':S})})
    s.assertIn('train.custom',r.config['provenance']['redacted_keys'])
   for p in Path(d).glob('wandb_config*.json'):
    v=json.loads(p.read_text());v['config']['train']['custom']=S;p.write_text(json.dumps(v))
   s.clean(start(rank=0,output_dir=d,secret_keys=iter(['custom'])),S)
  s.lacks(S,wbkit.run_name('m','d',{'train':{'custom':S}},secret_keys=iter(['custom'])))
 def test_public(s):
  # Exhaustive: 9 public keys; values equal a removed secret on purpose.
  keys=('token','max_tokens','pad_token','tokenizer','val/token','secretory',
        'assistant_token','passphrase_len','train_config')
  cfg=recipe(dict.fromkeys(keys,S));cfg['train']['api_key']=S
  r=start(rank=0,config=cfg,base_config=recipe({}),name=S,hypothesis=S,
          expect=S,tags=[S],settings={'value':S},entity=S,group=S)
  s.eq(r.config['train'],dict.fromkeys(keys,S))
  s.eq(r.name,S);s.eq(r.notes.splitlines()[:2],['H: '+S,'Expect: '+S])
  s.eq(r.tags,[S]);s.eq(r.settings,{'value':S});s.eq(r.entity,S);s.eq(r.group,S)
  for k in keys:s.assertIn(k+'=',r.notes)
  wbkit.log_step(r,1,{'val/password_accuracy':S,'val/token':.5})
  s.eq(r.logged[-1]['val/password_accuracy'],S)
  r.summary['password']=S
  wbkit.finish(r,S,'supported')
  s.eq(r.summary['password'],S);s.eq(r.summary['verdict'],'supported')
  s.assertIn('Outcome (supported): '+S,r.notes)
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
  for (raw,want),source in itertools.product(cases,(None,'wandb_provenance.json','wandb_config.json','wandb_config.backup.json')):
   with s.subTest(raw=raw),TD()as d:
    def git(*args,**kw):return raw if args[:2]==('config','--get')else None
    with patch.object(wbkit,'_git',side_effect=git):
     r=wbkit.start_run(FakeWandb(),project='sample',family='sample',job_type='train',model='m',data='d',hypothesis='A hypothesis.',config=recipe({}),output_dir=d,rank=0)
    if source:
     p=Path(d)/source;v=json.loads(p.read_text())
     prov=v if source=='wandb_provenance.json'else v['config']['provenance']
     prov['git_remote']=raw;p.write_text(json.dumps(v))
     if source.endswith('backup.json'):(Path(d)/'wandb_config.json').unlink()
     r=start(rank=0,output_dir=d)
    s.eq(r.config['provenance']['git_remote'],want)
    for p in Path(d).glob('*.json'):s.lacks('CANARY',p.read_text())
 def test_sdk_arguments(s):
  class Strict(FakeWandb):
   def init(self,*,config=None,**kw):
    s.assertIn('allow_val_change',kw)
    return super().init(config=config,**kw)
  r=start(Strict(),rank=0,config=recipe({'api_key':'config','default_token':'config'}))
  s.eq(r.config['train'],{'default_token':'config'})

class RoundOneRepairs(Checks):
 def test_generated_provenance(s):
  for key in ('provenance','family'):
   with TD()as d:
    for i in range(2):
     r=start(rank=0,output_dir=d,config=recipe({key:H}),secret_keys=[key])
     s.eq(r.config['provenance']['redacted_keys'],['train.'+key]);s.clean(r)
     s.eq(r.config['family'],'sample')
 def test_new_secret_on_resume(s):
  with TD()as d:
   c=recipe({'project_key':H})
   a=start(rank=0,output_dir=d,config=c,base_config=recipe({}))
   with warnings.catch_warnings(record=True)as found:
    warnings.simplefilter('always')
    r=start(rank=0,output_dir=d,config=c,base_config=recipe({}),secret_keys=['project_key'])
   s.eq((r.id,r.name,r.notes),(a.id,a.name,a.notes))
   s.assertIn(H,r.name);s.assertIn(H,r.notes)
   s.lacks('project_key',r.config['train'])
   s.assertIn('train.project_key',r.config['provenance']['redacted_keys'])
   alerts=[str(w.message)for w in found if 'resume.new-secret'in str(w.message)]
   s.eq(len(alerts),1)
   for text in ('train.project_key','first launch already sent its value','new output_dir','complete secret_keys'):
    s.assertIn(text,alerts[0])
   s.lacks(H,alerts[0])
   for p in Path(d).glob('wandb_config*.json'):
    saved=json.loads(p.read_text())
    s.eq((saved['name'],saved['notes']),(a.name,a.notes))
    s.lacks('project_key',saved['config']['train'])
   with warnings.catch_warnings(record=True)as found:
    warnings.simplefilter('always')
    b=start(rank=0,output_dir=d,config=c,secret_keys=['project_key'])
   s.eq((b.name,b.notes),(a.name,a.notes))
   s.assertFalse(any('resume.new-secret'in str(w.message)for w in found))

class ObjectContract(Checks):
 def test_structured_secret(s):
  @dataclasses.dataclass
  class D:
   api_key:str=S
   width:int=8
  # Exhaustive: 3 containers x 3 nesting shapes x 2 sources.
  for cls,shape,source in itertools.product(
      (D,argparse.Namespace,types.SimpleNamespace),range(3),('config','base_config')):
   obj=cls(api_key=S,width=8)
   value=(obj,{'nested':obj},[{'nested':obj}])[shape]
   path=('train.api_key','train.nested.api_key','train.0.nested.api_key')[shape]
   with s.subTest(cls=cls,shape=shape,source=source),TD()as d:
    args={source:recipe(value)}
    if source=='config':args['base_config']=recipe({})
    r=start(rank=0,output_dir=d,**args)
    s.clean(r,S)
    s.assertIn(path,r.config['provenance']['redacted_keys'])
    for p in Path(d).glob('*.json'):s.lacks(S,p.read_text())
    if source=='config':
     expected=({'width':8},{'nested':{'width':8}},[{'nested':{'width':8}}])[shape]
     s.eq(r.config['train'],expected)
    s.eq(obj.api_key,S)  # caller-owned containers are unchanged
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
   s.eq(r.config['train'],{'child':str(obj)})
   s.eq(r.config['provenance']['redacted_keys'],['train.password'])
   for p in Path(d).glob('wandb_config*.json'):
    s.eq(json.loads(p.read_text())['config']['train'],{'child':str(obj)})


class FinalRoundCounterexamples(Checks):
 # Final gate failed; retained regressions only, no repair or extra round.
 def test_enum_key(s):
  class Key(str,Enum):
   PASSWORD='password'
  with TD()as d:
   a=start(rank=0,output_dir=d,config=recipe({Key.PASSWORD:S}))
   b=start(rank=0,output_dir=d,config=recipe({Key.PASSWORD:S}))
   s.clean(a,S)
   s.eq(a.config,b.config)
   for p in Path(d).glob('wandb_config*.json'):s.lacks(S,p.read_text())
 def test_deque_entry(s):
  with TD()as d:
   r=start(rank=0,output_dir=d,config=recipe({'sched':collections.deque([{'api_key':S}])}))
   s.clean(r,S)
   s.assertIn('train.sched.0.api_key',r.config['provenance']['redacted_keys'])
   for p in Path(d).glob('wandb_config*.json'):s.lacks(S,p.read_text())
 def test_unnamespaced_axis(s):
  r=start(rank=0)
  with s.assertWarns(UserWarning):wbkit.log_step(r,10000,{'loss':.2})
  s.assertIn(('loss',AX,None),r.definitions)
 def test_delta_types(s):
  s.eq(wbkit.config_deltas(recipe({'warmup':True}),recipe({'warmup':1})),{'train.warmup':True})
 def test_small_name_bound(s):
  s.assertLessEqual(len(wbkit.run_name('abcdefgh','ijklmn',{},max_len=5)),5)

if __name__=='__main__':
 unittest.main()
