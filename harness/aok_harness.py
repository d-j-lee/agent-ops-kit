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
  session-start  On `startup`, or when this session id has no usable state yet,
                 record the baseline (HEAD, hashes of files already changed, hashes
                 of the harness files). A session that continues another one
                 (resume or fork without state) counts every changed file as its
                 own. Tell Claude which adapter and tool versions are active, or
                 that the harness is OFF and why.
  post-edit      After Edit/Write/MultiEdit of a file the adapter matches, run
                 the adapter's edit checks on that file. Failures go back to
                 Claude as a block reason.
  stop           If this session changed the tree (since the baseline) and the
                 tree is not one that already passed and was reviewed, run the
                 quick gate. A session that changed nothing is not judged; one
                 that changed anything is judged on the whole tree, and the block
                 asks Claude to report a failure it did not cause rather than edit
                 unrelated code. Every step must pass. A failure blocks the
                 stop with the failing steps' output; the gate not being able
                 to run blocks too (fail closed), and so does a damaged or missing
                 session state. It blocks 3 stops in a row at most; the third
                 block tells Claude to report, and the stop after it is let
                 through with a message to the user that the gate did NOT pass.
                 A pass (here, or before a push in the same turn) reports checks
                 the session weakened (suppressions, skipped or removed tests,
                 removed assertions) and changes to the harness files. While
                 background agents (subagents, teammates, ...) still run, it
                 leaves the gate to the real end; a background shell does not
                 count.
  prompt         The user wrote: reset the strike count.
  pre-bash       Deny a git command that switches git hooks off (--no-verify,
                 commit -n, core.hooksPath). For a command that publishes (git
                 push, gh pr create): deny it when it publishes anything but the
                 checked-out branch, moves HEAD in the same command, when the
                 harness files, its settings or the CI workflows changed in this
                 session, when uncommitted changes mean the gate would not see
                 what is pushed, or when the quick gate fails or cannot run.

The gate definition (.aok/harness.json) and the adapters copied into
.aok/harness/adapters are read from the commit the session started on, not from
the work tree, and a definition that differs is reported. A guardrail, not a
security boundary: an agent can still edit the harness code, its state or local
settings; the push refusal and the report put such changes in front of a
person. CI on a clean checkout, running the base branch's harness, is the gate
of record.

Stdlib only; runs with `python3 -I`.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADAPTER_DIR = HERE / "adapters"
CONFIG_PATH = ".aok/harness.json"
ADAPTER_REL = ".aok/harness/adapters"  # where install puts the adapters in a work repository
# the harness, its settings, its reviewer and the CI that is the gate of record: a person publishes changes to these
PROTECTED = (".aok/", ".claude/settings.json", ".claude/settings.local.json", ".claude/agents/aok-reviewer.md",
             ".github/workflows/")
STRIKES = 3
AGENT_TASKS = ("subagent", "workflow", "teammate", "cloud session")  # background_tasks types that are the turn's work
EDIT_BUDGET = 60.0
GATE_BUDGET = 600.0
# install sets these hook timeouts; a budget must leave room for the harness's own git calls
HOOK_TIMEOUT = {"session-start": 120, "prompt": 30, "post-edit": 120, "pre-bash": 900, "stop": 900}
BUDGET_MAX = {"edit": HOOK_TIMEOUT["post-edit"] - 20, "gate": HOOK_TIMEOUT["stop"] - 60}
TAIL_LINES = 40
EVENTS = ("session-start", "post-edit", "stop", "prompt", "pre-bash")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}
# variables that point git at another repository; steps never inherit them
GIT_REDIRECT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE")
# fallback when a command cannot be split into words: anything that looks like publishing counts as publishing
PUBLISH = re.compile(r"\bgit\b(?:\s+\S+)*?\s+push\b|\bgh\b(?:\s+\S+)*?\s+pr\s+create\b")
NO_VERIFY = re.compile(r"--no-verify\b|\bcore\.hookspath\b", re.I)  # git config keys ignore case
GIT_GLOBAL_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
MOVES_HEAD = {"merge", "rebase", "reset", "checkout", "switch", "commit", "cherry-pick", "pull", "am", "revert", "stash"}
SHELLS = {"bash", "sh", "zsh", "dash"}


class HarnessError(Exception):
    """The harness could not do its job (configuration unreadable, adapter unknown)."""


# ---------------------------------------------------------------- git and files

def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in GIT_REDIRECT}


def git(root: Path, *args: str, binary: bool = False, strip: bool = True):
    try:
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, env=clean_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    if binary:
        return p.stdout
    text = p.stdout.decode("utf-8", errors="replace")
    return text.strip() if strip else text  # file contents (git show) must keep their last newline


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


