# RECOVERY, NOT READY

This head has not passed review and must not be merged. The anchored reviewed
head is cded910fe2883abf3175b2d212a55095b6f24d7d. No PR was opened.

Round 1 returned REVIEW_FAIL. Re-raised wa4 limits have prior owner/Astra
adjudication. New unresolved consequences: recovery copies a newly declared
secret into a corrupt-snapshot archive; tuple/list resume changes emit no
mismatch warning. Bare-string secret_keys can also remove public single-character
keys; that argument shape is excluded. The set-limit wording admits an overly
broad reading; TypeError fallback succeeds for heterogeneous sets.

No behavior repair or second gate is authorized after re-raising wa4 code
findings. These local diagnostics used only fake SDK objects and synthetic data.

## Diagnostic source

```python
import json,sys,warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from decimal import Decimal, InvalidOperation
sys.path.insert(0,str(Path.cwd()))
from tests.test_wandb_logging import start,wbkit,FakeWandb
observed={}
with TemporaryDirectory()as d:
 c={'train':{'project_key':'SENTINEL'}}
 start(rank=0,output_dir=d,config=c)
 p=Path(d)/'wandb_config.json';p.write_text(p.read_text()+'}')
 with warnings.catch_warnings(record=True)as w:
  warnings.simplefilter('always');run=start(rank=0,output_dir=d,config=c,secret_keys=('project_key',))
 copies=[(p.name,'SENTINEL'in p.read_text())for p in Path(d).glob('wandb_config.json.corrupt-*')]
 assert copies and all(x[1]for x in copies)
 assert 'project_key'not in run.config['train']
 observed['archive']={'copies':copies,'sdk_pruned':True,'warnings':[str(x.message)for x in w]}
with TemporaryDirectory()as d:
 start(rank=0,output_dir=d,config={'train':{'layers':(1,2)}})
 with warnings.catch_warnings(record=True)as w:
  warnings.simplefilter('always');start(rank=0,output_dir=d,config={'train':{'layers':[1,2]}})
 assert not any('config-mismatch'in str(x.message)for x in w)
 observed['resume_type']={'warnings':[str(x.message)for x in w]}
with TemporaryDirectory()as d:
 a=start(rank=0,output_dir=d,config={'train':{1:'first','1':'second'}})
 with warnings.catch_warnings(record=True)as w:
  warnings.simplefilter('always');b=start(rank=0,output_dir=d,config={'train':{1:'changed','1':'second'}})
 assert a.config['train']==b.config['train']=={'1':'second'}
 assert not any('config-mismatch'in str(x.message)for x in w)
 observed['collision']={'train':b.config['train'],'warnings':[str(x.message)for x in w]}
for k,v in [('tuple',(1,2)),('set',{1,2})]:
 delta=wbkit.config_deltas({'train':{'lr':[1,2]}},{'train':{'lr':v}})
 assert delta=={};observed['delta_'+k]=delta
x=wbkit.config_deltas({'train.lr':9,'train':{'lr':1}},{'train.lr':8,'train':{'lr':1}})
assert x=={};observed['dotted']=x
sdk=FakeWandb()
try:start(sdk,rank=0,config={'data':{'category_codes':{Decimal('1'),Decimal('NaN')}}})
except InvalidOperation as e:observed['decimal_set']={'exception':type(e).__name__,'sdk_initialized':hasattr(sdk,'run')}
else:raise AssertionError('Decimal set did not reproduce')
assert not hasattr(sdk,'run')
# Safe equivalent of np.float32: no third-party import, same unbound item descriptor.
class DType:
 def item(self):return 1
sdk=FakeWandb()
try:start(sdk,rank=0,config={'train':{'dtype':DType}})
except TypeError as e:observed['class']={'exception':str(e),'sdk_initialized':hasattr(sdk,'run')}
else:raise AssertionError('Class item did not reproduce')
assert not hasattr(sdk,'run')
with TemporaryDirectory()as d:
 r=start(rank=0,output_dir=d,secret_keys='api_token',config={'train':{'api_token':'SENTINEL','a':7,'_':8}})
 assert r.config['train']=={'api_token':'SENTINEL'}
 observed['string_keys']={'train':r.config['train'],'redacted':r.config['provenance']['redacted_keys']}
assert set(wbkit._redact({'data':{1,'a'}})['data'])=={1,'a'}
observed['wording']={'heterogeneous_set':'TypeError caught, succeeds','decimal_set':'InvalidOperation escapes, raises','docs_only_paths':__import__('subprocess').check_output(['git','diff','--name-only','ef9ca052','HEAD'],text=True).splitlines()}
print(json.dumps(observed,indent=2))

```

## Observed output

```json
{
  "archive": {
    "copies": [
      [
        "wandb_config.json.corrupt-fa75b65f28fcc989cd8525c582b9b63d6b327d5878bc38d5b01f63a1c53809d6",
        true
      ]
    ],
    "sdk_pruned": true,
    "warnings": [
      "resume.new-secret: train.project_key; first launch already sent its value; start a new output_dir with complete secret_keys from the first launch"
    ]
  },
  "resume_type": {
    "warnings": []
  },
  "collision": {
    "train": {
      "1": "second"
    },
    "warnings": []
  },
  "delta_tuple": {},
  "delta_set": {},
  "dotted": {},
  "decimal_set": {
    "exception": "InvalidOperation",
    "sdk_initialized": false
  },
  "class": {
    "exception": "DType.item() missing 1 required positional argument: 'self'",
    "sdk_initialized": false
  },
  "string_keys": {
    "train": {
      "api_token": "SENTINEL"
    },
    "redacted": [
      "train._",
      "train.a"
    ]
  },
  "wording": {
    "heterogeneous_set": "TypeError caught, succeeds",
    "decimal_set": "InvalidOperation escapes, raises",
    "docs_only_paths": [
      "skills/hanig-wandb-logging/SKILL.md",
      "skills/hanig-wandb-logging/references/frameworks.md",
      "skills/hanig-wandb-logging/references/naming.md"
    ]
  }
}

```
