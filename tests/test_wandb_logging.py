"""W&B presentation with injected fakes; no network or installed SDK needed.

Secret properties exhaust itertools.product over 6 keys x 3 container shapes.
The optional SDK case uses offline mode exclusively.
"""
from pathlib import Path
import argparse
import collections
import importlib
import importlib.util
import itertools
import dataclasses
import json
import os
import sys
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/hanig-wandb-logging/scripts'
sys.path.insert(0, str(SCRIPTS))
import wbkit


class FakeRun:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.id = kw.get('id') or 'abc123xy'
        self.config = kw.get('config') or {}
        self.summary = {}
        self.disabled = kw.get('mode') == 'disabled'
        self.url = None
        self.definitions = []
        self.logged = []
        self.artifacts = []

    def define_metric(self, name, *, step_metric=None, summary=None):
        # Matches 0.31: passing goal raises TypeError.
        self.definitions.append((name, step_metric, summary))

    def log(self, metrics, **kw):
        self.logged.append(metrics)

    def log_artifact(self, artifact, **kw):
        self.artifacts.append((artifact, kw))
        return artifact

    def finish(self, **kw):
        self.finished = True




class FakeWandb:
    __version__ = '0.31.0'
    # Deliberately no util.generate_id.

    def init(self, **kw):
        self.run = FakeRun(**kw)
        return self.run


def start(sdk, **kw):
    args = dict(project='sample-train', family='sample', job_type='train',
                model='sample', data='cells', hypothesis='A stated test hypothesis.',
                expect='val/loss < 0.5', group='20261109-test',
                config={'arch': {'width': 8}, 'train': {'seed': 0}, 'data': {}})
    args.update(kw)
    # Isolate host Git state; provenance command and all egress still execute.
    with patch.object(wbkit, "_git", return_value=None):
        return wbkit.start_run(sdk, **args)


class Logging(unittest.TestCase):
    def test_name_and_deltas(self):
        self.assertEqual(wbkit.run_name('sample', 'cells', {'train.lr': 0.0003}, 2),
                         'sample-cells-lr3e-4-s2')
        self.assertEqual(wbkit.config_deltas({'train': {'lr': 2, 'seed': 1}},
                                            {'train': {'lr': 1, 'seed': 1}}),
                         {'train.lr': 2})
        self.assertEqual(wbkit.config_deltas({'lr': 1}, None), {})
        self.assertLessEqual(len(wbkit.run_name('x' * 90, 'cells')), 64)

    def test_secret_properties(self):
        # Exhaustive finite bound: 6 keys x 3 shapes, including secret parents.
        for key, shape in itertools.product(
                ('api_key', 'TOKEN', 'secret', 'password', 'passwd', 'credential'),
                ('scalar', 'mapping', 'list')):
            value = {'scalar': 'hidden-value', 'mapping': {'lr': 'hidden-value'},
                     'list': [{'lr': 'hidden-value'}]}[shape]
            with self.subTest(key=key, shape=shape):
                cfg = {'arch': {}, 'train': {key: value}, 'data': {}}
                self.assertNotIn('hidden-value', repr(wbkit._redact(cfg)))
                self.assertNotIn('hidden-value', wbkit.run_name('sample', 'cells', {key: value}))
                run = start(FakeWandb(), config=cfg, base_config={'train': {'lr': 1}})
                self.assertNotIn('hidden-value', repr(run.config))
                self.assertNotIn('hidden-value', run.name + run.notes)
        nested = {'train': {'layers': [{'token': 'hidden-value', 'lr': 2}]}}
        self.assertNotIn('hidden-value', repr(wbkit.config_deltas(nested, {'train': {}})))

    def test_argv_redaction(self):
        text = wbkit._redact_argv(['train.py', '--api-key=hidden', '--token', 'hidden2', '--lr', '3'])
        self.assertNotIn('hidden', text)
        self.assertIn('--lr 3', text)

    def test_stable_id_and_resume_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = start(FakeWandb(), output_dir=tmp)
            second = start(FakeWandb(), output_dir=tmp)
            self.assertRegex(first.id, r'^[a-z0-9]{8}$')
            self.assertEqual(second.id, first.id)
            self.assertEqual(first.config, second.config)
            self.assertEqual(second.config['train']['seed'], 0)
            self.assertEqual(second.summary['provenance/resume_count'], 1)
            self.assertEqual((Path(tmp) / 'wandb_run_id').read_text().strip(), first.id)

    def test_metrics_and_disabled_rank(self):
        run = start(FakeWandb(), summaries={'val/loss': 'min', 'val/score': 'max'})
        self.assertIn(('val/loss', 'trainer/step', 'min'), run.definitions)
        self.assertIn(('val/score', 'trainer/step', 'max'), run.definitions)
        wbkit.log_step(run, 3, {'train/loss': 0.1})
        self.assertEqual(run.logged[-1]['trainer/step'], 3)
        with patch.dict(os.environ, {'RANK': '1'}):
            disabled = start(FakeWandb())
        self.assertTrue(disabled.disabled)
        self.assertEqual(wbkit.finish(disabled, 'unused', 'inconclusive'), [])



    def test_lint(self):
        issues = wbkit.lint_run(name='happy-fox-2', notes='', group=None, job_type=None,
                               config={}, summary_keys=['loss'])
        for rule in ['name.random', 'notes.empty', 'group.missing', 'config.flat',
                     'metrics.unnamespaced']:
            self.assertTrue(any(i.startswith(rule + ':') for i in issues))