SETTINGS_WATCHED = ("hooks", "disableAllHooks", "env")  # what can switch the harness off or change what its steps see


def settings_hash(path: Path) -> str:
    """A Claude Code settings file, by the keys that matter to the harness only: Claude Code itself writes permission
    rules into .claude/settings.local.json when the user chooses "don't ask again", and that must not read as tampering."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return file_hash(path)
    if not isinstance(data, dict):
        return file_hash(path)
    watched = {k: data[k] for k in SETTINGS_WATCHED if k in data}
    return "settings:" + hashlib.sha256(json.dumps(watched, sort_keys=True).encode()).hexdigest()


def protected_hashes(root: Path) -> dict[str, str]:
    """Hashes of every PROTECTED file, ignored ones included (git status does not show those)."""
    files: set[str] = set()
    for entry in PROTECTED:
        if entry.endswith("/"):
            d = root / entry
            if d.is_dir():
                files |= {q.relative_to(root).as_posix() for q in d.rglob("*") if q.is_file() or q.is_symlink()}
        else:
            files.add(entry)
    return {f: settings_hash(root / f) if f.startswith(".claude/settings") else file_hash(root / f) for f in sorted(files)}


def protected_changes(root: Path, state: dict) -> list[str]:
    before = (state.get("baseline") or {}).get("protected")
    if not isinstance(before, dict):
        return []
    now = protected_hashes(root)
    return sorted(f for f in set(before) | set(now) if before.get(f, "missing") != now.get(f, "missing"))


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
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):  # unreadable, not UTF-8, not JSON
        return {"damaged": True}
    if not isinstance(data, dict):
        return {"damaged": True}
    b = data.get("baseline")
    if b is not None and not (isinstance(b, dict) and isinstance(b.get("head"), (str, type(None)))
                              and isinstance(b.get("pending", {}), dict) and isinstance(b.get("protected", {}), dict)):
        return {"damaged": True, "strikes": data.get("strikes", 0) if isinstance(data.get("strikes"), int) else 0}
    return data


def session_base(root: Path, state: dict) -> tuple[str | None, dict]:
    """The session's base commit and the files already changed when it started. Raises when the state cannot be
    trusted: then nothing says which changes are the session's or which commit's gate definition applies."""
    if state.get("damaged"):
        raise HarnessError(f"the session state in {state_dir(root)} is damaged (not as the harness wrote it); "
                           "a person checks it, and a new session starts a fresh one")
    b = state.get("baseline")
    if b is None:
        if is_repo(root):
            raise HarnessError("this session has no baseline (its SessionStart hook did not run, or the state was "
                               "removed); start a new session")
        return None, {}
    return b.get("head"), b.get("pending", {})


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
    base_text = git(root, "show", f"{base}:{CONFIG_PATH}", strip=False) if base else None
    text = base_text if base else work_text  # with a base commit, only what it holds counts
    if base_text is not None and work_text != base_text:
        notes.append(f"{CONFIG_PATH} differs from the version in {base[:12]}; the gate uses that version")
    elif base and base_text is None and work_text is not None:
        notes.append(f"{CONFIG_PATH} is not in {base[:12]}; it applies to sessions that start after it is committed")
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
    check_config(cfg)
    return cfg, text, notes


def check_config(cfg: dict) -> None:
    """Shapes and limits, so a bad value is a clear 'cannot run' instead of a crash or a gate that outlives its hook."""
    def bad(what: str) -> HarnessError:
        return HarnessError(f"{CONFIG_PATH}: {what}")
    if "adapter" in cfg and not isinstance(cfg["adapter"], str):
        raise bad("adapter must be a name")
    if "gate" in cfg and not (isinstance(cfg["gate"], dict) and set(cfg["gate"]) <= {"quick", "full"}
                              and all(isinstance(v, list) for v in cfg["gate"].values())):
        raise bad('gate must be {"quick": [steps], "full": [steps]}')
    if "edit" in cfg and not (isinstance(cfg["edit"], dict) and isinstance(cfg["edit"].get("match", []), list)
                              and isinstance(cfg["edit"].get("steps", []), list)):
        raise bad('edit must be {"match": [globs], "steps": [steps]}')
    if "extra" in cfg and not isinstance(cfg["extra"], list):
        raise bad("extra must be a list of steps")
    if "budget" in cfg:
        b = cfg["budget"]
        if not isinstance(b, dict) or not set(b) <= set(BUDGET_MAX):
            raise bad('budget must be {"gate": seconds, "edit": seconds}')
        for k, v in b.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= BUDGET_MAX[k]:
                raise bad(f"budget.{k} must be a number of seconds from 1 to {BUDGET_MAX[k]} (the hook's timeout "
                          "less a margin)")


def adapter_texts(root: Path, base: str | None, notes: list[str] | None = None) -> dict[str, str]:
    """name -> adapter JSON text. A work repository's adapters are read from the base commit when they are
    committed there, so a session (or a pull request, in CI) cannot loosen the gate by editing an adapter."""
    texts = {q.stem: q.read_text(encoding="utf-8") for q in sorted(ADAPTER_DIR.glob("*.json"))}
    if base:
        listing = git(root, "ls-tree", "--name-only", f"{base}:{ADAPTER_REL}") or ""
        for fname in filter(None, listing.splitlines()):
            if fname.endswith(".json"):
                committed = git(root, "show", f"{base}:{ADAPTER_REL}/{fname}", strip=False)
                if committed is not None:
                    if notes is not None and texts.get(fname[:-5]) not in (None, committed) \
                            and (root / ADAPTER_REL).resolve() == ADAPTER_DIR.resolve():
                        notes.append(f"{ADAPTER_REL}/{fname} changed in this session; the gate uses the version in {base[:12]}")
                    texts[fname[:-5]] = committed
    return texts


def load_adapter(root: Path, cfg: dict, base: str | None = None, notes: list[str] | None = None) -> dict:
    name = cfg.get("adapter")
    texts = adapter_texts(root, base, notes)
    try:
        parsed = {n: json.loads(t) for n, t in texts.items()}
    except json.JSONDecodeError as e:
        raise HarnessError(f"an adapter is not valid JSON: {e}") from e
    if not name:
        found = [a for _, a in sorted(parsed.items()) if any((root / d).exists() for d in a.get("detect", []))]
        if len(found) != 1:
            names = [a["name"] for a in found] or ["none"]
            raise HarnessError(f"cannot pick an adapter (detected: {', '.join(names)}); set \"adapter\" in {CONFIG_PATH}")
        adapter = found[0]
    else:
        if not re.fullmatch(r"[a-z0-9_-]+", str(name)) or name not in parsed:
            raise HarnessError(f"unknown adapter {name!r} (have: {', '.join(sorted(parsed))})")
        adapter = parsed[name]
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
    where = [(f"gate.{mode}", steps) for mode, steps in adapter["gate"].items()]
    where.append(("edit.steps", (adapter.get("edit") or {}).get("steps", [])))
    for label, steps in where:
        for s in steps:
            if not isinstance(s, dict) or not isinstance(s.get("cmd"), list) or not s["cmd"] \
                    or not all(isinstance(x, str) for x in s["cmd"]):
                raise HarnessError(f"{label}: each step needs a non-empty cmd list of strings: {s!r:.80}")
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
        p = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env={**clean_env(), **(env or {})}, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return {"name": name, "status": "cannot", "detail": f"cannot run {cmd[0]}: {e.strerror or e}"}
    try:
        stdout, stderr = p.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        try:  # the step's own session: its children (a test runner's workers) go too
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()
        p.communicate()
        return {"name": name, "status": "cannot", "detail": f"timed out after {budget:g}s"}
    out = (stdout + stderr).decode("utf-8", errors="replace").rstrip().splitlines()
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
    """Files this session changed: its own commits since base, plus dirty files whose content moved since first seen.

    Own commits are the first-parent, non-merge commits since base: what a merge brought in (a merged main) is
    not this session's. A rebase onto newer upstream commits still counts them (a limit)."""
    out: set[str] = set()
    if base:
        names = git(root, "log", "--format=", "--name-only", "--no-merges", "--first-parent", f"{base}..HEAD")
        out |= set(filter(None, (names or "").splitlines()))
    elif head(root):  # started before the first commit: the session made it, so all of it is the session's
        out |= set(filter(None, (git(root, "ls-tree", "-r", "--name-only", "HEAD") or "").splitlines()))
    for p in dirty_paths(root):
        if pending.get(p) != file_hash(root / p):
            out.add(p)
    return sorted(out)


