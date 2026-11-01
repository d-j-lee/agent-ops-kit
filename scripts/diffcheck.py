#!/usr/bin/env python3
"""Differential regression check (AOK feature ⑦, docs/patterns/regression.md).

Feeds the same input to a baseline command and a candidate command and
compares what they did. It is built to refuse the ways a "same output" check
fools itself:

  failed           a side exited non-zero (unless --allow-fail) or timed out
                   (always). A tool error that keeps a side from running at all
                   is not a verdict: it exits 2.
  empty            a side printed nothing or only whitespace, or nothing but
                   <N> placeholders is left after normalization (e.g. a reused
                   session id made a hook stay silent, or a --normalize regex
                   ate everything). Fails unless --allow-empty.
  nondeterministic a side did something different on a second run
                   (--determinism). Pin the clock, seed or session state, or
                   --normalize it.
  differs          the compared parts differ, or normalization replaced a
                   different number of matches on each side; a unified diff
                   head is printed.

What is compared is stated in the report ("compared"): always the exit code
and stdout, plus stderr with --stderr. Output is compared byte for byte (no
newline translation, no decoding loss): b"\\xff" and b"\\xfe" differ, and so do
LF and CRLF. --allow-fail only stops a non-zero exit from failing by itself;
exit codes are still compared, so base 0 vs cand 3 is "differs".

Normalization is explicit and counted: every --normalize REGEX replaces its
matches with <N> and the report says how many replacements each side got, so
a comparison can't silently become "identical after deleting everything".

{nonce} in a command argument or in the --stdin file is replaced by a fresh
random token for every single invocation, so tools that remember state per
key (sessions, caches) see a new key each time. With --same-nonce the two
sides of one comparison share a token (still fresh for every pair), for tools
that branch on the key itself, e.g. a holdout group chosen by hashing it. The
--determinism rerun is a new pair with a new key, so such a branch can still
differ between the two runs (the report says so).

A tool that reports "nothing" as a fixed value ({} or "ok") can be checked for
emptiness by normalizing that value away: what is left is only <N>, so the
empty-output check fails it.

--timing N runs the two sides alternately N times (base, cand, base, ...) so
that drift over time (other load, CPU clock) lands on both sides, and reports
min/median/max wall time in ms and the median change in percent. A failure or
timeout during timing fails the check; timing outputs are not compared.

Commands are given as one string each and split with shlex (no shell). Use
"bash -c '...'" explicitly when shell features are needed.

Usage:
  diffcheck.py --base CMD --cand CMD [--stdin FILE] [--normalize REGEX ...]
               [--stderr] [--determinism] [--timing N] [--same-nonce]
               [--allow-empty] [--allow-fail] [--timeout SEC] [--cwd DIR] [--json]

Exit codes:
  0  identical (in the compared parts, after the listed normalizations)
  1  differs, empty, failed or nondeterministic
  2  usage or tool error: the check could not run (bad option, command not
     found or not executable, missing --cwd or --stdin file)

Stdlib only; safe to run with `python3 -I`.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
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


def new_nonce() -> str:
    return secrets.token_hex(8)


def run_once(argv: list[str], stdin_data: bytes | None, timeout: float, cwd: str | None,
             nonce: str | None = None) -> dict:
    """Run one invocation with a nonce (fresh unless given); return output, exit code and wall time."""
    nonce = nonce or new_nonce()
    real_argv = [a.replace(NONCE, nonce) for a in argv]
    data = stdin_data.replace(NONCE.encode(), nonce.encode()) if stdin_data is not None else None
    start = time.perf_counter()
    try:
        proc = subprocess.run(real_argv, input=data, capture_output=True, timeout=timeout, cwd=cwd,
                              stdin=None if data is not None else subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise UsageError(f"command not found: {real_argv[0]}") from e
    except OSError as e:  # not executable, bad interpreter line, ...
        raise UsageError(f"cannot run {real_argv[0]}: {e.strerror or e}") from e
    except subprocess.TimeoutExpired:
        return {"out": b"", "err": b"", "exit": None, "timeout": True,
                "ms": (time.perf_counter() - start) * 1000}
    return {"out": proc.stdout, "err": proc.stderr, "exit": proc.returncode, "timeout": False,
            "ms": (time.perf_counter() - start) * 1000}


def normalize(data: bytes, patterns: list[re.Pattern]) -> tuple[str, list[int]]:
    # surrogateescape keeps every byte distinct (b"\xff" != b"\xfe") and lossless
    text = data.decode("utf-8", errors="surrogateescape")
    counts = []
    for p in patterns:
        text, n = p.subn("<N>", text)
        counts.append(n)
    return text, counts


def show(text: str) -> str:
    """Printable form: undecodable bytes as \\xNN, carriage returns and other controls escaped."""
    s = text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="backslashreplace")
    return "".join(c if c in "\t\n" or c.isprintable() else repr(c)[1:-1] for c in s)


def summarize_timing(samples: list[float]) -> dict:
    return {"n": len(samples), "min_ms": round(min(samples), 1),
            "median_ms": round(statistics.median(samples), 1), "max_ms": round(max(samples), 1)}


def observe(r: dict, patterns: list[re.Pattern], with_stderr: bool) -> dict:
    out, counts = normalize(r["out"], patterns)
    obs = {"exit": r["exit"], "timeout": r["timeout"], "out": out, "counts": counts}
    if with_stderr:
        obs["err"], obs["err_counts"] = normalize(r["err"], patterns)
    return obs


def same_behaviour(a: dict, b: dict) -> bool:
    return (a["exit"], a["timeout"], a["out"], a.get("err")) == (b["exit"], b["timeout"], b["out"], b.get("err"))


def check(args) -> dict:
    base_argv, cand_argv = build_argv(args.base), build_argv(args.cand)
    if args.cwd and not Path(args.cwd).is_dir():
        raise UsageError(f"--cwd is not a directory: {args.cwd}")
    stdin_data = None
    if args.stdin:
        p = Path(args.stdin)
        if not p.is_file():
            raise UsageError(f"stdin file not found: {args.stdin}")
        stdin_data = p.read_bytes()
    try:
        patterns = [re.compile(r) for r in args.normalize]
    except re.error as e:
        raise UsageError(f"bad --normalize regex: {e}") from e

    def pair_nonces() -> tuple[str | None, str | None]:
        if args.same_nonce:
            n = new_nonce()
            return n, n
        return None, None

    compared = ["exit", "stdout"] + (["stderr"] if args.stderr else [])
    report: dict = {"verdict": None, "compared": compared, "problems": [], "normalize": args.normalize}
    if args.same_nonce and args.determinism:
        report["notes"] = ["--same-nonce with --determinism: the second run gets a new key, so a tool that branches "
                           "on the key itself can look nondeterministic; normalize that branch or check determinism "
                           "separately with a fixed key"]
    sides: dict[str, dict] = {}
    raw: dict[str, dict] = {}
    nb, nc = pair_nonces()
    for name, argv, nonce in (("base", base_argv, nb), ("cand", cand_argv, nc)):
        r = run_once(argv, stdin_data, args.timeout, args.cwd, nonce)
        raw[name] = r
        sides[name] = observe(r, patterns, args.stderr)
        report[name] = {"exit": r["exit"], "timeout": r["timeout"], "bytes": len(r["out"]),
                        "normalized_counts": sides[name]["counts"],
                        "stderr_bytes": len(r["err"]), "stderr_head": show(r["err"].decode(
                            "utf-8", errors="surrogateescape")[:300])}
        if args.stderr:
            report[name]["stderr_normalized_counts"] = sides[name]["err_counts"]

    failed = empty = nondet = False
    for name in ("base", "cand"):
        r = raw[name]
        if r["timeout"]:
            report["problems"].append(f"{name} timed out after {args.timeout:g}s")
            failed = True
        elif r["exit"] != 0 and not args.allow_fail:
            report["problems"].append(f"{name} exited {r['exit']}")
            failed = True
    if not args.allow_empty:
        for name in ("base", "cand"):
            if raw[name]["timeout"]:
                continue
            if not raw[name]["out"].strip():
                report["problems"].append(f"empty output: {name} printed nothing or only whitespace")
                empty = True
            elif not sides[name]["out"].replace("<N>", "").strip():
                report["problems"].append(f"empty output: {name} has nothing left after normalization (only <N>)")
                empty = True
            if args.stderr and raw[name]["err"].strip() and not sides[name]["err"].replace("<N>", "").strip():
                report["problems"].append(f"empty output: {name} stderr has nothing left after normalization (only <N>)")
                empty = True

    if args.determinism:
        nb, nc = pair_nonces()
        for name, argv, nonce in (("base", base_argv, nb), ("cand", cand_argv, nc)):
            again = observe(run_once(argv, stdin_data, args.timeout, args.cwd, nonce), patterns, args.stderr)
            if not same_behaviour(again, sides[name]):
                what = [k for k, a, b in (("exit code", again["exit"], sides[name]["exit"]),
                                          ("timeout", again["timeout"], sides[name]["timeout"]),
                                          ("stdout", again["out"], sides[name]["out"]),
                                          ("stderr", again.get("err"), sides[name].get("err"))) if a != b]
                report["problems"].append(f"{name} is nondeterministic (second run differs in {', '.join(what)})")
                nondet = True

    b, c = sides["base"], sides["cand"]
    diff_lines: list[str] = []
    if b["exit"] != c["exit"] or b["timeout"] != c["timeout"]:
        diff_lines.append(f"exit code: base {b['exit']} · cand {c['exit']}")
    if b["counts"] != c["counts"] or b.get("err_counts") != c.get("err_counts"):
        # the same text after normalization but a different number of replacements is a different output
        diff_lines.append(f"normalization counts: base {b['counts']} · cand {c['counts']}")
    for label, key in (("stdout", "out"), ("stderr", "err")):
        if key in b and b[key] != c[key]:
            diff_lines += [show(d) for d in difflib.unified_diff(
                b[key].split("\n"), c[key].split("\n"), f"base {label}", f"cand {label}", lineterm="", n=1)]
    same = not diff_lines
    if not same:
        report["diff_head"] = diff_lines[:DIFF_HEAD_LINES]

    if failed:
        verdict = "failed"
    elif empty:
        verdict = "empty"
    elif nondet:
        verdict = "nondeterministic"
    elif not same:
        verdict = "differs"
    else:
        verdict = "identical"
    report["verdict"] = verdict

    if args.timing:
        tb, tc = [], []
        for _ in range(args.timing):
            nb, nc = pair_nonces()
            for name, argv, bucket, nonce in (("base", base_argv, tb, nb), ("cand", cand_argv, tc, nc)):
                r = run_once(argv, stdin_data, args.timeout, args.cwd, nonce)
                if r["timeout"] or (r["exit"] != 0 and not args.allow_fail):
                    report["problems"].append(f"{name} {'timed out' if r['timeout'] else 'exited ' + str(r['exit'])}"
                                              " during timing")
                    report["verdict"] = "failed"
                bucket.append(r["ms"])
        mb, mc = statistics.median(tb), statistics.median(tc)
        report["timing"] = {"interleaved": True, "base": summarize_timing(tb), "cand": summarize_timing(tc),
                            "median_change_pct": round((mc - mb) * 100 / mb, 1) if mb else None}
    return report


def render(report: dict) -> str:
    lines = [f"verdict: {report['verdict']}", f"compared: {', '.join(report['compared'])}"]
    for name in ("base", "cand"):
        s = report[name]
        norm = ""
        if report["normalize"]:
            norm = " · normalized " + ", ".join(f"{c}×/{p}/" for p, c in zip(report["normalize"], s["normalized_counts"]))
        status = "timeout" if s["timeout"] else f"exit {s['exit']}"
        lines.append(f"{name}: {status} · {s['bytes']} bytes{norm}")
    for p in report["problems"]:
        lines.append(f"problem: {p}")
    for n in report.get("notes", []):
        lines.append(f"note: {n}")
    for d in report.get("diff_head", []):
        lines.append(f"  {d}")
    t = report.get("timing")
    if t:
        change = "n/a" if t["median_change_pct"] is None else f"{t['median_change_pct']:+}%"
        lines.append(f"timing (interleaved, n={t['base']['n']}): base median {t['base']['median_ms']} ms "
                     f"[{t['base']['min_ms']}–{t['base']['max_ms']}] · cand median {t['cand']['median_ms']} ms "
                     f"[{t['cand']['min_ms']}–{t['cand']['max_ms']}] · {change}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Differential regression check: same input, baseline vs candidate.")
    ap.add_argument("--base", required=True, help="baseline command (one string, split with shlex, no shell)")
    ap.add_argument("--cand", required=True, help="candidate command")
    ap.add_argument("--stdin", help="file fed to both commands on stdin (bytes); {nonce} is replaced per run")
    ap.add_argument("--normalize", action="append", default=[], metavar="REGEX",
                    help="replace matches with <N> on both sides before comparing (counted in the report)")
    ap.add_argument("--stderr", action="store_true", help="compare stderr too (default: exit code and stdout)")
    ap.add_argument("--determinism", action="store_true", help="run each side twice and fail if a side varies")
    ap.add_argument("--timing", type=int, default=0, metavar="N", help="time N interleaved runs per side")
    ap.add_argument("--same-nonce", action="store_true",
                    help="give base and cand of one comparison the same {nonce} (fresh for every pair)")
    ap.add_argument("--allow-empty", action="store_true", help="do not fail on empty output")
    ap.add_argument("--allow-fail", action="store_true",
                    help="a non-zero exit is not a failure by itself (exit codes are still compared)")
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds per invocation (default 120)")
    ap.add_argument("--cwd", help="working directory for both commands")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    if args.timing < 0:
        print("error: --timing must be >= 0", file=sys.stderr)
        return 2
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("error: --timeout must be a finite number > 0", file=sys.stderr)
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
