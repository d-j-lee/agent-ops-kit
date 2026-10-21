#!/usr/bin/env python3
"""Blind-reproduction packet sanitizer (AOK v1, feature ①).

The author of a claim splits material into two directories:

  packet/    question, raw data, reproduction steps  -> given to the verifier
  withheld/  conclusions, claim ledger, canary       -> kept from the verifier

This tool checks that nothing from withheld/ leaked into packet/, and that no
canary token from withheld/ appears in verifier outputs.

Scan mode (--packet and --withheld):
  1. With --canary, write a fresh random token file withheld/CANARY-<hex>.txt
     and print the token on its own line.
  2. Collect "withheld terms": every canary token in withheld/, every
     distinctive line of the withheld text files (at least --min-len chars
     after stripping Markdown markers, at least 4 letters or digits,
     de-duplicated), and every line of --terms FILE (blank lines and lines
     starting with '#' are ignored).
  3. Scan every text file under packet/ for any term. Matching is
     case-insensitive with whitespace runs (line breaks included) collapsed
     to one space, so a term wrapped over two lines still matches.
  4. Print each hit as `path:line: term` (term shortened to 120 chars).

Output check mode (--check-output FILE_OR_DIR... with --withheld):
  Print `path:line: CANARY-...` for every withheld canary token found in the
  given verifier outputs. A hit means the run saw withheld/ and is void.

Both modes may be combined in one call.

Exit codes:
  0  clean (no hit)
  1  at least one hit (leak in packet/, or canary in outputs)
  2  usage error (bad arguments, missing directory, no canary to check)

Stdlib only; safe to run with `python3 -I`.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

CANARY_RE = re.compile(r"CANARY-[0-9a-f]{16,}", re.IGNORECASE)
ALNUM_RE = re.compile(r"[^\W_]", re.UNICODE)
# Leading Markdown / list / table markers stripped before a line becomes a term.
LEAD_RE = re.compile(r"^(?:[#>*+\-|]+\s*|\d+[.)]\s+)+")
# Trailing table pipes and sentence punctuation are stripped too.
TRAIL_RE = re.compile(r"[\s|.,;:!?]+$")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
BINARY_SNIFF = 8192
TERM_SHOW = 120


class UsageError(Exception):
    """Raised for conditions that map to exit code 2."""


def iter_files(root: Path):
    """Yield regular files under root (or root itself), skipping VCS dirs."""
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
    """Return file text, or None for binary files (NUL byte in the head)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:BINARY_SNIFF]:
        return None
    return data.decode("utf-8", errors="replace")


def normalize(s: str) -> str:
    """Case-fold and collapse whitespace runs to a single space."""
    return " ".join(s.casefold().split())


def normalize_with_lines(text: str) -> tuple[str, list[int]]:
    """Normalize text like normalize() and map each output char to its line."""
    out: list[str] = []
    lines: list[int] = []
    line = 1
    pending_space = False
    for ch in text:
        if ch.isspace():
            if out:
                pending_space = True
            if ch == "\n":
                line += 1
            continue
        if pending_space:
            out.append(" ")
            lines.append(line)
            pending_space = False
        for c in ch.casefold():
            out.append(c)
            lines.append(line)
    return "".join(out), lines


def line_to_term(raw: str, min_len: int) -> str | None:
    """Turn one withheld line into a normalized term, or None if not distinctive."""
    s = raw.strip()
    s = LEAD_RE.sub("", s)
    s = TRAIL_RE.sub("", s)
    s = normalize(s)
    if len(s) < min_len:
        return None
    if len(ALNUM_RE.findall(s)) < 4:
        return None
    return s


def write_canary(withheld: Path) -> str:
    token = "CANARY-" + secrets.token_hex(16)
    (withheld / f"{token}.txt").write_text(
        token + "\n"
        + "This file is a leak detector for blind verification (AOK feature 1).\n"
        + "If this token appears in a verifier's output, that run saw withheld/ and is void.\n",
        encoding="utf-8",
    )
    return token


def canonical_canary(tok: str) -> str:
    """Canonical token form: upper-case prefix, lower-case hex."""
    return "CANARY-" + tok.split("-", 1)[1].lower()


def is_canary_file(p: Path) -> bool:
    return bool(re.fullmatch(r"CANARY-[0-9a-fA-F]{16,}\.txt", p.name))


