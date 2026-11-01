#!/usr/bin/env python3
"""AOK ⑧ work-repo harness: one gate and Claude Code hooks, language-neutral core + adapters.

A work repository (where a dev session edits code) gets the same verification
in three places: the agent's loop (hooks), a person's terminal and CI (verify).
The core here is language-neutral. What to run for a language is data in
adapters/<name>.json: edit-time checks, the gate's steps, the patterns that
weaken a check. An adapter may hand everything to a fuller tool instead
("delegate"; the Rust adapter delegates to fsmap when it is installed).

Commands
  hook <event>          read the Claude Code hook payload on stdin and answer it
                        events: session-start, post-edit, stop, prompt, pre-bash
  verify [--mode quick|full] [--base REF] [--json]
                        run the gate once (with --base, as defined in that
                        commit: CI passes the base branch). Exit 0 passed, 1 a step failed,
                        2 the gate could not run (a required tool missing, a
                        step timed out, the configuration unreadable)
  install --repo DIR --adapter NAME [--yes]
                        copy the harness into DIR/.aok/harness and add the hooks
                        to DIR/.claude/settings.json. Without --yes it only
                        prints the plan: changing a repository's settings needs
                        a person's confirmation (AOK S4)

What the hooks do
  session-start  On `startup` only: record the baseline (HEAD, a fingerprint of
                 the work tree, hashes of files already changed). Tell Claude
                 which adapter and tool versions are active, or that the
                 harness is OFF and why.
  post-edit      After Edit/Write/MultiEdit of a file the adapter matches, run
                 the adapter's edit checks on that file. Failures go back to
                 Claude as a block reason.
  stop           If this session changed the tree (since the baseline) and the
                 tree is not one that already passed, run the quick gate. A
                 session that changed nothing is not judged: failures that were
                 already there are not its own. Every step must pass. A failure blocks the
                 stop with the failing steps' output; the gate not being able
                 to run blocks too (fail closed). After 3 blocked stops in a
                 row it lets go and tells the user the gate did NOT pass, so a
                 stuck session can report instead of looping. A pass reports
                 checks the session weakened (added suppressions, skipped
                 tests, removed assertions) and changes to the harness itself.
                 While background agents still run, it waits for the real end.
  prompt         The user wrote: reset the strike count.
  pre-bash       For a command that publishes (git push, gh pr create): deny
                 it when it bypasses hooks (--no-verify), when the harness or
                 its settings changed in this session, when uncommitted changes
                 mean the gate would not have seen what is pushed, or when the
                 quick gate fails or cannot run on this tree.

The gate definition is read from the commit the session started on
(.aok/harness.json in that commit), not from the work tree, so a session cannot
loosen the gate it is judged by; a changed definition is reported. A
guardrail, not a security boundary: an agent can still edit the harness code,
its state or local settings. CI on a clean checkout, using the base branch's
definition, is the gate of record.

Stdlib only; runs with `python3 -I`.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADAPTER_DIR = HERE / "adapters"
CONFIG_PATH = ".aok/harness.json"
PROTECTED = (".aok/", ".claude/settings.json", ".claude/settings.local.json")
STRIKES = 3
EDIT_BUDGET = 60.0
GATE_BUDGET = 600.0
TAIL_LINES = 40
EVENTS = ("session-start", "post-edit", "stop", "prompt", "pre-bash")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}
# variables that point git at another repository; steps never inherit them
GIT_REDIRECT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE")
PUBLISH = re.compile(r"\bgit\b(?:\s+-[^\s]+(?:\s+[^\s-][^\s]*)?)*\s+push\b|\bgh\s+pr\s+create\b")
NO_VERIFY = re.compile(r"(?:^|\s)--no-verify\b|(?:^|\s)-c\s*core\.hooksPath\b|\bcore\.hooksPath\b")


class HarnessError(Exception):
    """The harness could not do its job (configuration unreadable, adapter unknown)."""


# ---------------------------------------------------------------- git and files

def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in GIT_REDIRECT}


def git(root: Path, *args: str, binary: bool = False):
    try:
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, env=clean_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout if binary else p.stdout.decode("utf-8", errors="replace").strip()


def is_repo(root: Path) -> bool:
    return git(root, "rev-parse", "--is-inside-work-tree") == "true"


def head(root: Path) -> str | None:
    return git(root, "rev-parse", "--verify", "-q", "HEAD")


def file_hash(path: Path) -> str:
    try:
        if path.is_symlink():
            return "link:" + os.readlink(path)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def dirty_paths(root: Path) -> list[str]:
    """Paths git reports as changed or untracked (ignored files excluded), repo-relative."""
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", binary=True)
    if out is None:
        return []
    paths, parts, i = [], out.split(b"\0"), 0
    while i < len(parts):
        entry = parts[i]
        if len(entry) > 3:
            code, path = entry[:2].decode(), entry[3:].decode("utf-8", errors="surrogateescape")
            paths.append(path)
            if "R" in code or "C" in code:
                i += 1  # the source path of a rename/copy follows
        i += 1
    return sorted(set(paths))


def fingerprint(root: Path, config_text: str) -> str | None:
    """What the gate would see: HEAD, the dirty files' contents, the gate definition. None without git."""
    if not is_repo(root):
        return None
    h = hashlib.sha256()
    h.update((head(root) or "unborn").encode())
    for p in dirty_paths(root):
        h.update(b"\0" + p.encode("utf-8", errors="surrogateescape") + b"\0" + file_hash(root / p).encode())
    h.update(b"\0config\0" + config_text.encode())
    return h.hexdigest()


