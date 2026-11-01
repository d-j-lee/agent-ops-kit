#!/usr/bin/env python3
"""Turn an fsmap snapshot into a compact AOK atlas record (workspace atlas, W1-W2).

fsmap (https://github.com/d-j-lee/filesystemMap) maps a directory into one JSON
snapshot (`fsmap scan`, schema `fsmap/1`): sizes, code, git history and, per git
work tree, its hygiene (uncommitted files, commits ahead, stashes, branches with no
upstream). This script keeps only what the control tower needs and writes one JSON
object for the ledger document `atlas/<session_id>`:

  - totals: files, bytes, disk, code lines, LLM tokens
  - repos: path, head, branch, upstream, ahead, behind, dirty, untracked, stashes,
    unpushed branches, local commits, shallow
  - local_only: sums over repos, and `volatile` = true when any work exists only in
    this container (uncommitted, untracked, stashed, committed but on no remote, or on
    a branch that is not pushed)
  - coverage: whether the measurement is complete enough to call the workspace clean,
    and the gaps when it is not
  - findings: the top findings by severity (severity, rule id and title only)

fsmap's per-branch hygiene misses commits made on a detached HEAD and on a branch whose
upstream was deleted. When the scanned root exists on this machine, the script therefore
also asks git for the commits reachable from HEAD or a local branch but from no
remote-tracking ref (`rev-list --count HEAD --branches --not --remotes`). Without that
check (`--no-git-check`, or a snapshot from another machine) the record is marked
incomplete, so the console never treats it as proof that nothing is at risk.

Usage:
  fsmap scan WORKSPACE -o snap.json -q --no-dupes
  python3 -I scripts/atlas_record.py snap.json --session-id session_<id> [--label TEXT] \
      [--report-url URL] [--no-git-check] [-o record.json]

Reads the snapshot from stdin when the path is '-'. Paths stay relative to the scanned
root, which is reduced to its last component. Stdlib only; safe to run with `python3 -I`.
Exit codes: 0 ok, 2 usage or input error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

SCHEMA = "aok-atlas/1"
MAX_REPOS = 40
MAX_FINDINGS = 10
MAX_BRANCHES = 10
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}  # fsmap's severities
HYGIENE_KEYS = ("dirty", "ahead", "behind", "stashes", "unpushedBranches")
SESSION_ID = re.compile(r"^session_[A-Za-z0-9]+$")


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _severity(finding: dict) -> str:
    sev = str(finding.get("severity") or "info")
    return sev if sev in SEVERITY_ORDER else "info"


def local_commits(root: str, path: str) -> int | None:
    """Commits reachable from HEAD or a local branch but from no remote-tracking ref; None when git cannot say."""
    repo = root if path in ("", ".") else os.path.join(root, path)
    if not os.path.isdir(repo):
        return None
    # Read-only query; hooks and fsmonitor are disabled so a repository's config cannot run anything.
    cmd = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", repo,
           "rev-list", "--count", "HEAD", "--branches", "--not", "--remotes"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def repo_entry(repo: dict, git_root: str | None) -> tuple[dict, list[tuple[str, str]], int]:
    """One work tree's record, its coverage gaps as (code, path) pairs, and its unpushed-branch count before the cap."""
    path = str(repo.get("path") or ".")
    head = repo.get("head") if isinstance(repo.get("head"), str) else None
    raw = repo.get("hygiene")
    hygiene = raw if isinstance(raw, dict) else {}
    gaps: list[tuple[str, str]] = []
    if head and (not isinstance(raw, dict) or repo.get("error")):
        gaps.append(("repo_error", path))
    elif isinstance(raw, dict) and any(k not in raw for k in HYGIENE_KEYS):
        gaps.append(("hygiene_fields_missing", path))
    branches = [b for b in (hygiene.get("unpushedBranches") or []) if isinstance(b, str)]
    commits = local_commits(git_root, path) if (git_root and head) else None
    if head and commits is None:
        gaps.append(("local_commits_unchecked", path))
    entry = {
        "path": path,
        "head": head,
        "branch": hygiene.get("branch") or None,
        "upstream": hygiene.get("upstream") or None,
        "ahead": _int(hygiene.get("ahead")),
        "behind": _int(hygiene.get("behind")),
        "dirty": _int(hygiene.get("dirty")),
        "untracked": _int(repo.get("untracked")),
        "stashes": _int(hygiene.get("stashes")),
        "unpushed_branches": branches[:MAX_BRANCHES],
        "local_commits": commits,
        "shallow": bool(repo.get("shallow")),
    }
    return entry, gaps, len(branches)


