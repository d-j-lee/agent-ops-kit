"""Tests for the ⑧ work-repo harness core (harness/aok_harness.py). Stdlib unittest only.

Every documented behaviour has a test here; tests/mutations/harness.json switches the guards
off one by one and CI shows that the named test fails for each.

The tests build throw-away git repositories and call the hooks the way Claude Code does
(payload on stdin). PATH is hermetic: python3 and git are linked into a private bin
directory, and tools like ruff, cargo or fsmap are small stub scripts when a test needs them.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "harness" / "aok_harness.py"

ADD_OK = "def add(a, b):\n    return a + b\n"
ADD_BAD = "def add(a, b):\n    return a - b\n"
TEST = textwrap.dedent("""\
    import unittest
    from pkg import add


    class T(unittest.TestCase):
        def test_add(self):
            self.assertEqual(add(1, 2), 3)
    """)


class Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in ("python3", "git"):
            target = sys.executable if tool == "python3" else shutil.which("git")
            (self.bin / tool).symlink_to(target)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.env = {"PATH": str(self.bin), "HOME": str(self.tmp / "home"), "XDG_STATE_HOME": str(self.tmp / "state"),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "LANG": "C.UTF-8"}

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers
    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env, capture_output=True, text=True, check=True).stdout

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def python_repo(self, commit=True, config=None):
        self.write("pyproject.toml", '[project]\nname = "x"\n')
        self.write("pkg/__init__.py", ADD_OK)
        self.write("tests/__init__.py", "")
        self.write("tests/test_add.py", TEST)
        if config is not None:
            self.write(".aok/harness.json", json.dumps(config))
        self.git("init", "-q", "-b", "main")
        if commit:
            self.git("add", "-A")
            self.git("commit", "-qm", "init")

    def stub(self, name, script):
        p = self.bin / name
        p.write_text("#!/bin/sh\nPATH=\"$PATH:/usr/bin:/bin\"\n" + script, encoding="utf-8")  # the stub's own helpers
        p.chmod(0o755)

    def hook(self, event, payload=None, extra_env=None):
        payload = {"session_id": "s1", **(payload or {})}
        env = {**self.env, "CLAUDE_PROJECT_DIR": str(self.repo), **(extra_env or {})}
        p = subprocess.run([sys.executable, "-I", str(CORE), "hook", event], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else None

    def start(self, source="startup"):
        return self.hook("session-start", {"source": source})

    def stop(self, **payload):
        return self.hook("stop", payload)

    def bash(self, command):
        return self.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": command}})

    def edit(self, rel, tool="Edit"):
        return self.hook("post-edit", {"tool_name": tool, "tool_input": {"file_path": str(self.repo / rel)}})

    def verify(self, *args):
        p = subprocess.run([sys.executable, "-I", str(CORE), "verify", "--root", str(self.repo), *args],
                           capture_output=True, text=True, env=self.env, timeout=300)
        return p.returncode, p.stdout + p.stderr

    def log(self):
        p = self.repo / ".git" / "aok-harness" / "log.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def blocked(self, out):
        return bool(out) and out.get("decision") == "block"

    def denied(self, out):
        return bool(out) and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class SessionStartTests(Harness):
    def test_records_baseline_and_reports(self):
        self.python_repo()
        out = self.start()
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("adapter python", ctx)
        self.assertIn("Python 3", ctx)
        self.assertIn("ruff --version: not installed", ctx)
        state = json.loads((self.repo / ".git/aok-harness/session-s1.json").read_text())
        self.assertEqual(state["baseline"]["head"], self.git("rev-parse", "HEAD").strip())

    def test_baseline_is_kept_on_compact(self):
        self.python_repo()
        self.start()
        first = self.git("rev-parse", "HEAD").strip()
        self.write("pkg/__init__.py", ADD_BAD)
        self.git("commit", "-qam", "break")
        self.start(source="compact")
        state = json.loads((self.repo / ".git/aok-harness/session-s1.json").read_text())
        self.assertEqual(state["baseline"]["head"], first)

    def test_off_when_no_adapter_fits(self):
        self.write("README.md", "x\n")
        self.git("init", "-q", "-b", "main")
        out = self.start()
        self.assertIn("OFF", out["systemMessage"])
        self.assertIn("cannot pick an adapter", out["systemMessage"])

    def test_unknown_adapter_and_bad_config(self):
        self.python_repo(config={"adapter": "cobol"})
        self.assertIn("unknown adapter 'cobol'", self.start()["systemMessage"])
        self.write(".aok/harness.json", "{not json")
        self.git("add", "-A")
        self.git("commit", "-qm", "bad")
        self.assertIn("not valid JSON", self.start()["systemMessage"])


class PostEditTests(Harness):
    def test_blocks_a_syntax_error(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", "def add(a, b)\n    return a + b\n")
        out = self.edit("pkg/__init__.py")
        self.assertTrue(self.blocked(out))
        self.assertIn("pkg/__init__.py", out["reason"])
        self.assertIn("SyntaxError", out["reason"])

    def test_good_edit_is_silent_and_writes_no_bytecode(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        self.assertIsNone(self.edit("pkg/__init__.py"))
        self.assertFalse(list(self.repo.rglob("__pycache__")))

    def test_other_files_tools_and_paths_are_ignored(self):
        self.python_repo()
        self.start()
        self.write("README.md", "def broken(\n")
        self.assertIsNone(self.edit("README.md"))
        self.write("pkg/__init__.py", "def add(a, b)\n")
        self.assertIsNone(self.hook("post-edit", {"tool_name": "Read", "tool_input": {"file_path": str(self.repo / "pkg/__init__.py")}}))
        outside = self.tmp / "elsewhere.py"
        outside.write_text("def x(\n", encoding="utf-8")
        self.assertIsNone(self.hook("post-edit", {"tool_name": "Write", "tool_input": {"file_path": str(outside)}}))

    def test_optional_tool_runs_when_present(self):
        self.python_repo()
        self.start()
        self.stub("ruff", 'case "$*" in *--version*) echo "ruff 9.9.9";; *) echo "F401 unused import in $5"; exit 1;; esac\n')
        self.write("pkg/__init__.py", "import os\n" + ADD_OK)
        out = self.edit("pkg/__init__.py")
        self.assertTrue(self.blocked(out))
        self.assertIn("[ruff] fail", out["reason"])


class StopTests(Harness):
    def test_session_that_changed_nothing_is_not_judged(self):
        self.python_repo(commit=False)
        self.write("pkg/__init__.py", ADD_BAD)  # already failing before the session
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.start()
        self.assertIsNone(self.stop())
        self.assertEqual(self.log()[-1]["result"], "skip-no-session-change")

    def test_failing_gate_blocks_with_the_output(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("[test] fail", out["reason"])
        self.assertIn("AssertionError", out["reason"])
        self.assertIn("Try 1 of 3", out["reason"])

    def test_released_after_three_strikes_and_prompt_resets(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        self.assertTrue(self.blocked(self.stop()))
        self.assertTrue(self.blocked(self.stop(stop_hook_active=True)))
        out = self.stop(stop_hook_active=True)
        self.assertNotIn("decision", out)
        self.assertIn("did NOT pass after 3 tries", out["systemMessage"])
        self.assertIn("Try 1 of 3", self.stop()["reason"])  # the count starts again
        self.assertIn("Try 2 of 3", self.stop()["reason"])
        self.hook("prompt", {"prompt": "go on"})
        self.assertIn("Try 1 of 3", self.stop()["reason"])

    def test_pass_then_unchanged_tree_is_not_rerun(self):
        # a step that leaves a cache behind, as linters do: the tree it leaves is the one that passed
        cache = {"name": "cache", "cmd": ["python3", "-c", "import pathlib; p = pathlib.Path('.tool_cache'); p.mkdir(exist_ok=True); (p / 'x').write_text('1')"]}
        self.python_repo(config={"adapter": "python", "extra": [cache]})
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        self.assertIsNone(self.stop())
        self.assertIsNone(self.stop())
        self.assertEqual([e["result"] for e in self.log() if e["event"] == "stop"], ["pass", "skip-unchanged"])
        self.assertFalse(list(self.repo.rglob("__pycache__")), "steps run with PYTHONDONTWRITEBYTECODE")

    def test_editing_an_already_dirty_file_counts(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        self.assertIsNone(self.stop())
        self.write("pkg/__init__.py", ADD_BAD)  # same dirty path list, different content
        self.assertTrue(self.blocked(self.stop()))

    def test_commit_during_the_turn_counts(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        self.git("commit", "-qam", "fine")
        self.assertIsNone(self.stop())  # passes on a clean tree
        self.write("pkg/__init__.py", ADD_BAD)
        self.git("commit", "-qam", "looks clean now")  # clean again, but HEAD moved
        self.assertTrue(self.blocked(self.stop()))

    def test_first_commit_of_an_empty_repository_counts(self):
        self.python_repo(commit=False)
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        self.git("add", "-A")
        self.git("commit", "-qm", "first")
        self.assertTrue(self.blocked(self.stop()))

    def test_pass_reports_weakened_checks(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_OK.replace("a + b", "a + b  # noqa"))
        self.write("tests/test_add.py", TEST.replace("        self.assertEqual(add(1, 2), 3)\n", "        pass\n"))
        out = self.stop()
        msg = out["systemMessage"]
        self.assertIn("gate passed", msg)
        self.assertIn("pkg/__init__.py: noqa ×1", msg)
        self.assertIn("tests/test_add.py: assertion-removed ×1", msg)

    def test_gate_definition_comes_from_the_base_commit(self):
        self.python_repo(config={"adapter": "python"})
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        # loosen the gate in the work tree: the base commit's definition still applies
        self.write(".aok/harness.json", json.dumps({"adapter": "python", "gate": {"quick": [{"name": "ok", "cmd": ["python3", "-c", "pass"]}]}}))
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("[test] fail", out["reason"])

    def test_changed_definition_and_harness_files_are_reported(self):
        self.python_repo(config={"adapter": "python"})
        self.start()
        self.write(".aok/harness.json", json.dumps({"adapter": "python", "extra": []}))
        msg = self.stop()["systemMessage"]
        self.assertIn(".aok/harness.json changed in this session", msg)
        self.assertIn("harness file changed in this session: .aok/harness.json", msg)

    def test_required_tool_missing_blocks(self):
        self.python_repo(config={"adapter": "python", "extra": [{"name": "lint", "cmd": ["no-such-linter"]}]})
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("could not run", out["reason"])
        self.assertIn("no-such-linter not found", out["reason"])

    def test_optional_tool_missing_is_reported_once(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        out = self.stop()  # optional ruff missing: not a failure, but said
        self.assertNotIn("decision", out)
        self.assertIn("optional step(s) skipped, tool not installed: ruff", out["systemMessage"])
        self.write("pkg/__init__.py", ADD_OK.replace("a + b", "b + a"))
        self.assertIsNone(self.stop())  # and not repeated every turn
        self.assertEqual(self.log()[-1]["steps"], [["ruff", "missing"], ["test", "pass"]])

    def test_background_agents_postpone_the_gate(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        self.assertIsNone(self.stop(background_tasks=[{"id": "x", "type": "subagent"}]))
        self.assertEqual(self.log()[-1]["result"], "skip-background")
        self.assertTrue(self.blocked(self.stop(background_tasks=[])))

    def test_steps_do_not_inherit_git_redirect_variables(self):
        check = {"name": "env", "cmd": ["python3", "-c", "import os, sys; sys.exit(1 if 'GIT_DIR' in os.environ else 0)"]}
        self.python_repo(config={"adapter": "python", "extra": [check]})
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        self.assertIsNone(self.hook("stop", {}, extra_env={"GIT_DIR": str(self.repo / ".git")}))

    def test_without_git_the_gate_runs_every_time(self):
        self.python_repo(commit=False)
        shutil.rmtree(self.repo / ".git")
        self.stub("ruff", "exit 0\n")
        self.start()
        self.assertIsNone(self.stop())
        self.write("pkg/__init__.py", ADD_BAD)
        self.assertIn("Try 1 of 3", self.stop()["reason"])
        self.assertIn("Try 2 of 3", self.stop()["reason"])  # strikes kept in XDG_STATE_HOME

    def test_a_crash_is_reported_not_passed(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        bad = self.repo / ".git" / "aok-harness" / "session-s1.json"
        bad.write_text(json.dumps({"baseline": {"head": 12345, "pending": []}}), encoding="utf-8")
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("crashed", out["reason"])


class PreBashTests(Harness):
    def test_other_commands_are_left_alone(self):
        self.python_repo()
        self.start()
        self.assertIsNone(self.bash("ls -la && git status"))

    def test_no_verify_is_denied(self):
        self.python_repo()
        self.start()
        self.assertTrue(self.denied(self.bash("git commit -m x --no-verify")))
        self.assertTrue(self.denied(self.bash("git -c core.hooksPath=/dev/null push")))

    def test_push_with_uncommitted_changes_is_denied(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        out = self.bash("git add -A && git commit -m x && git push")
        self.assertTrue(self.denied(out))
        self.assertIn("uncommitted", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_push_of_a_failing_commit_is_denied_and_a_passing_one_allowed(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        self.git("commit", "-qam", "bad")
        out = self.bash("git -C . push origin main")
        self.assertTrue(self.denied(out))
        self.assertIn("[test] fail", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.write("pkg/__init__.py", ADD_OK)
        self.git("commit", "-qam", "good")
        self.assertIsNone(self.bash("gh pr create --fill"))
        self.assertEqual(self.log()[-1], {**self.log()[-1], "event": "pre-bash", "result": "pass"})

    def test_push_after_a_harness_change_is_denied(self):
        self.python_repo(config={"adapter": "python"})
        self.start()
        self.write(".aok/harness.json", json.dumps({"adapter": "python", "extra": []}))
        self.git("commit", "-qam", "tweak harness")
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn(".aok/harness.json", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_crash_denies_a_publishing_command(self):
        self.python_repo()
        self.start()
        (self.repo / ".git" / "aok-harness" / "session-s1.json").write_text(
            json.dumps({"baseline": {"head": 12345, "pending": []}}), encoding="utf-8")
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn("crashed", out["hookSpecificOutput"]["permissionDecisionReason"])


class VerifyTests(Harness):
    def test_exit_codes(self):
        self.python_repo()
        code, out = self.verify("--mode", "quick")
        self.assertEqual(code, 0, out)
        self.assertIn("verdict: pass", out)
        self.write("pkg/__init__.py", ADD_BAD)
        self.assertEqual(self.verify("--mode", "quick")[0], 1)
        self.write(".aok/harness.json", json.dumps({"adapter": "python", "extra": [{"name": "x", "cmd": ["no-such-tool"]}]}))
        self.assertEqual(self.verify()[0], 2)
        self.write(".aok/harness.json", json.dumps({"adapter": "python", "colour": "red"}))
        code, out = self.verify()
        self.assertEqual(code, 2)
        self.assertIn("unknown keys", out)

    def test_base_ref_definition(self):
        self.python_repo(config={"adapter": "python"})
        self.write(".aok/harness.json", json.dumps({"adapter": "python", "gate": {"quick": [{"name": "ok", "cmd": ["python3", "-c", "pass"]}]}}))
        self.write("pkg/__init__.py", ADD_BAD)
        self.assertEqual(self.verify("--mode", "quick")[0], 0)  # the loosened work-tree definition
        code, out = self.verify("--mode", "quick", "--base", "main")
        self.assertEqual(code, 1, out)  # the committed one still runs the tests
        self.assertEqual(self.verify("--base", "no-such-ref")[0], 2)

    def test_every_step_runs_after_a_failure(self):
        self.python_repo(config={"adapter": "python", "extra": [{"name": "later", "cmd": ["python3", "-c", "pass"]}]})
        self.write("pkg/__init__.py", ADD_BAD)
        code, out = self.verify("--json")
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual([(s["name"], s["status"]) for s in report["steps"]], [("ruff", "missing"), ("test", "fail"), ("later", "pass")])

    def test_a_step_that_overruns_is_cannot(self):
        slow = {"name": "slow", "cmd": ["python3", "-c", "import time; time.sleep(30)"]}
        self.python_repo(config={"adapter": "python", "extra": [slow], "budget": {"gate": 3}})
        code, out = self.verify("--mode", "quick", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["steps"][-1]["status"], "cannot")


class RustAdapterTests(Harness):
    def rust_repo(self):
        self.write("Cargo.toml", '[package]\nname = "x"\nversion = "0.1.0"\n')
        self.write("src/lib.rs", "pub fn f() {}\n")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def test_delegates_every_hook_to_fsmap(self):
        self.rust_repo()
        record = self.tmp / "fsmap.log"
        self.stub("fsmap", f'echo "$*" >> {record}\ncase "$*" in "hook --help") exit 0;; esac\n'
                           f'cat >> {record}\necho \'{{"decision": "block", "reason": "from fsmap"}}\'\n')
        out = self.stop()
        self.assertEqual(out, {"decision": "block", "reason": "from fsmap"})
        log = record.read_text()
        self.assertIn("hook stop", log)
        self.assertIn('"session_id": "s1"', log)

    def test_verify_delegates_to_fsmap(self):
        self.rust_repo()
        record = self.tmp / "fsmap.log"
        self.stub("fsmap", f'echo "$*" >> {record}\ncase "$*" in "hook --help") exit 0;; esac\nexit 1\n')
        code, out = self.verify("--mode", "full", "--json", "--base", "HEAD")
        self.assertEqual(code, 1, out)  # fsmap's own exit code: 0 pass, 1 fail, 2 cannot run
        self.assertIn("verify --mode full --json", record.read_text())
        self.assertIn("--base chose the adapter only", out)

    def test_old_fsmap_without_hook_falls_back(self):
        self.rust_repo()
        self.stub("fsmap", 'case "$*" in "hook --help") echo "unknown command hook" >&2; exit 2;; esac\necho \'{"decision": "block", "reason": "should not be asked"}\'\n')
        self.stub("cargo", "exit 0\n")
        self.start()
        self.write("src/lib.rs", "pub fn f() { }\n")
        self.assertIsNone(self.stop())

    def test_falls_back_to_cargo_without_fsmap(self):
        self.rust_repo()
        self.stub("cargo", 'case "$1" in clippy) echo "warning: unused variable"; exit 101;; esac\nexit 0\n')
        self.stub("rustc", 'echo "rustc 1.99.1"\n')
        ctx = self.start()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("fsmap is not installed", ctx)
        self.write("src/lib.rs", "pub fn f() { let x = 1; }\n")
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("[clippy] fail", out["reason"])
        self.assertNotIn("[fmt] fail", out["reason"])

    def test_fallback_reports_rust_weakening(self):
        self.rust_repo()
        self.stub("cargo", "exit 0\n")
        self.start()
        self.write("src/lib.rs", "#[allow(dead_code)]\npub fn f() {}\n")
        self.assertIn("src/lib.rs: allow-attribute ×1", self.stop()["systemMessage"])


@unittest.skipUnless(os.environ.get("AOK_FSMAP"), "set AOK_FSMAP to a built fsmap (d-j-lee/filesystemMap) to run")
class RealFsmapTests(Harness):
    """The Rust adapter against a real fsmap, not a stub: the hooks and verify are handed over and fsmap's exit
    codes come back unchanged."""

    def setUp(self):
        super().setUp()
        self.assertIsNotNone(shutil.which("cargo"), "AOK_FSMAP is set, so cargo must be there too")  # fail, never skip
        (self.bin / "fsmap").symlink_to(Path(os.environ["AOK_FSMAP"]).resolve())
        home = Path(os.environ.get("HOME", "/root"))
        self.env.update(PATH=f"{self.bin}:{Path(shutil.which('cargo')).parent}:/usr/bin:/bin",
                        CARGO_HOME=os.environ.get("CARGO_HOME", str(home / ".cargo")),
                        RUSTUP_HOME=os.environ.get("RUSTUP_HOME", str(home / ".rustup")))
        self.write("Cargo.toml", '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2021"\n')
        self.write("src/lib.rs", "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n")
        subprocess.run(["cargo", "generate-lockfile", "--offline"], cwd=self.repo, env=self.env, check=True,
                       capture_output=True)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def test_hooks_and_verify_go_to_real_fsmap(self):
        ctx = self.start()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("fsmap", ctx)
        self.assertNotIn("[aok-harness]", ctx)
        self.assertTrue((self.repo / ".git" / "hooks" / "pre-push").is_file(), "fsmap's session start installs it")
        code, out = self.verify("--mode", "quick", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["mode"], "quick")
        self.write("src/lib.rs", "pub fn add(a: i32, b: i32) -> i32 {\n    a +  b\n}\n")  # not rustfmt-clean
        code, out = self.verify("--mode", "quick")
        self.assertEqual(code, 1, out)


class InstallTests(Harness):
    def install(self, *args):
        p = subprocess.run([sys.executable, "-I", str(CORE), "install", "--repo", str(self.repo), *args],
                           capture_output=True, text=True, env=self.env, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def test_dry_run_writes_nothing(self):
        self.python_repo()
        code, out = self.install("--adapter", "python")
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertFalse((self.repo / ".aok").exists())
        self.assertFalse((self.repo / ".claude").exists())

    def test_yes_installs_and_merges_idempotently(self):
        self.python_repo()
        self.write(".claude/settings.json", json.dumps({"permissions": {"deny": ["Bash(rm -rf *)"]}, "model": "x"}))
        self.assertEqual(self.install("--adapter", "python", "--yes")[0], 0)
        self.assertEqual(self.install("--adapter", "python", "--yes")[0], 0)
        s = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertEqual(s["model"], "x")
        self.assertEqual(s["permissions"]["deny"], ["Bash(rm -rf *)"])
        self.assertIn("Edit(/.aok/**)", s["permissions"]["ask"])
        self.assertEqual(len(s["hooks"]["Stop"]), 1)
        self.assertEqual(s["hooks"]["PostToolUse"][0]["matcher"], "Edit|Write|MultiEdit")
        self.assertTrue((self.repo / ".aok/harness/aok_harness.py").is_file())
        self.assertTrue((self.repo / ".aok/harness/adapters/python.json").is_file())
        self.assertEqual(json.loads((self.repo / ".aok/harness.json").read_text()), {"adapter": "python"})
        self.assertTrue((self.repo / ".claude/agents/aok-reviewer.md").is_file())

    def test_installed_copy_answers_hooks(self):
        self.python_repo()
        self.install("--adapter", "python", "--yes")
        self.git("add", "-A")
        self.git("commit", "-qm", "harness")
        cmd = json.loads((self.repo / ".claude/settings.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
        env = {**self.env, "CLAUDE_PROJECT_DIR": str(self.repo)}
        subprocess.run(["/bin/sh", "-c", cmd.replace(" stop", " session-start")], input='{"session_id": "s9", "source": "startup"}',
                       text=True, capture_output=True, env=env, check=True)
        self.write("pkg/__init__.py", ADD_BAD)
        p = subprocess.run(["/bin/sh", "-c", cmd], input='{"session_id": "s9"}', text=True, capture_output=True, env=env)
        self.assertEqual(json.loads(p.stdout)["decision"], "block")

    def test_bad_inputs(self):
        self.python_repo()
        self.assertEqual(self.install("--adapter", "cobol")[0], 2)
        self.write(".claude/settings.json", "{oops")
        self.assertEqual(self.install("--adapter", "python", "--yes")[0], 2)


if __name__ == "__main__":
    unittest.main()