def state_dir(root: Path) -> Path:
    gd = git(root, "rev-parse", "--absolute-git-dir")
    if gd:
        return Path(gd) / "aok-harness"
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "aok-harness" / hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


def safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s or "no-session")[:80]


def load_state(root: Path, session: str) -> dict:
    p = state_dir(root) / f"session-{safe_id(session)}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(root: Path, session: str, state: dict) -> None:
    d = state_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".session-{safe_id(session)}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / f"session-{safe_id(session)}.json")


def log(root: Path, event: str, **fields) -> None:
    try:
        d = state_dir(root)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "event": event, **fields}, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- configuration

def read_config(root: Path, base: str | None) -> tuple[dict, str, list[str]]:
    """The gate definition from the session's base commit when it has one, else the work tree.

    Returns (config, its text, notes). A definition that differs between the base
    commit and the work tree is used as committed, and the difference is a note.
    """
    notes: list[str] = []
    work = root / CONFIG_PATH
    work_text = work.read_text(encoding="utf-8") if work.is_file() else None
    base_text = git(root, "show", f"{base}:{CONFIG_PATH}") if base else None
    text = base_text if base_text is not None else work_text
    if base_text is not None and work_text != base_text:
        notes.append(f"{CONFIG_PATH} changed in this session; the gate uses the version in {base[:12]}")
    if text is None:
        return {}, "", notes
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        raise HarnessError(f"{CONFIG_PATH} is not valid JSON: {e}") from e
    if not isinstance(cfg, dict):
        raise HarnessError(f"{CONFIG_PATH} must hold a JSON object")
    unknown = set(cfg) - {"adapter", "gate", "edit", "extra", "budget"}
    if unknown:
        raise HarnessError(f"{CONFIG_PATH}: unknown keys {sorted(unknown)}")
    return cfg, text, notes