def name_status(root: Path, rev: str) -> tuple[dict[str, str], list[str]]:
    """Between rev and the work tree: {new path: old path} for renames, and deleted paths."""
    out = git(root, "diff", "-M", "--name-status", "-z", rev, binary=True)
    renames, deleted = {}, []
    if not out:
        return renames, deleted
    parts = [x.decode("utf-8", errors="surrogateescape") for x in out.split(b"\0")]
    i = 0
    while i < len(parts) and parts[i]:
        code = parts[i]
        if code.startswith(("R", "C")) and i + 2 < len(parts):
            renames[parts[i + 2]] = parts[i + 1]
            i += 3
            continue
        if code.startswith("D") and i + 1 < len(parts):
            deleted.append(parts[i + 1])
        i += 2
    return renames, deleted


def changed_lines(root: Path, base: str | None, f: str, renames: dict[str, str]) -> tuple[list[str], list[str]]:
    """Lines the session added and removed in f: its own commits since base (following a rename) plus the work tree."""
    def split(diff: str) -> tuple[list[str], list[str]]:
        lines = diff.splitlines()
        return ([ln[1:] for ln in lines if ln.startswith("+") and not ln.startswith("+++")],
                [ln[1:] for ln in lines if ln.startswith("-") and not ln.startswith("---")])
    if not base:  # no commit to compare with: the whole file is new
        try:
            return (root / f).read_text(encoding="utf-8", errors="replace").splitlines(), []
        except OSError:
            return [], []
    added, removed = split(git(root, "log", "-p", "-U0", "--format=", "--no-merges", "--first-parent", "--follow",
                               f"{base}..HEAD", "--", f, strip=False) or "")
    src = renames.get(f)
    a2, r2 = split(git(root, "diff", "-M", "-U0", "HEAD", "--", *([src] if src else []), f, strip=False) or "")
    if not a2 and not r2 and git(root, "cat-file", "-e", f"HEAD:{src or f}") is None and (root / f).is_file():
        a2 = (root / f).read_text(encoding="utf-8", errors="replace").splitlines()  # untracked new file
    return added + a2, removed + r2


