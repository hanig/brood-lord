"""Core tests use fakes, no network, and exhaustive itertools products:
6 secret keys x 3 config shapes; 4 record types x 3 positions;
2 global x 2 local ranks x 2 task-count settings;
6 keys x 3 argv layouts x 3 value shapes. R1 exhausts 5 delimiters x 2 name
paths, 8 science/3 credential fields, 3 verbatim strings and 4 interrupted writes.
SDK absence skips visibly.
"""
from pathlib import Path
import argparse
import collections
import importlib
import importlib.util
import itertools
import dataclasses
from enum import Enum
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1]/'skills/hanig-wandb-logging/scripts'
sys.path.insert(0,str(SCRIPTS))
import wbkit
H='hidden-value'
S='synthetic-hidden'
T='SENTINEL'

def recipe(train):
  return{'arch':{},'train':train,'data':{}}

def shown(r):
  return repr(r.config)+r.name+r.notes
class Checks(unittest.TestCase):
  eq=unittest.TestCase.assertEqual
  lacks=unittest.TestCase.assertNotIn
class FakeRun:
  def __init__(self,**kw):
    self.__dict__.update(kw)
    self.id=kw.get('id')or 'abc123xy'
    self.config=kw.get('config')or{}
    self.summary={}
    self.disabled=kw.get('mode')=='disabled'
    self.url=None
    self.definitions=[]
    self.logged=[]
  def define_metric(self,name,*,step_metric=None,summary=None):
    self.definitions.append((name,step_metric,summary))
  def log(self,metrics,**kw):
    self.logged.append(metrics)
  def finish(self,**kw):
    self.finished=True
class FakeWandb:
  __version__='0.31.0'
  def init(self,**kw):
    self.run=FakeRun(**kw)
    return self.run

def start(sdk=None,**kw):
  sdk=FakeWandb()if sdk is None else sdk
  args=dict(project='sample-train',family='sample',job_type='train',model='sample',data='cells',hypothesis='A stated test hypothesis.',expect='val/loss < 0.5',group='20261109-test',config={'arch':{'width':8},'train':{'seed':0},'data':{}})
  args.update(kw)
  with patch.object(wbkit,'_git',return_value=None):
    return wbkit.start_run(sdk,**args)
class Logging(Checks):
  def test_name_and_deltas(self):
    self.eq(wbkit.run_name('sample','cells',{'train.lr':0.0003},2),'sample-cells-lr3e-4-s2')
    self.eq(wbkit.config_deltas({'train':{'lr':2,'seed':1}},{'train':{'lr':1,'seed':1}}),{'train.lr':2})
    self.eq(wbkit.config_deltas({'lr':1},None),{})
    self.assertLessEqual(len(wbkit.run_name('x'*90,'cells')),64)
  def test_secret_properties(self):
    for key,shape in itertools.product(('api_key','TOKEN','secret','password','passwd','credential'),('scalar','mapping','list')):
      value={'scalar':H,'mapping':{'lr':H},'list':[{'lr':H}]}[shape]
      with self.subTest(key=key,shape=shape):
        c=recipe({key:value})
        self.lacks(H,repr(wbkit._redact(c)))
        self.lacks(H,wbkit.run_name('sample','cells',{key:value}))
        r=start(config=c,base_config={'train':{'lr':1}})
        self.lacks(H,repr(r.config))
        self.lacks(H,r.name+r.notes)
    nested={'train':{'layers':[{'token':H,'lr':2}]}}
    self.lacks(H,repr(wbkit.config_deltas(nested,{'train':{}})))
  def test_argv_redaction(self):
    text=wbkit._redact_argv(['train.py','--api-key=hidden','--token','hidden2','--lr','3'])
    self.lacks('hidden',text)
    self.assertIn('--lr 3',text)
  def test_stable_id_and_resume_config(self):
    with tempfile.TemporaryDirectory()as tmp:
      first=start(output_dir=tmp)
      second=start(output_dir=tmp)
      self.assertRegex(first.id,'^[a-z0-9]{8}$')
      self.eq(second.id,first.id)
      self.eq(first.config,second.config)
      self.eq(second.config['train']['seed'],0)
      self.eq(second.summary['provenance/resume_count'],1)
      self.eq((Path(tmp)/'wandb_run_id').read_text().strip(),first.id)
  def test_metrics_and_disabled_rank(self):
    r=start(summaries={'val/loss':'min','val/score':'max'})
    self.assertIn(('val/loss','trainer/step','min'),r.definitions)
    self.assertIn(('val/score','trainer/step','max'),r.definitions)
    wbkit.log_step(r,3,{'train/loss':0.1})
    self.eq(r.logged[-1]['trainer/step'],3)
    with patch.dict(os.environ,{'RANK':'1'}):
      disabled=start()
    self.assertTrue(disabled.disabled)
    self.eq(wbkit.finish(disabled,'unused','inconclusive'),[])
  def test_lint(self):
    issues=wbkit.lint_run(name='happy-fox-2',notes='',group=None,job_type=None,config={},summary_keys=['loss'])
    for rule in['name.random','notes.empty','group.missing','config.flat','metrics.unnamespaced']:
      self.assertTrue(any((i.startswith(rule+':')for i in issues)))