def load_adapter(root: Path, cfg: dict) -> dict:
    name = cfg.get("adapter")
    if not name:
        found = []
        for p in sorted(ADAPTER_DIR.glob("*.json")):
            a = json.loads(p.read_text(encoding="utf-8"))
            if any((root / d).exists() for d in a.get("detect", [])):
                found.append(a)
        if len(found) != 1:
            names = [a["name"] for a in found] or ["none"]
            raise HarnessError(f"cannot pick an adapter (detected: {', '.join(names)}); set \"adapter\" in {CONFIG_PATH}")
        adapter = found[0]
    else:
        p = ADAPTER_DIR / f"{name}.json"
        if not re.fullmatch(r"[a-z0-9_-]+", str(name)) or not p.is_file():
            raise HarnessError(f"unknown adapter {name!r} (have: {', '.join(sorted(q.stem for q in ADAPTER_DIR.glob('*.json')))})")
        adapter = json.loads(p.read_text(encoding="utf-8"))
    adapter = json.loads(json.dumps(adapter))  # private copy
    if "gate" in cfg:
        for mode, steps in cfg["gate"].items():
            adapter.setdefault("gate", {})[mode] = steps
    if "edit" in cfg:
        adapter["edit"] = cfg["edit"]
    for mode in ("quick", "full"):
        adapter.setdefault("gate", {}).setdefault(mode, [])
        adapter["gate"][mode] = list(adapter["gate"][mode]) + list(cfg.get("extra", []))
    if not isinstance(adapter.get("env", {}), dict) or not all(isinstance(v, str) for v in adapter.get("env", {}).values()):
        raise HarnessError(f"adapter {adapter.get('name')}: env must map names to strings")
    for mode, steps in adapter["gate"].items():
        for s in steps:
            if not isinstance(s, dict) or not isinstance(s.get("cmd"), list) or not s["cmd"] \
                    or not all(isinstance(x, str) for x in s["cmd"]):
                raise HarnessError(f"gate.{mode}: each step needs a name and a non-empty cmd list of strings: {s!r:.80}")
    return adapter


def delegate(adapter: dict) -> dict | None:
    d = adapter.get("delegate")
    if not d or not shutil.which(d["bin"]):
        return None
    try:
        ok = subprocess.run(d["probe"], capture_output=True, timeout=20, stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return d if ok else None


# ---------------------------------------------------------------- running steps

def run_step(root: Path, step: dict, budget: float, file: str | None = None, env: dict | None = None) -> dict:
    cmd = [a.replace("{file}", file) if file else a for a in step["cmd"]]
    name = step.get("name") or cmd[0]
    if shutil.which(cmd[0]) is None and not Path(cmd[0]).is_file():
        return {"name": name, "status": "missing" if step.get("optional") else "cannot",
                "detail": f"{cmd[0]} not found"}
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, timeout=budget, env={**clean_env(), **(env or {})},
                           stdin=subprocess.DEVNULL, start_new_session=True)
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "cannot", "detail": f"timed out after {budget:g}s"}
    except OSError as e:
        return {"name": name, "status": "cannot", "detail": f"cannot run {cmd[0]}: {e.strerror or e}"}
    out = (p.stdout + p.stderr).decode("utf-8", errors="replace").rstrip().splitlines()
    return {"name": name, "status": "pass" if p.returncode == 0 else "fail", "exit": p.returncode,
            "seconds": round(time.monotonic() - start, 1), "tail": out[-TAIL_LINES:]}


def run_gate(root: Path, adapter: dict, mode: str, budget: float) -> dict:
    steps = adapter["gate"].get(mode)
    if not steps:
        return {"verdict": "cannot", "mode": mode, "steps": [], "reason": f"the adapter has no {mode} gate"}
    results, deadline = [], time.monotonic() + budget
    for s in steps:  # every step runs even after a failure, so one round reports everything
        left = deadline - time.monotonic()
        if left <= 1:
            results.append({"name": s.get("name", s["cmd"][0]), "status": "cannot", "detail": "gate budget used up"})
            continue
        results.append(run_step(root, s, left, env=adapter.get("env")))
    st = {r["status"] for r in results}
    verdict = "cannot" if "cannot" in st else "fail" if "fail" in st else "pass"
    return {"verdict": verdict, "mode": mode, "steps": results}


