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
import threading
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

    def test_input_history_project_and_entity(self):
        # Exhaust three distinct entity/project scopes sharing one run ID.
        for entity, project in (('team', 'alpha'), ('team', 'beta'), ('other', 'alpha')):
            run = start(FakeWandb(), entity=entity, project=project)
            card = lambda r: wbkit.artifact_card(summary='s', contents='c', intended_use='u', run=r)
            self.assertIn('Inputs:** none recorded', card(run))
            ref = entity + '/' + project + '/cells:v1'
            run.use_artifact = lambda *a, **kw: types.SimpleNamespace(
                name='cells:v1', entity=entity, project=project, version='v1')
            wbkit.use_input(run, ref)
            wbkit.finish(run, 'Fixture.', 'inconclusive')
            resumed = start(FakeWandb(), entity=entity, project=project)
            self.assertIn(ref, card(resumed))

    def test_descriptive_artifact_names(self):
        for name in ('old-world-monkeys', 'new-york-images', 'best-practice-corpus'):
            with self.subTest(name=name):
                wbkit._check_artifact_name(name)
                self.assertFalse(any(i.startswith('artifact.versioned-name:') for i in
                    wbkit.lint_artifact(name=name, type='dataset', description='', metadata={})))

    def test_auditor_ignores_unrelated_loaded_module(self):
        code = '''
import sys, types
sys.modules['wbkit'] = types.ModuleType('wbkit')
import audit_project
run = types.SimpleNamespace(id='a', name='sample', notes='', group=None,
    job_type=None, config={}, summary_metrics={}, tags=[], state='finished', url='offline')
api = types.SimpleNamespace(runs=lambda *a, **kw: [run], artifact_types=lambda **kw: [])
assert audit_project.audit(api, 'team/sample')['runs_checked'] == 1
'''
        result = subprocess.run([sys.executable, '-c',
            'import sys; sys.path.insert(0, ' + repr(str(SCRIPTS)) + ')\n' + code],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lightning_checkpoint_collective(self):
        text = (SCRIPTS.parent / 'references/frameworks.md').read_text()
        callback = text[text.index('class WbkitCheckpoint'):].split('```', 1)[0]
        saved, uploaded, errors = [], [], []
        barrier = threading.Barrier(2, timeout=2)
        values = {}
        namespace = {'L': types.SimpleNamespace(Callback=object), 'wandb': object(),
                     'wbkit': types.SimpleNamespace(log_model=lambda *a, **kw: uploaded.append(len(saved)))}
        exec(callback, namespace)
        def worker(rank):
            def broadcast(value):
                values[rank] = value
                barrier.wait()
                answer = values[0]
                barrier.wait()
                return answer
            def save(path):
                saved.append(rank)
                barrier.wait()
            trainer = types.SimpleNamespace(is_global_zero=rank == 0,
                callback_metrics={}, strategy=types.SimpleNamespace(broadcast=broadcast),
                default_root_dir='output', global_step=1, save_checkpoint=save)
            cb = namespace['WbkitCheckpoint'](object())
            try:
                # Better, worse, better, missing: every rank follows the same decision.
                for val in (2, 3, 1, None):
                    trainer.callback_metrics = {'val/loss': val}
                    cb.on_validation_end(trainer, None)
            except Exception as exc:
                errors.append(repr(exc))
        threads = [threading.Thread(target=worker, args=(rank,)) for rank in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(saved), [0, 0, 1, 1])
        self.assertEqual(uploaded, [2, 4])


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