def weakenings(root: Path, adapter: dict, base: str | None, files: list[str]) -> list[str]:
    rules = adapter.get("weakening", [])
    if not files:
        return []
    found = []
    renames, deleted = name_status(root, base) if base else ({}, [])
    tests = adapter.get("test_files", [])
    for old in sorted(deleted + [o for n, o in renames.items() if not any(fnmatch.fnmatch(n, g) for g in tests)]):
        if any(fnmatch.fnmatch(old, g) for g in tests):
            found.append(f"{old}: test file removed or moved where the tests are not collected")
    for f in files:
        added, removed = changed_lines(root, base, f, renames)
        for r in rules:
            rx = re.compile(r.get("added") or r.get("removed"))
            plus = sum(1 for ln in added if rx.search(ln))
            minus = sum(1 for ln in removed if rx.search(ln))
            # a removed line counts only when it is not put back: rewriting an assertion is not removing one
            n = plus if "added" in r else max(0, minus - plus)
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
    source = payload.get("source", "startup")
    first: list[str] = []
    if source == "startup" or "baseline" not in state:
        if state.get("damaged"):
            first.append("The session state was damaged; the baseline restarts here, so earlier changes are not "
                         "counted as this session's.")
        # a resume or fork without state carries on someone's work: every changed file counts as this session's
        carried = source in ("resume", "fork") and not state.get("damaged")
        state = {"baseline": {"head": head(root), "at": time.time(),
                              "pending": {} if carried else {p: file_hash(root / p) for p in dirty_paths(root)},
                              "protected": protected_hashes(root)},
                 "strikes": 0, "last_pass": None}
        save_state(root, session, state)
        if carried and dirty_paths(root):
            first.append(f"This session continues another ({source}): files already changed count as its own.")
    base = state["baseline"]["head"]
    try:
        cfg, _, notes = read_config(root, base)
        adapter = load_adapter(root, cfg, base, notes)
    except HarnessError as e:
        msg = (f"[aok-harness] OFF: {e}. Edits are not checked; while this session has changes the end of a turn is "
               "blocked and pushes are refused, until a person fixes and commits the definition.")
        return say({"systemMessage": msg, **context("SessionStart", msg + " Run the project's checks yourself and tell the user the harness is off.")})
    d = delegate(adapter)
    if d:
        return relay(d, "session-start", payload, notice)
    lines = [f"[aok-harness] adapter {adapter['name']}: edit checks on {', '.join(adapter.get('edit', {}).get('match', [])) or 'nothing'}, "
             f"a gate of {', '.join(s.get('name', s['cmd'][0]) for s in adapter['gate']['quick'])} at the end of each turn, "
             f"and before git push / gh pr create."]
    lines += ["Tool versions (use these, not remembered ones): " + "; ".join(versions(adapter))]
    if adapter.get("delegate"):
        lines.append(f"{adapter['delegate']['bin']} is not installed, so the basic {adapter['name']} steps run instead; "
                     "its fuller checks (configuration integrity, push audit) are off.")
    lines += first + notes
    lines.append("Do not weaken a check to get green (suppressions, skipped tests, removed assertions, editing .aok/ or "
                 ".claude/settings.json); the user sees such changes. The gate blocks at most three times in a row; at the "
                 "third block, if you cannot fix the cause, report what still fails and end the turn.")
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
    try:
        base, _ = session_base(root, state)
        cfg, _, _ = read_config(root, base)
        adapter = load_adapter(root, cfg, base)
    except HarnessError as e:
        return say({"systemMessage": f"[aok-harness] edit checks did not run: {e}"})
    d = delegate(adapter)
    if d:
        return relay(d, "post-edit", payload, notice)
    try:
        rel = str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return 0  # outside the project
    edit = adapter.get("edit") or {}
    if not any(fnmatch.fnmatch(rel, m) or fnmatch.fnmatch(Path(rel).name, m) for m in edit.get("match", [])):
        return 0
    if not (root / rel).is_file():
        return 0
    deadline = time.monotonic() + float(cfg.get("budget", {}).get("edit", EDIT_BUDGET))
    results = []
    for s in edit.get("steps", []):  # the steps share one budget: together they must finish inside the hook's timeout
        left = deadline - time.monotonic()
        results.append(run_step(root, s, left, file=rel, env=adapter.get("env")) if left > 1 else
                       {"name": s.get("name", s["cmd"][0]), "status": "cannot", "detail": "edit budget used up"})
    bad = [r for r in results if r["status"] in ("fail", "cannot")]
    log(root, "post-edit", file=rel, result=[r["status"] for r in results])
    if not bad:
        return 0
    text = describe({"steps": bad})
    return say({"decision": "block", "reason": f"[aok-harness] checks failed after editing {rel}:\n{text}"})