def has_local_work(r: dict) -> bool:
    # A checked commit count replaces `ahead`: commits ahead of the upstream may already be on another remote ref.
    commits = r["local_commits"] if r["local_commits"] is not None else r["ahead"]
    return bool(r["dirty"] or r["untracked"] or r["stashes"] or commits or r["unpushed_branches"])


def _timestamp(value) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def build_record(snapshot: dict, session_id: str, label: str | None = None, report_url: str | None = None,
                 git_root: str | None = None) -> dict:
    """git_root: where the scanned root is on this machine, for the local-commit check; None skips it."""
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "fsmap/1":
        found = snapshot.get("schema") if isinstance(snapshot, dict) else type(snapshot).__name__
        raise ValueError(f"unsupported snapshot schema: {found!r} (expected 'fsmap/1')")
    if not SESSION_ID.match(session_id or ""):
        raise ValueError(f"session id must look like session_<id>, got {session_id!r}")
    totals = snapshot.get("totals") if isinstance(snapshot.get("totals"), dict) else {}
    built = [repo_entry(r, git_root) for r in (snapshot.get("repos") or []) if isinstance(r, dict)]
    repos = [e for e, _, _ in built]
    gaps: dict[str, list[str]] = {}
    for _, repo_gaps, _ in built:
        for code, path in repo_gaps:
            gaps.setdefault(code, []).append(path)
    not_analysed = _int(snapshot.get("reposNotAnalysed"))
    gap_list = [{"code": code, "detail": ", ".join(paths[:10]) + (f" (+{len(paths) - 10})" if len(paths) > 10 else "")}
                for code, paths in sorted(gaps.items())]
    if not_analysed:
        gap_list.append({"code": "repos_not_analysed", "detail": str(not_analysed)})
    if not repos:
        gap_list.append({"code": "no_repos", "detail": None})
    # Repositories with local-only work first, then by path, so a truncated list keeps what matters.
    repos.sort(key=lambda r: (not has_local_work(r), r["path"]))
    findings = [f for f in (snapshot.get("findings") or []) if isinstance(f, dict) and f.get("title")]
    findings.sort(key=lambda f: SEVERITY_ORDER[_severity(f)])
    local = [r for r in repos if has_local_work(r)]
    root = str(snapshot.get("root") or "").rstrip("/").rsplit("/", 1)[-1] or "/"
    return {
        "schema": SCHEMA,
        "session_id": session_id,
        "label": label,
        "generated_at": _timestamp(snapshot.get("generatedAt")),
        "tool": snapshot.get("tool") if isinstance(snapshot.get("tool"), str) else None,
        "root": root,
        "totals": {k: _int(totals.get(k)) for k in ("files", "bytes", "disk", "code", "tokens")},
        "repos": repos[:MAX_REPOS],
        "repos_total": len(repos) + not_analysed,
        "local_only": {
            "volatile": bool(local),
            "repos": len(local),
            "dirty": sum(r["dirty"] for r in repos),
            "untracked": sum(r["untracked"] for r in repos),
            "stashes": sum(r["stashes"] for r in repos),
            "ahead": sum(r["ahead"] for r in repos),
            "unpushed_branches": sum(n for _, _, n in built),  # counted before the per-repo cap
            "local_commits": sum(r["local_commits"] or 0 for r in repos),
        },
        "coverage": {"complete": not gap_list, "gaps": gap_list},
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
    ap.add_argument("--no-git-check", action="store_true",
                    help="skip the local-commit check (the record is then marked incomplete)")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    args = ap.parse_args(argv)
    try:
        text = sys.stdin.read() if args.snapshot == "-" else open(args.snapshot, encoding="utf-8").read()
        snapshot = json.loads(text)
        root = snapshot.get("root") if isinstance(snapshot, dict) else None
        git_root = None if args.no_git_check or not isinstance(root, str) or not os.path.isdir(root) else root
        record = build_record(snapshot, args.session_id, args.label, args.report_url, git_root)
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