class ReviewRegressions(Checks):
  def test_seed_containers(self):
    for item in([{'api_key':H}],{'token':H}):
      with self.subTest(container=item):
        c=recipe({'seed':item})
        r=start(rank=0,config=c)
        self.lacks(H,shown(r))
        self.lacks(H,wbkit.run_name('sample','cells',seed=item))
  def test_global_and_local_rank(self):
    for global_rank,local_rank in itertools.product(('0','4'),('0','1')):
      env={'SLURM_NTASKS':'8','SLURM_PROCID':global_rank,'LOCAL_RANK':local_rank}
      with self.subTest(env=env),patch.dict(os.environ,env,clear=True):
        self.eq(start().disabled,global_rank!='0')
  def test_axis_redefinition(self):
    r=start(summaries={'eval/loss':'min'})
    wbkit.define_metrics(r,namespaces=('aux',))
    wbkit.define_metrics(r,step_metric='train/global_step')
    definitions={n:(axis,summary)for n,axis,summary in r.definitions}
    self.eq(definitions['train/*'][0],'train/global_step')
    self.eq(definitions['aux/*'][0],'train/global_step')
    self.eq(definitions['eval/loss'],('train/global_step','min'))
    wbkit.log_step(r,7,{'train/loss':0.2})
    self.eq(r.logged[-1]['train/global_step'],7)
    other=start()
    self.lacks('eval/loss',[d[0]for d in other.definitions])
  def test_family_is_config_text(self):
    for family in('GeneFormer','Gene Former'):
      self.eq(start(family=family).config['family'],family)
class SecondReviewRegressions(Checks):
  def test_dataclass_secrets(self):

    @dataclasses.dataclass
    class Recipe:
      api_key:str
      width:int=8
    records=(Recipe,argparse.Namespace,types.SimpleNamespace,collections.namedtuple('Record','api_key width'))
    wraps=(lambda x:x,lambda x:[x],lambda x:{'recipe':x})
    for record,wrap in itertools.product(records,wraps):
      c=recipe(wrap(record(api_key=H,width=8)))
      base={'train':wrap(record(api_key='old-value',width=8))}
      r=start(config=c,base_config=base)
      self.lacks(H,shown(r))
      self.assertIn('8',repr(r.config))
  def test_token_hyperparameters_survive(self):
    c={'train':{'max_tokens':1024,'tokens_per_batch':32,'tokenizer':'wordpiece'}}
    base={'train':{'max_tokens':512,'tokens_per_batch':16,'tokenizer':'character'}}
    r=start(config=c,base_config=base)
    self.eq(r.config['train'],c['train'])
    self.eq(wbkit.config_deltas(c,base),{'train.'+k:v for k,v in c['train'].items()})
    for key in('token','access_token','api_key','client_secret','password','credential'):
      self.eq(wbkit._redact({key:H})[key],'***')
  def test_rank_without_task_count(self):
    for proc,local,count in itertools.product(('0','4'),('0','1'),(None,'8')):
      env={'SLURM_PROCID':proc,'LOCAL_RANK':local}
      if count is not None:
        env['SLURM_NTASKS']=count
      with self.subTest(env=env),patch.dict(os.environ,env,clear=True):
        self.eq(start().disabled,proc!='0')

