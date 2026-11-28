"""Synthetic explicit-only skills for tests of the invocation policy.

No shipped authored skill carries ``disable-model-invocation: true`` since
2026-11-27, so tests that exercise the installer and ``bin/doctor`` policy
checks flag copies instead of relying on the live tree.
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLAG_LINE = "disable-model-invocation: true"
OPENAI_YAML = b"policy:\n  allow_implicit_invocation: false\n"


def flag_skill(skill_dir):
    """Add the canonical flag after ``name:`` and write the canonical yaml."""
    skill_dir = Path(skill_dir)
    skill_md = skill_dir / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    head, sep, rest = text.partition("\nname: ")
    if not sep or text.count("\n%s\n" % FLAG_LINE):
        raise ValueError("%s: no name line, or already flagged" % skill_md)
    name_line, newline, tail = rest.partition("\n")
    skill_md.write_text(head + sep + name_line + newline + FLAG_LINE + "\n" + tail,
                        encoding="utf-8")
    (skill_dir / "agents").mkdir(exist_ok=True)
    (skill_dir / "agents" / "openai.yaml").write_bytes(OPENAI_YAML)
    return skill_dir


def flagged_checkout(dest, names):
    """Copy this checkout (no Git metadata or caches) to ``dest`` and flag ``names``."""
    dest = Path(dest)
    shutil.copytree(str(ROOT), str(dest), symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"))
    for name in names:
        flag_skill(dest / "skills" / name)
    return dest
