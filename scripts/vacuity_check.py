#!/usr/bin/env python3
"""Vacuous-test check (AOK feature ⑦, docs/patterns/regression.md).

A regression test is only worth keeping if it fails when the code it guards is
broken. This script proves that on a throw-away copy of the repository:

  1. copy the repo to a temp dir (the original is never touched)
  2. run the test command on the unmodified copy      -> must pass ("baseline")
  3. for each mutation: apply it to a fresh copy, run the test again
                                                       -> must fail ("guarded")

A mutation replaces one exact string in one file. It must match exactly once,
so a typo can't make the check pass by mutating nothing. A test that still
passes under its mutation is reported as "vacuous".

Mutations come from repeated --mutate FILE OLD NEW options or from a JSON file
(--mutations) holding a list of {"file", "old", "new", "label"} objects. Use
"" as NEW to delete OLD.

Usage:
  vacuity_check.py --repo DIR --test CMD (--mutate FILE OLD NEW ... | --mutations FILE.json)
                   [--ignore NAME ...] [--timeout SEC] [--json]

The test command is split with shlex (no shell) and runs with the copy as its
working directory. --ignore skips top-level-or-nested names while copying
(default: .git target node_modules .venv __pycache__); pass --ignore none to
copy everything.

Exit codes:
  0  baseline passes and every mutation makes the test fail
  1  baseline fails, a mutation did not match exactly once, or a test is vacuous
  2  usage error

Stdlib only; safe to run with `python3 -I`.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_IGNORE = [".git", "target", "node_modules", ".venv", "__pycache__"]


class UsageError(Exception):
    pass


def load_mutations(args) -> list[dict]:
    muts: list[dict] = []
    for f, old, new in args.mutate or []:
        muts.append({"file": f, "old": old, "new": new, "label": f"{f}: {old[:40]!r}"})
    if args.mutations:
        p = Path(args.mutations)
        if not p.is_file():
            raise UsageError(f"mutations file not found: {args.mutations}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise UsageError(f"mutations file is not JSON: {e}") from e
        if not isinstance(data, list):
            raise UsageError("mutations file must hold a JSON list")
        for i, m in enumerate(data):
            if not isinstance(m, dict) or not all(isinstance(m.get(k), str) for k in ("file", "old", "new")):
                raise UsageError(f"mutation #{i} needs string fields file, old, new")
            muts.append({"file": m["file"], "old": m["old"], "new": m["new"],
                         "label": m.get("label") or f"{m['file']}: {m['old'][:40]!r}"})
    if not muts:
        raise UsageError("give at least one --mutate or --mutations")
    for m in muts:
        if not m["old"]:
            raise UsageError(f"mutation {m['label']}: OLD must not be empty")
        if Path(m["file"]).is_absolute() or ".." in Path(m["file"]).parts:
            raise UsageError(f"mutation file must be a path inside the repo: {m['file']}")
    return muts


def copy_repo(src: Path, dst: Path, ignore: list[str]) -> None:
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(*ignore) if ignore else None)


def run_test(argv: list[str], cwd: Path, timeout: float) -> dict:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise UsageError(f"test command not found: {argv[0]}") from e
    except subprocess.TimeoutExpired:
        return {"exit": None, "tail": "timeout"}
    tail = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")[-400:]
    return {"exit": proc.returncode, "tail": tail}


def check(args) -> dict:
    repo = Path(args.repo)
    if not repo.is_dir():
        raise UsageError(f"repo dir not found: {args.repo}")
    try:
        argv = shlex.split(args.test)
    except ValueError as e:
        raise UsageError(f"cannot parse test command: {e}") from e
    if not argv:
        raise UsageError("empty test command")
    muts = load_mutations(args)
    ignore = [] if args.ignore == ["none"] else (args.ignore or DEFAULT_IGNORE)

    report: dict = {"baseline": None, "mutations": [], "verdict": None}
    with tempfile.TemporaryDirectory(prefix="vacuity-") as tmp:
        base = Path(tmp) / "baseline"
        copy_repo(repo, base, ignore)
        b = run_test(argv, base, args.timeout)
        report["baseline"] = {"exit": b["exit"], "passed": b["exit"] == 0, "tail": b["tail"]}
        if b["exit"] != 0:
            report["verdict"] = "baseline_fails"
            return report
        for i, m in enumerate(muts):
            work = Path(tmp) / f"m{i}"
            copy_repo(repo, work, ignore)
            target = work / m["file"]
            row = {"label": m["label"], "file": m["file"]}
            if not target.is_file():
                row.update(status="no_match", detail="file not found in copy")
                report["mutations"].append(row)
                continue
            text = target.read_text(encoding="utf-8")
            n = text.count(m["old"])
            if n != 1:
                row.update(status="no_match", detail=f"OLD occurs {n} times (must be exactly 1)")
                report["mutations"].append(row)
                continue
            target.write_text(text.replace(m["old"], m["new"], 1), encoding="utf-8")
            r = run_test(argv, work, args.timeout)
            row.update(exit=r["exit"], status="guarded" if r["exit"] != 0 else "vacuous",
                       tail=r["tail"] if r["exit"] == 0 else "")
            report["mutations"].append(row)
            shutil.rmtree(work, ignore_errors=True)
    statuses = {m["status"] for m in report["mutations"]}
    if "no_match" in statuses:
        report["verdict"] = "mutation_error"
    elif "vacuous" in statuses:
        report["verdict"] = "vacuous"
    else:
        report["verdict"] = "guarded"
    return report


def render(report: dict) -> str:
    b = report["baseline"]
    lines = [f"verdict: {report['verdict']}", f"baseline: exit {b['exit']} ({'pass' if b['passed'] else 'FAIL'})"]
    if not b["passed"]:
        lines.append("  the test must pass on the unmodified copy before mutations mean anything")
        lines.append("  " + b["tail"].strip().replace("\n", "\n  ")[-300:])
    for m in report["mutations"]:
        extra = f" — {m['detail']}" if m.get("detail") else ""
        lines.append(f"{m['status']:>8}: {m['label']}{extra}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prove a test fails when the code it guards is broken (on a copy).")
    ap.add_argument("--repo", required=True, help="repository directory to copy (never modified)")
    ap.add_argument("--test", required=True, help="test command, split with shlex, run in the copy")
    ap.add_argument("--mutate", nargs=3, action="append", metavar=("FILE", "OLD", "NEW"),
                    help="replace the single occurrence of OLD in FILE (repo-relative) with NEW")
    ap.add_argument("--mutations", help="JSON list of {file, old, new, label}")
    ap.add_argument("--ignore", action="append", metavar="NAME",
                    help=f"names to skip when copying (default: {' '.join(DEFAULT_IGNORE)}; 'none' copies all)")
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds per test run (default 600)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    try:
        report = check(args)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return 0 if report["verdict"] == "guarded" else 1


if __name__ == "__main__":
    sys.exit(main())