class ReviewRegressions(unittest.TestCase):
    def test_seed_containers(self):
        for container in ([{'api_key': 'hidden-value'}], {'token': 'hidden-value'}):
            with self.subTest(container=container):
                cfg = {'arch': {}, 'train': {'seed': container}, 'data': {}}
                run = start(FakeWandb(), rank=0, config=cfg)
                self.assertNotIn('hidden-value', run.name + run.notes + repr(run.config))
                self.assertNotIn('hidden-value', wbkit.run_name('sample', 'cells', seed=container))

    def test_global_and_local_rank(self):
        # Exhaust all four Slurm/global x local rank combinations.
        for global_rank, local_rank in itertools.product(('0', '4'), ('0', '1')):
            env = {'SLURM_NTASKS': '8', 'SLURM_PROCID': global_rank, 'LOCAL_RANK': local_rank}
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(start(FakeWandb()).disabled,
                                 (global_rank, local_rank) != ('0', '0'))


    def test_axis_redefinition(self):
        run = start(FakeWandb(), summaries={'eval/loss': 'min'})
        wbkit.define_metrics(run, namespaces=('aux',))
        wbkit.define_metrics(run, step_metric='train/global_step')
        definitions = {n: (axis, summary) for n, axis, summary in run.definitions}
        self.assertEqual(definitions['train/*'][0], 'train/global_step')
        self.assertEqual(definitions['aux/*'][0], 'train/global_step')
        self.assertEqual(definitions['eval/loss'], ('train/global_step', 'min'))
        wbkit.log_step(run, 7, {'train/loss': 0.2})
        self.assertEqual(run.logged[-1]['train/global_step'], 7)
        other = start(FakeWandb())  # Same fake ID, distinct run object.
        self.assertNotIn('eval/loss', [d[0] for d in other.definitions])


    def test_family_is_config_text(self):
        for family in ('GeneFormer', 'Gene Former'):
            self.assertEqual(start(FakeWandb(), family=family).config['family'], family)



class SecondReviewRegressions(unittest.TestCase):
    def test_dataclass_secrets(self):
        @dataclasses.dataclass
        class Recipe:
            api_key: str
            width: int = 8
        # Exhaust four stdlib record types x three container positions.
        records = (Recipe, argparse.Namespace, types.SimpleNamespace,
                   collections.namedtuple('Record', 'api_key width'))
        wraps = (lambda x: x, lambda x: [x], lambda x: {'recipe': x})
        for record, wrap in itertools.product(records, wraps):
            cfg = {'arch': {}, 'train': wrap(record(api_key='hidden-value', width=8)), 'data': {}}
            base = {'train': wrap(record(api_key='old-value', width=8))}
            run = start(FakeWandb(), config=cfg, base_config=base)
            self.assertNotIn('hidden-value', repr(run.config) + run.name + run.notes)
            self.assertIn('8', repr(run.config))

    def test_token_hyperparameters_survive(self):
        cfg = {'train': {'max_tokens': 1024, 'tokens_per_batch': 32, 'tokenizer': 'wordpiece'}}
        base = {'train': {'max_tokens': 512, 'tokens_per_batch': 16, 'tokenizer': 'character'}}
        run = start(FakeWandb(), config=cfg, base_config=base)
        self.assertEqual(run.config['train'], cfg['train'])
        self.assertEqual(wbkit.config_deltas(cfg, base),
                         {'train.' + k: v for k, v in cfg['train'].items()})
        for key in ('token', 'access_token', 'api_key', 'client_secret', 'password', 'credential'):
            self.assertEqual(wbkit._redact({key: 'hidden-value'})[key], '***')

    def test_rank_without_task_count(self):
        # Exhaust 2 process ranks x 2 local ranks x presence/absence of task count.
        for proc, local, count in itertools.product(('0', '4'), ('0', '1'), (None, '8')):
            env = {'SLURM_PROCID': proc, 'LOCAL_RANK': local}
            if count is not None:
                env['SLURM_NTASKS'] = count
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(start(FakeWandb()).disabled, (proc, local) != ('0', '0'))






