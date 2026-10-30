#!/usr/bin/env python3
"""Turn an fsmap snapshot into a compact AOK atlas record (workspace atlas, W1-W2).

fsmap (https://github.com/d-j-lee/filesystemMap) maps a directory into one JSON
snapshot (`fsmap scan`, schema `fsmap/1`): sizes, code, git history and, per git
work tree, its hygiene (uncommitted files, commits ahead, stashes, branches with no
upstream). This script keeps only what the control tower needs and writes one JSON
object for the ledger document `atlas/<session_id>`:

  - totals: files, bytes, disk, code lines, LLM tokens
  - repos: path, head, branch, upstream, ahead, behind, dirty, untracked, stashes,
    unpushed branches, shallow
  - local_only: sums over repos, and `volatile` = true when any work exists only in
    this container (uncommitted, untracked, stashed, or on a branch that is not pushed)
  - findings: the top findings by severity (severity, rule id and title only)

Usage:
  fsmap scan WORKSPACE -o snap.json -q --no-dupes
  python3 -I scripts/atlas_record.py snap.json --session-id session_<id> [--label TEXT] \
      [--report-url URL] [-o record.json]

Reads the snapshot from stdin when the path is '-'. Paths stay relative to the scanned
root, which is reduced to its last component. Stdlib only; safe to run with `python3 -I`.
Exit codes: 0 ok, 2 usage or input error.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

SCHEMA = "aok-atlas/1"
MAX_REPOS = 40
MAX_FINDINGS = 10
MAX_BRANCHES = 10
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}  # fsmap's severities


def _int(value) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _severity(finding: dict) -> str:
    sev = str(finding.get("severity") or "info")
    return sev if sev in SEVERITY_ORDER else "info"


def repo_entry(repo: dict) -> dict:
    hygiene = repo.get("hygiene") or {}
    branches = [b for b in (hygiene.get("unpushedBranches") or []) if isinstance(b, str)]
    return {
        "path": str(repo.get("path") or "."),
        "head": repo.get("head") or None,
        "branch": hygiene.get("branch") or None,
        "upstream": hygiene.get("upstream") or None,
        "ahead": _int(hygiene.get("ahead")),
        "behind": _int(hygiene.get("behind")),
        "dirty": _int(hygiene.get("dirty")),
        "untracked": _int(repo.get("untracked")),
        "stashes": _int(hygiene.get("stashes")),
        "unpushed_branches": branches[:MAX_BRANCHES],
        "shallow": bool(repo.get("shallow")),
    }


def has_local_work(r: dict) -> bool:
    return bool(r["dirty"] or r["untracked"] or r["stashes"] or r["ahead"] or r["unpushed_branches"])


def build_record(snapshot: dict, session_id: str, label: str | None = None, report_url: str | None = None) -> dict:
    if snapshot.get("schema") != "fsmap/1":
        raise ValueError(f"unsupported snapshot schema: {snapshot.get('schema')!r} (expected 'fsmap/1')")
    totals = snapshot.get("totals") or {}
    repos = [repo_entry(r) for r in (snapshot.get("repos") or []) if isinstance(r, dict)]
    # Repositories with local-only work first, then by path, so a truncated list keeps what matters.
    repos.sort(key=lambda r: (not has_local_work(r), r["path"]))
    findings = [f for f in (snapshot.get("findings") or []) if isinstance(f, dict) and f.get("title")]
    findings.sort(key=lambda f: SEVERITY_ORDER[_severity(f)])
    local = [r for r in repos if has_local_work(r)]
    generated = snapshot.get("generatedAt")
    root = str(snapshot.get("root") or "").rstrip("/").rsplit("/", 1)[-1] or "/"
    return {
        "schema": SCHEMA,
        "session_id": session_id,
        "label": label,
        "generated_at": (datetime.fromtimestamp(generated, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                         if isinstance(generated, (int, float)) else None),
        "tool": snapshot.get("tool"),
        "root": root,
        "totals": {k: _int(totals.get(k)) for k in ("files", "bytes", "disk", "code", "tokens")},
        "repos": repos[:MAX_REPOS],
        "repos_total": len(repos),
        "local_only": {
            "volatile": bool(local),
            "repos": len(local),
            "dirty": sum(r["dirty"] for r in repos),
            "untracked": sum(r["untracked"] for r in repos),
            "stashes": sum(r["stashes"] for r in repos),
            "ahead": sum(r["ahead"] for r in repos),
            "unpushed_branches": sum(len(r["unpushed_branches"]) for r in repos),
        },
        "findings": [{"severity": _severity(f), "rule": str(f["rule"]) if f.get("rule") else None, "title": str(f["title"])[:200]}
                     for f in findings[:MAX_FINDINGS]],
        "findings_total": len(findings),
        "report_url": report_url,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas_record.py", description="fsmap snapshot -> AOK atlas record (JSON)")
    ap.add_argument("snapshot", help="fsmap scan output (fsmap/1 JSON), or '-' for stdin")
    ap.add_argument("--session-id", required=True, help="session the workspace belongs to (session_<id>)")
    ap.add_argument("--label", help="short human label, e.g. the session title")
    ap.add_argument("--report-url", help="link to the session's fsmap HTML report artifact")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    args = ap.parse_args(argv)
    try:
        text = sys.stdin.read() if args.snapshot == "-" else open(args.snapshot, encoding="utf-8").read()
        record = build_record(json.loads(text), args.session_id, args.label, args.report_url)
    except (OSError, ValueError) as e:  # json.JSONDecodeError is a ValueError
        print(f"atlas_record: error: {e}", file=sys.stderr)
        return 2
    out = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