def describe(gate: dict) -> str:
    lines = []
    for r in gate["steps"]:
        if r["status"] == "pass":
            continue
        head_ = f"[{r['name']}] {r['status']}" + (f" (exit {r['exit']})" if "exit" in r else "")
        lines.append(head_ + (f": {r['detail']}" if r.get("detail") else ""))
        lines += ["  " + ln for ln in r.get("tail", [])[-20:]]
    return "\n".join(lines) or gate.get("reason", "")


# ---------------------------------------------------------------- session changes

def changed_since(root: Path, base: str | None, pending: dict) -> list[str]:
    """Files this session changed: committed since base, plus dirty files whose content moved since first seen."""
    out: set[str] = set()
    if base:
        diff = git(root, "diff", "--name-only", base, "HEAD")
        out |= set(filter(None, (diff or "").splitlines()))
    elif head(root):  # started before the first commit: committed files count unless unchanged since first seen
        for p in filter(None, (git(root, "ls-tree", "-r", "--name-only", "HEAD") or "").splitlines()):
            if pending.get(p) != file_hash(root / p):
                out.add(p)
    for p in dirty_paths(root):
        if pending.get(p) != file_hash(root / p):
            out.add(p)
    return sorted(out)


def weakenings(root: Path, adapter: dict, base: str | None, files: list[str]) -> list[str]:
    rules = adapter.get("weakening", [])
    if not rules or not files:
        return []
    found = []
    for f in files:
        if base and git(root, "cat-file", "-e", f"{base}:{f}") is not None:
            diff = git(root, "diff", "--no-color", "-U0", base, "--", f) or ""
            added = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
            removed = [ln[1:] for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]
        else:
            try:
                added, removed = (root / f).read_text(encoding="utf-8", errors="replace").splitlines(), []
            except (OSError, IsADirectoryError):
                continue
        for r in rules:
            lines = added if "added" in r else removed
            rx = re.compile(r.get("added") or r.get("removed"))
            n = sum(1 for ln in lines if rx.search(ln))
            if n:
                found.append(f"{f}: {r['name']} ×{n}")
    return found


# ---------------------------------------------------------------- hook answers

def tool_input(payload: dict) -> dict:
    ti = payload.get("tool_input")
    return ti if isinstance(ti, dict) else {}


def say(obj: dict | None) -> int:
    if obj:
        print(json.dumps(obj, ensure_ascii=False))
    return 0


def context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def project_root(payload: dict) -> Path:
    p = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    root = Path(p)
    top = git(root, "rev-parse", "--show-toplevel")
    return Path(top) if top else root


def versions(adapter: dict) -> list[str]:
    out = []
    for cmd in adapter.get("versions", []):
        if shutil.which(cmd[0]) is None:
            out.append(f"{' '.join(cmd[:2])}: not installed")
            continue
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
            out.append((p.stdout or p.stderr).decode("utf-8", errors="replace").strip().splitlines()[0])
        except (OSError, subprocess.TimeoutExpired, IndexError):
            out.append(f"{' '.join(cmd[:2])}: no answer")
    return out


def on_session_start(root: Path, payload: dict, session: str) -> int:
    state = load_state(root, session)
    if payload.get("source", "startup") == "startup" or "baseline" not in state:
        base = head(root)
        state = {"baseline": {"head": base, "at": time.time(),
                              "pending": {p: file_hash(root / p) for p in dirty_paths(root)}},
                 "strikes": 0, "last_pass": None}
        save_state(root, session, state)
    base = state["baseline"]["head"]
    try:
        cfg, _, notes = read_config(root, base)
        adapter = load_adapter(root, cfg)
    except HarnessError as e:
        msg = f"[aok-harness] OFF: {e}. Nothing checks edits or gates the end of a turn in this session."
        return say({"systemMessage": msg, **context("SessionStart", msg + " Run the project's checks yourself and tell the user the harness is off.")})
    d = delegate(adapter)
    if d:
        return relay(d, "session-start", payload)
    lines = [f"[aok-harness] adapter {adapter['name']}: edit checks on {', '.join(adapter.get('edit', {}).get('match', [])) or 'nothing'}, "
             f"a gate of {', '.join(s.get('name', s['cmd'][0]) for s in adapter['gate']['quick'])} at the end of each turn, "
             f"and before git push / gh pr create."]
    lines += ["Tool versions (use these, not remembered ones): " + "; ".join(versions(adapter))]
    if adapter.get("delegate"):
        lines.append(f"{adapter['delegate']['bin']} is not installed, so the basic {adapter['name']} steps run instead; "
                     "its fuller checks (configuration integrity, push audit) are off.")
    lines += notes
    lines.append("Do not weaken a check to get green (suppressions, skipped tests, removed assertions, editing .aok/ or "
                 ".claude/settings.json); the user sees such changes. If the gate blocks you three times, stop and report.")
    if not base:
        lines.append("The repository has no commit yet: every change counts as this session's.")
    log(root, "session-start", source=payload.get("source"), adapter=adapter["name"])
    return say(context("SessionStart", "\n".join(lines)))


