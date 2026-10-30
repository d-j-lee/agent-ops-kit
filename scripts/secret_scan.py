#!/usr/bin/env python3
"""Secret and public-repo hygiene scanner (AOK v1).

Scans files and directories (recursively, skipping .git and binary files) for
credentials and identifiers that must not land in a public repository.

Rules (severity):
  github_token      ghp_/gho_/ghu_/ghs_/ghr_ + 36+ chars          (error)
  github_pat        github_pat_ + 22+ chars                         (error)
  anthropic_key     sk-ant-...                                      (error)
  aws_access_key    AKIA/ASIA + 16 upper-case letters or digits     (error)
  private_key       -----BEGIN ... PRIVATE KEY-----                 (error)
  slack_token       xoxb-/xoxa-/xoxp-/xoxr-/xoxs-...                (error)
  google_api_key    AIza + 35 chars                                 (error)
  jwt               eyJ....eyJ....<signature>                       (error)
  email             addresses outside reserved example domains      (warn)
  claude_session_id session_0 + 20+ letters or digits               (warn)
  claude_cse_id     cse_ + 16+ letters or digits                    (warn)
  claude_trigger_id trig_ + 16+ letters or digits                   (warn)
  claude_artifact_url claude.ai/artifact/<id> or .../code/artifact/  (warn)

Warn rules are public-repo hygiene: real session/trigger ids, artifact links
and emails should be replaced by placeholders such as `session_<id>`,
`trig_<id>`, `claude.ai/artifact/<id>` or `user@example.com`.

Obvious placeholders are ignored: a token body with fewer than 4 distinct
characters (e.g. ghp_xxxx...) or containing EXAMPLE / placeholder / redacted.
Email addresses at example.com/org/net, *.example, *.test, *.invalid,
*.localhost, noreply addresses and `git@host` remotes are ignored. A line
containing the marker `secret-scan: allow` is skipped entirely.

Output: one line per finding, `path:line: rule (severity)`; the secret itself
is never printed. With --json, a JSON object with a redacted preview instead.

Exit codes:
  0  no error-severity finding (warnings may exist)
  1  at least one error-severity finding
  2  usage error (path not found)

Stdlib only; safe to run with `python3 -I`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ERROR, WARN = "error", "warn"
ALLOW_MARKER = "secret-scan: allow"
SKIP_DIRS = {".git"}
BINARY_SNIFF = 8192

# (rule, severity, compiled regex, length of the fixed prefix to strip before
#  the placeholder check)
RULES: list[tuple[str, str, re.Pattern, int]] = [
    ("github_token", ERROR, re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,255}\b"), 4),
    ("github_pat", ERROR, re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"), 11),
    ("anthropic_key", ERROR, re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), 7),
    ("aws_access_key", ERROR, re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 4),
    ("private_key", ERROR, re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"), -1),
    ("slack_token", ERROR, re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), 5),
    ("google_api_key", ERROR, re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])"), 4),
    ("jwt", ERROR, re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), 0),
    ("email", WARN, re.compile(
        r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}\b"), -1),
    ("claude_session_id", WARN, re.compile(r"\bsession_0[0-9A-Za-z]{20,}\b"), 9),
    ("claude_cse_id", WARN, re.compile(r"\bcse_[0-9A-Za-z]{16,}\b"), 4),
    ("claude_trigger_id", WARN, re.compile(r"\btrig_[0-9A-Za-z]{16,}\b"), 5),
    ("claude_environment_id", WARN, re.compile(r"\benv_[0-9A-Za-z]{20,}\b"), 4),
    ("claude_container_id", WARN, re.compile(r"\bcontainer_0[0-9A-Za-z]{16,}\b"), 10),
    ("claude_artifact_url", WARN, re.compile(
        r"\bclaude\.ai/(?:code/)?artifact/[0-9A-Za-z][0-9A-Za-z\-]{7,}"), 0),
]

PLACEHOLDER_WORDS = ("example", "placeholder", "redacted", "dummy")
EMAIL_OK_DOMAINS = {"example.com", "example.org", "example.net", "localhost"}
EMAIL_OK_SUFFIXES = (".example", ".test", ".invalid", ".localhost", ".noreply.github.com")
EMAIL_FILE_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "css", "js", "ts", "json"}


def is_placeholder(value: str, prefix_len: int) -> bool:
    if prefix_len < 0:
        return False
    body = value[prefix_len:]
    low = body.lower()
    if any(w in low for w in PLACEHOLDER_WORDS):
        return True
    return len(set(body.replace("-", "").replace("_", "").replace(".", ""))) < 4


def email_allowed(addr: str) -> bool:
    local, _, domain = addr.rpartition("@")
    local, domain = local.lower(), domain.lower()
    if domain in EMAIL_OK_DOMAINS or domain.endswith(EMAIL_OK_SUFFIXES):
        return True
    if domain.rsplit(".", 1)[-1] in EMAIL_FILE_TLDS:
        return True
    if local == "git" or local.startswith(("noreply", "no-reply")):
        return True
    return False


def redact(value: str) -> str:
    keep = 4 if len(value) > 8 else 1
    return f"{value[:keep]}…({len(value)} chars)"


def iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_file() and not p.is_symlink():
                yield p


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:BINARY_SNIFF]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_text(text: str):
    """Yield (line, col, rule, severity, value) for each finding in text."""
    for lineno, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for rule, sev, rx, prefix_len in RULES:
            for m in rx.finditer(line):
                value = m.group(0)
                if rule == "email":
                    if email_allowed(value):
                        continue
                elif is_placeholder(value, prefix_len):
                    continue
                yield lineno, m.start() + 1, rule, sev, value


def scan_paths(paths: list[Path]):
    """Return (findings, files_scanned). Findings are dicts sorted by path/line."""
    findings = []
    scanned = 0
    for root in paths:
        for p in iter_files(root):
            text = read_text(p)
            if text is None:
                continue
            scanned += 1
            for line, col, rule, sev, value in scan_text(text):
                findings.append({"path": str(p), "line": line, "col": col, "rule": rule,
                                 "severity": sev, "preview": redact(value)})
    findings.sort(key=lambda f: (f["path"], f["line"], f["col"]))
    return findings, scanned


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="secret_scan.py",
        description="Scan files/directories for secrets and public-repo hygiene issues "
        "(skips .git and binary files).",
        epilog="Exit codes: 0 no error finding, 1 error finding(s) (with --strict: any finding), 2 usage error. "
        f"Lines containing '{ALLOW_MARKER}' are skipped.",
    )
    ap.add_argument("paths", nargs="+", type=Path, metavar="PATH", help="file or directory to scan")
    ap.add_argument("--json", action="store_true", help="print findings as one JSON object")
    ap.add_argument("--strict", action="store_true",
                    help="treat warn-level hygiene findings (emails, session/trigger/environment IDs, "
                    "artifact URLs) as errors; use before committing to a public repo")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    missing = [str(p) for p in args.paths if not p.exists()]
    if missing:
        print(f"secret_scan: error: path not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    findings, scanned = scan_paths(args.paths)
    if args.strict:
        for f in findings:
            f["severity"] = ERROR
    n_err = sum(1 for f in findings if f["severity"] == ERROR)
    n_warn = len(findings) - n_err
    if args.json:
        print(json.dumps({"files_scanned": scanned, "errors": n_err, "warnings": n_warn,
                          "findings": findings}, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f"{f['path']}:{f['line']}: {f['rule']} ({f['severity']})")
        print(f"secret_scan: {scanned} file(s), {n_err} error(s), {n_warn} warning(s)",
              file=sys.stderr)
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
