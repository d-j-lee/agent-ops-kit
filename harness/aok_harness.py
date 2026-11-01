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
  git-pre-push          git's pre-push hook (session-start installs it): in what Claude
                        Code spawns (CLAUDE_CODE_CHILD_SESSION), refuse to push a ref
                        whose tip the gate has not passed on a clean tree
  install --repo DIR --adapter NAME [--yes]
                        copy the harness into DIR/.aok/harness and add the hooks
                        to DIR/.claude/settings.json. Without --yes it only
                        prints the plan: changing a repository's settings needs
                        a person's confirmation (AOK S4)

What the hooks do
  session-start  On `startup`, or when this session id has no usable state yet,
                 record the baseline (HEAD, the refs that exist, hashes of files
                 already changed and of the harness files) and install the git
                 pre-push hook in the git directory. A session that continues another one
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
                 removed assertions, a root file shadowing a standard module) and
                 changes to the harness files. A changed pre-push hook is put back
                 and said; core.hooksPath set during the session blocks. While
                 background agents (subagents, teammates, ...) still run, it
                 leaves the gate to the real end; a background shell does not
                 count.
  prompt         The user wrote: reset the strike count.
  pre-bash       Every command: deny one that switches git hooks off (--no-verify
                 and its abbreviations, commit -n, core.hooksPath; unsetting it is
                 let through) or changes what the pre-push hook sees
                 (CLAUDE_CODE_CHILD_SESSION, CLAUDECODE, env -i, GIT_CONFIG_*, files
                 under .git/hooks, a shell alias), and
                 put a changed pre-push hook back first. For a command that
                 publishes (git push or an alias of it, gh pr create; send-pack is
                 refused outright): deny it when it publishes anything but the
                 checked-out branch, moves HEAD or a branch in the same command,
                 right after the hook was put back or while it cannot run, when
                 the harness files, its settings or the CI workflows changed in
                 this session, when uncommitted changes mean the gate would not see
                 what is pushed, or when the quick gate fails or cannot run. A pass
                 records the commit for the pre-push hook, which then judges the
                 tip of each ref git actually sends.

The tree judged is the one Claude works in: Claude Code keeps CLAUDE_PROJECT_DIR
where the session started and passes the current directory as the hook input's
cwd, so a session that moves into another worktree of the same repository is
judged there, with the baseline it started with.

The gate definition (.aok/harness.json) and the adapters copied into
.aok/harness/adapters are read from the commit the session started on, not from
the work tree, and a definition that differs is reported. A guardrail, not a
security boundary: an agent can still edit the harness code, its state or local
settings, and a script can change the hook's view and push in one run; the push
refusal, the restore and the report put such changes in front of a person. CI on
a clean checkout, running the base branch's harness, is the gate of record.

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
import tempfile
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
PUBLISH = re.compile(r"\bgit\b(?:\s+\S+)*?\s+(?:push|send-pack|http-push)\b|\bgh\b(?:\s+\S+)*?\s+pr\s+(?:create|new)\b")
# git takes any unambiguous prefix of a long option (--no-veri), and its config keys ignore case
NO_VERIFY = re.compile(r"--no-v(?:e(?:r(?:i(?:fy?)?)?)?)?(?![\w-])|hookspath", re.I)
GIT_GLOBAL_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
# commands that move HEAD or the branches a push sends: run in the same command as a push, the gate saw another tree
MOVES_HEAD = {"merge", "rebase", "reset", "checkout", "switch", "commit", "cherry-pick", "pull", "am", "revert", "stash",
              "update-ref", "symbolic-ref", "branch", "tag", "restore", "worktree", "filter-branch", "replace"}
# options with which `git branch` / `git tag` only list; with any other option or a name they create, move or delete
LISTING_COMMON = {"-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--format",
                  "--sort", "--column", "--no-column", "--color", "--no-color", "-i", "--ignore-case"}
BRANCH_LISTING = LISTING_COMMON | {"--show-current", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
                                   "--abbrev", "--no-abbrev"}
TAG_LISTING = LISTING_COMMON | {"-n", "-v", "--verify"}  # -n<lines> too
LIST_MODE = {"-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "-v", "--verify"}
# git's own commands: an alias can only be another name (git runs a builtin before an alias of the same name)
GIT_BUILTINS = {"status", "log", "diff", "show", "add", "commit", "push", "pull", "fetch", "merge", "rebase", "reset",
                "checkout", "switch", "branch", "tag", "stash", "config", "rev-parse", "ls-files", "grep", "blame",
                "remote", "init", "clone", "mv", "rm", "restore", "cherry-pick", "revert", "worktree", "submodule",
                "describe", "shortlog", "reflog", "clean", "apply", "am", "format-patch", "bisect", "notes", "gc",
                "fsck", "cat-file", "ls-tree", "for-each-ref", "update-ref", "symbolic-ref", "rev-list", "name-rev",
                "send-pack", "http-push", "filter-branch", "replace", "help", "version", "var", "archive",
                "range-diff", "sparse-checkout", "maintenance", "check-ignore", "hash-object", "merge-base",
                "ls-remote", "show-ref", "whatchanged", "difftool", "mergetool", "citool", "gui", "request-pull"}
# what an environment change in a command can do to the pre-push hook (it judges only when CLAUDE_CODE_CHILD_SESSION,
# which Claude Code sets in what it spawns, is set; CLAUDECODE is kept as well, for tools that read it) or to
# git's configuration (hooks switched off, aliases defined where the command reading cannot see them)
HOOK_VARS = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION")
_HV = r"(?:CLAUDECODE|CLAUDE_CODE_CHILD_SESSION)"
CLAUDECODE_CHANGE = re.compile(rf"(?:^|[\s;&|(`$])(?:export\s+|declare\s+-x\s+)?{_HV}=|"
                               rf"\bunset\b[^;&|\n]*\b{_HV}\b|(?:^|\s)(?:-u\s*|--unset[=\s]){_HV}\b|"
                               rf"\bexport\s+-n\b[^;&|\n]*\b{_HV}\b")
CLEAR_ENV = re.compile(r"\benv(?:\s+(?:-u\s*\S+|--unset=\S+|-[^\s]+))*?\s+(?:-i|--ignore-environment|-)(?=\s|$)")
GIT_CONFIG_ENV = re.compile(r"\bGIT_CONFIG(?:_GLOBAL|_SYSTEM|_PARAMETERS|_COUNT|_KEY_\w+|_VALUE_\w+)?=|--config-env\b")
GIT_HOOK_FILES = re.compile(r"\.git/hooks\b|\bhooks/pre-push\b")
PUBLISHING_WORDS = re.compile(r"\b(?:push|send-pack|http-push)\b|\bpr\s+(?:create|new)\b")
PUSH_ALL = {"--all", "--branches", "--mirror", "--tags", "--follow-tags"}
PUSH_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
REDIRECT = re.compile(r"^\d*(?:>>?|<<?<?|>&|<&|&>>?|>\|)(.*)$")  # an operator, with its target attached or not
SHELLS = {"bash", "sh", "zsh", "dash"}


class HarnessError(Exception):
    """The harness could not do its job (configuration unreadable, adapter unknown)."""


# ---------------------------------------------------------------- git and files

def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in GIT_REDIRECT}