def on_stop(root: Path, payload: dict, session: str) -> int:
    tasks = payload.get("background_tasks")
    agents = [t.get("type") for t in tasks if isinstance(t, dict) and t.get("type") in AGENT_TASKS] \
        if isinstance(tasks, list) else []
    if agents:  # agents still work on this turn; the real end comes later. A shell or monitor does not count
        log(root, "stop", result="skip-background", agents=agents)
        return say({"systemMessage": f"[aok-harness] gate not run yet: background agents are still working "
                                     f"({', '.join(agents)}); it runs when the turn really ends"})
    state = load_state(root, session)
    try:
        base, pending = session_base(root, state)
    except HarnessError as e:
        return strike(root, session, state, f"the gate cannot run: {e}")
    try:
        cfg, cfg_text, notes = read_config(root, base)
        adapter = load_adapter(root, cfg, base, notes)
    except HarnessError as e:
        if is_repo(root) and not changed_since(root, base, pending):  # nothing of this session's to judge
            log(root, "stop", result="skip-no-session-change", error=str(e)[:200])
            return 0
        return strike(root, session, state, f"the gate cannot run: {e}")
    d = delegate(adapter)
    if d:
        return relay(d, "stop", payload, lambda m: strike(root, session, state, m))
    fp = fingerprint(root, cfg_text)
    mine: list[str] = []
    gate = None
    if fp is not None:  # with git: skip a tree already passed and reviewed, and a session that changed nothing
        if fp == state.get("last_pass") and state.get("reviewed") == fp:
            log(root, "stop", result="skip-unchanged")
            return 0
        mine = changed_since(root, base, pending)
        if not mine and fp != state.get("last_pass"):
            log(root, "stop", result="skip-no-session-change")
            return 0
    if fp is None or fp != state.get("last_pass"):  # passed before a push in this turn: only its review is due
        gate = run_gate(root, adapter, "quick", float(cfg.get("budget", {}).get("gate", GATE_BUDGET)))
        log(root, "stop", result=gate["verdict"], steps=[(r["name"], r["status"]) for r in gate["steps"]])
        if gate["verdict"] != "pass":
            what = "failed" if gate["verdict"] == "fail" else "could not run"
            return strike(root, session, state, f"the quick gate {what}:\n{describe(gate)}\n\nThe whole tree is judged once "
                          "this session changed anything: if a failure is in code it did not touch and was already "
                          "there, tell the user instead of editing unrelated code.")
        fp = fingerprint(root, cfg_text)  # the tree as the gate left it (a step may write caches)
    state["last_pass"], state["reviewed"], state["strikes"] = fp, fp, 0
    report = notes + [f"weakened check: {w}" for w in weakenings(root, adapter, base, mine)]
    report += [f"harness file changed in this session: {f}"
               for f in sorted({f for f in mine if f.startswith(PROTECTED)} | set(protected_changes(root, state)))]
    told = set(state.get("told_missing", []))
    missing = [r["name"] for r in (gate or {}).get("steps", []) if r["status"] == "missing" and r["name"] not in told]
    if missing:  # said once per session: a skipped check must be visible, not repeated every turn
        report.append(f"optional step(s) skipped, tool not installed: {', '.join(missing)}")
        state["told_missing"] = sorted(told | set(missing))
    save_state(root, session, state)
    if report:
        return say({"systemMessage": "[aok-harness] gate passed. Review:\n- " + "\n- ".join(report)})
    return 0


