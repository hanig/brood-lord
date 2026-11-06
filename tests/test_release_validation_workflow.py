"""Policy checks for the release-validation GitHub Actions workflow."""

import ast
import itertools
import json
import re
import unittest
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
                self.assertEqual(yaml_scalar(block, "runs-on", 4), "${{ matrix.os }}")
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
        mutated = workflow.replace(regression, mutated_regression, 1)
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


if __name__ == "__main__":
    unittest.main()