def git(root: Path, *args: str, binary: bool = False, strip: bool = True, stdin: str | None = None):
    try:
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, env=clean_env(), timeout=60,
                           input=stdin.encode() if stdin is not None else None,
                           stdin=None if stdin is not None else subprocess.DEVNULL)
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


def settings_digest(text: str | None) -> str:
    """A Claude Code settings file's text (None: no file), by the keys that matter to the harness only. Claude Code
    itself writes permission rules into .claude/settings.local.json when the user chooses "don't ask again", and that
    must not read as tampering; a missing file and one without those keys switch off the same nothing."""
    try:
        data = json.loads(text) if text is not None else {}
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return "raw:" + hashlib.sha256((text or "").encode("utf-8", errors="surrogateescape")).hexdigest()
    watched = {k: data[k] for k in SETTINGS_WATCHED if k in data}
    return "settings:" + hashlib.sha256(json.dumps(watched, sort_keys=True).encode()).hexdigest()


def settings_hash(path: Path) -> str:
    try:
        return settings_digest(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return settings_digest(None)
    except (OSError, ValueError):  # unreadable or not UTF-8: by its bytes
        return file_hash(path)


def is_settings(f: str) -> bool:
    parts = Path(f).parts
    return len(parts) >= 2 and parts[-2] == ".claude" and parts[-1].startswith("settings")


def main_checkout(root: Path) -> Path | None:
    """The main checkout of a linked worktree (Claude Code reads its local settings from there), else None."""
    common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    own = git(root, "rev-parse", "--absolute-git-dir")
    if common and own and Path(common) != Path(own) and Path(common).name == ".git":
        return Path(common).parent
    return None


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
    main = main_checkout(root)
    if main is not None:
        files.add(str(main / ".claude" / "settings.local.json"))
    return {f: settings_hash(root / f) if is_settings(f) else file_hash(root / f) for f in sorted(files)}


def protected_changes(root: Path, state: dict) -> list[str]:
    before = (state.get("baseline") or {}).get("protected")
    if not isinstance(before, dict):
        return []
    now = protected_hashes(root)
    return sorted(f for f in set(before) | set(now) if before.get(f, "missing") != now.get(f, "missing"))


def harness_changes(root: Path, base: str | None, mine: list[str], state: dict) -> list[str]:
    """The harness files this session changed: its own changes under PROTECTED (a settings file only when the keys
    the harness watches differ from the base commit's), and any protected file whose hash moved, ignored ones too."""
    out = set(protected_changes(root, state))
    for f in mine:
        if not f.startswith(PROTECTED):
            continue
        if is_settings(f):
            before = git(root, "show", f"{base}:{f}", strip=False) if base else None
            if settings_digest(before) == settings_hash(root / f):
                continue
        out.add(f)
    return sorted(out)


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


PASSED_MAX = 500
HOOK_MARK = "# aok-harness pre-push"


def passed_commits(root: Path) -> list[str]:
    try:
        data = json.loads((state_dir(root) / "passed.json").read_text(encoding="utf-8"))
        return [c for c in data if isinstance(c, str)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record_passed(root: Path) -> None:
    """HEAD passed the gate on a clean tree: its commit may be pushed (the pre-push hook checks this list)."""
    h = head(root)
    if not h or dirty_paths(root):
        return
    commits = [c for c in passed_commits(root) if c != h] + [h]
    d = state_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".passed.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(commits[-PASSED_MAX:]), encoding="utf-8")
    tmp.replace(d / "passed.json")


def hooks_dir(root: Path) -> Path | None:
    """Where git looks for this repository's hooks; None when core.hooksPath hands them to something else."""
    if git(root, "config", "--get", "core.hooksPath"):
        return None
    d = git(root, "rev-parse", "--git-path", "hooks")
    return (root / d).resolve() if d else None


def pre_push_script(core: str | None = None) -> str:
    return ("#!/bin/sh\n"
            f"{HOOK_MARK}: inside Claude Code, only commits the AOK harness gate passed may be pushed.\n"
            "# Outside Claude Code's own subprocesses (a person in a terminal, or in an IDE's terminal) it does nothing.\n"
            '[ -n "$CLAUDE_CODE_CHILD_SESSION" ] || exit 0\n'
            f'exec python3 -I {shlex.quote(core or str(Path(__file__).resolve()))} git-pre-push "$@"\n')


def harness_cores(root: Path) -> set[str]:
    """The cores the hook may run: this one, and the installed copy in each worktree of the repository (they share
    one hooks directory, so a session in another worktree may have written the hook)."""
    cores = {str(Path(__file__).resolve())}
    for line in (git(root, "worktree", "list", "--porcelain") or "").splitlines():
        if line.startswith("worktree "):
            cores.add(str((Path(line[len("worktree "):]) / ".aok" / "harness" / "aok_harness.py").resolve()))
    return cores


def hook_is_harness(root: Path, text: str) -> bool:
    """Whether a pre-push hook's text is the harness's script, running one of harness_cores."""
    head_, _, _ = pre_push_script("CORE").partition("exec ")
    m = re.fullmatch(re.escape(head_) + r'exec python3 -I (.+) git-pre-push "\$@"\n', text)
    if not m:
        return False
    try:
        words = shlex.split(m.group(1))
    except ValueError:
        return False
    return len(words) == 1 and Path(words[0]).is_file() and str(Path(words[0]).resolve()) in harness_cores(root)


def hook_text(d: Path) -> str | None:
    try:
        return (d / "pre-push").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def write_pre_push(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "pre-push").write_text(pre_push_script(), encoding="utf-8")
    (d / "pre-push").chmod(0o755)


def ensure_pre_push(root: Path) -> str:
    """Install or refresh the harness's git pre-push hook (in the git directory, not the work tree). The line to tell.
    The marker file says the hook is the harness's to keep: without it nothing restores or demands the hook."""
    marker = state_dir(root) / "pre-push.installed"
    d = hooks_dir(root)
    try:
        if d is None:
            marker.unlink(missing_ok=True)
            return ("core.hooksPath is set, so the harness does not install its git pre-push hook; pushes are checked "
                    "only by reading the command.")
        hook = d / "pre-push"
        text = hook_text(d)
        if text is not None and HOOK_MARK not in text:
            marker.unlink(missing_ok=True)  # someone else's hook: never overwritten
            return (f"A git pre-push hook that is not the harness's is installed ({hook}); pushes are checked only by "
                    "reading the command.")
        if text is None or not hook_is_harness(root, text):
            write_pre_push(d)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(hook), encoding="utf-8")
    except OSError as e:
        return f"The harness could not install its git pre-push hook ({e}); pushes are checked only by reading the command."
    return ("A git pre-push hook (in the git directory, not the work tree) refuses, inside Claude Code, a push of any "
            "commit the gate has not passed.")


def restore_pre_push(root: Path) -> str | None:
    """Before every shell command: put back the harness's pre-push hook if this session's command changed or removed
    it (a script the command reading cannot see). What to tell, else None; pre_push_problem reports what stays broken."""
    if not (state_dir(root) / "pre-push.installed").is_file():
        return None
    d = hooks_dir(root)
    if d is None:
        return None  # core.hooksPath: the harness does not undo configuration; pre-bash and stop refuse instead
    text = hook_text(d)
    if text is not None and hook_is_harness(root, text):
        return None
    try:
        write_pre_push(d)
    except OSError:
        return None
    log(root, "pre-bash", result="pre-push-restored")
    return "the harness's git pre-push hook was removed or changed in this session; it is restored now"


def pre_push_problem(root: Path) -> str | None:
    """Why the installed pre-push hook can no longer be trusted, else None."""
    marker = state_dir(root) / "pre-push.installed"
    if not marker.is_file():
        return None
    d = hooks_dir(root)
    if d is None:
        return ("core.hooksPath was set in this session, so git no longer runs the harness's pre-push hook; unset it "
                "(git config --unset core.hooksPath), or tell the user if a person set it")
    text = hook_text(d)
    intact = text is not None and hook_is_harness(root, text)
    return None if intact else ("the harness's git pre-push hook was removed or changed and could not be restored; "
                                "start a new session to restore it")


def git_pre_push(root: Path) -> int:
    """git's pre-push hook: each line on stdin is `<local ref> <local sha> <remote ref> <remote sha>`."""
    passed = set(passed_commits(root))
    refused = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4 or set(parts[1]) == {"0"}:  # a deletion sends no commit
            continue
        if parts[1] not in passed:
            refused.append(f"{parts[0]} -> {parts[2]} ({parts[1][:12]})")
    if refused:
        print("[aok-harness] push refused: the gate has not passed these commits in this repository: " +
              ", ".join(refused) + ". Check the commit out and let the gate pass (end the turn, or push from it), "
              "then push. A person can push from a terminal outside Claude Code.", file=sys.stderr)
        return 1
    return 0


def start_refs(state: dict) -> list[str]:
    refs = (state.get("baseline") or {}).get("refs")
    return [r for r in refs if isinstance(r, str)] if isinstance(refs, list) else []


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
    tmp.write_text(json.dumps(state, ensure_ascii=True, indent=1), encoding="utf-8")
    tmp.replace(d / f"session-{safe_id(session)}.json")


def log(root: Path, event: str, **fields) -> None:
    try:
        d = state_dir(root)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "event": event, **fields}, ensure_ascii=True) + "\n")
    except (OSError, ValueError):
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
                    or not all(isinstance(x, str) for x in s["cmd"]) \
                    or not isinstance(s.get("optional", False), bool) or not isinstance(s.get("name", ""), str):
                raise HarnessError(f"{label}: each step needs a non-empty cmd list of strings, a name that is a string "
                                   f"and optional true or false: {s!r:.80}")
    match = (adapter.get("edit") or {}).get("match", [])
    if not isinstance(match, list) or not all(isinstance(m, str) for m in match):
        raise HarnessError(f"edit.match must be a list of file patterns: {match!r:.80}")
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