def on_post_edit(root: Path, payload: dict, session: str) -> int:
    if payload.get("tool_name") not in EDIT_TOOLS:
        return 0
    path = tool_input(payload).get("file_path")
    if not isinstance(path, str) or not path:
        return 0
    state = load_state(root, session)
    cfg, _, _ = read_config(root, (state.get("baseline") or {}).get("head"))
    adapter = load_adapter(root, cfg)
    d = delegate(adapter)
    if d:
        return relay(d, "post-edit", payload)
    try:
        rel = str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return 0  # outside the project
    edit = adapter.get("edit") or {}
    if not any(fnmatch.fnmatch(rel, m) or fnmatch.fnmatch(Path(rel).name, m) for m in edit.get("match", [])):
        return 0
    if not (root / rel).is_file():
        return 0
    budget = float(cfg.get("budget", {}).get("edit", EDIT_BUDGET))
    results = [run_step(root, s, budget, file=rel, env=adapter.get("env")) for s in edit.get("steps", [])]
    bad = [r for r in results if r["status"] in ("fail", "cannot")]
    log(root, "post-edit", file=rel, result=[r["status"] for r in results])
    if not bad:
        return 0
    text = describe({"steps": bad})
    return say({"decision": "block", "reason": f"[aok-harness] checks failed after editing {rel}:\n{text}"})


def on_stop(root: Path, payload: dict, session: str) -> int:
    tasks = payload.get("background_tasks")
    if isinstance(tasks, list) and tasks:
        log(root, "stop", result="skip-background", n=len(tasks))
        return 0  # background agents still run; the real end of the turn comes later
    state = load_state(root, session)
    base = (state.get("baseline") or {}).get("head")
    pending = (state.get("baseline") or {}).get("pending", {})
    try:
        cfg, cfg_text, notes = read_config(root, base)
        adapter = load_adapter(root, cfg)
    except HarnessError as e:
        return strike(root, session, state, f"the gate cannot run: {e}", None)
    d = delegate(adapter)
    if d:
        return relay(d, "stop", payload)
    fp = fingerprint(root, cfg_text)
    mine: list[str] = []
    if fp is not None:  # with git: skip a tree that already passed, and a session that changed nothing
        if fp == state.get("last_pass"):
            log(root, "stop", result="skip-unchanged")
            return 0
        mine = changed_since(root, base, pending)
        if not mine:
            log(root, "stop", result="skip-no-session-change")
            return 0
    budget = float(cfg.get("budget", {}).get("gate", GATE_BUDGET))
    gate = run_gate(root, adapter, "quick", budget)
    log(root, "stop", result=gate["verdict"], steps=[(r["name"], r["status"]) for r in gate["steps"]])
    if gate["verdict"] == "pass":
        # the tree as the gate left it (a step may write caches); the next stop compares against this
        state["last_pass"], state["strikes"] = fingerprint(root, cfg_text), 0
        save_state(root, session, state)
        report = notes + [f"weakened check: {w}" for w in weakenings(root, adapter, base, mine)]
        report += [f"harness file changed in this session: {f}" for f in mine if f.startswith(PROTECTED)]
        told = set(state.get("told_missing", []))
        missing = [r["name"] for r in gate["steps"] if r["status"] == "missing" and r["name"] not in told]
        if missing:  # said once per session: a skipped check must be visible, not repeated every turn
            report.append(f"optional step(s) skipped, tool not installed: {', '.join(missing)}")
            state["told_missing"] = sorted(told | set(missing))
            save_state(root, session, state)
        if report:
            return say({"systemMessage": "[aok-harness] gate passed. Review:\n- " + "\n- ".join(report)})
        return 0
    what = "failed" if gate["verdict"] == "fail" else "could not run"
    return strike(root, session, state, f"the quick gate {what}:\n{describe(gate)}", fp)


