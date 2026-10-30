#!/usr/bin/env python3
"""Differential regression check (AOK feature ⑦, docs/patterns/regression.md).

Feeds the same input to a baseline command and a candidate command and
compares their output. It is built to refuse the ways a "same output" check
fools itself:

  empty           both sides printed nothing (e.g. a reused session id made a
                  hook stay silent). Fails unless --allow-empty.
  failed          a side exited non-zero. Fails unless --allow-fail.
  nondeterministic a side gave different output on two runs (--determinism).
                  Pin the clock, seed or session state, or --normalize it.
  differs         normalized outputs differ; a unified diff head is printed.

Normalization is explicit and counted: every --normalize REGEX replaces its
matches with <N> and the report says how many replacements each side got, so
a comparison can't silently become "identical after deleting everything".

{nonce} in a command argument or in the --stdin file is replaced by a fresh
random token for every single invocation, so tools that remember state per
key (sessions, caches) see a new key each time.

--timing N runs the two sides alternately N times (base, cand, base, ...) so
that drift over time (other load, CPU clock) lands on both sides, and reports
min/median/max wall time in ms and the median change in percent.

Commands are given as one string each and split with shlex (no shell). Use
"bash -c '...'" explicitly when shell features are needed.

Usage:
  diffcheck.py --base CMD --cand CMD [--stdin FILE] [--normalize REGEX ...]
               [--determinism] [--timing N] [--allow-empty] [--allow-fail]
               [--timeout SEC] [--cwd DIR] [--json]

Exit codes:
  0  identical (after the listed normalizations)
  1  differs, empty, failed or nondeterministic
  2  usage error

Stdlib only; safe to run with `python3 -I`.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import secrets
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

NONCE = "{nonce}"
DIFF_HEAD_LINES = 40


class UsageError(Exception):
    pass


def build_argv(cmd: str) -> list[str]:
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise UsageError(f"cannot parse command {cmd!r}: {e}") from e
    if not argv:
        raise UsageError("empty command")
    return argv


def run_once(argv: list[str], stdin_text: str | None, timeout: float, cwd: str | None) -> dict:
    """Run one invocation with a fresh nonce; return output, exit code and wall time."""
    nonce = secrets.token_hex(8)
    real_argv = [a.replace(NONCE, nonce) for a in argv]
    data = stdin_text.replace(NONCE, nonce).encode("utf-8") if stdin_text is not None else None
    start = time.perf_counter()
    try:
        proc = subprocess.run(real_argv, input=data, capture_output=True, timeout=timeout, cwd=cwd,
                              stdin=None if data is not None else subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise UsageError(f"command not found: {real_argv[0]}") from e
    except subprocess.TimeoutExpired:
        return {"out": b"", "err": b"timeout", "exit": None, "ms": (time.perf_counter() - start) * 1000}
    return {"out": proc.stdout, "err": proc.stderr, "exit": proc.returncode,
            "ms": (time.perf_counter() - start) * 1000}


def normalize(data: bytes, patterns: list[re.Pattern]) -> tuple[str, list[int]]:
    text = data.decode("utf-8", errors="replace")
    counts = []
    for p in patterns:
        text, n = p.subn("<N>", text)
        counts.append(n)
    return text, counts


def summarize_timing(samples: list[float]) -> dict:
    return {"n": len(samples), "min_ms": round(min(samples), 1),
            "median_ms": round(statistics.median(samples), 1), "max_ms": round(max(samples), 1)}


def check(args) -> dict:
    base_argv, cand_argv = build_argv(args.base), build_argv(args.cand)
    stdin_text = None
    if args.stdin:
        p = Path(args.stdin)
        if not p.is_file():
            raise UsageError(f"stdin file not found: {args.stdin}")
        stdin_text = p.read_text(encoding="utf-8")
    try:
        patterns = [re.compile(r) for r in args.normalize]
    except re.error as e:
        raise UsageError(f"bad --normalize regex: {e}") from e

    report: dict = {"verdict": None, "problems": [], "normalize": args.normalize}
    sides = {}
    for name, argv in (("base", base_argv), ("cand", cand_argv)):
        r = run_once(argv, stdin_text, args.timeout, args.cwd)
        text, counts = normalize(r["out"], patterns)
        sides[name] = {"run": r, "text": text, "counts": counts}
        report[name] = {"exit": r["exit"], "bytes": len(r["out"]), "normalized_counts": counts,
                        "stderr_head": r["err"].decode("utf-8", errors="replace")[:300]}

    for name in ("base", "cand"):
        if sides[name]["run"]["exit"] != 0 and not args.allow_fail:
            report["problems"].append(f"{name} exited {sides[name]['run']['exit']}")
    if not args.allow_empty and (report["base"]["bytes"] == 0 or report["cand"]["bytes"] == 0):
        empty = [n for n in ("base", "cand") if report[n]["bytes"] == 0]
        report["problems"].append("empty output: " + ", ".join(empty))

    if args.determinism:
        for name, argv in (("base", base_argv), ("cand", cand_argv)):
            again, _ = normalize(run_once(argv, stdin_text, args.timeout, args.cwd)["out"], patterns)
            if again != sides[name]["text"]:
                report["problems"].append(f"{name} is nondeterministic (two runs differ after normalization)")

    same = sides["base"]["text"] == sides["cand"]["text"]
    if not same:
        diff = difflib.unified_diff(sides["base"]["text"].splitlines(), sides["cand"]["text"].splitlines(),
                                    "base", "cand", lineterm="", n=1)
        report["diff_head"] = list(diff)[:DIFF_HEAD_LINES]

    if any(" exited " in p for p in report["problems"]):
        verdict = "failed"
    elif any(p.startswith("empty output") for p in report["problems"]):
        verdict = "empty"
    elif any("nondeterministic" in p for p in report["problems"]):
        verdict = "nondeterministic"
    elif not same:
        verdict = "differs"
    else:
        verdict = "identical"
    report["verdict"] = verdict

    if args.timing:
        tb, tc = [], []
        for _ in range(args.timing):
            for name, argv, bucket in (("base", base_argv, tb), ("cand", cand_argv, tc)):
                r = run_once(argv, stdin_text, args.timeout, args.cwd)
                if r["exit"] != 0 and not args.allow_fail:
                    report["problems"].append(f"{name} exited {r['exit']} during timing")
                    report["verdict"] = "failed"
                bucket.append(r["ms"])
        report["timing"] = {"interleaved": True, "base": summarize_timing(tb), "cand": summarize_timing(tc),
                            "median_change_pct": round((statistics.median(tc) - statistics.median(tb))
                                                       * 100 / statistics.median(tb), 1)}
    return report


def render(report: dict) -> str:
    lines = [f"verdict: {report['verdict']}"]
    for name in ("base", "cand"):
        s = report[name]
        norm = ""
        if report["normalize"]:
            norm = " · normalized " + ", ".join(f"{c}×/{p}/" for p, c in zip(report["normalize"], s["normalized_counts"]))
        lines.append(f"{name}: exit {s['exit']} · {s['bytes']} bytes{norm}")
    for p in report["problems"]:
        lines.append(f"problem: {p}")
    for d in report.get("diff_head", []):
        lines.append(f"  {d}")
    t = report.get("timing")
    if t:
        lines.append(f"timing (interleaved, n={t['base']['n']}): base median {t['base']['median_ms']} ms "
                     f"[{t['base']['min_ms']}–{t['base']['max_ms']}] · cand median {t['cand']['median_ms']} ms "
                     f"[{t['cand']['min_ms']}–{t['cand']['max_ms']}] · {t['median_change_pct']:+}%")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Differential regression check: same input, baseline vs candidate.")
    ap.add_argument("--base", required=True, help="baseline command (one string, split with shlex, no shell)")
    ap.add_argument("--cand", required=True, help="candidate command")
    ap.add_argument("--stdin", help="file fed to both commands on stdin; {nonce} is replaced per run")
    ap.add_argument("--normalize", action="append", default=[], metavar="REGEX",
                    help="replace matches with <N> on both sides before comparing (counted in the report)")
    ap.add_argument("--determinism", action="store_true", help="run each side twice and fail if a side varies")
    ap.add_argument("--timing", type=int, default=0, metavar="N", help="time N interleaved runs per side")
    ap.add_argument("--allow-empty", action="store_true", help="do not fail on empty output")
    ap.add_argument("--allow-fail", action="store_true", help="do not fail on non-zero exit codes")
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds per invocation (default 120)")
    ap.add_argument("--cwd", help="working directory for both commands")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    if args.timing < 0:
        print("error: --timing must be >= 0", file=sys.stderr)
        return 2
    try:
        report = check(args)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return 0 if report["verdict"] == "identical" else 1


if __name__ == "__main__":
    sys.exit(main())