def own_revs(base: str | None, refs: list[str] | None) -> str:
    """git --stdin revisions of the session's own commits: from HEAD, none that the base or any ref at session start
    (other branches, remote-tracking refs) already reached. A branch the session made and merged is its own; a branch
    that was there before is not. Commits fetched during the session and merged in count as its own (a limit)."""
    return "\n".join(["HEAD", *[f"^{r}" for r in sorted(set(refs or []) | ({base} if base else set()))]]) + "\n"


def changed_since(root: Path, base: str | None, pending: dict, refs: list[str] | None = None) -> list[str]:
    """Files this session changed: its own commits (own_revs), plus dirty files whose content moved since first seen."""
    out: set[str] = set()
    if base:
        names = git(root, "log", "--format=", "--name-only", "--no-merges", "--stdin", stdin=own_revs(base, refs))
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


def changed_lines(root: Path, base: str | None, f: str, renames: dict[str, str],
                  refs: list[str] | None = None) -> tuple[list[str], list[str]]:
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
    added, removed = split(git(root, "log", "-p", "-U0", "--format=", "--no-merges", "--follow", "--stdin", "--", f,
                               strip=False, stdin=own_revs(base, refs)) or "")
    src = renames.get(f)
    a2, r2 = split(git(root, "diff", "-M", "-U0", "HEAD", "--", *([src] if src else []), f, strip=False) or "")
    if not a2 and not r2 and git(root, "cat-file", "-e", f"HEAD:{src or f}") is None and (root / f).is_file():
        a2 = (root / f).read_text(encoding="utf-8", errors="replace").splitlines()  # untracked new file
    return added + a2, removed + r2