def strike(root: Path, session: str, state: dict, reason: str, fp: str | None) -> int:
    n = int(state.get("strikes") or 0) + 1
    state["strikes"] = n
    save_state(root, session, state)
    if n >= STRIKES:
        state["strikes"] = 0
        save_state(root, session, state)
        log(root, "stop", result="released", strikes=n)
        msg = (f"[aok-harness] the gate did NOT pass after {n} tries; the turn ends so the session can report. "
               f"Last result:\n{reason}")
        return say({"systemMessage": msg})
    return say({"decision": "block",
                "reason": f"[aok-harness] {reason}\n\nFix the cause (do not weaken the check), then end the turn again. "
                          f"Try {n} of {STRIKES}; after that the turn ends and you report what is still failing."})


def on_prompt(root: Path, payload: dict, session: str) -> int:
    state = load_state(root, session)
    if state.get("strikes"):
        state["strikes"] = 0
        save_state(root, session, state)
    return 0


def deny(reason: str) -> int:
    return say({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"[aok-harness] {reason}"}})


def on_pre_bash(root: Path, payload: dict, session: str) -> int:
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = str(tool_input(payload).get("command") or "")
    if NO_VERIFY.search(cmd) and re.search(r"\b(push|commit)\b", cmd):
        return deny("this command switches git hooks off (--no-verify / core.hooksPath); run it without that")
    if not PUBLISH.search(cmd):
        return 0
    state = load_state(root, session)
    base = (state.get("baseline") or {}).get("head")
    try:
        cfg, cfg_text, _ = read_config(root, base)
        adapter = load_adapter(root, cfg)
    except HarnessError as e:
        return deny(f"the gate cannot run, so nothing is published: {e}")
    d = delegate(adapter)
    if d:
        return relay(d, "pre-bash", payload)
    mine = changed_since(root, base, (state.get("baseline") or {}).get("pending", {}))
    touched = [f for f in mine if f.startswith(PROTECTED)]
    if touched:
        return deny("the harness or its settings changed in this session (" + ", ".join(touched[:5]) +
                    "); a person reviews and publishes such a change")
    if dirty_paths(root):
        return deny("there are uncommitted changes, so the gate would check a different tree than the commit being "
                    "pushed. Commit (or remove) them in one command, then push in another")
    fp = fingerprint(root, cfg_text)
    if fp is not None and fp == state.get("last_pass"):
        return 0
    gate = run_gate(root, adapter, "quick", float(cfg.get("budget", {}).get("gate", GATE_BUDGET)))
    log(root, "pre-bash", result=gate["verdict"])
    if gate["verdict"] == "pass":
        state["last_pass"] = fingerprint(root, cfg_text)
        save_state(root, session, state)
        return 0
    return deny(f"the quick gate {'failed' if gate['verdict'] == 'fail' else 'could not run'} on this tree:\n{describe(gate)}")