def strike(root: Path, session: str, state: dict, reason: str) -> int:
    """Block the end of the turn, at most STRIKES times in a row; the stop after that is let through."""
    n = int(state.get("strikes") or 0) + 1
    if n > STRIKES:
        state["strikes"] = 0
        save_state(root, session, state)
        log(root, "stop", result="released", blocks=STRIKES)
        return say({"systemMessage": f"[aok-harness] the gate did NOT pass after {STRIKES} blocked attempts; the turn "
                                     f"ends so the session can report. Last result:\n{reason}"})
    state["strikes"] = n
    save_state(root, session, state)
    last = (" This is the last block: if you cannot fix the cause now, report what still fails (a dev child sends its "
            "parent a blocked envelope) and end the turn; that end is let through.") if n == STRIKES else ""
    return say({"decision": "block",
                "reason": f"[aok-harness] {reason}\n\nFix the cause (do not weaken the check), then end the turn again. "
                          f"Block {n} of {STRIKES}.{last}"})


def on_prompt(root: Path, payload: dict, session: str) -> int:
    state = load_state(root, session)
    if state.get("strikes"):
        state["strikes"] = 0
        save_state(root, session, state)
    try:
        base, _ = session_base(root, state)
        cfg, _, _ = read_config(root, base)
        d = delegate(load_adapter(root, cfg, base))
    except HarnessError:
        return 0  # SessionStart already said the harness is off
    return relay(d, "prompt", payload, notice) if d else 0


SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
SEPARATORS = set(";&|()\n")


def command_segments(cmd: str, depth: int = 0) -> list[list[str]] | None:
    """Every git or gh invocation the command may run, as word lists that start with git or gh.

    Simple commands are split at ; & | ( ) and newlines. An invocation is found wherever its word appears, so
    wrappers and assignments in front (env, time, sudo, xargs, FOO=1) do not hide it; `sh -c "..."` and `eval`
    are opened up, and so are $(...) and backticks, also inside double quotes. None when the command cannot be
    split (unbalanced quotes) or nests too deep."""
    if depth > 4:
        return None
    # a backslash before a newline continues the line (backticks are opened below, with $(...))
    lex = shlex.shlex(cmd.replace("\\\n", " "), posix=True, punctuation_chars=";&|()\n")
    lex.whitespace = " \t\r"  # a newline separates commands, so it is punctuation, not space
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        words = list(lex)
    except ValueError:
        return None
    segs, cur = [], []
    for w in words:
        if w and set(w) <= SEPARATORS:
            segs.append(cur)
            cur = []
        else:
            cur.append(w)
    segs.append(cur)
    inner_cmds = [a or b for a, b in SUBSTITUTION.findall(re.sub(r"'[^']*'", " ", cmd))]
    out: list[list[str]] = []
    for seg in filter(None, segs):
        for k, w in enumerate(seg):
            name = Path(w).name
            if name in SHELLS and "-c" in seg[k + 1:]:
                c = seg.index("-c", k + 1)
                if c + 1 < len(seg):
                    inner_cmds.append(seg[c + 1])
            elif name == "eval":
                inner_cmds.append(" ".join(seg[k + 1:]))
            elif name in ("git", "gh"):
                out.append(seg[k:])
    for inner in inner_cmds:
        more = command_segments(inner, depth + 1)
        if more is None:
            return None
        out += more
    return out


def git_subcommand(seg: list[str]) -> tuple[str | None, list[str]]:
    if not seg or Path(seg[0]).name != "git":
        return None, []
    i = 1
    while i < len(seg) and seg[i].startswith("-"):
        i += 2 if seg[i] in GIT_GLOBAL_WITH_ARG else 1
    return (seg[i], seg[i + 1:]) if i < len(seg) else (None, [])


def gh_pr_create(seg: list[str]) -> list[str] | None:
    """The arguments of `gh [global flags] pr create ...`, else None."""
    if not seg or Path(seg[0]).name != "gh":
        return None
    rest = [w for i, w in enumerate(seg[1:]) if not w.startswith("-") and not (i > 0 and seg[i] in ("-R", "--repo"))]
    if rest[:2] != ["pr", "create"]:
        return None
    return seg[seg.index("create") + 1:]


COMMIT_LONG_WITH_VALUE = {"--message", "--file", "--author", "--date", "--template", "--reuse-message",
                          "--reedit-message", "--fixup", "--squash", "--cleanup", "--trailer", "--pathspec-from-file"}