def test_file(adapter: dict, path: str, root: Path | None = None) -> bool:
    """Whether the test runner collects path: it matches test_files and test_name_re, and (with root) every directory
    on the way holds test_package_marker (unittest does not look into a directory that is not a package)."""
    if not any(fnmatch.fnmatch(path, g) for g in adapter.get("test_files", [])):
        return False
    name_re = adapter.get("test_name_re")
    if name_re and not re.fullmatch(name_re, Path(path).name):
        return False
    marker = adapter.get("test_package_marker")
    if marker and root is not None:
        return all((root / d / marker).is_file() for d in Path(path).parents if str(d) != ".")
    return True


def weakenings(root: Path, adapter: dict, base: str | None, files: list[str], refs: list[str] | None = None) -> list[str]:
    rules = adapter.get("weakening", [])
    if not files:
        return []
    found = []
    renames, deleted = name_status(root, base) if base else ({}, [])
    for old in sorted(deleted + [o for n, o in renames.items() if not test_file(adapter, n, root)]):
        if test_file(adapter, old):
            found.append(f"{old}: test file removed or moved where the tests are not collected")
    settings = sorted(f for f in files if any(fnmatch.fnmatch(f, g) for g in adapter.get("check_config", [])))
    if settings:
        found.append(f"check settings changed in this session, review what they switch off: {', '.join(settings)}")
    if adapter.get("shadow_check") == "python-stdlib":  # the test run puts the repository root first on sys.path
        for f in files:
            p = Path(f)
            name = p.stem if len(p.parts) == 1 and p.suffix == ".py" else \
                p.parts[0] if len(p.parts) == 2 and p.name == "__init__.py" else None
            if name in sys.stdlib_module_names and (root / f).exists():
                found.append(f"{f}: shadows the standard module {name} (the tests import it from the repository root)")
    for f in files:
        added, removed = changed_lines(root, base, f, renames, refs)
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
        print(json.dumps(obj, ensure_ascii=True))  # a file name that is not UTF-8 must not break the answer
    return 0


