#!/usr/bin/env python3
"""Let Claude Code run conclave.py without the auto-mode classifier blocking it.

Auto mode can deny `conclave.py round` and `ratify` as data exfiltration when a
brief carries unpublished results, since the script sends the brief to outside
model providers. An allow rule in ~/.claude/settings.json for this installed
copy of conclave.py lets the moderator session run every conclave command
itself. Run this yourself, not from a Claude session: it changes your own
permission settings.

    allow.py              add the rules (idempotent)
    allow.py --dry-run    show what would change
    allow.py --remove     take the rules out again

The file is backed up next to itself before any write. Standard library only.
"""

import argparse
import datetime
import json
import os
import shlex
import shutil
import sys
import tempfile


def _home_form(path, home):
    """The ~ spelling of an absolute path under home, or None."""
    home = home.rstrip(os.sep)
    if home and path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return None


def _shell_form(path):
    """path as a shell command line spells it; a leading ~/ stays unquoted
    so the shell still expands it."""
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def default_script():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "conclave.py")


def rules(script=None, home=None):
    """Allow rules for the conclave.py beside this file, as it may be typed.

    Bash rules match the command prefix as written, so cover the path this
    installed copy was loaded from, its ~ spelling when it sits under HOME, and
    the real file when the skill directory is a symlink (a link install).
    A path that needs shell quoting (a space, say) also gets the quoted
    spelling, which is how the printed command and a pasted one write it.
    """
    script = script or default_script()
    home = os.path.expanduser("~") if home is None else home
    real = os.path.realpath(script)
    paths = [script, real]
    paths += [form for form in (_home_form(script, home), _home_form(real, home)) if form]
    out = []
    for p in paths:
        for spelling in (p, _shell_form(p)):
            r = f"Bash(python3 {spelling}:*)"
            if r not in out:
                out.append(r)
    return out


def legacy_rules(home=None):
    """Rules an earlier hanig-conclave allow.py wrote; --remove takes them out too."""
    home = os.path.expanduser("~") if home is None else home
    path = "~/.claude/skills/hanig-conclave/scripts/conclave.py"
    return [f"Bash(python3 {path}:*)",
            f"Bash(python3 {os.path.join(home, '.claude/skills/hanig-conclave/scripts/conclave.py')}:*)"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--settings", default=os.path.expanduser("~/.claude/settings.json"))
    ap.add_argument("--remove", action="store_true", help="remove the rules instead of adding them")
    ap.add_argument("--dry-run", action="store_true", help="print the change without writing")
    a = ap.parse_args()

    path = a.settings
    if os.path.exists(path):
        with open(path) as f:
            try:
                settings = json.load(f)
            except json.JSONDecodeError as e:
                sys.exit(f"allow: {path} is not valid JSON ({e}); fix it first")
        if not isinstance(settings, dict):
            sys.exit(f"allow: {path} holds a JSON {type(settings).__name__}, "
                     "not an object; fix it first")
    else:
        settings = {}

    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        sys.exit(f"allow: \"permissions\" in {path} is not an object; fix it first")
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        sys.exit(f"allow: \"permissions.allow\" in {path} is not a list; fix it first")
    want = rules()
    if a.remove:
        want += [r for r in legacy_rules() if r not in want]
        changed = [r for r in want if r in allow]
        allow[:] = [r for r in allow if r not in want]
    else:
        changed = [r for r in want if r not in allow]
        allow.extend(changed)

    verb = "remove" if a.remove else "add"
    if not a.remove:
        print(f"allow: call conclave.py as `python3 {shlex.quote(default_script())} ...`")
    if not changed:
        print(f"allow: nothing to {verb}; {path} already {'lacks' if a.remove else 'has'} the rules")
        return
    for r in changed:
        print(f"  {verb} {r}")
    if a.dry_run:
        print("allow: dry run, nothing written")
        return

    if os.path.exists(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup, n = f"{path}.bak-{stamp}", 1
        while os.path.exists(backup):
            backup, n = f"{path}.bak-{stamp}-{n}", n + 1
        shutil.copy2(path, backup)
        print(f"allow: backup {backup}")
    # Write to a temp file in the same directory, then rename, so a crash
    # never leaves a half-written settings file.
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".settings.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)
    print(f"allow: wrote {path}; new Claude Code sessions pick it up (restart this one if it does not)")


if __name__ == "__main__":
    main()
