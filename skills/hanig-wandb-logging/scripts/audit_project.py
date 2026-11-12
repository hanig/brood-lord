#!/usr/bin/env python3
"""Read-only per-rule run/artifact audit; call audit(api, "entity/project", 200)."""

from __future__ import annotations

import argparse
import collections
import json
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("_audit_wbkit", Path(__file__).with_name("wbkit.py"))
wbkit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wbkit)

def _code(issue: str) -> str:
    return issue.split(":", 1)[0]


def audit(api, path: str, limit: int = 200) -> dict:
    if len(path.split("/")) != 2 or not all(path.split("/")):
        raise ValueError("path must be entity/project")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a nonnegative integer")
    if hasattr(api, "Api"):
        api = api.Api(timeout=120)
    entity, project = path.split("/", 1)
    runs = api.runs(path, order="-created_at", per_page=min(limit, 100)) if limit else []
    run_rows = []
    for run in runs[:limit]:
        issues = wbkit.lint_run(
            name=run.name,
            notes=run.notes,
            group=run.group,
            job_type=run.job_type,
            config=run.config,
            summary_keys=list(run.summary_metrics.keys()),
            tags=run.tags,
            state=run.state,
        )
        run_rows.append({"id": run.id, "name": run.name, "state": run.state, "url": run.url, "issues": issues})

    art_rows = []
    for atype in api.artifact_types(project=path):
        if atype.name.startswith("wandb-") or atype.name in ("run_table", "job"):
            continue
        for coll in atype.collections():
            latest = next(iter(coll.artifacts()), None) if hasattr(coll, "artifacts") else next(iter(coll.versions()), None)
            issues = wbkit.lint_artifact(
                name=coll.name,
                type=atype.name,
                description=(getattr(latest, "description", None) or getattr(coll, "description", None)),
                metadata=getattr(latest, "metadata", None),
            )
            art_rows.append({"name": coll.name, "type": atype.name, "issues": issues})

    by_rule: dict[str, list[str]] = collections.defaultdict(list)
    for r in run_rows:
        for i in r["issues"]:
            by_rule[_code(i)].append(r["name"] or r["id"])
    for a in art_rows:
        for i in a["issues"]:
            by_rule[_code(i)].append(a["name"])

    clean_runs = sum(1 for r in run_rows if not r["issues"])
    clean_arts = sum(1 for a in art_rows if not a["issues"])
    return {
        "project": path,
        "runs_checked": len(run_rows),
        "runs_clean": clean_runs,
        "artifact_collections_checked": len(art_rows),
        "artifact_collections_clean": clean_arts,
        "by_rule": {k: v for k, v in sorted(by_rule.items(), key=lambda kv: -len(kv[1]))},
        "runs": run_rows,
        "artifacts": art_rows,
    }


def main(api=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="entity/project")
    ap.add_argument("--limit", type=int, default=200, help="newest N runs to check")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if "/" not in args.path:
        ap.error("path must be entity/project")
    if api is None:
        ap.error("pass an Api object to main(api), or call audit(api, path, limit) from your own caller")
    rep = audit(api, args.path, args.limit)
    if args.json:
        json.dump(rep, sys.stdout, indent=2, default=str)
        print()
        return 0
    print(
        f"{rep['project']}: {rep['runs_clean']}/{rep['runs_checked']} runs clean, "
        f"{rep['artifact_collections_clean']}/{rep['artifact_collections_checked']} artifact collections clean"
    )
    for rule, who in rep["by_rule"].items():
        sample = ", ".join(who[:5]) + (" ..." if len(who) > 5 else "")
        print(f"  {len(who):4d}  {rule}  [{sample}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