def relay(d: dict, event: str, payload: dict) -> int:
    cmd = [a.replace("{event}", d["events"][event]) for a in d["hook"]]
    try:
        p = subprocess.run(cmd, input=json.dumps(payload).encode(), capture_output=True, timeout=GATE_BUDGET + 60)
    except (OSError, subprocess.TimeoutExpired) as e:
        if event == "pre-bash":
            return deny(f"{d['bin']} hook failed ({e}); nothing is published")
        return say({"systemMessage": f"[aok-harness] {d['bin']} hook {event} failed: {e}"})
    sys.stdout.write(p.stdout.decode("utf-8", errors="replace"))
    sys.stderr.write(p.stderr.decode("utf-8", errors="replace"))
    return p.returncode


HANDLERS = {"session-start": on_session_start, "post-edit": on_post_edit, "stop": on_stop,
            "prompt": on_prompt, "pre-bash": on_pre_bash}


def hook(event: str) -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    root = project_root(payload)
    session = str(payload.get("session_id") or "no-session")
    try:
        return HANDLERS[event](root, payload, session)
    except Exception as e:  # noqa: BLE001 — a crashing hook must say so, never pass silently
        log(root, event, result="crash", error=repr(e)[:300])
        if event == "pre-bash" and PUBLISH.search(str(tool_input(payload).get("command") or "")):
            return deny(f"the harness crashed ({e!r:.200}); nothing is published")
        if event == "stop":
            state = load_state(root, session)
            return strike(root, session, state, f"the harness crashed: {e!r:.300}", None)
        return say({"systemMessage": f"[aok-harness] {event} hook crashed: {e!r:.300}"})


# ---------------------------------------------------------------- verify and install

def verify(root: Path, mode: str, as_json: bool, base: str | None = None) -> int:
    try:
        if base:
            sha = git(root, "rev-parse", "--verify", "-q", f"{base}^{{commit}}")
            if not sha:
                raise HarnessError(f"--base {base} is not a commit here")
            base = sha
        cfg, _, notes = read_config(root, base)
        adapter = load_adapter(root, cfg)
    except HarnessError as e:
        print(f"verify: cannot run: {e}", file=sys.stderr)
        return 2
    d = delegate(adapter)
    if d and d.get("verify"):
        cmd = [a.replace("{mode}", mode) for a in d["verify"]]
        if as_json:
            if not d.get("json"):
                print(f"verify: cannot run: {d['bin']} has no JSON report in the adapter", file=sys.stderr)
                return 2
            cmd += d["json"]
        if base:  # the delegate reads its own gate definition from the work tree
            print(f"verify: --base chose the adapter only; {d['bin']} checks the work tree's gate settings itself",
                  file=sys.stderr)
        return subprocess.run(cmd, cwd=root).returncode
    gate = run_gate(root, adapter, mode, float(cfg.get("budget", {}).get("gate", GATE_BUDGET)))
    if as_json:
        print(json.dumps({"adapter": adapter["name"], **gate}, ensure_ascii=False, indent=1))
    else:
        for r in gate["steps"]:
            extra = f" ({r['seconds']}s)" if "seconds" in r else (f" — {r['detail']}" if r.get("detail") else "")
            print(f"{r['status']:>7}  {r['name']}{extra}")
        if gate["verdict"] != "pass":
            print(describe(gate))
        print(f"verdict: {gate['verdict']} ({adapter['name']}, {mode})")
    return {"pass": 0, "fail": 1}.get(gate["verdict"], 2)


def hook_settings(cmd_prefix: str) -> dict:
    def h(event: str, timeout: int) -> list:
        return [{"type": "command", "command": f"{cmd_prefix} hook {event}", "timeout": timeout}]
    return {
        "SessionStart": [{"hooks": h("session-start", 120)}],
        "UserPromptSubmit": [{"hooks": h("prompt", 30)}],
        "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": h("post-edit", 120)}],
        "PreToolUse": [{"matcher": "Bash", "hooks": h("pre-bash", 900)}],
        "Stop": [{"hooks": h("stop", 900)}],
    }