def short_flag(args: list[str], flag: str, with_value: str, long_with_value: set[str]) -> bool:
    """Whether a short flag appears, alone or in a cluster (-nm), before `--`. A letter in with_value takes a
    value: the rest of its cluster, or the next word when it ends the cluster (`-m -n` is the message "-n")."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return False
        if a in long_with_value:
            i += 2
            continue
        if a.startswith("-") and not a.startswith("--"):
            for j, ch in enumerate(a[1:], 1):
                if ch == flag:
                    return True
                if ch in with_value:
                    if j == len(a) - 1:
                        i += 1  # the value is the next word
                    break
        i += 1
    return False


def hooks_off(cmd: str) -> str | None:
    """Why a command switches git hooks off, else None. The text check is broad on purpose (a message that merely
    mentions --no-verify is refused too); the word check catches `commit -n` and its clusters."""
    segs = command_segments(cmd)
    if NO_VERIFY.search(cmd) and (segs is None or any(Path(sg[0]).name == "git" for sg in segs)):
        return "this command switches git hooks off (--no-verify / core.hooksPath); run it without that"
    for seg in segs or []:
        sub, args = git_subcommand(seg)
        if sub == "commit" and short_flag(args, "n", "mFcCt", COMMIT_LONG_WITH_VALUE):  # commit -n is --no-verify (push -n is a dry run)
            return "this commit switches git hooks off (-n is --no-verify); run it without that"
    return None


def publish_problems(root: Path, cmd: str) -> tuple[bool, list[str]]:
    """Whether the command publishes, and why it may not: the gate checks the checked-out tree, so a publishing
    command may only publish that tree, and nothing in the same command may move HEAD first."""
    segs = command_segments(cmd)
    if segs is None:
        return bool(PUBLISH.search(cmd)), ["the command could not be read (unbalanced quotes); publish in a plain command"] \
            if PUBLISH.search(cmd) else []
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD") or ""
    ok_refs = {"HEAD", branch, f"refs/heads/{branch}"} - {""}
    publishes, problems, movers = False, [], []
    for seg in segs:
        sub, args = git_subcommand(seg)
        if sub in MOVES_HEAD:
            movers.append(sub)
        if sub == "push":
            publishes = True
            if {"--all", "--mirror", "--tags"} & set(args):
                problems.append("push the checked-out branch only (no --all, --mirror or --tags): the gate checked that tree")
            positional = [a for a in args if not a.startswith("-")]
            for ref in positional[1:]:
                src = ref.lstrip("+").split(":", 1)[0]
                if src not in ok_refs:
                    problems.append(f"push the checked-out branch only: {ref!r} is not {branch or 'HEAD'}, and the gate "
                                    "checked the checked-out tree")
        pr = gh_pr_create(seg)
        if pr is not None:
            publishes = True
            for flag in ("--head", "-H"):
                if flag in pr and pr.index(flag) + 1 < len(pr):
                    head_ref = pr[pr.index(flag) + 1].split(":")[-1]
                    if head_ref != branch:
                        problems.append(f"open the pull request from the checked-out branch: --head {head_ref!r} is not "
                                        f"{branch or 'HEAD'}")
    if publishes and movers:
        problems.append(f"{', '.join(sorted(set(movers)))} and publishing in one command: the gate sees the tree before "
                        "the command runs. Run them first, then publish in its own command")
    return publishes, problems


def deny(reason: str) -> int:
    return say({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"[aok-harness] {reason}"}})


def on_pre_bash(root: Path, payload: dict, session: str) -> int:
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = str(tool_input(payload).get("command") or "")
    off = hooks_off(cmd)
    if off:
        return deny(off)
    publishes, problems = publish_problems(root, cmd)
    if not publishes:
        return 0
    if problems:
        return deny("; ".join(problems))
    state = load_state(root, session)
    try:
        base, pending = session_base(root, state)
        cfg, cfg_text, _ = read_config(root, base)
        adapter = load_adapter(root, cfg, base)
    except HarnessError as e:
        return deny(f"the gate cannot run, so nothing is published: {e}")
    mine = changed_since(root, base, pending)
    touched = sorted({f for f in mine if f.startswith(PROTECTED)} | set(protected_changes(root, state)))
    if touched:
        return deny("the harness, its settings or the CI workflows changed in this session (" + ", ".join(touched[:5]) +
                    "); a person reviews and publishes such a change")
    d = delegate(adapter)
    if d:
        return relay(d, "pre-bash", payload, lambda m: deny(f"{m}; nothing is published"))
    dirty = dirty_paths(root)
    if dirty:
        return deny(f"there are uncommitted changes ({', '.join(dirty[:5])}{', ...' if len(dirty) > 5 else ''}), so the "
                    "gate would check a different tree than the commit being pushed. Commit them (or remove them, or "
                    "ignore files a check leaves behind) in one command, then push in another")
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


def notice(msg: str) -> int:
    return say({"systemMessage": f"[aok-harness] {msg}"})


def relay(d: dict, event: str, payload: dict, fail) -> int:
    """Hand the hook to the delegate. Its answer passes through when it exits 0 or 2 (the two codes Claude Code
    reads as an answer); anything else (cannot start, timeout, a crash such as a Rust panic's 101) is `fail`."""
    cmd = [a.replace("{event}", d["events"][event]) for a in d["hook"]]
    try:
        p = subprocess.run(cmd, input=json.dumps(payload).encode(), capture_output=True,
                           timeout=int(HOOK_TIMEOUT[event] * 0.8))  # inside the hook's own timeout
    except (OSError, subprocess.TimeoutExpired) as e:
        return fail(f"{d['bin']} hook {event} did not run ({e})")
    if p.returncode not in (0, 2):
        err = p.stderr.decode("utf-8", errors="replace").strip().splitlines()[-5:]
        return fail(f"{d['bin']} hook {event} failed (exit {p.returncode})" + (": " + " | ".join(err) if err else ""))
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
        if event == "pre-bash":  # the plain text test only: the parsing may be what crashed
            if PUBLISH.search(str(tool_input(payload).get("command") or "")):
                return deny(f"the harness crashed ({e!r:.200}); nothing is published")
            return 0
        if event == "stop":
            try:
                return strike(root, session, load_state(root, session), f"the harness crashed: {e!r:.300}")
            except Exception as e2:  # noqa: BLE001 — even the strike count is broken: block; Claude Code's cap ends it
                return say({"decision": "block", "reason": f"[aok-harness] the harness crashed ({e!r:.200}; "
                                                           f"{e2!r:.200}); the gate did not run. Report this."})
        return notice(f"{event} hook crashed: {e!r:.300}")


# ---------------------------------------------------------------- verify and install

def verify(root: Path, mode: str, as_json: bool, base: str | None = None) -> int:
    try:
        if base:
            sha = git(root, "rev-parse", "--verify", "-q", f"{base}^{{commit}}")
            if not sha:
                raise HarnessError(f"--base {base} is not a commit here")
            base = sha
        cfg, _, notes = read_config(root, base)
        adapter = load_adapter(root, cfg, base, notes)
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
    def h(event: str, **extra) -> list:
        return [{"type": "command", "command": f"{cmd_prefix} hook {event}", "timeout": HOOK_TIMEOUT[event], **extra}]
    return {
        "SessionStart": [{"hooks": h("session-start")}],
        "UserPromptSubmit": [{"hooks": h("prompt")}],
        "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": h("post-edit")}],
        # a pre-bash hook that cannot start or times out blocks the command instead of letting a push through
        # (Claude Code v2.1.295+; the field has no effect on Stop, whose failures the core turns into blocks itself)
        "PreToolUse": [{"matcher": "Bash", "hooks": h("pre-bash", onFailure="block")}],
        "Stop": [{"hooks": h("stop")}],
    }


