#!/usr/bin/env python3
"""Codex invocation policy for explicit-only authored skills.

An authored ``brood-*`` skill whose ``SKILL.md`` frontmatter carries the
canonical line ``disable-model-invocation: true`` must ship a byte-identical
``agents/openai.yaml`` that turns off implicit invocation, and no other
authored skill may ship that file.  ``--write`` generates the file; ``--check``
fails closed on every drift class.

Only directories whose names start with ``brood-`` are inspected.  Vendored
bundles are never opened: the skills root is listed by name and every other
entry is skipped before any stat or read.  Frontmatter parsing is line-level
over top-level scalar keys, not a YAML parser.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# The single authoritative list of authored skills that must not be invoked
# implicitly.  Unit h1-hillclimb-skill appends "brood-hillclimb".
EXPLICIT_ONLY_SKILLS = (
    "brood-debrief",
    "brood-hillclimb",
    "brood-orchestrate",
    "brood-portable-handoff",
    "brood-project",
    "brood-swarm",
)

AUTHORED_PREFIX = "brood-"
FLAG_KEY = "disable-model-invocation"
# The exact text after the colon that counts as canonical true.
CANONICAL_FLAG_VALUE = " true"
OPENAI_YAML_RELPATH = ("agents", "openai.yaml")
OPENAI_YAML_BYTES = b"policy:\n  allow_implicit_invocation: false\n"

FLAG_TRUE = "true"
FLAG_ABSENT = "absent"
FLAG_NONCANONICAL = "noncanonical"
FLAG_MALFORMED = "malformed"

DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


def parse_flag(text):
    # type: (str) -> Dict[str, str]
    """Classify the frontmatter flag as true, absent, noncanonical or malformed.

    Returns ``{"state": ..., "detail": ...}``.  Lines are split on ``\\n`` only,
    so a CRLF opening fence is malformed rather than silently accepted.
    """
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return {"state": FLAG_MALFORMED, "detail": "SKILL.md does not open with a '---' line"}
    end = None
    for index in range(1, len(lines)):
        if lines[index] == "---":
            end = index
            break
    if end is None:
        return {"state": FLAG_MALFORMED, "detail": "frontmatter has no closing '---' line"}
    values = []
    for line in lines[1:end]:
        # Indented lines belong to the preceding key's block value.
        if not line or line[0] in " \t#":
            continue
        key, sep, rest = line.partition(":")
        if sep and key == FLAG_KEY:
            values.append(rest)
    if not values:
        return {"state": FLAG_ABSENT, "detail": ""}
    if len(values) > 1:
        return {"state": FLAG_NONCANONICAL,
                "detail": "%s appears %d times" % (FLAG_KEY, len(values))}
    if values[0] != CANONICAL_FLAG_VALUE:
        return {"state": FLAG_NONCANONICAL,
                "detail": "%s value %r is not the canonical unquoted true"
                          % (FLAG_KEY, values[0].strip())}
    return {"state": FLAG_TRUE, "detail": ""}


def authored_skill_names(skills_root):
    # type: (Path) -> List[str]
    """Names of ``brood-*`` entries, filtered by name before any stat."""
    return sorted(name for name in os.listdir(str(skills_root))
                  if name.startswith(AUTHORED_PREFIX))


def _error(errors, skill, code, path, detail):
    errors.append({"skill": skill, "code": code, "path": str(path), "detail": detail})


def _read_flag(skill_dir):
    # type: (Path) -> Dict[str, str]
    skill_md = skill_dir / "SKILL.md"
    if skill_md.is_symlink() or not skill_md.is_file():
        return {"state": FLAG_MALFORMED, "detail": "SKILL.md is missing or not a regular file"}
    try:
        text = skill_md.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"state": FLAG_MALFORMED, "detail": "SKILL.md unreadable: %s" % exc}
    return parse_flag(text)


def _yaml_state(skill_dir):
    # type: (Path) -> Dict[str, str]
    """One of absent, ok, drift, unsafe (symlink or non-regular path)."""
    agents = skill_dir / OPENAI_YAML_RELPATH[0]
    target = agents.joinpath(*OPENAI_YAML_RELPATH[1:])
    if agents.is_symlink() or (os.path.lexists(str(agents)) and not agents.is_dir()):
        return {"state": "unsafe", "detail": "agents is a symlink or not a directory"}
    if not os.path.lexists(str(target)):
        return {"state": "absent", "detail": ""}
    if target.is_symlink() or not target.is_file():
        return {"state": "unsafe", "detail": "agents/openai.yaml is a symlink or not a regular file"}
    try:
        data = target.read_bytes()
    except OSError as exc:
        return {"state": "unsafe", "detail": "agents/openai.yaml unreadable: %s" % exc}
    if data != OPENAI_YAML_BYTES:
        return {"state": "drift", "detail": "agents/openai.yaml differs from the canonical bytes"}
    return {"state": "ok", "detail": ""}


def _write_yaml(skill_dir):
    # type: (Path) -> None
    agents = skill_dir / OPENAI_YAML_RELPATH[0]
    agents.mkdir(exist_ok=True)
    target = agents.joinpath(*OPENAI_YAML_RELPATH[1:])
    fd, tmp = tempfile.mkstemp(prefix=".openai.yaml.", dir=str(agents))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(OPENAI_YAML_BYTES)
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(target))
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def run(skills_root, write=False, explicit_only=EXPLICIT_ONLY_SKILLS):
    # type: (Path, bool, Sequence[str]) -> Dict[str, object]
    """Inspect (and with ``write`` repair) the tree; return a JSON-ready report."""
    skills_root = Path(skills_root)
    errors = []  # type: List[Dict[str, str]]
    written = []  # type: List[str]
    skills = []  # type: List[Dict[str, str]]
    if skills_root.is_symlink() or not skills_root.is_dir():
        _error(errors, "", "skills_root_invalid", skills_root,
               "skills root is missing, a symlink, or not a directory")
        return _report(skills_root, write, skills, written, errors)
    for name in authored_skill_names(skills_root):
        skill_dir = skills_root / name
        if skill_dir.is_symlink() or not skill_dir.is_dir():
            _error(errors, name, "unsafe_skill_dir", skill_dir,
                   "authored skill entry is a symlink or not a directory")
            continue
        flag = _read_flag(skill_dir)
        yaml_path = skill_dir.joinpath(*OPENAI_YAML_RELPATH)
        yaml = _yaml_state(skill_dir)
        if write and flag["state"] == FLAG_TRUE and yaml["state"] in ("absent", "drift"):
            _write_yaml(skill_dir)
            written.append(name)
            yaml = _yaml_state(skill_dir)
        skills.append({"name": name, "flag": flag["state"], "openai_yaml": yaml["state"]})

        skill_md = skill_dir / "SKILL.md"
        if flag["state"] == FLAG_MALFORMED:
            _error(errors, name, "malformed_frontmatter", skill_md, flag["detail"])
        elif flag["state"] == FLAG_NONCANONICAL:
            _error(errors, name, "noncanonical_flag", skill_md, flag["detail"])
        elif flag["state"] == FLAG_ABSENT and name in explicit_only:
            _error(errors, name, "explicit_only_missing_flag", skill_md,
                   "%s is listed in EXPLICIT_ONLY_SKILLS but lacks '%s: true'"
                   % (name, FLAG_KEY))

        if yaml["state"] == "unsafe":
            _error(errors, name, "unsafe_openai_yaml", yaml_path, yaml["detail"])
        elif flag["state"] == FLAG_TRUE:
            if yaml["state"] == "absent":
                _error(errors, name, "missing_openai_yaml", yaml_path,
                       "'%s: true' requires agents/openai.yaml" % FLAG_KEY)
            elif yaml["state"] == "drift":
                _error(errors, name, "openai_yaml_drift", yaml_path, yaml["detail"])
        elif yaml["state"] != "absent":
            _error(errors, name, "orphan_openai_yaml", yaml_path,
                   "agents/openai.yaml exists without '%s: true'" % FLAG_KEY)
    return _report(skills_root, write, skills, written, errors)


def _report(skills_root, write, skills, written, errors):
    return {
        "ok": not errors,
        "mode": "write" if write else "check",
        "skills_root": str(skills_root),
        "skills": skills,
        "written": written,
        "errors": errors,
    }


def main(argv=None):
    # type: (Optional[Sequence[str]]) -> int
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skills-root", default=str(DEFAULT_SKILLS_ROOT))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true",
                      help="generate agents/openai.yaml for flagged skills, then check")
    mode.add_argument("--check", action="store_true", help="report drift only")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run(Path(args.skills_root), write=args.write)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for name in report["written"]:
            print("wrote %s/agents/openai.yaml" % name)
        for err in report["errors"]:
            print("ERROR %s %s: %s (%s)" % (err["code"], err["skill"], err["detail"],
                                            err["path"]), file=sys.stderr)
        if report["ok"]:
            print("ok: %d authored skills consistent" % len(report["skills"]))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
