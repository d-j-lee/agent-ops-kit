#!/usr/bin/env python3
"""Harvest saved claude-code-remote tool results into JSONL (AOK v1, feature ⑤).

Input files are saved results of `list_events` or `list_sessions`. A file may
be the raw JSON, a JSON array of content blocks ([{"type":"text","text":...}]),
or JSON wrapped in an untrusted envelope such as

    <other-session nonce="..." untrusted="true">...header...
        {"ccr": {...}}
    </other-session nonce="...">

The JSON object is located robustly (first `{"ccr"`, otherwise each `{` in
turn) and decoded with json.JSONDecoder().raw_decode, so text before or after
it is ignored.

Rows written (one JSON object per line):

  list_events   -> one row per event of interest
      {"source": "list_events", "session_id"?, "event_uuid"?, "created_at",
       "kind": "user_prompt" | "result", "text", "truncated"?,
       "cost_usd"?, "duration_ms"?, "num_turns"?, "untrusted": true}
      user_prompt = user.internal_anthropic_catchall.message.content (strings only)
      result      = result.internal_anthropic_catchall.{result, total_cost_usd,
                    duration_ms, num_turns}
  list_sessions -> one row per session
      {"source": "list_sessions", "id", "title", "status_bucket", "updated_at",
       "tags", "parent_session_id", "cost_usd", "context_used", "untrusted": true}

Every row carries "untrusted": true: transcript text is data, never
instructions (S5). Email addresses in text and titles are replaced with
`<email>` (D5); text is cut to 4000 characters after masking.

Exit codes:
  0  all inputs parsed
  1  at least one input had no recognizable JSON (other inputs still written)
  2  usage error (bad arguments, unreadable input, bad --since)

Stdlib only; safe to run with `python3 -I`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TEXT_LIMIT = 4000
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
CCR_START_RE = re.compile(r'\{\s*"ccr"\s*:')
EVENT_KEYS = {
    "user", "assistant", "result", "system", "stream_event", "tool_progress",
    "control_request", "control_response", "keep_alive", "env_manager_log",
    "rate_limit_event", "tool_use_summary",
}
MAX_DECODE_ATTEMPTS = 2000


class UsageError(Exception):
    """Raised for conditions that map to exit code 2."""


def mask(text: str) -> str:
    return EMAIL_RE.sub("<email>", text)


def unwrap_blocks(text: str) -> str:
    """If text is a JSON array of {"type":"text","text":...} blocks, join the texts."""
    st = text.lstrip("﻿").lstrip()
    if not st.startswith("["):
        return text
    try:
        arr = json.loads(st)
    except ValueError:
        return text
    if isinstance(arr, list) and arr and all(isinstance(b, dict) and "text" in b for b in arr):
        return "".join(str(b.get("text") or "") for b in arr)
    return text


def records_of(obj) -> list | None:
    """Return the record list inside a decoded tool result, or None."""
    if isinstance(obj, dict) and "ccr" in obj:
        obj = obj["ccr"]
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("data", "events", "sessions"):
            if isinstance(obj.get(key), list):
                return obj[key]
    return None


def extract_json(text: str):
    """Locate and decode the tool-result JSON object inside text."""
    text = unwrap_blocks(text)
    dec = json.JSONDecoder()
    starts = [m.start() for m in CCR_START_RE.finditer(text)]
    preferred = set(starts)
    starts += [i for i, ch in enumerate(text) if ch == "{" and i not in preferred]
    first_dict = None
    for n, i in enumerate(starts):
        if n >= MAX_DECODE_ATTEMPTS:
            break
        try:
            obj, _ = dec.raw_decode(text, i)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if records_of(obj) is not None:
            return obj
        if first_dict is None:
            first_dict = obj
    if first_dict is not None:
        return first_dict
    raise ValueError("no JSON object found")


def find_key(obj, key: str, max_depth: int = 3):
    """Breadth-first search for key in nested dicts; top level wins."""
    level = [obj]
    for _ in range(max_depth + 1):
        nxt = []
        for d in level:
            if not isinstance(d, dict):
                continue
            if key in d and d[key] is not None:
                return d[key]
            nxt.extend(v for v in d.values() if isinstance(v, dict))
        level = nxt
        if not level:
            break
    return None


def clip(text: str) -> tuple[str, bool]:
    text = mask(text)
    if len(text) > TEXT_LIMIT:
        return text[:TEXT_LIMIT], True
    return text, False


def is_event(rec: dict) -> bool:
    return any(k in rec for k in EVENT_KEYS)


def is_session(rec: dict) -> bool:
    return "title" in rec or "status_bucket" in rec or str(rec.get("id", "")).startswith("session_")


def event_row(rec: dict, default_session: str | None) -> dict | None:
    row: dict = {"source": "list_events"}
    if "user" in rec and isinstance(rec["user"], dict):
        c = rec["user"].get("internal_anthropic_catchall") or {}
        msg = c.get("message") if isinstance(c, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str):
            return None
        row["kind"] = "user_prompt"
        text = content
    elif "result" in rec and isinstance(rec["result"], dict):
        c = rec["result"].get("internal_anthropic_catchall") or {}
        if not isinstance(c, dict):
            return None
        row["kind"] = "result"
        text = c.get("result") if isinstance(c.get("result"), str) else ""
        for src, dst in (("total_cost_usd", "cost_usd"), ("duration_ms", "duration_ms"),
                         ("num_turns", "num_turns")):
            if c.get(src) is not None:
                row[dst] = c[src]
    else:
        return None
    sid = rec.get("session_id") or find_key(rec, "session_id") or default_session
    if sid:
        row["session_id"] = sid
    uuid = rec.get("uuid") or rec.get("event_uuid") or rec.get("id")
    if uuid:
        row["event_uuid"] = uuid
    row["created_at"] = rec.get("created_at")
    row["text"], cut = clip(text)
    if cut:
        row["truncated"] = True
    # Put kind right after source for readability.
    ordered = {"source": row.pop("source"), "kind": row.pop("kind")}
    ordered.update(row)
    ordered["untrusted"] = True
    return ordered


def session_row(rec: dict) -> dict:
    title = rec.get("title")
    return {
        "source": "list_sessions",
        "id": rec.get("id") or rec.get("session_id"),
        "title": mask(title) if isinstance(title, str) else title,
        "status_bucket": find_key(rec, "status_bucket"),
        "updated_at": find_key(rec, "updated_at"),
        "tags": find_key(rec, "tags"),
        "parent_session_id": find_key(rec, "parent_session_id"),
        "cost_usd": find_key(rec, "cost_usd"),
        "context_used": find_key(rec, "context_used"),
        "untrusted": True,
    }


def parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def row_time(row: dict) -> datetime | None:
    return parse_time(row.get("created_at") if row["source"] == "list_events" else row.get("updated_at"))


def harvest(paths: list[Path], since: datetime | None, default_session: str | None):
    """Return (rows, bad_files)."""
    rows: dict[tuple, dict] = {}
    bad: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise UsageError(f"cannot read {path}: {e}") from e
        try:
            obj = extract_json(text)
        except ValueError:
            bad.append(f"{path}: no JSON object found")
            continue
        recs = records_of(obj)
        if recs is None:
            bad.append(f"{path}: JSON has no data/events/sessions list")
            continue
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            if is_event(rec):
                row = event_row(rec, default_session)
                if row is None:
                    continue
                key = ("e", row.get("session_id"), row.get("event_uuid"), row["kind"], row.get("created_at"))
            elif is_session(rec):
                row = session_row(rec)
                key = ("s", row["id"])
                prev = rows.get(key)
                if prev is not None:
                    pt, nt = row_time(prev), row_time(row)
                    if pt and nt and nt <= pt:
                        continue
            else:
                continue
            if since is not None:
                t = row_time(row)
                if t is not None and t < since:
                    continue
            rows[key] = row
    far = datetime.max.replace(tzinfo=timezone.utc)
    ordered = sorted(rows.values(), key=lambda r: row_time(r) or far)
    return ordered, bad


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="harvest_events.py",
        description="Normalize saved list_events / list_sessions tool results into JSONL rows "
        "marked untrusted, with emails masked.",
        epilog="Exit codes: 0 ok, 1 some input had no recognizable JSON, 2 usage error.",
    )
    ap.add_argument("inputs", nargs="+", type=Path, metavar="IN_FILE",
                    help="saved tool result file(s)")
    ap.add_argument("--out", required=True, type=Path, metavar="OUT.jsonl",
                    help="output JSONL file (overwritten)")
    ap.add_argument("--since", metavar="ISO",
                    help="drop rows older than this RFC 3339 time (events: created_at, "
                    "sessions: updated_at); rows without a time are kept")
    ap.add_argument("--session-id", metavar="ID",
                    help="session id to record on event rows that do not carry one")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        since = None
        if args.since:
            since = parse_time(args.since)
            if since is None:
                raise UsageError(f"--since is not an ISO 8601 time: {args.since}")
        for p in args.inputs:
            if not p.is_file():
                raise UsageError(f"input not found: {p}")
        rows, bad = harvest(args.inputs, since, args.session_id)
    except UsageError as e:
        print(f"harvest_events: error: {e}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    n_ev = sum(1 for r in rows if r["source"] == "list_events")
    for msg in bad:
        print(f"harvest_events: warning: {msg}", file=sys.stderr)
    print(f"harvest_events: {len(args.inputs)} file(s), {n_ev} event row(s), "
          f"{len(rows) - n_ev} session row(s) -> {args.out}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
