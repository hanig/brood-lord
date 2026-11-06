"""Policy checks for the release-validation GitHub Actions workflow."""

import ast
import itertools
import json
import os
import runpy
import shlex
import subprocess
import sys
import tempfile
import textwrap
import re
import unittest
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-validation.yml"
EXPECTED_PYTHONS = ["3.9", "3.10", "3.12"]


def yaml_atom(value):
    value = value.strip()
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value


def mapping_block(text, key, indent):
    lines = text.splitlines()
    marker = f"{' ' * indent}{key}:"
    matches = [index for index, line in enumerate(lines) if line == marker]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one {key!r} mapping at indent {indent}, found {len(matches)}"
        )
    start = matches[0] + 1
    body = []
    for line in lines[start:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return "\n".join(body)


def yaml_scalar(text, key, indent):
    marker = f"{' ' * indent}{key}:"
    values = [
        line[len(marker):].strip()
        for line in text.splitlines()
        if line.startswith(marker) and line[:len(marker)] == marker
    ]
    if len(values) != 1 or not values[0]:
        raise AssertionError(
            f"expected one scalar {key!r} at indent {indent}, found {values!r}"
        )
    value = values[0]
    return yaml_atom(value)


def yaml_sequence(text, key, indent):
    lines = text.splitlines()
    marker = f"{' ' * indent}{key}:"
    matches = [
        (index, line[len(marker):].strip())
        for index, line in enumerate(lines)
        if line.startswith(marker) and line[:len(marker)] == marker
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one sequence {key!r} at indent {indent}, found {len(matches)}"
        )
    index, inline = matches[0]
    if inline:
        if not inline.startswith("[") or not inline.endswith("]"):
            raise AssertionError(f"{key!r} is not a sequence")
        contents = inline[1:-1].strip()
        return [] if not contents else [yaml_atom(item) for item in contents.split(",")]

    item_prefix = f"{' ' * (indent + 2)}- "
    values = []
    for line in lines[index + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        if not line.strip():
            continue
        if not line.startswith(item_prefix):
            raise AssertionError(f"unsupported entry in {key!r} sequence: {line!r}")
        value = line[len(item_prefix):].strip()
        values.append(yaml_atom(value))
    return values


class TestReleaseValidationWorkflow(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def assert_trigger_and_python_matrices(self, workflow):
        event_block = mapping_block(workflow, "on", 0)
        push_block = mapping_block(event_block, "push", 2)
        self.assertEqual(yaml_sequence(push_block, "branches", 4), ["main"])

        jobs_block = mapping_block(workflow, "jobs", 0)
        for job_name in ("regression", "native-discovery"):
            job_block = mapping_block(jobs_block, job_name, 2)
            strategy_block = mapping_block(job_block, "strategy", 4)
            matrix_block = mapping_block(strategy_block, "matrix", 6)
            self.assertEqual(
                yaml_sequence(matrix_block, "python-version", 8),
                EXPECTED_PYTHONS,
            )
            self.assertEqual(
                yaml_scalar(job_block, "python-version", 10),
                "${{ matrix.python-version }}",
            )

    def test_main_push_and_supported_python_versions_are_covered(self):
        self.assert_trigger_and_python_matrices(self.workflow)

    def assert_event_os_matrices(self, workflow):
        jobs = mapping_block(workflow, "jobs", 0)
        regression = mapping_block(jobs, "regression", 2)
        matrix = mapping_block(mapping_block(regression, "strategy", 4), "matrix", 6)
        expression = yaml_scalar(matrix, "os", 8)
        # Parse only this declared expression grammar, never eval workflow text.
        parsed = re.fullmatch(
            r"\$\{\{ fromJSON\(github\.event_name == 'push' && '(\[[^']+\])'"
            r" \|\| '(\[[^']+\])'\) \}\}", expression,
        )
        self.assertIsNotNone(parsed, "Regression OS must be event-conditional fromJSON")
        push_json, other_json = parsed.groups()
        # GitHub &&/|| selects the first string for push; it must be truthy.
        self.assertTrue(push_json)
        native = mapping_block(jobs, "native-discovery", 2)
        native_matrix = mapping_block(mapping_block(native, "strategy", 4), "matrix", 6)
        self.assertEqual(yaml_sequence(native_matrix, "os", 8),
                         ["ubuntu-22.04", "macos-15"])
        # Exhaustive finite bound: 3 events x 2 jobs x 3 Python versions = 18.
        for event, job, python in itertools.product(
                ("push", "pull_request", "workflow_dispatch"),
                ("regression", "native-discovery"), EXPECTED_PYTHONS):
            with self.subTest(event=event, job=job, python=python):
                block = mapping_block(jobs, job, 2)
                title = "Regression" if job == "regression" else "Native discovery"
                self.assertEqual(yaml_scalar(block, "name", 4),
                                 title + " (${{ matrix.os }}, Python ${{ matrix.python-version }})")
                self.assert_runner_selection(block)
                oses = (json.loads(push_json if event == "push" else other_json)
                        if job == "regression" else yaml_sequence(native_matrix, "os", 8))
                expected = (["ubuntu-22.04"] if event == "push" and job == "regression"
                            else ["ubuntu-22.04", "macos-15"])
                self.assertEqual(oses, expected)
        self.assert_trigger_and_python_matrices(workflow)
        events = mapping_block(workflow, "on", 0)
        for event in ("pull_request", "workflow_dispatch"):
            mapping_block(events, event, 2)

    def test_event_os_matrices_and_check_names(self):
        self.assert_event_os_matrices(self.workflow)

    def assert_static_two_os_rejected(self, workflow):
        matrix = mapping_block(mapping_block(mapping_block(
            mapping_block(workflow, "jobs", 0), "regression", 2), "strategy", 4),
            "matrix", 6)
        original = "        os: " + yaml_scalar(matrix, "os", 8)
        regression = mapping_block(mapping_block(workflow, "jobs", 0), "regression", 2)
        lines = regression.splitlines()
        self.assertEqual(lines.count(original), 1)
        mutated_regression = "\n".join("        os: [ubuntu-22.04, macos-15]"
                                       if line == original else line for line in lines)
        header = "\n  regression:\n"
        mutated = workflow.replace(header + regression, header + mutated_regression, 1)
        self.assertNotEqual(mutated, workflow)
        with self.assertRaises(AssertionError):
            self.assert_event_os_matrices(mutated)

    def test_static_two_os_regression_matrix_is_rejected(self):
        self.assert_static_two_os_rejected(self.workflow)

    def test_mutation_targets_matrix_when_comment_copies_expression(self):
        # Reviewer counterexample: a prior comment must not absorb the mutation.
        line = next(line for line in self.workflow.splitlines()
                    if line.startswith("        os: ${{ fromJSON("))
        self.assert_static_two_os_rejected("#" + line + "\n" + self.workflow)

    def test_mutation_targets_regression_with_identical_unrelated_matrix(self):
        line = next(line for line in self.workflow.splitlines()
                    if line.startswith("        os: ${{ fromJSON("))
        unrelated = ("\n  checkout-check:\n    strategy:\n      matrix:\n" + line
                     + "\n    runs-on: ${{ matrix.os }}\n    steps:\n"
                     "      - uses: actions/checkout@v4\n")
        self.assert_static_two_os_rejected(self.workflow + unrelated)
        regression = mapping_block(mapping_block(self.workflow, "jobs", 0), "regression", 2)
        clone = "\n  checkout-check:\n" + regression + "\n"
        # Exhaust both placements of an identical job body: before and after.
        for workflow in (self.workflow + clone,
                         self.workflow.replace("\n  regression:", clone + "\n  regression:", 1)):
            self.assert_static_two_os_rejected(workflow)

    def assert_regression_timeout(self, workflow):
        jobs_block = mapping_block(workflow, "jobs", 0)
        regression_block = mapping_block(jobs_block, "regression", 2)
        timeout = yaml_scalar(regression_block, "timeout-minutes", 4)
        # Keep this workflow's timeout a plain integer, without coercing strings.
        self.assertIs(type(timeout), int)
        self.assertGreaterEqual(timeout, 40)

    def test_regression_timeout_allows_full_suite_to_finish(self):
        self.assert_regression_timeout(self.workflow)

    def test_regression_has_90_minutes_and_native_timeout_is_unchanged(self):
        jobs = mapping_block(self.workflow, "jobs", 0)
        self.assertEqual(yaml_scalar(mapping_block(jobs, "regression", 2),
                                     "timeout-minutes", 4), 90)
        self.assertEqual(yaml_scalar(mapping_block(jobs, "native-discovery", 2),
                                     "timeout-minutes", 4), 15)

    def test_timeout_policy_requires_plain_integer_at_least_40(self):
        workflow = "jobs:\n  regression:\n    timeout-minutes: "
        for value in ("40", "45", "45 # headroom"):
            with self.subTest(value=value):
                self.assert_regression_timeout(workflow + value)
        for value in ("25", '"45"'):
            with self.subTest(value=value):
                with self.assertRaises(AssertionError):
                    self.assert_regression_timeout(workflow + value)

    def test_policy_accepts_equivalent_block_style_sequences(self):
        block_style = self.workflow.replace(
            "    branches: [main]",
            "    branches:\n      - main",
        ).replace(
            "        python-version: ['3.9', '3.10', '3.12']",
            "        python-version:\n"
            "          - '3.9'\n"
            "          - '3.10'\n"
            "          - '3.12'",
        )
        self.assert_trigger_and_python_matrices(block_style)

    def test_python_versions_must_be_quoted_strings(self):
        quoted = "python-version: ['3.9', '3.10', '3.12']"
        unquoted = "python-version: ['3.9', 3.10, '3.12']"
        self.assertEqual(self.workflow.count(quoted), 2)

        first = self.workflow.index(quoted)
        second = self.workflow.index(quoted, first + len(quoted))
        for start in (first, second):
            with self.subTest(matrix_offset=start):
                mutated = (
                    self.workflow[:start]
                    + unquoted
                    + self.workflow[start + len(quoted):]
                )
                with self.assertRaises(AssertionError):
                    self.assert_trigger_and_python_matrices(mutated)

    def test_an_unrelated_job_cannot_substitute_for_a_release_job_matrix(self):
        missing_regression_matrix = self.workflow.replace(
            "        python-version: ['3.9', '3.10', '3.12']\n",
            "",
            1,
        )
        missing_regression_matrix += """
  unrelated:
    strategy:
      matrix:
        python-version: ['3.9', '3.10', '3.12']
    steps:
      - uses: actions/setup-python@example
        with:
          python-version: ${{ matrix.python-version }}
"""
        with self.assertRaises(AssertionError):
            self.assert_trigger_and_python_matrices(missing_regression_matrix)

    def test_matrix_artifact_names_are_unique_per_python_version(self):
        jobs_block = mapping_block(self.workflow, "jobs", 0)
        suffix = "${{ matrix.os }}-python-${{ matrix.python-version }}"
        for job_name in ("regression", "native-discovery"):
            job_block = mapping_block(jobs_block, job_name, 2)
            self.assertEqual(
                yaml_scalar(job_block, "name", 10),
                f"{job_name}-{suffix}",
            )


    def job_steps(self, workflow, job):
        block = mapping_block(mapping_block(workflow, "jobs", 0), job, 2)
        return ["      - " + part for part in block.split("      - ")[1:]]

    def named_step(self, workflow, job, name):
        steps = [step for step in self.job_steps(workflow, job)
                 if step.splitlines()[0] == "      - name: " + name]
        self.assertEqual(len(steps), 1)
        return steps[0]

    def script(self, step):
        return textwrap.dedent(step.split("        run: |\n", 1)[1])

    def assert_runner_selection(self, job):
        expression = yaml_scalar(job, "runs-on", 4)
        parsed = re.fullmatch(
            r"\$\{\{ matrix\.os == 'macos-15' && vars\.BROOD_MAC_RUNNER == 'self-hosted'"
            r" && fromJSON\('([^']+)'\) \|\| matrix\.os == 'ubuntu-22.04'"
            r" && vars\.BROOD_LINUX_RUNNER == 'self-hosted' && fromJSON\('([^']+)'\)"
            r" \|\| matrix\.os \}\}", expression)
        self.assertIsNotNone(parsed, "runs-on must preserve both variable switches and fallback")
        mac, linux = map(json.loads, parsed.groups())
        self.assertEqual(mac, ["self-hosted", "macOS", "ARM64", "brood-mac"])
        self.assertEqual(linux, ["self-hosted", "Linux", "X64", "brood-chimera"])
        # Exhaustive bound: 2 OSes x 3 Mac values x 3 Linux values = 18.
        for os_name, mac_var, linux_var in itertools.product(
                ("macos-15", "ubuntu-22.04"), ("", "self-hosted", "other"),
                ("", "self-hosted", "other")):
            actual = (mac if os_name == "macos-15" and mac_var == "self-hosted" else
                      linux if os_name == "ubuntu-22.04" and linux_var == "self-hosted" else os_name)
            variable = mac_var if os_name == "macos-15" else linux_var
            expected = (mac if os_name == "macos-15" else linux) if variable == "self-hosted" else os_name
            self.assertEqual(actual, expected)

    def test_runner_switches_and_static_runner_mutation(self):
        for job in ("regression", "native-discovery"):
            block = mapping_block(mapping_block(self.workflow, "jobs", 0), job, 2)
            self.assert_runner_selection(block)
            expression = yaml_scalar(block, "runs-on", 4)
            with self.assertRaises(AssertionError):
                self.assert_runner_selection(block.replace(expression, "${{ matrix.os }}", 1))
        concurrency = mapping_block(self.workflow, "concurrency", 0)
        self.assertEqual(yaml_scalar(concurrency, "group", 2), "release-validation-${{ github.ref }}")
        self.assertEqual(yaml_scalar(concurrency, "cancel-in-progress", 2), "true")

    def run_shell(self, script, environment):
        return subprocess.run(["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                              cwd=str(ROOT), env=environment, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_preinstalled_interpreter_conditions_and_failure_paths(self):
        for job in ("regression", "native-discovery"):
            steps = self.job_steps(self.workflow, job)
            setup = next(step for step in steps if "- uses: actions/setup-python@" in step)
            self.assertEqual(yaml_scalar(setup, "if", 8),
                             "runner.environment != 'self-hosted'")
            step = self.named_step(self.workflow, job, "Select preinstalled Python")
            self.assertEqual(yaml_scalar(step, "if", 8),
                             "runner.environment == 'self-hosted'")
            self.assertEqual(yaml_scalar(step, "PYTHON_VERSION", 10), "${{ matrix.python-version }}")
            # Exhaustive bound per job: 2 OSes x 3 values for each variable.
            block = mapping_block(mapping_block(self.workflow, "jobs", 0), job, 2)
            self.assert_runner_selection(block)
            for os_name, mac, linux in itertools.product(
                    ("macos-15", "ubuntu-22.04"), ("", "self-hosted", "other"),
                    ("", "self-hosted", "other")):
                selected = (mac if os_name == "macos-15" else linux) == "self-hosted"
                environment = "self-hosted" if selected else "github-hosted"
                # Interpret the exact conditions asserted above, for either OS.
                self.assertEqual(environment == "self-hosted", selected)
                self.assertEqual(environment != "self-hosted", not selected)
            script = self.script(step)
            # Exhaustive bound per job: unset, nonexecutable, mismatched, valid.
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path_file = root / "path"
                bad = root / "nonexecutable"
                bad.write_text("not executable", encoding="utf-8")
                version = "%d.%d" % sys.version_info[:2]
                key = "BROOD_PY_" + version.replace(".", "_")
                base = {"PATH": os.defpath, "RUNNER_TEMP": directory, "GITHUB_PATH": str(path_file),
                        "PYTHON_VERSION": version}
                for value in (None, str(bad), sys.executable):
                    env = dict(base)
                    if value is not None:
                        env[key] = value
                    result = self.run_shell(script, env)
                    self.assertEqual(result.returncode, 0 if value == sys.executable else 1, result.stdout)
                python_bin = Path(path_file.read_text().strip())
                for name in ("python", "python3"):
                    self.assertEqual((python_bin / name).resolve(), Path(sys.executable).resolve())
                mismatch = dict(base, PYTHON_VERSION="0.0", BROOD_PY_0_0=sys.executable)
                self.assertNotEqual(self.run_shell(script, mismatch).returncode, 0)
                start = script.index('if [[ "$actual" != ')
                end = script.index("fi", start) + 2
                mutated = script[:start] + script[end:]
                # Reverting the version check admits the wrong interpreter.
                self.assertEqual(self.run_shell(mutated, mismatch).returncode, 0)

    def test_probe_ignores_printing_startup_hooks(self):
        # Exhaustive: both jobs, usercustomize and sitecustomize, two flag states.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            target = root / "venv"
            venv.EnvBuilder(with_pip=False, symlinks=True).create(str(target))
            interpreter = str(target / "bin/python3")
            site_dir = subprocess.check_output(
                [interpreter, "-I", "-c", "import site; print(site.getsitepackages()[0])"],
                text=True).strip()
            # Put the synthetic site ahead of distributor sitecustomize.
            Path(site_dir, "probe.pth").write_text(
                "import sys; sys.path.insert(0, %r)\n" % site_dir)
            Path(site_dir, "sitecustomize.py").write_text("print('SITE NOTICE')\n")
            user_env = {"HOME": str(home), "PYTHONUSERBASE": str(root / "user")}
            user_site = subprocess.check_output(
                [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
                env=user_env, text=True).strip()
            Path(user_site).mkdir(parents=True)
            Path(user_site, "usercustomize.py").write_text("print('USER NOTICE')\n")
            control = subprocess.check_output(
                [sys.executable, "-c", "pass"], env=user_env, text=True)
            self.assertIn("USER NOTICE", control)
            for job in ("regression", "native-discovery"):
                script = self.script(self.named_step(self.workflow, job, "Select preinstalled Python"))
                version = "%d.%d" % sys.version_info[:2]
                env = dict(user_env, PATH=os.defpath, RUNNER_TEMP=directory,
                           GITHUB_PATH=str(root / "path"), PYTHON_VERSION=version)
                key = "BROOD_PY_" + version.replace(".", "_")
                # Matching real Python with a printing user hook still selects.
                env[key] = sys.executable
                self.assertEqual(self.run_shell(script, env).returncode, 0)
                env[key] = interpreter
                self.assertEqual(self.run_shell(script, env).returncode, 0)
                # Removing -S admits the venv's site hook, contaminating stdout.
                mutated = script.replace(" -S", "", 1)
                self.assertNotEqual(mutated, script)
                result = self.run_shell(mutated, env)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("SITE NOTICE", result.stdout)

    def test_probe_requires_empty_environment_flags_and_exact_stdout(self):
        # Exhaustive: 2 jobs x 5 stdout shapes, synthetic wrapper only.
        version = "%d.%d" % sys.version_info[:2]
        for job in ("regression", "native-discovery"):
            script = self.script(self.named_step(self.workflow, job, "Select preinstalled Python"))
            self.assertIn('env -i "$interpreter" -I -S -c ', script)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                wrapper = root / "python"
                env = {"HOME": directory, "PATH": os.defpath, "RUNNER_TEMP": directory,
                       "GITHUB_PATH": str(root / "path"), "PYTHON_VERSION": version,
                       "BROOD_PY_" + version.replace(".", "_"): str(wrapper),
                       "B5_PROBE_LEAK": "synthetic"}
                for output in (version + "\n", version, version + "\n\n",
                               "NOTICE\n" + version + "\n", version + "x\n"):
                    wrapper.write_text("#!/bin/sh\n"
                                       'test "$1" = --version && exit 0\n'
                                       'test -z "${B5_PROBE_LEAK:-}" || exit 7\n'
                                       "printf %s " + shlex.quote(output) + "\n")
                    wrapper.chmod(0o755)
                    result = self.run_shell(script, env)
                    self.assertEqual(result.returncode == 0, output == version + "\n", result.stdout)
                    if output == version + "\n":
                        mutated = script.replace("env -i ", "", 1)
                        self.assertNotEqual(self.run_shell(mutated, env).returncode, 0)

    def test_relative_interpreter_path_is_refused(self):
        # Reviewer counterexample; exhaustive two-job bound, synthetic symlink only.
        for job in ("regression", "native-discovery"):
            step = self.named_step(self.workflow, job, "Select preinstalled Python")
            with tempfile.TemporaryDirectory(prefix="brood relative ", dir=str(ROOT)) as directory:
                root = Path(directory)
                source = root / "source-python"
                source.symlink_to(sys.executable)
                runner_temp = root / "runner-temp"
                runner_temp.mkdir()
                path_file = root / "path"
                version = "%d.%d" % sys.version_info[:2]
                env = {"PATH": os.defpath, "RUNNER_TEMP": str(runner_temp),
                       "GITHUB_PATH": str(path_file), "PYTHON_VERSION": version,
                       "BROOD_PY_" + version.replace(".", "_"): os.path.relpath(str(source), str(ROOT))}
                result = self.run_shell(self.script(step), env)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(path_file.exists())

    def write_python_probe(self, path, interpreter):
        source = "import os; print(repr(dict(os.environ)))"
        path.write_text("#!/bin/sh\nexec " + shlex.quote(interpreter) + " -c " +
                        shlex.quote(source) + "\n", encoding="utf-8")
        path.chmod(0o755)

    def test_python_probe_accepts_interpreter_path_with_spaces(self):
        # Reviewer counterexample: harmless fallback, never recurse into discovery.
        # Exhaustive finite bound: plain and spaced interpreter paths.
        for name in ("python", "runner temp/python"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                interpreter = root / name
                interpreter.parent.mkdir(exist_ok=True)
                interpreter.symlink_to(sys.executable)
                probe_bin = root / "probe"
                probe_bin.mkdir()
                self.write_python_probe(probe_bin / "python3", str(interpreter))
                path = str(probe_bin) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.defpath
                result = subprocess.run(
                    ["/usr/bin/env", "-i", "HOME=" + directory, "PATH=" + path, "BROOD_PROBE_MARKER=synthetic",
                     "python3", "-c", "print('FALLBACK')"], text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("FALLBACK", result.stdout)
                self.assertEqual(ast.literal_eval(result.stdout)["BROOD_PROBE_MARKER"], "synthetic")
                self.assertEqual(ast.literal_eval(result.stdout)["HOME"], directory)

    def assert_isolation(self, workflow, job):
        denied = runpy.run_path(str(ROOT / "skills/hanig-swarm/scripts/child_environment.py"))["DENIED_ENV_NAMES"]
        homes = ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR")
        prepare = self.named_step(workflow, job, "Isolate self-hosted test environment")
        check = self.named_step(workflow, job, "Check self-hosted test isolation")
        for step in (prepare, check):
            self.assertEqual(yaml_scalar(step, "if", 8), "runner.environment == 'self-hosted'")
        steps = self.job_steps(workflow, job)
        consumer_name = ("Run full suite without user configuration or credentials" if job == "regression"
                         else "Validate native loaders and installed helper without accounts")
        consumer = self.named_step(workflow, job, consumer_name)
        self.assertLess(steps.index(prepare), steps.index(check))
        self.assertLess(steps.index(check), steps.index(consumer))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Synthetic values only; do not pass this process's credentials.
            env = dict.fromkeys(denied, "synthetic")
            env.update(PATH=str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                       RUNNER_TEMP=directory, GITHUB_ENV=str(root / "env"))
            result = self.run_shell(self.script(prepare), env)
            self.assertEqual(result.returncode, 0, result.stdout)
            exports = dict(line.split("=", 1) for line in (root / "env").read_text().splitlines())
            self.assertEqual(set(exports), set(homes) | {"BASH_ENV"})
            self.assertEqual(set(Path(exports["BASH_ENV"]).read_text().split()[1:]), denied)
            for name in homes:
                self.assertTrue(Path(exports[name]).is_relative_to(root))
                self.assertEqual(list(Path(exports[name]).iterdir()), [])
            env.update(exports)
            result = self.run_shell(self.script(check), env)
            self.assertEqual(result.returncode, 0, result.stdout)
            # Consume BASH_ENV exactly as subsequent Actions bash steps do.
            probe = "import os; print(repr(dict(os.environ)))"
            result = self.run_shell("python3 -B -c " + repr(probe), env)
            self.assertEqual(result.returncode, 0, result.stdout)
            received = ast.literal_eval(result.stdout)
            self.assertFalse(denied.intersection(received))
            self.assertEqual({name: received[name] for name in homes},
                             {name: exports[name] for name in homes})
            # Run the real test-step shell with a probe replacing only Python.
            # This checks the environment delivered through Regression's env -i too.
            probe_bin = root / "bin"
            probe_bin.mkdir()
            probe_python = probe_bin / "python3"
            self.write_python_probe(probe_python, sys.executable)
            env.update(PATH=str(probe_bin) + os.pathsep + env["PATH"],
                       RUNNER_ENVIRONMENT="self-hosted")
            result = self.run_shell(self.script(consumer), env)
            self.assertEqual(result.returncode, 0, result.stdout)
            output = (result.stdout if job == "regression" else
                      (root / "native-agent-validation.json").read_text())
            received = ast.literal_eval(output)
            self.assertFalse(denied.intersection(received))
            for name in homes:
                directory_path = Path(received[name])
                self.assertTrue(directory_path.resolve().is_relative_to(root.resolve()))
                self.assertEqual(list(directory_path.iterdir()), [])
            # The check itself refuses a credential surviving startup.
            bad_env = dict(env)
            bad_env.pop("BASH_ENV")
            bad_env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.defpath
            self.assertNotEqual(self.run_shell(self.script(check), bad_env).returncode, 0)

    def test_self_hosted_isolation_and_dropped_variable_mutation(self):
        for job in ("regression", "native-discovery"):
            self.assert_isolation(self.workflow, job)
            step = self.named_step(self.workflow, job, "Isolate self-hosted test environment")
            mutated = step.replace('"XDG_STATE_HOME", ', '', 1)
            with self.assertRaises(AssertionError):
                block = mapping_block(mapping_block(self.workflow, "jobs", 0), job, 2)
                changed = block.replace(step, mutated, 1)
                header = "\n  " + job + ":\n"
                self.assert_isolation(self.workflow.replace(header + block, header + changed, 1), job)


if __name__ == "__main__":
    unittest.main()
