"""W&B presentation with injected fakes; no network or installed SDK needed.

Secret properties exhaust itertools.product over 6 keys x 3 container shapes.
The optional SDK case uses offline mode exclusively.
"""
from pathlib import Path
import importlib
import importlib.util
import itertools
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/hanig-wandb-logging/scripts'
sys.path.insert(0, str(SCRIPTS))
import wbkit
import audit_project


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


class FakeArtifact:
    def __init__(self, name, type):
        self.name, self.type = name, type
        self.sources = []

    def add_file(self, path):
        self.sources.append(path)

    add_dir = add_file

    def add_reference(self, path, **kw):
        self.sources.append(path)

    def add(self, table, name):
        self.table = table


class FakeWandb:
    __version__ = '0.31.0'
    Artifact = FakeArtifact
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
        self.assertIsNone(wbkit.log_model(FakeWandb(), disabled, 'absent', step=1))
        self.assertIsNone(wbkit.log_dataset(FakeWandb(), disabled, 'sample', 'absent',
                         summary='s', contents='c', intended_use='u'))
        self.assertEqual(wbkit.finish(disabled, 'unused', 'inconclusive'), [])

    def test_artifacts_lineage_and_finish(self):
        sdk = FakeWandb()
        run = start(sdk)
        run.use_artifact = lambda ref, **kw: types.SimpleNamespace(
            name='cells:v3', entity='team', project='sample', version='v3')
        wbkit.use_input(run, 'team/sample/cells:v3')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'weights.bin'
            path.write_bytes(b'weights')
            ds = wbkit.log_dataset(sdk, run, 'cells-clean', path, summary='Clean cells.',
                    contents='One file.', intended_use='Training.', metadata={'n_samples': 5})
            self.assertEqual(ds.metadata['n_samples'], 5)
            self.assertIn('team/sample/cells:v3', ds.description)
            model = wbkit.log_model(sdk, run, path, step=3, metrics={'val/loss': 0.2},
                                    is_best=True, caveats='post hoc checkpoint selection')
            self.assertIn('post hoc', model.description)
            self.assertEqual(run.artifacts[-1][1]['aliases'], ['step-3', 'best'])
        wbkit.finish(run, 'Declared criterion met.', 'supported')
        self.assertTrue(run.notes.endswith('Outcome (supported): Declared criterion met.'))
        self.assertTrue(run.finished)
        self.assertEqual(run.summary['verdict'], 'supported')

    def test_artifact_secrets_from_caller_owned_config(self):
        sdk = FakeWandb()
        run = start(sdk)
        # Sweep/framework callers can supply runs without start_run redaction.
        run.config['arch']['api_key'] = 'synthetic-secret'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'weights.bin'
            path.write_bytes(b'weights')
            art = wbkit.log_model(sdk, run, path, step=1,
                                 metrics={'token': 'synthetic-secret'})
            self.assertNotIn('synthetic-secret', repr(art.metadata) + art.description)
            self.assertEqual(art.metadata['arch']['width'], 8)
            ds = wbkit.log_dataset(sdk, run, 'cells-clean', path, summary='Data.',
                                  contents='One file.', intended_use='Training.',
                                  metadata={'secret': 'synthetic-secret', 'n_samples': 3})
            self.assertNotIn('synthetic-secret', repr(ds.metadata))
            self.assertEqual(ds.metadata['n_samples'], 3)
        table = types.SimpleNamespace(columns=['score'], data=[[1]])
        preds = wbkit.log_predictions(sdk, run, 'preds-sample', table,
                                      eval_set='sample', summary='Predictions.',
                                      metadata={'credential': 'synthetic-secret'})
        self.assertNotIn('synthetic-secret', repr(preds.metadata))

    def test_lint(self):
        issues = wbkit.lint_run(name='happy-fox-2', notes='', group=None, job_type=None,
                               config={}, summary_keys=['loss'])
        for rule in ['name.random', 'notes.empty', 'group.missing', 'config.flat',
                     'metrics.unnamespaced']:
            self.assertTrue(any(i.startswith(rule + ':') for i in issues))
        self.assertTrue(wbkit.lint_artifact(name='cells', type='dataset', description='', metadata={}))
        self.assertEqual(wbkit.lint_artifact(name='internal', type='run_table', description='', metadata={}), [])
        for name in ['final', 'latest', 'v2', '20261109', 'cells-final', 'cells-v2']:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    wbkit._check_artifact_name(name)
                self.assertTrue(any(i.startswith('artifact.versioned-name:') for i in
                    wbkit.lint_artifact(name=name, type='dataset', description='', metadata={})))

    def test_audit(self):
        run = types.SimpleNamespace(id='a', name='happy-fox-2', notes='', group=None,
             job_type=None, config={}, summary_metrics={}, tags=[], state='finished', url='offline')
        latest = types.SimpleNamespace(description='', metadata={})
        coll = types.SimpleNamespace(name='cells', artifacts=lambda: iter([latest]))
        api = types.SimpleNamespace(runs=lambda *a, **kw: [run], artifact_types=lambda **kw: [
            types.SimpleNamespace(name='dataset', collections=lambda: [coll])])
        report = audit_project.audit(api, 'team/sample', 1)
        self.assertEqual(report['runs_checked'], 1)
        self.assertEqual(report['artifact_collections_checked'], 1)
        self.assertIn('notes.empty', report['by_rule'])
        self.assertEqual(audit_project.audit(api, 'team/sample', 0)['runs_checked'], 0)
        self.assertEqual(audit_project.audit(types.SimpleNamespace(Api=lambda **kw: api),
                                           'team/sample', 1), report)


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

    def test_disabled_input_is_noop(self):
        calls = []
        run = types.SimpleNamespace(disabled=True, id=None,
                                    use_artifact=lambda *a, **kw: calls.append(a))
        self.assertIsNone(wbkit.use_input(run, 'team/project/data:v1'))
        self.assertEqual(calls, [])

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

    def test_malformed_provenance_audit(self):
        for value in ('migrated', [1], 1):
            run = types.SimpleNamespace(id='a', name='sample-cells-s0', notes='H: baseline',
                group='baseline', job_type='train', config={'provenance': value},
                summary_metrics={}, tags=[], state='finished', url='offline')
            api = types.SimpleNamespace(runs=lambda *a, **kw: [run], artifact_types=lambda **kw: [])
            with self.subTest(value=value):
                self.assertIn('config.no-commit', audit_project.audit(api, 'team/project')['by_rule'])

    def test_family_is_config_text(self):
        for family in ('GeneFormer', 'Gene Former'):
            self.assertEqual(start(FakeWandb(), family=family).config['family'], family)

    def test_generated_checkpoint_names(self):
        # Exhaust 128 version-like and 64 date-like valid IDs, plus two reported cases.
        ids = ['v' + ''.join(d) for d in itertools.product('01', repeat=7)]
        ids += ['20' + ''.join(d) for d in itertools.product('01', repeat=6)]
        ids += ['v1234567', '20261109']
        sdk = FakeWandb(); run = start(sdk)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'weights.bin'; path.write_bytes(b'weights')
            for identity in ids:
                with self.subTest(identity=identity):
                    run.id = identity
                    first = wbkit.log_model(sdk, run, path, step=1)
                    second = wbkit.log_model(sdk, run, path, step=2)
                    self.assertEqual(first.name, second.name)
                    self.assertEqual(first.metadata['run_id'], identity)
                    self.assertEqual(wbkit.lint_artifact(name=first.name, type='model',
                        description=first.description, metadata=first.metadata), [])
            for name in ('final', 'latest', 'v2', '20261109'):
                with self.assertRaises(ValueError):
                    wbkit.log_model(sdk, run, path, step=1, name=name)


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
            path = Path(tmp) / 'sample.bin'
            path.write_bytes(b'sample')
            wbkit.log_dataset(sdk, run, 'cells-clean', path, summary='Clean data.',
                              contents='One file.', intended_use='Training.')
            wbkit.log_model(sdk, run, path, step=1)
            wbkit.finish(run, 'Offline check only.', 'inconclusive')
            disabled = start(sdk, rank=1)
            self.assertTrue(disabled.disabled)
            disabled.finish()


if __name__ == '__main__':
    unittest.main()