@unittest.skipUnless(importlib.util.find_spec('wandb'),
                     'wandb is absent; optional offline integration requires caller-installed wandb')
class OfflineIntegration(unittest.TestCase):
    def test_real_offline_lifecycle(self):
        sdk = importlib.import_module('wandb')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'WANDB_MODE': 'offline'}):
            run = start(sdk, output_dir=tmp, rank=0)
            identity, config = run.id, dict(run.config)
            run.finish()
            run = start(sdk, output_dir=tmp, rank=0)
            self.assertEqual(run.id, identity)
            self.assertEqual(dict(run.config), config)
            self.assertEqual(run.summary['provenance/resume_count'], 1)
            wbkit.finish(run, 'Offline check only.', 'inconclusive')
            disabled = start(sdk, rank=1)
            self.assertTrue(disabled.disabled)
            disabled.finish()


class PartOneRegressions(unittest.TestCase):
    def test_argv_json(self):
        # Exhaustive 6 keys x 3 JSON argv layouts x 3 value shapes; synthetic only.
        for key, layout, shape in itertools.product(
                ('api_key', 'TOKEN', 'secret', 'password', 'passwd', 'credential'),
                ('plain', 'equals', 'nested'), ('scalar', 'mapping', 'list')):
            value = {'scalar': 'synthetic-hidden', 'mapping': {'inner': 'synthetic-hidden'},
                     'list': ['synthetic-hidden']}[shape]
            obj = {key: value, 'lr': 0.1}
            text = json.dumps({'train': [obj]} if layout == 'nested' else obj)
            argv = ['train.py', '--config=' + text] if layout == 'equals' else ['train.py', '--config', text]
            with self.subTest(key=key, layout=layout), patch.object(sys, 'argv', argv):
                run = start(FakeWandb())
                self.assertNotIn('synthetic-hidden', repr(run.config))
                self.assertIn('0.1', run.config['provenance']['command'])

    def test_policy_warnings(self):
        for project in ('state3_pretrain', 'sample.train'):
            with self.subTest(project=project), self.assertWarns(UserWarning):
                run = start(FakeWandb(), project=project, hypothesis='H0')
            self.assertEqual(run.project, project)
            self.assertEqual(run.notes.splitlines()[0], 'H: H0')
            issues = wbkit.lint_run(name=run.name, notes=run.notes, group=run.group,
                job_type=run.job_type, config=run.config, summary_keys=[], project=project)
            self.assertTrue(any(x.startswith('project.slug:') for x in issues))
            self.assertTrue(any(x.startswith('notes.short-hypothesis:') for x in issues))

    def test_all_egress(self):
        secret = 'synthetic-hidden'
        sdk = FakeWandb()
        run = start(sdk, model='model token=' + secret, data='cells',
                    name='sample secret=' + secret,
                    hypothesis='Check password=' + secret,
                    expect='val/loss < 1; api_key=' + secret,
                    tags=['secret=' + secret], group='group token=' + secret,
                    config={'arch': {}, 'train': {'note': 'token=' + secret}, 'data': {}})
        self.assertNotIn(secret, repr(run.__dict__))
        wbkit.log_step(run, 1, {'train/text': 'token=' + secret})
        run.summary['message'] = 'secret=' + secret
        wbkit.finish(run, 'token=' + secret, 'inconclusive')
        self.assertNotIn(secret, repr(run.__dict__))
        self.assertTrue(run.finished)
        self.assertIn('Outcome (inconclusive):', run.notes)

    def test_resume_first_config_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = start(FakeWandb(), output_dir=tmp)
            two = start(FakeWandb(), output_dir=tmp, config={'train': {'seed': 9}})
            three = start(FakeWandb(), output_dir=tmp)
            self.assertEqual(two.config, one.config)
            self.assertEqual(three.config, one.config)
            self.assertEqual(three.summary['provenance/resume_count'], 2)


if __name__ == '__main__':
    unittest.main()