@unittest.skipUnless(importlib.util.find_spec('wandb'),'wandb is absent; optional offline integration requires caller-installed wandb')
class OfflineIntegration(Checks):
  def test_real_offline_lifecycle(self):
    sdk=importlib.import_module('wandb')
    with tempfile.TemporaryDirectory()as tmp,patch.dict(os.environ,{'WANDB_MODE':'offline'}):
      r=start(sdk,output_dir=tmp,rank=0)
      rid,c=(r.id,dict(r.config))
      r.finish()
      r=start(sdk,output_dir=tmp,rank=0)
      self.eq(r.id,rid)
      self.eq(dict(r.config),c)
      self.eq(r.summary['provenance/resume_count'],1)
      wbkit.finish(r,'Offline check only.','inconclusive')
      disabled=start(sdk,rank=1)
      self.assertTrue(disabled.disabled)
      disabled.finish()
class PartOneRegressions(Checks):
  def test_argv_json(self):
    for key,layout,shape in itertools.product(('api_key','TOKEN','secret','password','passwd','credential'),('plain','equals','nested'),('scalar','mapping','list')):
      value={'scalar':S,'mapping':{'inner':S},'list':[S]}[shape]
      obj={key:value,'lr':0.1}
      text=json.dumps({'train':[obj]}if layout=='nested' else obj)
      argv=['train.py','--config='+text]if layout=='equals' else['train.py','--config',text]
      with self.subTest(key=key,layout=layout),patch.object(sys,'argv',argv):
        r=start()
        self.lacks(S,repr(r.config))
        self.assertIn('0.1',r.config['provenance']['command'])
  def test_policy_warnings(self):
    for project in('state3_pretrain','sample.train'):
      with self.subTest(project=project),self.assertWarns(UserWarning):
        r=start(project=project,hypothesis='H0')
      self.eq(r.project,project)
      self.eq(r.notes.splitlines()[0],'H: H0')
      issues=wbkit.lint_run(name=r.name,notes=r.notes,group=r.group,job_type=r.job_type,config=r.config,summary_keys=[],project=project)
      self.assertTrue(any((x.startswith('project.slug:')for x in issues)))
      self.assertTrue(any((x.startswith('notes.short-hypothesis:')for x in issues)))
  def test_all_egress(self):
    secret=S
    sdk=FakeWandb()
    r=start(sdk,model='model token='+secret,data='cells',name='sample secret='+secret,hypothesis='Check password='+secret,expect='val/loss < 1; api_key='+secret,tags=['secret='+secret],group='group token='+secret,config={'arch':{'note token='+secret:'public'},'train':{'note':'token='+secret},'data':{}})
    self.lacks(secret,repr(r.__dict__))
    wbkit.log_step(r,1,{'train/text':'token='+secret})
    with self.assertWarns(UserWarning)as warning:
      wbkit.log_step(r,1,{'token='+secret:1})
    self.lacks(secret,str(warning.warning))
    r.summary['message']='secret='+secret
    r.summary['note token='+secret]='public'
    wbkit.finish(r,'token='+secret,'inconclusive')
    self.lacks(secret,repr(r.__dict__))
    self.assertTrue(r.finished)
    self.assertIn('Outcome (inconclusive):',r.notes)
  def test_resume_first_config_and_count(self):
    with tempfile.TemporaryDirectory()as tmp:
      one=start(output_dir=tmp)
      two=start(output_dir=tmp,config={'train':{'seed':9}})
      three=start(output_dir=tmp)
      self.eq(two.config,one.config)
      self.eq(three.config,one.config)
      self.eq(three.summary['provenance/resume_count'],2)
