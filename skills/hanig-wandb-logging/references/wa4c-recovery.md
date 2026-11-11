# RECOVERY, NOT READY

Reviewed head: 24afcef52cb026bf3c73158bb8b4e436396d9a8e.
The single gate returned REVIEW_FAIL; must not merge. No PR or repairs.
Anchored branch retains that reviewed head. This recovery record is diagnostic.

Open reproduced wording failures:
- Schema-invalid JSON may be overwritten from backup without a corrupt archive.
- Using a new output_dir after recovery leaves the old secret-bearing archive.
- A singleton Decimal NaN set succeeds; a two-element NaN set raises on comparison.
- Corrupt archives copy raw corrupt bytes; a former secret value is absent when corruption replaced it.

Recovery: preserve suspect metadata before resume, delete or securely retain existing archives, use lists for set values and request orchestrator adjudication before changing the task-prescribed wording.
Runtime code and tests remain byte-identical to cded910f.
197 scoped tests passed (one absent-SDK skip), 17 floor invariants passed, declaration checks passed.
Complete reviewed diff was 71,997 bytes. Only SKILL.md changed since cded910f.

## Safe reproduction

Bundled fake SDK, temporary directories and synthetic SENTINEL; no network or credentials.
The following observations are diagnostics, not a fix or passing gate.

```json
{
  "schema_invalid": {
    "archives": [],
    "epoch": 1
  },
  "switch_directory": {
    "archive_exists": true,
    "archive_contains_sentinel": true
  },
  "singleton_nan": {
    "values": [
      "NaN"
    ]
  },
  "two_nan": {
    "exception": "InvalidOperation"
  },
  "modified_corrupt_bytes": {
    "sentinel_present": false,
    "replacement_present": true
  },
  "docs_only_paths": [
    "skills/hanig-wandb-logging/SKILL.md"
  ]
}
```

## Dispositions

```json
{
  "0f109f730790ffbc340d0a3bf855ed80c28008dfc8b63f39c7d16236b2c5ef79": {
    "location": "skills/hanig-wandb-logging/SKILL.md:81",
    "summary": "The promised corrupt-snapshot archive is not created for a valid JSON object with an invalid snapshot schema.",
    "reviewer": "terra",
    "disposition": "reproduced",
    "reason": "Reproduced overbroad archive promise for schema-invalid JSON: primary overwritten from backup with no archive. Recovery: preserve suspect snapshots before resume; use fresh output_dir. No repair permitted after this single gate.",
    "reproduction": {
      "setup": "Inspected counterexamples before execution. Bundled fake W&B, synthetic SENTINEL, TemporaryDirectory, no network, actual credentials, third-party import or unrelated writes. Exhaustive comparison of singleton/two-element NaN sets; no random generator.",
      "command": "PYTHONPATH=\"$PWD\" /opt/homebrew/bin/python3.12 \"$SWARM_UNIT_DIR/reproduce-wa4c.py\"",
      "expected": "Measure each claimed documentation discrepancy against unchanged reviewed behavior.",
      "observed": "{\"archives\": [], \"epoch\": 1}",
      "revision": "24afcef52cb026bf3c73158bb8b4e436396d9a8e"
    }
  },
  "fd3704a712a273a449a7a36d587d781d79fa0a1c598ac00c516fc3040c3b788b": {
    "location": "skills/hanig-wandb-logging/SKILL.md:81",
    "summary": "Switching output directories after recovery does not remove the secret-bearing archive, despite being offered as an alternative to deleting it.",
    "reviewer": "terra",
    "disposition": "reproduced",
    "reason": "Reproduced: switching directories does not remove an existing secret-bearing archive. Use a new output_dir before recovery to avoid creating it; after recovery delete or securely retain the old archive. Documentation ambiguity remains; no repair round authorized.",
    "reproduction": {
      "setup": "Inspected counterexamples before execution. Bundled fake W&B, synthetic SENTINEL, TemporaryDirectory, no network, actual credentials, third-party import or unrelated writes. Exhaustive comparison of singleton/two-element NaN sets; no random generator.",
      "command": "PYTHONPATH=\"$PWD\" /opt/homebrew/bin/python3.12 \"$SWARM_UNIT_DIR/reproduce-wa4c.py\"",
      "expected": "Measure each claimed documentation discrepancy against unchanged reviewed behavior.",
      "observed": "{\"archive_contains_sentinel\": true, \"archive_exists\": true}",
      "revision": "24afcef52cb026bf3c73158bb8b4e436396d9a8e"
    }
  },
  "b83b5927547ba720336e205d8989ee5903d6cc4f84096c06b86dae86adeb3c58": {
    "location": "skills/hanig-wandb-logging/SKILL.md:78",
    "summary": "The Decimal NaN warning incorrectly implies that every set containing it raises in start_run.",
    "reviewer": "terra",
    "disposition": "reproduced",
    "reason": "Reproduced: singleton Decimal NaN set succeeds; a comparison-triggering two-element set raises InvalidOperation. The exact task-supplied sentence is overbroad. Use a list; no wording change permitted after this gate.",
    "reproduction": {
      "setup": "Inspected counterexamples before execution. Bundled fake W&B, synthetic SENTINEL, TemporaryDirectory, no network, actual credentials, third-party import or unrelated writes. Exhaustive comparison of singleton/two-element NaN sets; no random generator.",
      "command": "PYTHONPATH=\"$PWD\" /opt/homebrew/bin/python3.12 \"$SWARM_UNIT_DIR/reproduce-wa4c.py\"",
      "expected": "Measure each claimed documentation discrepancy against unchanged reviewed behavior.",
      "observed": "{\"singleton\": {\"values\": [\"NaN\"]}, \"two_elements\": {\"exception\": \"InvalidOperation\"}}",
      "revision": "24afcef52cb026bf3c73158bb8b4e436396d9a8e"
    }
  },
  "97ac7b9b7563dbd7716d8a232464f60af9a4c79dadf0c62e1cdd44e51d05892e": {
    "location": "skills/hanig-wandb-logging/SKILL.md:80",
    "summary": "The corrected set limit says a set holding Decimal('NaN') raises decimal.InvalidOperation in start_run, but that is only true when the set has another element to compare against.",
    "reviewer": "sonnet-5.5",
    "disposition": "reproduced",
    "reason": "Reproduced: singleton Decimal NaN set succeeds; a comparison-triggering two-element set raises InvalidOperation. The exact task-supplied sentence is overbroad. Use a list; no wording change permitted after this gate.",
    "reproduction": {
      "setup": "Inspected counterexamples before execution. Bundled fake W&B, synthetic SENTINEL, TemporaryDirectory, no network, actual credentials, third-party import or unrelated writes. Exhaustive comparison of singleton/two-element NaN sets; no random generator.",
      "command": "PYTHONPATH=\"$PWD\" /opt/homebrew/bin/python3.12 \"$SWARM_UNIT_DIR/reproduce-wa4c.py\"",
      "expected": "Measure each claimed documentation discrepancy against unchanged reviewed behavior.",
      "observed": "{\"singleton\": {\"values\": [\"NaN\"]}, \"two_elements\": {\"exception\": \"InvalidOperation\"}}",
      "revision": "24afcef52cb026bf3c73158bb8b4e436396d9a8e"
    }
  }
}
```