def is_harness_hook(command: str, event: str) -> bool:
    """An existing hook entry that already runs this harness for event, whatever interpreter or quoting it uses."""
    return "aok_harness.py" in command and re.search(rf"\bhook\s+{re.escape(event)}\b", command) is not None


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
    shape = [] if isinstance(merged, dict) else ["the file is not a JSON object"]
    if not shape:
        if not isinstance(merged.get("hooks", {}), dict) or not all(isinstance(v, list) for v in merged.get("hooks", {}).values()):
            shape.append('"hooks" is not an object of event lists')
        if not isinstance(merged.get("permissions", {}), dict) or not isinstance(merged.get("permissions", {}).get("ask", []), list):
            shape.append('"permissions.ask" is not a list')
    if shape:
        print(f"install: {settings}: {'; '.join(shape)}; merge by hand", file=sys.stderr)
        return 2
    if config.is_file():
        try:
            have = json.loads(config.read_text(encoding="utf-8")).get("adapter")
        except (json.JSONDecodeError, AttributeError):
            have = None
        if have != adapter_name:
            print(f"install: {config} already sets adapter {have!r}, not {adapter_name!r}; change it by hand "
                  "(install does not overwrite it)", file=sys.stderr)
            return 2
    hooks = merged.setdefault("hooks", {})
    for ev, entries in hook_settings(prefix).items():
        existing = hooks.setdefault(ev, [])
        event = entries[0]["hooks"][0]["command"].rsplit(" ", 1)[1]
        commands = [h.get("command", "") for e in existing if isinstance(e, dict)
                    for h in (e.get("hooks") if isinstance(e.get("hooks"), list) else []) if isinstance(h, dict)]
        if not any(is_harness_hook(c, event) for c in commands):
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