def install(repo: Path, adapter_name: str, yes: bool) -> int:
    if not repo.is_dir():
        print(f"install: not a directory: {repo}", file=sys.stderr)
        return 2
    if not (ADAPTER_DIR / f"{adapter_name}.json").is_file():
        print(f"install: unknown adapter {adapter_name!r}", file=sys.stderr)
        return 2
    target = repo / ".aok" / "harness"
    settings = repo / ".claude" / "settings.json"
    config = repo / CONFIG_PATH
    prefix = 'python3 -I "$CLAUDE_PROJECT_DIR/.aok/harness/aok_harness.py"'
    try:
        current = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
    except json.JSONDecodeError as e:
        print(f"install: {settings} is not valid JSON ({e}); merge by hand", file=sys.stderr)
        return 2
    merged = json.loads(json.dumps(current))
    hooks = merged.setdefault("hooks", {})
    for ev, entries in hook_settings(prefix).items():
        existing = hooks.setdefault(ev, [])
        commands = {h.get("command", "") for e in existing if isinstance(e, dict) for h in e.get("hooks", []) if isinstance(h, dict)}
        if not any(prefix in c for c in commands):
            existing.extend(entries)
    perms = merged.setdefault("permissions", {})
    ask = perms.setdefault("ask", [])
    for rule in ("Edit(/.aok/**)", "Write(/.aok/**)", "Edit(/.claude/**)", "Write(/.claude/**)"):
        if rule not in ask:
            ask.append(rule)
    reviewer = repo / ".claude" / "agents" / "aok-reviewer.md"
    plan = [f"copy harness/aok_harness.py and adapters/ → {target}",
            f"{'keep' if reviewer.is_file() else 'write'} {reviewer} (adversarial reviewer subagent; works on a copy, never pushes)",
            f"{'keep' if config.is_file() else 'write'} {config} ({{\"adapter\": \"{adapter_name}\"}})",
            f"{'merge into' if settings.is_file() else 'create'} {settings}: hooks for SessionStart, UserPromptSubmit, "
            "PostToolUse(Edit|Write|MultiEdit), PreToolUse(Bash), Stop; ask before editing .aok/ and .claude/"]
    print("install plan:\n  " + "\n  ".join(plan))
    if not yes:
        print("dry run: nothing written. Changing a repository's .claude/settings.json needs a person's "
              "confirmation (AOK S4); run again with --yes after they agree.")
        return 0
    (target / "adapters").mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).resolve(), target / "aok_harness.py")
    for a in ADAPTER_DIR.glob("*.json"):
        shutil.copy2(a, target / "adapters" / a.name)
    (target / "agents").mkdir(exist_ok=True)
    shutil.copy2(HERE / "agents" / "aok-reviewer.md", target / "agents" / "aok-reviewer.md")
    if not config.is_file():
        config.write_text(json.dumps({"adapter": adapter_name}, indent=2) + "\n", encoding="utf-8")
    if not reviewer.is_file():
        reviewer.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / "agents" / "aok-reviewer.md", reviewer)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("installed. Commit .aok/ and .claude/ (settings.json, agents/aok-reviewer.md); the hooks apply from the next session.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AOK ⑧ work-repo harness (hooks, gate, install).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook", help="answer a Claude Code hook (payload on stdin)")
    h.add_argument("event", choices=EVENTS)
    v = sub.add_parser("verify", help="run the gate once (exit 0 pass, 1 fail, 2 cannot run)")
    v.add_argument("--root", default=".")
    v.add_argument("--mode", choices=("quick", "full"), default="full")
    v.add_argument("--json", action="store_true")
    v.add_argument("--base", metavar="REF", help="read the gate definition from this commit (e.g. origin/main in CI)")
    i = sub.add_parser("install", help="install into a work repository (dry run without --yes)")
    i.add_argument("--repo", required=True)
    i.add_argument("--adapter", required=True)
    i.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "hook":
        return hook(args.event)
    if args.cmd == "verify":
        return verify(Path(args.root).resolve(), args.mode, args.json, args.base)
    return install(Path(args.repo), args.adapter, args.yes)


if __name__ == "__main__":
    sys.exit(main())
