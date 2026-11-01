#!/usr/bin/env python3
"""Vacuous-test check (AOK feature ⑦, docs/patterns/regression.md).

A regression test is only worth keeping if it fails when the code it guards is
broken. This script shows that on throw-away copies of the repository:

  1. copy the repo to a temp dir
  2. run the test command on the unmodified copy      -> must pass ("baseline")
  3. for each mutation: apply it to a fresh copy, run the test again
                                                       -> must fail ("guarded")

A mutation replaces one exact string in one file (compared as UTF-8 bytes, so
any file encoding works). It must match exactly once, so a typo can't make the
check pass by mutating nothing. A test that still passes under its mutation is
"vacuous".

A non-zero exit only says the test run failed, not why. So:
  - a mutated .py file must still compile; a mutation that breaks the syntax
    proves nothing about the test and is reported as "invalid"
  - a run that times out under a mutation is "timeout", not "guarded"
  - "expect" (per mutation in the JSON file, or --expect for all) is a word the
    failing run's output must contain as a whole word (test_a does not match
    test_ab), typically the name of the test that is supposed to catch it; a
    failure without it is "other_failure". A mutation that crashes the code
    at run time still fails the named test: keep mutations runnable
Mutate the code under test, not the tests themselves, and keep mutations valid
code: for languages other than Python the tool cannot tell a crash from a
caught regression unless you give "expect".

Mutations are written only inside the copy. A target that resolves outside it
(through a symbolic link) is refused as "unsafe", and the original repository
is never written by this tool. The test command itself runs in the copy with
your permissions: what it does through absolute paths or links is up to it.

Mutations come from repeated --mutate FILE OLD NEW options or from a JSON file
(--mutations) holding a list of {"file", "old", "new", "label", "expect"}
objects ("label" and "expect" optional; an "expect" given is a non-empty
string, and a mutation without one gets --expect). Use "" as NEW to delete OLD.

Usage:
  vacuity_check.py --repo DIR --test CMD (--mutate FILE OLD NEW ... | --mutations FILE.json)
                   [--expect TEXT] [--ignore NAME ...] [--timeout SEC] [--jobs N] [--json]

The test command is split with shlex (no shell) and runs with the copy as its
working directory. Copies skip .git target node_modules .venv __pycache__;
--ignore NAME skips more names (top level or nested); --ignore none drops the
default list (other --ignore names still apply). --jobs N runs N mutations at once (each has its own copy;
timing-sensitive tests may need a longer --timeout).

Verdict (worst first): baseline_fails, mutation_error (a mutation did not
match exactly once, is invalid or unsafe), vacuous, inconclusive (timeout or
other_failure), guarded.

Exit codes:
  0  guarded: the baseline passes and every mutation makes the test fail as expected
  1  any other verdict
  2  usage or tool error: the check could not run (missing or unreadable
     repository or mutations file, a bad mutation entry, --timeout outside
     (0, 86400], a test command that cannot start)

Stdlib only; safe to run with `python3 -I`.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_IGNORE = [".git", "target", "node_modules", ".venv", "__pycache__"]
ERROR_STATUSES = {"no_match", "invalid", "unsafe"}
INCONCLUSIVE_STATUSES = {"timeout", "other_failure"}


class UsageError(Exception):
    pass


def load_mutations(args) -> list[dict]:
    muts: list[dict] = []
    for f, old, new in args.mutate or []:
        muts.append({"file": f, "old": old, "new": new, "label": f"{f}: {old[:40]!r}", "expect": args.expect})
    if args.mutations:
        p = Path(args.mutations)
        if not p.is_file():
            raise UsageError(f"mutations file not found: {args.mutations}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise UsageError(f"mutations file is not UTF-8 JSON: {e}") from e
        except OSError as e:
            raise UsageError(f"cannot read mutations file {args.mutations}: {e}") from e
        if not isinstance(data, list):
            raise UsageError("mutations file must hold a JSON list")
        for i, m in enumerate(data):
            if not isinstance(m, dict) or not all(isinstance(m.get(k), str) for k in ("file", "old", "new")):
                raise UsageError(f"mutation #{i} needs string fields file, old, new")
            if "expect" in m and (not isinstance(m["expect"], str) or not m["expect"].strip()):
                # an empty expect would silently switch the check (and --expect) off for this mutation
                raise UsageError(f"mutation #{i}: expect must be a non-empty string")
            muts.append({"file": m["file"], "old": m["old"], "new": m["new"],
                         "label": m.get("label") or f"{m['file']}: {m['old'][:40]!r}",
                         "expect": m.get("expect", args.expect)})
    if not muts:
        raise UsageError("give at least one --mutate or --mutations")
    for m in muts:
        if not m["old"]:
            raise UsageError(f"mutation {m['label']}: OLD must not be empty")
        if Path(m["file"]).is_absolute() or ".." in Path(m["file"]).parts:
            raise UsageError(f"mutation file must be a path inside the repo: {m['file']}")
    return muts


def copy_repo(src: Path, dst: Path, ignore: list[str]) -> None:
    # symlinks are copied as links (not followed): a link can point anywhere, so
    # mutations check where their target really is before writing (see mutate)
    try:
        shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(*ignore) if ignore else None)
    except (shutil.Error, OSError) as e:  # a socket or named pipe, an unreadable file: the check cannot run
        raise UsageError(f"cannot copy the repository: {str(e)[:300]}") from e


def run_test(argv: list[str], cwd: Path, timeout: float) -> dict:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise UsageError(f"test command not found: {argv[0]}") from e
    except OSError as e:
        raise UsageError(f"cannot run {argv[0]}: {e.strerror or e}") from e
    except subprocess.TimeoutExpired:
        return {"exit": None, "timeout": True, "output": ""}
    return {"exit": proc.returncode, "timeout": False,
            "output": (proc.stdout + proc.stderr).decode("utf-8", errors="replace")}


def mutate(work: Path, m: dict) -> tuple[str, str] | None:
    """Apply m inside the copy at work. Return (status, detail) on refusal, None when applied."""
    target = work / m["file"]
    root = work.resolve()
    real = target.resolve()
    if real != root and root not in real.parents:
        return "unsafe", f"{m['file']} resolves outside the copy (symbolic link to {real}); not written"
    if not real.is_file():
        return "no_match", "file not found in copy"
    data = real.read_bytes()
    old, new = m["old"].encode("utf-8"), m["new"].encode("utf-8")
    n = len(re.findall(b"(?=" + re.escape(old) + b")", data))  # overlapping matches count too
    if n != 1:
        return "no_match", f"OLD occurs {n} times (must be exactly 1)"
    mutated = data.replace(old, new, 1)
    if real.suffix == ".py":
        try:
            compile(mutated, str(m["file"]), "exec")
        except (SyntaxError, ValueError) as e:
            return "invalid", f"mutated file does not compile ({e.__class__.__name__}: {e}); proves nothing"
    real.write_bytes(mutated)
    return None


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
    given = args.ignore or []
    ignore = [n for n in given if n != "none"] + ([] if "none" in given else DEFAULT_IGNORE)  # 'none' drops the defaults

    report: dict = {"baseline": None, "mutations": [], "verdict": None}
    with tempfile.TemporaryDirectory(prefix="vacuity-") as tmp:
        base = Path(tmp) / "baseline"
        copy_repo(repo, base, ignore)
        b = run_test(argv, base, args.timeout)
        report["baseline"] = {"exit": b["exit"], "timeout": b["timeout"], "passed": b["exit"] == 0,
                              "tail": b["output"][-400:] if not b["timeout"] else "timeout"}
        if b["exit"] != 0:
            report["verdict"] = "baseline_fails"
            return report

        def one(i: int, m: dict) -> dict:
            work = Path(tmp) / f"m{i}"
            copy_repo(repo, work, ignore)
            row = {"label": m["label"], "file": m["file"]}
            refused = mutate(work, m)
            if refused:
                row.update(status=refused[0], detail=refused[1])
            else:
                r = run_test(argv, work, args.timeout)
                row["exit"] = r["exit"]
                if r["timeout"]:
                    row.update(status="timeout", detail=f"no result within {args.timeout:g}s")
                elif r["exit"] == 0:
                    row.update(status="vacuous", tail=r["output"][-400:])
                elif m.get("expect") and not re.search(r"(?<![\w.])" + re.escape(m["expect"]) + r"(?![\w])", r["output"]):
                    row.update(status="other_failure", detail=f"failed without {m['expect']!r} in the output",
                               tail=r["output"][-400:])
                else:
                    row["status"] = "guarded"
            shutil.rmtree(work, ignore_errors=True)
            return row

        # each mutation has its own copy, so they can run side by side; the report keeps input order
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            report["mutations"] = list(pool.map(one, range(len(muts)), muts))
    statuses = {m["status"] for m in report["mutations"]}
    if statuses & ERROR_STATUSES:
        report["verdict"] = "mutation_error"
    elif "vacuous" in statuses:
        report["verdict"] = "vacuous"
    elif statuses & INCONCLUSIVE_STATUSES:
        report["verdict"] = "inconclusive"
    else:
        report["verdict"] = "guarded"
    return report


def render(report: dict) -> str:
    b = report["baseline"]
    state = "timeout" if b["timeout"] else f"exit {b['exit']}"
    lines = [f"verdict: {report['verdict']}", f"baseline: {state} ({'pass' if b['passed'] else 'FAIL'})"]
    if not b["passed"]:
        lines.append("  the test must pass on the unmodified copy before mutations mean anything")
        lines.append("  " + b["tail"].strip().replace("\n", "\n  ")[-300:])
    for m in report["mutations"]:
        extra = f" — {m['detail']}" if m.get("detail") else ""
        lines.append(f"{m['status']:>13}: {m['label']}{extra}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Show a test fails when the code it guards is broken (on a copy).")
    ap.add_argument("--repo", required=True, help="repository directory to copy (never written)")
    ap.add_argument("--test", required=True, help="test command, split with shlex, run in the copy")
    ap.add_argument("--mutate", nargs=3, action="append", metavar=("FILE", "OLD", "NEW"),
                    help="replace the single occurrence of OLD in FILE (repo-relative) with NEW")
    ap.add_argument("--mutations", help="JSON list of {file, old, new, label, expect}")
    ap.add_argument("--expect", metavar="TEXT",
                    help="text a failing run must print to count as guarded (default for every mutation)")
    ap.add_argument("--ignore", action="append", metavar="NAME",
                    help=f"more names to skip when copying (also skipped unless 'none' is given: {' '.join(DEFAULT_IGNORE)})")
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds per test run (default 600)")
    ap.add_argument("--jobs", type=int, default=1, metavar="N",
                    help="mutations to run at the same time, each on its own copy (default 1)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 86400 or args.jobs < 1:
        print("error: --timeout must be a number of seconds in (0, 86400] and --jobs >= 1", file=sys.stderr)
        return 2
    try:
        report = check(args)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return 0 if report["verdict"] == "guarded" else 1


if __name__ == "__main__":
    sys.exit(main())