def context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def toplevel(p) -> Path | None:
    if not isinstance(p, str) or not p or not Path(p).is_dir():
        return None
    top = git(Path(p), "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def common_dir(root: Path) -> Path | None:
    c = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(c).resolve() if c else None


def project_roots(payload: dict) -> tuple[Path, Path]:
    """(the tree to judge, the session's project directory). Claude Code keeps CLAUDE_PROJECT_DIR where the session
    started and passes the directory Claude works in as the input's cwd, after `cd` and after entering a worktree
    (hooks docs). A cwd in another worktree of the same repository is judged there; a subdirectory, another
    repository or no repository leaves the project directory."""
    start = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    proj = toplevel(start) or Path(start)
    here = toplevel(payload.get("cwd"))
    if here is not None and here.resolve() != proj.resolve():
        mine = common_dir(here)
        if mine is not None and mine == common_dir(proj):
            return here, proj
    return proj, proj


def inherit_session(root: Path, proj: Path, session: str) -> None:
    """The session moved from its project directory into another worktree of the same repository. Start that
    worktree's state from the session's baseline: its base commit and the refs at its start (so commits made before
    the session stay out), every file changed there counted as the session's, and the HEAD found there now, so a
    worktree the session has not touched is not judged. The pre-push hook's marker comes along (one hooks directory)."""
    if load_state(root, session) or not is_repo(root):
        return
    src = load_state(proj, session)
    b = src.get("baseline")
    if src.get("damaged") or not isinstance(b, dict):
        return  # nothing to carry over: session_base refuses there, as for any session without a usable baseline
    state = {"baseline": {"head": b.get("head"), "at": time.time(), "pending": {}, "protected": protected_hashes(root),
                          "refs": start_refs(src), "seen_head": head(root), "from": str(proj)},
             "strikes": 0, "last_pass": None}
    save_state(root, session, state)
    marker = state_dir(proj) / "pre-push.installed"
    if marker.is_file():
        (state_dir(root) / "pre-push.installed").write_text(marker.read_text(encoding="utf-8"), encoding="utf-8")
    log(root, "inherit", source=str(proj))


def start_head(state: dict, base: str | None) -> str | None:
    """HEAD when the session started here: the base commit, or for a worktree it moved into, the HEAD found there."""
    seen = (state.get("baseline") or {}).get("seen_head")
    return seen if isinstance(seen, str) else base


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
        refs = (git(root, "for-each-ref", "--format=%(objectname)") or "").split()
        state = {"baseline": {"head": head(root), "at": time.time(),
                              "pending": {} if carried else {p: file_hash(root / p) for p in dirty_paths(root)},
                              "protected": protected_hashes(root), "refs": sorted(set(refs))},
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
    if is_repo(root):
        lines.append(ensure_pre_push(root))
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
        if is_repo(root) and head(root) == start_head(state, base) \
                and not changed_since(root, base, pending, start_refs(state)):
            log(root, "stop", result="skip-no-session-change", error=str(e)[:200])
            return 0
        return strike(root, session, state, f"the gate cannot run: {e}")
    d = delegate(adapter)
    if d:
        return relay(d, "stop", payload, lambda m: strike(root, session, state, m))
    restored = restore_pre_push(root)
    hook_problem = pre_push_problem(root)
    if hook_problem:  # git push would no longer be judged, whatever this turn changed
        return strike(root, session, state, hook_problem)
    fp = fingerprint(root, cfg_text)
    mine: list[str] = []
    gate = None
    if fp is not None:  # with git: skip a tree already passed and reviewed, and a session that changed nothing
        if fp == state.get("last_pass") and state.get("reviewed") == fp:
            log(root, "stop", result="skip-unchanged")
            return notice(restored) if restored else 0
        mine = changed_since(root, base, pending, start_refs(state))
        if not mine and fp != state.get("last_pass") and head(root) == start_head(state, base):  # HEAD moved: judge
            log(root, "stop", result="skip-no-session-change")
            return notice(restored) if restored else 0
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
    if fp is not None:
        record_passed(root)
    report = notes + [f"weakened check: {w}" for w in weakenings(root, adapter, base, mine, start_refs(state))]
    report += [f"harness file changed in this session: {f}" for f in harness_changes(root, base, mine, state)]
    if restored:
        report.append(restored)
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


def fallback_strike(root: Path, session: str, reason: str) -> int:
    """strike() when the session state cannot be written: the count goes to the temp directory, so the exit after
    STRIKES blocks still holds. If that fails too, block; Claude Code's own cap on continued turns ends it."""
    f = Path(tempfile.gettempdir()) / f"aok-harness-{hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]}" \
        f"-{safe_id(session)}.strikes"
    try:
        n = int(f.read_text(encoding="utf-8") or 0) + 1 if f.is_file() else 1
    except (OSError, ValueError):
        n = 1
    try:
        if n > STRIKES:
            f.unlink(missing_ok=True)
            return say({"systemMessage": f"[aok-harness] the gate did NOT pass after {STRIKES} blocked attempts; the "
                                         f"turn ends so the session can report. Last result:\n{reason}"})
        f.write_text(str(n), encoding="utf-8")
    except OSError:
        n = 0
    last = " This is the last block: report what still fails and end the turn; that end is let through." \
        if n == STRIKES else ""
    return say({"decision": "block", "reason": f"[aok-harness] {reason}. Report this.\n\n" +
                (f"Block {n} of {STRIKES}.{last}" if n else "The block count cannot be kept either.")})


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


def outside_single_quotes(cmd: str) -> str:
    """The command with the inside of single-quoted strings blanked: the shell runs $(...) and backticks everywhere
    else, double quotes included (where an apostrophe is just a character)."""
    out, quote, i = [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote == "'":
            out.append(" " if ch != "'" else ch)
            quote = None if ch == "'" else quote
        elif ch == "\\" and quote != "'" and i + 1 < len(cmd):
            out.append(cmd[i:i + 2])
            i += 1
        elif ch in "'\"" and quote is None:
            quote = ch
            out.append(ch)
        elif ch == '"' and quote == '"':
            quote = None
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def drop_redirections(seg: list[str]) -> list[str]:
    out, skip = [], False
    for w in seg:
        if skip:
            skip = False
            continue
        m = REDIRECT.match(w)
        if m:
            skip = not m.group(1)  # `> log`: the target is the next word
            continue
        out.append(w)
    return out


def simple_commands(cmd: str, depth: int = 0) -> list[list[str]] | None:
    """Every simple command the command line may run, as its words (redirections included).

    Split at ; & | ( ) and newlines; `sh -c "..."` and `eval` are opened up, and so are $(...) and backticks, also
    inside double quotes. None when the line cannot be split (unbalanced quotes) or nests too deep."""
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
    out = [sg for sg in segs if sg]
    inner_cmds = [a or b for a, b in SUBSTITUTION.findall(outside_single_quotes(cmd))]
    for seg in out:
        for k, w in enumerate(seg):
            name = Path(w).name
            if name in SHELLS and "-c" in seg[k + 1:]:
                c = seg.index("-c", k + 1)
                if c + 1 < len(seg):
                    inner_cmds.append(seg[c + 1])
            elif name == "eval":
                inner_cmds.append(" ".join(seg[k + 1:]))
    for inner in inner_cmds:
        more = simple_commands(inner, depth + 1)
        if more is None:
            return None
        out = out + more
    return out


def command_segments(cmd: str) -> list[list[str]] | None:
    """Every git or gh invocation the command may run, as word lists that start with git or gh, redirections
    dropped. An invocation is found wherever its word appears, so wrappers and assignments in front (env, time,
    sudo, xargs, FOO=1) do not hide it. None when the command cannot be split."""
    cmds = simple_commands(cmd)
    if cmds is None:
        return None
    out: list[list[str]] = []
    for seg in filter(None, (drop_redirections(sg) for sg in cmds)):
        for k, w in enumerate(seg):
            if Path(w).name in ("git", "gh"):
                out.append(seg[k:])
    return out


def git_subcommand(seg: list[str]) -> tuple[str | None, list[str]]:
    if not seg or Path(seg[0]).name != "git":
        return None, []
    i = 1
    while i < len(seg) and seg[i].startswith("-"):
        i += 2 if seg[i] in GIT_GLOBAL_WITH_ARG else 1
    return (seg[i], seg[i + 1:]) if i < len(seg) else (None, [])


def gh_pr_create(seg: list[str]) -> list[str] | None:
    """The arguments of `gh [global flags] pr create|new ...`, else None."""
    if not seg or Path(seg[0]).name != "gh":
        return None
    rest = [w for i, w in enumerate(seg[1:]) if not w.startswith("-") and not (i > 0 and seg[i] in ("-R", "--repo"))]
    if len(rest) < 2 or rest[0] != "pr" or rest[1] not in ("create", "new"):
        return None
    return seg[seg.index(rest[1]) + 1:]


def gh_head(args: list[str]) -> str | None:
    """The branch of `--head X`, `--head=X`, `-H X` or `-HX`, as gh reads it (owner:branch → branch)."""
    for i, w in enumerate(args):
        value = None
        if w in ("--head", "-H") and i + 1 < len(args):
            value = args[i + 1]
        elif w.startswith("--head="):
            value = w.split("=", 1)[1]
        elif w.startswith("-H") and len(w) > 2:
            value = w[2:]
        if value is not None:
            return value.split(":")[-1]
    return None


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


def unsets_hooks_path(seg: list[str]) -> bool:
    """`git config --unset core.hooksPath` (or `git config unset ...`): the way back, which the harness asks for."""
    sub, args = git_subcommand(seg)
    keys = [a for a in args if not a.startswith("-") and a != "unset"]
    return sub == "config" and ({"--unset", "--unset-all"} & set(args) or args[:1] == ["unset"]) \
        and [k.lower() for k in keys] == ["core.hookspath"]


def hooks_off(cmd: str) -> str | None:
    """Why a command switches git hooks off, else None. The text check is broad on purpose (a message that merely
    mentions --no-verify is refused too); the word check catches `commit -n` and its clusters."""
    segs = command_segments(cmd)
    git_segs = [sg for sg in segs or [] if Path(sg[0]).name == "git" and not unsets_hooks_path(sg)]
    if NO_VERIFY.search(cmd) and (segs is None or git_segs):
        return "this command switches git hooks off (--no-verify / core.hooksPath); run it without that"
    for seg in git_segs:
        # the words as the shell hands them to git: quotes and backslashes are gone (--no-"verify", core.hooks\Path)
        if any(NO_VERIFY.fullmatch(w.split("=", 1)[0].rsplit(".", 1)[-1]) or "hookspath" in w.lower() for w in seg[1:]):
            return "this command switches git hooks off (--no-verify / core.hooksPath); run it without that"
        sub, args = git_subcommand(seg)
        if sub == "commit" and short_flag(args, "n", "mFcCt", COMMIT_LONG_WITH_VALUE):  # commit -n is --no-verify (push -n is a dry run)
            return "this commit switches git hooks off (-n is --no-verify); run it without that"
    return None


CLAUDECODE_MSG = ("this command changes CLAUDECODE or CLAUDE_CODE_CHILD_SESSION, which the git pre-push hook reads (it "
                  "judges only what Claude Code runs); run it without that")
CLEAR_ENV_MSG = ("this command clears the environment (env -i / exec -c), which hides CLAUDE_CODE_CHILD_SESSION from the git "
                 "pre-push hook; pass the variables you need instead")
GIT_CONFIG_MSG = ("this command points git at other configuration (GIT_CONFIG_* / --config-env), where hooks can be "
                  "switched off and aliases defined out of the harness's sight; use the repository's configuration")
GIT_HOOKS_MSG = "the harness owns the git hooks (.git/hooks); leave them alone, or ask the user"
SHELL_ALIAS_MSG = ("this command defines a shell alias (alias.*=!...), which can publish where the command reading "
                   "cannot see; run the commands directly")
ASSIGN_GIT_CONFIG = re.compile(r"GIT_CONFIG(?:_GLOBAL|_SYSTEM|_PARAMETERS|_COUNT|_KEY_\w+|_VALUE_\w+)?=")
ENV_WITH_VALUE = {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}


def tamper_problem(cmd: str) -> str | None:
    """Why a command changes what the git pre-push hook or git's configuration sees, else None. Words are read as
    the shell hands them over (CLAUDE""CODE= is CLAUDECODE=), wherever they stand; quoted text stays one word."""
    cmds = simple_commands(cmd)
    if cmds is None:  # cannot be split: judge the text
        for rx, msg in ((CLAUDECODE_CHANGE, CLAUDECODE_MSG), (CLEAR_ENV, CLEAR_ENV_MSG),
                        (GIT_CONFIG_ENV, GIT_CONFIG_MSG), (GIT_HOOK_FILES, GIT_HOOKS_MSG)):
            if rx.search(cmd):
                return msg
        return None
    for seg in cmds:
        for k, w in enumerate(seg):
            if any(w.startswith(f"{v}=") or w in (f"-u{v}", f"--unset={v}") for v in HOOK_VARS) or (
                    w in HOOK_VARS and k and (seg[k - 1] in ("-u", "--unset") or "unset" in seg[:k]
                                                 or ("export" in seg[:k] and "-n" in seg[:k]))):
                return CLAUDECODE_MSG
            if ASSIGN_GIT_CONFIG.match(w) or w == "--config-env" or w.startswith("--config-env="):
                return GIT_CONFIG_MSG
            if " " not in w and (".git/hooks" in w or w.endswith("hooks/pre-push")):  # `>.git/hooks/x` too
                return GIT_HOOKS_MSG
            if Path(w).name == "env":
                j = k + 1
                while j < len(seg) and seg[j].startswith("-"):
                    o = seg[j]
                    if o in ("-", "-i", "--ignore-environment") or (not o.startswith("--")
                                                                     and re.fullmatch(r"-[A-Za-z]*i[A-Za-z]*", o)):
                        return CLEAR_ENV_MSG
                    j += 2 if o in ENV_WITH_VALUE else 1
            if w == "exec" and k + 1 < len(seg) and re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", seg[k + 1]):
                return CLEAR_ENV_MSG
    for seg in command_segments(cmd) or []:
        sub, args = git_subcommand(seg)
        defs = [seg[i + 1] for i in range(len(seg) - 1) if seg[i] == "-c"]  # git -c alias.x=!...
        if sub == "config":
            for i, a in enumerate(args):
                if a.lower().startswith("alias."):
                    value = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
                    defs.append(f"{a.split('=', 1)[0]}={value}")
        if any(d.lower().startswith("alias.") and d.split("=", 1)[-1].lstrip().startswith("!") for d in defs):
            return SHELL_ALIAS_MSG
    return None


def moves_head(sub: str | None, args: list[str]) -> bool:
    """Whether `git <sub> <args>` may move HEAD or a branch. Listing branches, tags, stashes or worktrees and reading
    a symbolic ref do not."""
    if sub not in MOVES_HEAD:
        return False
    flags = [a.split("=", 1)[0] for a in args if a.startswith("-")]
    names, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in ("--format", "--sort", "--points-at"):
            skip = True
        elif not a.startswith("-"):
            names.append(a)
    if sub in ("branch", "tag"):
        listing = BRANCH_LISTING if sub == "branch" else TAG_LISTING
        lists = all(f in listing or (sub == "tag" and re.fullmatch(r"-n\d*", f)) for f in flags)
        return not (lists and (not names or bool(LIST_MODE & set(flags))))
    if sub == "stash":
        return names[:1] not in (["list"], ["show"])
    if sub == "worktree":
        return names[:1] != ["list"]
    if sub == "symbolic-ref":
        return len(names) != 1 or bool({"-d", "--delete"} & set(flags))
    return True


def alias_of(root: Path, seg: list[str], sub: str) -> str | None:
    """What `git <sub>` runs when sub is an alias: from `-c alias.<sub>=...` in the same command, else the config."""
    for i in range(len(seg) - 1):
        if seg[i] == "-c" and seg[i + 1].lower().startswith(f"alias.{sub.lower()}="):
            return seg[i + 1].split("=", 1)[1]
    return git(root, "config", "--get", f"alias.{sub}")


def publish_problems(root: Path, cmd: str) -> tuple[bool, list[str]]:
    """Whether the command publishes, and why it may not: the gate checks the checked-out tree, so a publishing
    command may only publish that tree, and nothing in the same command may move HEAD first. An alias is read as
    what it runs."""
    segs = command_segments(cmd)
    if segs is None:
        return bool(PUBLISH.search(cmd)), ["the command could not be read (unbalanced quotes); publish in a plain command"] \
            if PUBLISH.search(cmd) else []
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD") or ""
    ok_refs = {"HEAD", "@", branch, f"refs/heads/{branch}"} - {""}
    publishes, problems, movers = False, [], []
    work, expanded = list(segs), 0
    while work:
        seg = work.pop(0)
        sub, args = git_subcommand(seg)
        if sub and Path(seg[0]).name == "git" and sub not in GIT_BUILTINS and expanded < 5:
            alias = alias_of(root, seg, sub)
            if alias:
                expanded += 1
                if alias.startswith("!"):
                    if PUBLISHING_WORDS.search(alias):
                        publishes = True
                        problems.append(f"git {sub} is a shell alias that publishes ({alias[:60]}); run its commands "
                                        "directly so the harness reads them")
                    continue
                try:
                    words = shlex.split(alias)
                except ValueError:
                    words = []
                if words:
                    k = len(seg) - len(args) - 1  # where the alias name stands
                    work.insert(0, seg[:k] + words + args)
                    continue
        if moves_head(sub, args):
            movers.append(sub)
        if sub in ("send-pack", "http-push"):
            publishes = True
            problems.append(f"git {sub} sends without the pre-push hook; publish with git push")
        if sub == "push":
            publishes = True
            config = [seg[i + 1] for i in range(len(seg) - 1) if seg[i] == "-c"]
            if any(c.lower().startswith(("remote.", "push.", "branch.")) for c in config):
                problems.append("this push changes what git sends (-c remote.* / push.* / branch.*); push without that")
            if PUSH_ALL & set(args) or any(a.startswith(tuple(f"{o}=" for o in PUSH_ALL)) for a in args):
                problems.append("push the checked-out branch only (no --all, --branches, --mirror or --tags): the gate "
                                "checked that tree")
            positional, i = [], 0
            while i < len(args):
                if args[i] in PUSH_WITH_VALUE:
                    i += 2
                    continue
                if not args[i].startswith("-"):
                    positional.append(args[i])
                i += 1
            for ref in positional[1:]:
                src = ref.lstrip("+").split(":", 1)[0]
                if src not in ok_refs:
                    problems.append(f"push the checked-out branch only: {ref!r} is not {branch or 'HEAD'}, and the gate "
                                    "checked the checked-out tree")
        pr = gh_pr_create(seg)
        if pr is not None:
            publishes = True
            head_ref = gh_head(pr)
            if head_ref is not None and head_ref != branch:
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
    off = hooks_off(cmd) or tamper_problem(cmd)
    if off:
        return deny(off)
    restored = restore_pre_push(root)  # a script may have changed it since the last command
    publishes, problems = publish_problems(root, cmd)
    state = load_state(root, session)
    if not publishes:  # a delegate (fsmap) judges every command; the core only publishing ones
        try:
            base, _ = session_base(root, state)
            cfg, _, _ = read_config(root, base)
            d = delegate(load_adapter(root, cfg, base))
        except HarnessError:
            d = None
        if d:
            return relay(d, "pre-bash", payload, lambda m: notice(m))
        return notice(restored) if restored else 0
    if re.search(rf"\b{_HV}\b", cmd):
        problems.append("this command mentions CLAUDECODE or CLAUDE_CODE_CHILD_SESSION, which the git pre-push hook "
                        "reads; publish without that")
    if restored:
        problems.append(f"{restored}; run the command again")
    if problems:
        return deny("; ".join(problems))
    try:
        base, pending = session_base(root, state)
        cfg, cfg_text, _ = read_config(root, base)
        adapter = load_adapter(root, cfg, base)
    except HarnessError as e:
        return deny(f"the gate cannot run, so nothing is published: {e}")
    mine = changed_since(root, base, pending, start_refs(state))
    touched = harness_changes(root, base, mine, state)
    if touched:
        return deny("the harness, its settings or the CI workflows changed in this session (" + ", ".join(touched[:5]) +
                    "); a person reviews and publishes such a change")
    d = delegate(adapter)
    if d:
        return relay(d, "pre-bash", payload, lambda m: deny(f"{m}; nothing is published"))
    hook_problem = pre_push_problem(root)
    if hook_problem:
        return deny(hook_problem)
    dirty = dirty_paths(root)
    if dirty:
        return deny(f"there are uncommitted changes ({', '.join(dirty[:5])}{', ...' if len(dirty) > 5 else ''}), so the "
                    "gate would check a different tree than the commit being pushed. Commit them (or remove them; a "
                    "file a check leaves behind can be ignored, but the gate also sees ignored files the commit lacks, "
                    "so CI on a clean checkout stays the check of record) in one command, then push in another")
    fp = fingerprint(root, cfg_text)
    if fp is not None and fp == state.get("last_pass"):
        record_passed(root)
        return 0
    gate = run_gate(root, adapter, "quick", float(cfg.get("budget", {}).get("gate", GATE_BUDGET)))
    log(root, "pre-bash", result=gate["verdict"])
    if gate["verdict"] == "pass":
        state["last_pass"] = fingerprint(root, cfg_text)
        save_state(root, session, state)
        record_passed(root)
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
    root, proj = project_roots(payload)
    session = str(payload.get("session_id") or "no-session")
    try:
        if root != proj:
            inherit_session(root, proj, session)
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
            except Exception as e2:  # noqa: BLE001 — the state cannot be written: count the blocks elsewhere
                return fallback_strike(root, session, f"the harness crashed ({e!r:.200}; {e2!r:.200}); the gate did not run")
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
    except (ValueError, OSError) as e:  # not JSON, not UTF-8, unreadable
        print(f"install: {settings} cannot be read as JSON ({e}); merge by hand", file=sys.stderr)
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
    if merged.get("disableAllHooks") is True:
        print(f"install: {settings} sets disableAllHooks: true, so no hook would run; remove it (with the person's "
              "agreement) and run install again", file=sys.stderr)
        return 2
    if config.is_file():
        try:
            have = json.loads(config.read_text(encoding="utf-8")).get("adapter")
        except (ValueError, OSError, AttributeError) as e:  # not JSON, not UTF-8, not an object, unreadable
            print(f"install: {config} cannot be read ({e!r:.120}); fix it by hand", file=sys.stderr)
            return 2
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
    local = repo / ".claude" / "settings.local.json"
    try:
        if local.is_file() and json.loads(local.read_text(encoding="utf-8")).get("disableAllHooks") is True:
            plan.append(f"warning: {local} sets disableAllHooks: true; the hooks will not run while it does")
    except (ValueError, OSError, AttributeError):
        plan.append(f"warning: {local} cannot be read; check that it does not switch hooks off")
    print("install plan:\n  " + "\n  ".join(plan))
    if not yes:
        print("dry run: nothing written. Changing a repository's .claude/settings.json needs a person's "
              "confirmation (AOK S4); run again with --yes after they agree.")
        return 0
    def copy(src: Path, dst: Path) -> None:
        if src.resolve() != dst.resolve():  # installing again from the installed copy itself
            shutil.copy2(src, dst)
    (target / "adapters").mkdir(parents=True, exist_ok=True)
    copy(Path(__file__), target / "aok_harness.py")
    for a in ADAPTER_DIR.glob("*.json"):
        copy(a, target / "adapters" / a.name)
    (target / "agents").mkdir(exist_ok=True)
    copy(HERE / "agents" / "aok-reviewer.md", target / "agents" / "aok-reviewer.md")
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
    gp = sub.add_parser("git-pre-push", help="git's pre-push hook (installed by session-start; reads git's ref lines)")
    gp.add_argument("remote", nargs="*", help="the remote's name and URL, as git passes them")
    i = sub.add_parser("install", help="install into a work repository (dry run without --yes)")
    i.add_argument("--repo", required=True)
    i.add_argument("--adapter", required=True)
    i.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "hook":
        return hook(args.event)
    if args.cmd == "verify":
        return verify(Path(args.root).resolve(), args.mode, args.json, args.base)
    if args.cmd == "git-pre-push":
        return git_pre_push(Path(git(Path.cwd(), "rev-parse", "--show-toplevel") or "."))  # git runs hooks at the top
    return install(Path(args.repo), args.adapter, args.yes)


if __name__ == "__main__":
    sys.exit(main())