def collect_canaries(withheld: Path) -> list[str]:
    found: dict[str, None] = {}
    for p in iter_files(withheld):
        for m in CANARY_RE.finditer(p.name):
            found.setdefault(canonical_canary(m.group(0)), None)
        text = read_text(p)
        if text is None:
            continue
        for m in CANARY_RE.finditer(text):
            found.setdefault(canonical_canary(m.group(0)), None)
    return list(found)


def collect_terms(withheld: Path, terms_file: Path | None, min_len: int) -> list[str]:
    terms: dict[str, None] = {}
    for tok in collect_canaries(withheld):
        terms.setdefault(normalize(tok), None)
    for p in iter_files(withheld):
        if is_canary_file(p):
            continue  # its token is already a term; skip the boilerplate lines
        text = read_text(p)
        if text is None:
            continue
        for raw in text.splitlines():
            t = line_to_term(raw, min_len)
            if t:
                terms.setdefault(t, None)
    if terms_file is not None:
        text = read_text(terms_file)
        if text is None:
            raise UsageError(f"cannot read --terms file: {terms_file}")
        for raw in text.splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            terms.setdefault(normalize(s), None)
    return list(terms)


def scan(root: Path, terms: list[str]):
    """Yield (path, line, term) for every occurrence of a term under root."""
    for p in iter_files(root):
        text = read_text(p)
        if text is None:
            continue
        norm, line_of = normalize_with_lines(text)
        if not norm:
            continue
        seen: set[tuple[int, str]] = set()
        for term in terms:
            start = norm.find(term)
            while start != -1:
                key = (line_of[start], term)
                if key not in seen:
                    seen.add(key)
                    yield p, line_of[start], term
                start = norm.find(term, start + 1)


def show(term: str) -> str:
    return term if len(term) <= TERM_SHOW else term[: TERM_SHOW - 1] + "…"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sanitize_packet.py",
        description="Check that a blind-verification packet/ does not leak withheld/ "
        "content, and that verifier outputs contain no withheld canary.",
        epilog="Exit codes: 0 clean, 1 hit found, 2 usage error.",
    )
    ap.add_argument("--packet", type=Path, help="directory given to the verifier")
    ap.add_argument("--withheld", type=Path, help="directory kept from the verifier")
    ap.add_argument("--canary", action="store_true",
                    help="write a new random CANARY-<hex> file into --withheld and print the token")
    ap.add_argument("--terms", type=Path, metavar="FILE",
                    help="extra withheld terms, one per line ('#' comments allowed)")
    ap.add_argument("--min-len", type=int, default=12, metavar="N",
                    help="minimum length of a withheld line to count as a term (default 12)")
    ap.add_argument("--check-output", nargs="+", type=Path, metavar="FILE",
                    help="verifier output files or directories to check for canary tokens")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        if args.withheld is None:
            raise UsageError("--withheld is required")
        if not args.withheld.is_dir():
            raise UsageError(f"--withheld is not a directory: {args.withheld}")
        if args.packet is None and not args.check_output:
            raise UsageError("give --packet (scan mode) and/or --check-output FILE...")
        if args.packet is not None and not args.packet.is_dir():
            raise UsageError(f"--packet is not a directory: {args.packet}")
        if args.min_len < 1:
            raise UsageError("--min-len must be >= 1")
        if args.packet is not None:
            pr = args.packet.resolve()
            wr = args.withheld.resolve()
            if pr == wr or wr in pr.parents or pr in wr.parents:
                raise UsageError("--packet and --withheld must be separate, non-nested directories")

        hits = 0
        if args.canary:
            print(write_canary(args.withheld))

        if args.packet is not None:
            terms = collect_terms(args.withheld, args.terms, args.min_len)
            for path, line, term in scan(args.packet, terms):
                print(f"{path}:{line}: {show(term)}")
                hits += 1
            print(f"sanitize_packet: {len(terms)} withheld terms, {hits} hit(s) in {args.packet}",
                  file=sys.stderr)

        if args.check_output:
            canaries = collect_canaries(args.withheld)
            if not canaries:
                raise UsageError("no CANARY-<hex> token in --withheld; run once with --canary first")
            out_hits = 0
            for target in args.check_output:
                if not target.exists():
                    raise UsageError(f"--check-output path not found: {target}")
                for path, line, term in scan(target, [normalize(c) for c in canaries]):
                    print(f"{path}:{line}: {canonical_canary(term)}")
                    out_hits += 1
            if out_hits:
                print(f"sanitize_packet: canary found {out_hits} time(s); the verification run is void",
                      file=sys.stderr)
            hits += out_hits
    except UsageError as e:
        print(f"sanitize_packet: error: {e}", file=sys.stderr)
        return 2
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