class RoundOne(Checks):
  def test_labeled_punctuation(self):
    for sep,custom in itertools.product(',;})]',(False,True)):
      value='password='+sep+T
      with self.subTest(delimiter=sep,custom=custom),patch.object(sys,'argv',['train.py','--'+value]):
        options=dict(config={'train':{'note':value}},base_config={'train':{'note':'public'}},hypothesis='Testing '+value)
        if custom:options['name']=value
        r=start(**options)
        self.lacks(T,shown(r))
  def test_token_config(self):
    fields=('token_count','token_embedding_dim','pad_token_id','mask_token_id','token_acc','token_loss','token_budget','token-count')
    c={'train':dict.fromkeys(fields,8192)}
    r=start(config=c,base_config={'train':dict.fromkeys(fields,4096)})
    self.eq(r.config['train'],c['train'])
    self.eq(wbkit.config_deltas(c,{'train':{}}),{'train.'+k:8192 for k in fields})
    for key in fields:self.assertIn(key+'=8192',r.notes)
    for key in('token','access_token','train.token.value'):
      self.eq(wbkit._redact({key:T})[key],'***')
  def test_token_metrics(self):
    r=start()
    wbkit.log_step(r,5,{'train/token_acc':.91})
    self.eq(r.logged[-1]['train/token_acc'],.91)
    r.summary['val/token_loss']={'min':.1}
    wbkit.finish(r,'A recorded result.','inconclusive')
    self.eq(r.summary['val/token_loss'],{'min':.1})
  def test_resume_labels(self):
    with tempfile.TemporaryDirectory()as tmp:
      base={'train':{'lr':.1,'seed':0}}
      one=start(output_dir=tmp,base_config=base,config={'train':{'lr':.2,'seed':1}})
      two=start(output_dir=tmp,base_config=base,config={'train':{'lr':.9,'seed':9}})
      self.eq(two.config,one.config)
      self.eq(two.name,one.name)
      self.eq(two.notes,one.notes)
  def test_partial_provenance(self):
    with tempfile.TemporaryDirectory()as tmp:
      path=Path(tmp)/'wandb_provenance.json'
      path.write_text('{')
      (Path(tmp)/'wandb_run_id').write_text('abc123xy\n')
      r=start(output_dir=tmp)
      self.eq(r.id,'abc123xy')
      self.eq(r.summary['provenance/resume_count'],1)
      self.assertIsInstance(json.loads(path.read_text()),dict)
      self.assertIn(b'{',[p.read_bytes()for p in Path(tmp).glob('*.corrupt-*')])
  def test_family_precedence(self):
    r=start(family='ExpectedFamily',config={'family':'UnexpectedFamily','train':{}})
    self.eq(r.config['family'],'ExpectedFamily')
    self.eq(wbkit.config_deltas(r.config,{'train':{}}),{})
  def test_json_safe_config(self):
    class Choice(Enum):
      A='chosen'
    class Scalar:
      def item(self):return 7
    class Array:
      def tolist(self):return[1,2]
    with tempfile.TemporaryDirectory()as tmp:
      c={'train':{'path':Path(tmp)/'data','choice':Choice.A,'number':Scalar(),'array':Array()}}
      r=start(output_dir=tmp,config=c)
      self.eq(r.config['train'],{'path':str(Path(tmp)/'data'),'choice':'chosen','number':7,'array':[1,2]})
      self.eq(start(output_dir=tmp,config=c).config,r.config)
  def test_exact_nonsecret_strings(self):
    for text in('val/loss in [0.50,0.60]','--lrs=[1e-3,1e-4]','{"x":  1}'):
      with self.subTest(text=text):self.eq(wbkit._redact(text),text)
    text='val/loss in [0.50,0.60]'
    with patch.object(sys,'argv',['train.py','--lrs=[1e-3,1e-4]']):
      r=start(expect=text)
    self.assertIn('Expect: '+text,r.notes)
    self.eq(r.config['provenance']['command'],'train.py --lrs=[1e-3,1e-4]')

class AtomicInitialization(Checks):
  def test_interrupted_writers(self):
    real=Path.write_text
    for stop in range(4):
      with self.subTest(stop=stop),tempfile.TemporaryDirectory()as tmp:
        calls=[0]
        def partial(path,text,*args,**kw):
          n=calls[0]
          calls[0]+=1
          if n==stop:
            real(path,text[:1])
            raise InterruptedError('simulated preemption')
          return real(path,text,*args,**kw)
        with patch.object(Path,'write_text',partial),self.assertRaises(InterruptedError):
          start(output_dir=tmp)
        for name in('wandb_run_id','wandb_provenance.json','wandb_config.json'):
          path=Path(tmp)/name
          if path.exists():
            text=path.read_text()
            if name.endswith('.json'):self.assertIsInstance(json.loads(text),dict)
            else:self.assertRegex(text,r'^[a-z0-9]{8}\n$')
        self.assertTrue(start(output_dir=tmp).id)


class RoundTwo(Checks):
  def test_authorization(self):
    text='Authorization: Bearer '+T
    with patch.object(sys,'argv',['train.py','--header',text]):
      r=start(config=recipe({'header':text,'Authorization':'Bearer '+T}),hypothesis=text,name=text)
    self.lacks(T,shown(r))
    r.summary['text']=text
    wbkit.finish(r,text,'inconclusive')
    self.lacks(T,repr(r.__dict__))

  def test_secretory(self):
    fields=('secretory_cell_fraction','secreted_protein','secretome_size')
    c={'data':dict.fromkeys(fields,.25)}
    r=start(config=c,base_config={'data':dict.fromkeys(fields,.1)})
    self.eq(r.config['data'],c['data'])
    self.eq(wbkit.config_deltas(c,{'data':{}}),{'data.'+k:.25 for k in fields})
    wbkit.log_step(r,1,{'data/'+k:.25 for k in fields})
    for k in fields:self.eq(r.logged[-1]['data/'+k],.25)
    for k in('secret','client_secret','secret_key'):
      self.eq(wbkit._redact({k:T})[k],'***')

  def test_rank_priority(self):
    with patch.dict(os.environ,{'SLURM_PROCID':'0','LOCAL_RANK':'1'},clear=True):
      self.assertFalse(start().disabled)

  def test_resume_name(self):
    for legacy in(False,True):
      with tempfile.TemporaryDirectory()as tmp:
        a=start(output_dir=tmp,model='small',data='cells',config={'arch':{'model':'small'},'data':{'dataset':'cells'},'train':{'seed':0}})
        if legacy:
          for p in Path(tmp).glob('wandb_config*.json'):
            value=json.loads(p.read_text());value.pop('name',None);p.write_text(json.dumps(value))
        b=start(output_dir=tmp,model='large',data='images',config={'train':{'seed':9}})
        self.eq(a.config,b.config)
        if not legacy:self.eq(a.name,b.name)
        self.lacks('large',b.name);self.lacks('images',b.name)

  def test_combined_summary(self):
    ops='min max mean last first copy none'.split()
    cases=[','.join(p)for n in(1,2)for p in itertools.product(ops,repeat=n)]+[None,'','MIN,MAX']
    for how in cases:
      r=start(summaries={'val/loss':how})
      self.assertIn(('val/loss','trainer/step',how),r.definitions)
    for how in('bad','min,bad'):
      with self.assertRaises(ValueError):start(summaries={'val/loss':how})

  def test_corrupt_config(self):
    with tempfile.TemporaryDirectory()as tmp:
      a=start(output_dir=tmp,config={'train':{'lr':.1}})
      p=Path(tmp)/'wandb_config.json';p.write_text('{')
      b=start(output_dir=tmp,config={'train':{'lr':.9}})
      self.eq(a.id,b.id);self.eq(a.config,b.config)
      self.eq(b.summary['provenance/resume_count'],1)
      self.assertIn(b'{',[q.read_bytes()for q in Path(tmp).glob('*.corrupt-*')])
      for q in Path(tmp).glob('*backup*'):q.unlink()
      p.write_text('{');sdk=FakeWandb()
      with self.assertRaisesRegex(ValueError,'recover'):start(sdk,output_dir=tmp)
      self.eq(p.read_text(),'{');self.assertFalse(hasattr(sdk,'run'))

  def test_passphrase(self):
    text='password=my '+T+' phrase'
    self.lacks(T,wbkit._redact(text))
    r=start(hypothesis=text,config=recipe({'note':text}),name=text)
    self.lacks(T,shown(r))

  def test_argv_words(self):
    for words in(['my '+T+' phrase'],['my',T,'phrase']):
      text=wbkit._redact_argv(['train.py','--password']+words+['--lr','3'])
      self.lacks(T,text);self.assertIn('--lr 3',text)

  def test_empty_rank(self):
    keys=('RANK','SLURM_PROCID','LOCAL_RANK')
    for vals in itertools.product((None,'','0','4'),repeat=3):
      env={k:v for k,v in zip(keys,vals)if v is not None}
      want=next((int(v)for v in vals if v),0)
      with self.subTest(env=env),patch.dict(os.environ,env,clear=True):
        self.eq(start().disabled,want!=0)

if __name__=='__main__':
  unittest.main()
