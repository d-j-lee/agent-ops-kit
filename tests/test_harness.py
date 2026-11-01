"""Tests for the ⑧ work-repo harness core (harness/aok_harness.py). Stdlib unittest only.

The hooks, verify, install and both adapters are tested here. tests/mutations/harness.json
switches the guards of the core and the adapters off one by one, and CI shows that the test named
for each one fails. The list is chosen by hand: a guard missing from it is not shown to be tested.

The tests build throw-away git repositories and call the hooks the way Claude Code does
(payload on stdin). PATH is hermetic: python3 and git are linked into a private bin
directory, and tools like ruff, cargo or fsmap are small stub scripts when a test needs them.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
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

    def hook(self, event, payload=None, extra_env=None, core=None, code=0):
        payload = {"session_id": "s1", **(payload or {})}
        env = {**self.env, "CLAUDE_PROJECT_DIR": str(self.repo), **(extra_env or {})}
        p = subprocess.run([sys.executable, "-I", str(core or CORE), "hook", event], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(p.returncode, code, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else None

    def faulty_core(self, function):
        """A copy of the core whose `function` raises: the crash paths are tested on the real handler code."""
        kit = self.tmp / f"faulty-{function}"
        shutil.copytree(CORE.parent, kit, ignore=shutil.ignore_patterns("__pycache__"))
        src = (kit / "aok_harness.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == function)
        lines = src.splitlines(keepends=True)
        first = fn.body[0].lineno - 1  # the docstring or first statement; the raise goes before it
        lines.insert(first, " " * fn.body[0].col_offset + 'raise RuntimeError("injected fault")\n')
        (kit / "aok_harness.py").write_text("".join(lines), encoding="utf-8")
        return kit / "aok_harness.py"

    def state_file(self, session="s1"):
        return self.repo / ".git" / "aok-harness" / f"session-{session}.json"

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
        self.assertIn("OFF", out["hookSpecificOutput"]["additionalContext"])  # Claude is told too, not only the user

    def test_python_is_detected_by_any_of_its_files(self):
        for marker in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"):
            with self.subTest(marker=marker):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                self.write(marker, "\n")
                self.git("init", "-q", "-b", "main")
                self.assertIn("adapter python", self.start()["hookSpecificOutput"]["additionalContext"])

    def test_continued_session_without_state_owns_the_changes(self):
        self.python_repo()
        self.write("pkg/__init__.py", ADD_BAD)  # left by the session this one continues
        self.stub("ruff", "exit 0\n")
        ctx = self.hook("session-start", {"source": "fork", "session_id": "f1"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("continues another (fork)", ctx)
        self.assertTrue(self.blocked(self.hook("stop", {"session_id": "f1"})))
        self.hook("session-start", {"source": "startup", "session_id": "n1"})  # a new session: not its change
        self.assertIsNone(self.hook("stop", {"session_id": "n1"}))

    def test_damaged_state_restarts_the_baseline_with_a_notice(self):
        self.python_repo()
        self.start()
        self.state_file().write_text("[]", encoding="utf-8")
        ctx = self.start(source="compact")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("state was damaged", ctx)
        self.assertEqual(json.loads(self.state_file().read_text())["baseline"]["head"], self.git("rev-parse", "HEAD").strip())

    def test_the_harness_git_calls_ignore_redirect_variables(self):
        self.python_repo()
        other = self.tmp / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other, env=self.env, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "o"], cwd=other, env=self.env, check=True)
        self.hook("session-start", {"source": "startup"}, extra_env={"GIT_DIR": str(other / ".git")})
        self.assertEqual(json.loads(self.state_file().read_text())["baseline"]["head"], self.git("rev-parse", "HEAD").strip())

    def test_session_ids_cannot_leave_the_state_directory(self):
        self.python_repo()
        self.hook("session-start", {"source": "startup", "session_id": "../../../escape"})
        self.assertEqual([q.name for q in (self.repo / ".git" / "aok-harness").glob("session-*")], ["session-.._.._.._escape.json"])
        self.assertFalse(list(self.tmp.glob("*escape*")))

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

    def test_every_edit_tool_is_checked(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", "def add(a, b)\n")
        for tool in ("Edit", "Write", "MultiEdit"):
            self.assertTrue(self.blocked(self.edit("pkg/__init__.py", tool=tool)), tool)

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

    def test_an_edit_check_that_cannot_run_blocks_and_shares_the_budget(self):
        slow = {"name": "slow", "cmd": ["python3", "-c", "import time; time.sleep(2)"]}
        self.python_repo(config={"adapter": "python", "budget": {"edit": 3},
                                 "edit": {"match": ["*.py"], "steps": [{"name": "lint", "cmd": ["no-such-linter", "{file}"]},
                                                                      slow, slow]}})
        self.start()
        out = self.edit("pkg/__init__.py")
        self.assertTrue(self.blocked(out))
        self.assertIn("[lint] cannot: no-such-linter not found", out["reason"])
        self.assertIn("[slow] cannot: edit budget used up", out["reason"])  # 2 s + 2 s does not fit in 3 s

    def test_an_edit_override_replaces_the_adapter_checks(self):
        self.python_repo(config={"adapter": "python", "edit": {"match": ["*.md"], "steps": [
            {"name": "md", "cmd": ["python3", "-c", "import sys; sys.exit('bad markdown')"]}]}})
        self.start()
        self.write("pkg/__init__.py", "def add(a, b)\n")
        self.assertIsNone(self.edit("pkg/__init__.py"))  # *.py is no longer matched
        self.write("README.md", "x\n")
        self.assertIn("[md] fail", self.edit("README.md")["reason"])

    def test_an_unreadable_state_is_said_not_skipped(self):
        self.python_repo()
        self.start()
        self.state_file().write_text("[]", encoding="utf-8")
        self.write("pkg/__init__.py", ADD_OK + "\n")
        self.assertIn("edit checks did not run", self.edit("pkg/__init__.py")["systemMessage"])

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
        self.assertIn("Block 1 of 3.", out["reason"])
        self.assertNotIn("last block", out["reason"])
        self.assertIn("tell the user instead of editing unrelated code", out["reason"])

    def test_three_blocks_then_released_and_prompt_resets(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        self.assertIn("Block 1 of 3.", self.stop()["reason"])
        self.assertIn("Block 2 of 3.", self.stop(stop_hook_active=True)["reason"])
        third = self.stop(stop_hook_active=True)
        self.assertIn("Block 3 of 3. This is the last block", third["reason"])  # said while Claude can still report
        out = self.stop(stop_hook_active=True)
        self.assertNotIn("decision", out)
        self.assertIn("did NOT pass after 3 blocked attempts", out["systemMessage"])
        self.assertIn("Block 1 of 3.", self.stop()["reason"])  # the count starts again
        self.assertIn("Block 2 of 3.", self.stop()["reason"])
        self.hook("prompt", {"prompt": "go on"})
        self.assertIn("Block 1 of 3.", self.stop()["reason"])

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

    def test_a_file_dirty_at_the_start_becomes_the_sessions_when_it_changes(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        self.write("pkg/__init__.py", ADD_OK + "\n")  # someone's uncommitted change, there before the session
        self.start()
        self.assertIsNone(self.stop())
        self.assertEqual(self.log()[-1]["result"], "skip-no-session-change")
        self.write("pkg/__init__.py", ADD_BAD)
        self.assertTrue(self.blocked(self.stop()))

    def test_a_deleted_test_file_is_reported(self):
        self.python_repo()
        self.write("tests/test_gone.py", TEST.replace("def test_add", "def test_gone"))
        self.git("add", "-A")
        self.git("commit", "-qm", "more")
        self.stub("ruff", "exit 0\n")
        self.start()
        (self.repo / "tests/test_gone.py").unlink()
        self.assertIn("tests/test_gone.py: test file removed or moved where the tests are not collected",
                      self.stop()["systemMessage"])

    def test_a_file_shadowing_a_standard_module_is_reported(self):
        for rel in ("json.py", "json/__init__.py"):
            with self.subTest(rel=rel):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                self.python_repo()
                self.write("tests/test_add.py", "import json\n" + TEST)
                self.git("commit", "-qam", "the tests use json")
                self.stub("ruff", "exit 0\n")
                self.start()
                # discover -t . puts the root first on the import path: the run ends, green, at the first `import json`
                self.write(rel, "import os\nos._exit(0)\n")
                self.write("pkg/__init__.py", ADD_BAD)
                self.assertIn(f"{rel}: shadows the standard module json", self.stop()["systemMessage"])

    def test_the_installed_adapters_come_from_the_base_commit(self):
        self.python_repo()
        code, out = InstallTests.install(self, "--adapter", "python", "--yes")
        self.assertEqual(code, 0, out)
        self.git("add", "-A")
        self.git("commit", "-qm", "harness")
        core = self.repo / ".aok/harness/aok_harness.py"
        self.hook("session-start", {"source": "startup"}, core=core)
        adapter = self.repo / ".aok/harness/adapters/python.json"
        loose = json.loads(adapter.read_text())
        loose["gate"]["quick"] = [{"name": "ok", "cmd": ["python3", "-c", "pass"]}]
        adapter.write_text(json.dumps(loose))
        ctx = self.hook("session-start", {"source": "compact"}, core=core)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(".aok/harness/adapters/python.json changed in this session; the gate uses the version in", ctx)
        self.write("pkg/__init__.py", ADD_BAD)
        out = self.hook("stop", {}, core=core)
        self.assertTrue(self.blocked(out))
        self.assertIn("[test] fail", out["reason"])

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
        self.write("pkg/__init__.py", ADD_BAD)  # there before the session, committed by it unchanged
        self.start()
        self.git("add", "-A")
        self.git("commit", "-qm", "first")
        self.assertTrue(self.blocked(self.stop()))
        self.stub("ruff", "exit 0\n")
        self.write("pkg/__init__.py", ADD_OK.replace("a + b", "a + b  # noqa"))
        self.git("commit", "-qam", "fixed, quietly")
        self.assertIn("pkg/__init__.py: noqa ×1", self.stop()["systemMessage"])  # the first commit's lines are read too

    def test_pass_reports_weakened_checks(self):
        self.python_repo()
        self.write("tests/test_more.py", TEST.replace("def test_add", "def test_more"))
        self.write("tests/test_gone.py", TEST.replace("def test_add", "def test_gone"))
        self.write("tests/test_keep.py", TEST.replace("def test_add", "def test_keep"))  # the gate still runs a test
        self.git("add", "-A")
        self.git("commit", "-qm", "more tests")
        self.start()
        self.write("pkg/__init__.py", ADD_OK.replace("a + b", "a + b  # noqa  # type: ignore  # pragma: no cover")
                   + "# ruff: noqa\n")
        self.write("tests/test_add.py", TEST.replace("        self.assertEqual(add(1, 2), 3)\n", "        pass\n")
                   .replace("    def test_add", "    @unittest.expectedFailure\n    def test_add")
                   + "\n\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite()\n")
        self.write("tests/test_more.py", TEST.replace("def test_add", "def xtest_more")
                   .replace("    def ", "    @unittest.skipIf(True, 'later')\n    def "))
        self.git("mv", "tests/test_gone.py", "tests/check_gone.py")
        msg = self.stop()["systemMessage"]
        self.assertIn("gate passed", msg)
        for item in ("pkg/__init__.py: noqa ×2", "pkg/__init__.py: type-ignore ×1", "pkg/__init__.py: no-cover ×1",
                     "tests/test_add.py: assertion-removed ×1", "tests/test_add.py: skipped-test ×1",
                     "tests/test_add.py: custom-test-loading ×1", "tests/test_more.py: test-removed-or-renamed ×1",
                     "tests/test_more.py: skipped-test ×1",
                     "tests/test_gone.py: test file removed or moved where the tests are not collected"):
            self.assertIn(item, msg)

    def test_more_weakening_forms_are_reported(self):
        self.python_repo()
        base = {"t_plain": "    def test_a(self):\n        assert add(1, 2) == 3\n",
                "t_raises": "    def test_a(self):\n        with self.assertRaises(TypeError):\n            add(1)\n",
                "t_mock": "    def test_a(self):\n        m = mock.Mock()\n        m(1)\n        m.assert_called_once_with(1)\n",
                "t_pyraises": "    def test_a(self):\n        with pytest.raises(TypeError):\n            add(1)\n",
                "t_camel": "    def testAdd(self):\n        self.assertEqual(add(1, 2), 3)\n",
                "t_async": "    async def test_a(self):\n        self.assertEqual(add(1, 2), 3)\n",
                "t_class": "    def test_a(self):\n        self.assertEqual(add(1, 2), 3)\n"}
        head_ = "import unittest\nfrom unittest import mock\nfrom pkg import add\ntry:\n    import pytest\nexcept ImportError:\n    pytest = None\n\n\nclass T(unittest.TestCase):\n"
        for name, body in base.items():
            self.write(f"tests/{name}.py", head_ + body)
        self.write("tests/test_keep.py", TEST.replace("def test_add", "def test_keep"))
        self.git("add", "-A")
        self.git("commit", "-qm", "tests")
        self.stub("ruff", "exit 0\n")
        self.start()
        weakened = {"t_plain": base["t_plain"].replace("        assert add(1, 2) == 3\n", "        pass\n"),
                    "t_raises": "    def test_a(self):\n        pass\n",
                    "t_mock": base["t_mock"].replace("        m.assert_called_once_with(1)\n", ""),
                    "t_pyraises": "    def test_a(self):\n        pass\n",
                    "t_camel": base["t_camel"].replace("testAdd", "xtestAdd"),
                    "t_async": base["t_async"].replace("async def test_a", "async def check_a"),
                    "t_class": base["t_class"]}
        for name, body in weakened.items():
            self.write(f"tests/{name}.py", (head_.replace("(unittest.TestCase)", "") if name == "t_class" else head_) + body)
        self.write("tests/test_skips.py", "import unittest\ntry:\n    import pytest\nexcept ImportError:\n    pytest = None\n"
                   "if pytest:\n    @pytest.mark.skip\n    def test_a():\n        pass\n\n"
                   "    @pytest.mark.xfail\n    def test_b():\n        pass\n\n"
                   "    def test_c():\n        pytest.skip('later')\n\n\n"
                   "class T(unittest.TestCase):\n    __unittest_skip__ = True\n\n"
                   "    @unittest.skipUnless(False, 'x')\n    def test_d(self):\n        self.skipTest('x')\n\n"
                   "    def test_e(self):\n        raise unittest.SkipTest('x')\n\n\nload_tests = None\n")
        self.write("pkg/__init__.py", ADD_OK + "# flake8: noqa\nX = 1  # NOQA\n")
        self.write("pyproject.toml", '[project]\nname = "x"\n\n[tool.ruff.lint.per-file-ignores]\n"pkg/*" = ["F"]\n')
        msg = self.stop()["systemMessage"]
        for item in ("tests/t_plain.py: assertion-removed ×1", "tests/t_raises.py: assertion-removed ×1",
                     "tests/t_mock.py: assertion-removed ×1", "tests/t_pyraises.py: assertion-removed ×1",
                     "tests/t_camel.py: test-removed-or-renamed ×1", "tests/t_async.py: test-removed-or-renamed ×1",
                     "tests/t_class.py: test-class-removed ×1", "tests/test_skips.py: skipped-test ×7",
                     "tests/test_skips.py: custom-test-loading ×1", "pkg/__init__.py: noqa ×2",
                     "check settings changed in this session, review what they switch off: pyproject.toml"):
            self.assertIn(item, msg)

    def test_a_test_file_moved_where_it_is_not_collected_is_reported(self):
        for dest, reported in (("tests/sub/test_more.py", True), ("tests/test-more.py", True),
                               ("tests/pkg2/test_more.py", False)):
            with self.subTest(dest=dest):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                self.python_repo()
                self.write("tests/test_more.py", TEST.replace("def test_add", "def test_more"))
                self.write("tests/pkg2/__init__.py", "")
                self.git("add", "-A")
                self.git("commit", "-qm", "more")
                self.stub("ruff", "exit 0\n")
                self.start()
                (self.repo / dest).parent.mkdir(parents=True, exist_ok=True)
                self.git("mv", "tests/test_more.py", dest)
                out = self.stop()
                msg = (out or {}).get("systemMessage", "")
                self.assertEqual("tests/test_more.py: test file removed or moved where the tests are not collected" in msg,
                                 reported, msg)

    def test_a_merged_side_branch_is_the_sessions(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.git("checkout", "-qb", "side")
        self.write("pkg/__init__.py", ADD_BAD)
        self.git("commit", "-qam", "broken on the side")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.assertTrue(self.blocked(self.stop()))  # the session's own merge is judged
        self.write("pkg/__init__.py", ADD_OK.replace("a + b", "a + b  # noqa"))
        self.git("checkout", "-qb", "side2")
        self.git("commit", "-qam", "quiet on the side")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge side2", "side2")
        self.assertIn("pkg/__init__.py: noqa ×1", self.stop()["systemMessage"])

    def test_a_branch_from_before_the_session_is_not_its_own(self):
        self.python_repo()
        self.git("checkout", "-qb", "theirs")
        self.write("pkg/__init__.py", ADD_OK.replace("a + b", "a + b  # noqa"))
        self.git("commit", "-qam", "someone else's")
        self.git("checkout", "-q", "main")
        self.stub("ruff", "exit 0\n")
        self.start()
        self.git("merge", "-q", "--no-ff", "-m", "merge theirs", "theirs")
        out = self.stop()  # HEAD moved, so the tree is judged; the noqa is not this session's
        self.assertNotIn("noqa", (out or {}).get("systemMessage", ""))
        self.assertEqual(self.log()[-1]["result"], "pass")

    def test_file_names_that_are_not_utf8_do_not_break_the_hooks(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        (self.repo / os.fsdecode(b"caf\xe9.txt")).write_text("x\n", encoding="utf-8")
        self.assertIn("adapter python", self.start()["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.stop())
        self.assertIn("uncommitted changes", self.bash("git push")["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_new_untracked_test_file_is_read(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("tests/test_new.py", TEST.replace("    def test_add", "    @unittest.skip('later')\n    def test_add"))
        self.assertIn("tests/test_new.py: skipped-test ×1", self.stop()["systemMessage"])

    def test_ignored_harness_changes_are_reported_at_the_end(self):
        self.python_repo()
        self.write(".gitignore", ".claude/settings.local.json\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "ignore local settings")
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write(".claude/settings.local.json", json.dumps({"env": {"PATH": "/tmp"}}))
        self.write("pkg/__init__.py", ADD_OK + "\n")
        self.assertIn("harness file changed in this session: .claude/settings.local.json", self.stop()["systemMessage"])

    def test_a_rewritten_assertion_is_not_a_removed_one(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("tests/test_add.py", TEST.replace("3)", "3, 'add is plain addition')"))
        self.assertIsNone(self.stop(), "a stronger assertion is not a weakened check")

    def test_weakening_is_reported_after_a_push_passed_the_gate(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("pkg/__init__.py", ADD_OK.replace("a + b", "a + b  # noqa"))
        self.git("commit", "-qam", "quiet")
        self.assertIsNone(self.bash("git push"))  # the gate passes here first
        self.assertIn("pkg/__init__.py: noqa ×1", self.stop()["systemMessage"])
        self.assertIsNone(self.stop())  # reviewed once

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
        self.assertIn(".aok/harness.json differs from the version in", msg)
        self.assertIn("harness file changed in this session: .aok/harness.json", msg)

    def test_an_unchanged_definition_is_not_reported(self):
        self.python_repo()
        self.write(".aok/harness.json", '{\n  "adapter": "python"\n}\n')  # as install writes it, last newline included
        self.git("add", "-A")
        self.git("commit", "-qm", "harness")
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        self.assertIsNone(self.stop())

    def test_a_definition_the_base_commit_lacks_is_not_used(self):
        self.python_repo()  # no .aok/harness.json committed: the adapter is detected
        self.write(".aok/harness.json", json.dumps({"adapter": "python", "gate": {"quick": [{"name": "ok", "cmd": ["python3", "-c", "pass"]}]}}))
        ctx = self.start()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("applies to sessions that start after it is committed", ctx)
        self.write("pkg/__init__.py", ADD_BAD)
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("[test] fail", out["reason"])

    def test_an_unreadable_base_definition_blocks_only_a_session_with_changes(self):
        self.python_repo(config={"adapter": "cobol"})
        self.start()
        self.assertIsNone(self.stop())  # nothing of this session's to judge
        self.write("pkg/__init__.py", ADD_OK + "\n")
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("the gate cannot run: unknown adapter 'cobol'", out["reason"])
        self.git("commit", "-qam", "x")
        self.assertIn("gate cannot run", self.bash("git push")["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_damaged_or_missing_state_blocks(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        head_ = self.git("rev-parse", "HEAD").strip()
        self.state_file().write_text(json.dumps({"baseline": {"head": head_, "pending": {}, "protected": []}}), encoding="utf-8")
        self.assertIn("damaged", self.stop()["reason"])  # a baseline whose protected hashes are not a mapping
        self.state_file().write_text(json.dumps({"baseline": {"head": 12345, "pending": []}}), encoding="utf-8")
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("session state", out["reason"])
        self.assertIn("damaged", out["reason"])
        self.git("commit", "-qam", "x")
        self.assertIn("damaged", self.bash("git push")["hookSpecificOutput"]["permissionDecisionReason"])
        self.state_file().unlink()
        self.assertIn("no baseline", self.stop()["reason"])
        self.assertIn("no baseline", self.bash("git push")["hookSpecificOutput"]["permissionDecisionReason"])

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
        for kind in ("subagent", "workflow", "teammate", "cloud session"):
            out = self.stop(background_tasks=[{"id": "x", "type": kind, "status": "running"}])
            self.assertFalse(self.blocked(out), kind)
            self.assertIn(f"background agents are still working ({kind})", out["systemMessage"])
        self.assertEqual(self.log()[-1]["result"], "skip-background")
        # a shell or monitor left running is not the turn's work: it must not switch the gate off
        self.assertTrue(self.blocked(self.stop(background_tasks=[{"id": "y", "type": "shell", "command": "sleep 999"},
                                                                  {"id": "z", "type": "monitor"}])))
        self.assertTrue(self.blocked(self.stop(background_tasks=[])))

    def test_steps_get_the_adapter_env(self):
        probe = {"name": "env", "cmd": ["python3", "-c", "import os, sys; sys.exit(os.environ.get('PYTHONDONTWRITEBYTECODE') != '1')"]}
        self.python_repo(config={"adapter": "python", "extra": [probe]})
        self.stub("ruff", "exit 0\n")
        code, out = self.verify("--mode", "quick", "--json")
        self.assertEqual(code, 0, out)

    def test_steps_do_not_inherit_git_redirect_variables(self):
        names = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_COMMON_DIR", "GIT_NAMESPACE")
        check = {"name": "env", "cmd": ["python3", "-c", f"import os, sys; sys.exit(any(n in os.environ for n in {names!r}))"]}
        self.python_repo(config={"adapter": "python", "extra": [check]})
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        # values that name this same repo, so git works whether or not they leak (how git reads a wrong value
        # differs by version); only the step's view of its environment tells the two apart
        git_dir = self.repo / ".git"
        same = {"GIT_DIR": git_dir, "GIT_WORK_TREE": self.repo, "GIT_INDEX_FILE": git_dir / "index",
                "GIT_OBJECT_DIRECTORY": git_dir / "objects", "GIT_ALTERNATE_OBJECT_DIRECTORIES": git_dir / "objects",
                "GIT_COMMON_DIR": git_dir, "GIT_NAMESPACE": ""}
        self.assertEqual(set(same), set(names))
        self.assertIsNone(self.hook("stop", {}, extra_env={n: str(v) for n, v in same.items()}))

    def test_without_git_the_gate_runs_every_time(self):
        self.python_repo(commit=False)
        shutil.rmtree(self.repo / ".git")
        self.stub("ruff", "exit 0\n")
        self.start()
        self.assertIsNone(self.stop())
        self.write("pkg/__init__.py", ADD_BAD)
        self.assertIn("Block 1 of 3.", self.stop()["reason"])
        self.assertIn("Block 2 of 3.", self.stop()["reason"])  # strikes kept in XDG_STATE_HOME
        # without git there is no baseline to miss: a session whose start was not seen is still judged by the gate
        self.assertIn("[test] fail", self.hook("stop", {"session_id": "other"})["reason"])

    def test_a_crash_is_reported_not_passed(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        out = self.hook("stop", core=self.faulty_core("changed_since"))
        self.assertTrue(self.blocked(out))
        self.assertIn("the harness crashed: RuntimeError('injected fault')", out["reason"])
        bad = self.state_file()
        bad.unlink()
        bad.mkdir()  # the state itself cannot be read or written: still a block, not a silent end
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("the gate did not run", out["reason"])
        self.assertIn("Block 1 of 3.", out["reason"])  # counted elsewhere, so the exit still comes
        self.assertIn("Block 2 of 3.", self.stop()["reason"])
        self.assertIn("This is the last block", self.stop()["reason"])
        out = self.stop()
        self.assertNotIn("decision", out)
        self.assertIn("did NOT pass after 3 blocked attempts", out["systemMessage"])


class PreBashTests(Harness):
    def test_other_commands_are_left_alone(self):
        self.python_repo()
        self.start()
        self.assertIsNone(self.bash("ls -la && git status"))

    def test_no_verify_is_denied(self):
        self.python_repo()
        self.start()
        for cmd in ('git commit --no-"verify" -m x', "git commit --no-verif -m x", "git push --no-veri",
                    "git config core.hooks\\Path /dev/null", 'git -c core."hooksPath"=/dev/null commit -m x',
                    "git commit -m x --no-verify", "git -c core.hooksPath=/dev/null push", "git commit -n -m x",
                    'git commit --no-verify -m "unbalanced',
                    "git commit -nm x", "git commit -am x -n", "git commit -vnm x", "git config core.hooksPath /dev/null",
                    "git -c CORE.HOOKSPATH=x commit -m y", "git push '--no-verify'", 'sh -c "git commit -n -m x"'):
            out = self.bash(cmd)
            self.assertTrue(self.denied(out), cmd)
            self.assertIn("hooks off", out["hookSpecificOutput"]["permissionDecisionReason"], cmd)
        for cmd in ("git commit -m -n", "git commit --message -n", "git commit -- -n", "git log -n 3",
                    "grep -i core.hookspath .git/config", "git commit --no-verbose -m x"):
            self.assertIsNone(self.bash(cmd), cmd)

    def test_push_with_uncommitted_changes_is_denied(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn("uncommitted", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.bash("git add -A && git commit -m x && git push")
        self.assertTrue(self.denied(out))
        self.assertIn("commit and publishing in one command", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.bash('git commit -am "docs: say git push in the readme"'))  # not publishing

    def test_only_the_checked_out_tree_is_published(self):
        self.python_repo()
        self.git("branch", "bad")
        self.start()
        reason = lambda out: out["hookSpecificOutput"]["permissionDecisionReason"]  # noqa: E731
        for cmd, why in (("git push origin bad", "push the checked-out branch only"),
                         ("git push origin bad:main", "push the checked-out branch only"),
                         ("git push --all origin", "no --all"),
                         ("git merge bad && git push", "merge and publishing in one command"),
                         ("gh pr create --fill --head bad", "open the pull request from the checked-out branch"),
                         ('bash -c "git push origin bad"', "push the checked-out branch only"),
                         ('git push "origin', "could not be read"),
                         # wrappers, substitutions and line breaks must not hide the push
                         ("git status\ngit push origin bad", "push the checked-out branch only"),
                         ("git \\\n  push origin bad", "push the checked-out branch only"),
                         ("env FOO=1 git push origin bad", "push the checked-out branch only"),
                         ("FOO=1 time git push origin bad", "push the checked-out branch only"),
                         ("sudo -u me git push origin bad", "push the checked-out branch only"),
                         ('echo "$(git push origin bad)"', "push the checked-out branch only"),
                         ("echo `git push origin bad`", "push the checked-out branch only"),
                         ('eval "git push origin bad"', "push the checked-out branch only"),
                         ("(cd . && git push origin bad)", "push the checked-out branch only"),
                         ('xargs -I{} sh -c "git push origin bad"', "push the checked-out branch only"),
                         ('echo "it\'s $(git push origin bad:main) and that\'s it"', "push the checked-out branch only"),
                         ("git push origin `git rev-parse bad`:main", "push the checked-out branch only"),
                         ("git -c remote.origin.push=refs/heads/bad:refs/heads/main push origin", "changes what git sends"),
                         ("git push --branches origin", "no --all"),
                         ("git push --mirror origin", "no --all"),
                         ("git push --tags origin", "no --all"),
                         ("gh pr create --fill -H bad", "open the pull request from the checked-out branch"),
                         ("gh pr create --fill --head=bad", "open the pull request from the checked-out branch"),
                         ("gh pr create --fill -Hbad", "open the pull request from the checked-out branch"),
                         ("gh pr new --fill --head bad", "open the pull request from the checked-out branch"),
                         ('gh pr create --title "x', "could not be read"),
                         ('gh pr new --title "x', "could not be read"),
                         ("env -u CLAUDECODE git push", "CLAUDECODE"),
                         ("X=CLAUDECODE; unset $X; git push", "mentions CLAUDECODE"),  # not spelled out as a change
                         ("git -c push.default=matching push", "changes what git sends"),
                         ("git -c branch.main.remote=elsewhere push", "changes what git sends"),
                         ("git commit -n -m x", "hooks off")):
            out = self.bash(cmd)
            self.assertTrue(self.denied(out), cmd)
            self.assertIn(why, reason(out), cmd)
        for cmd in ("echo '$(git push origin bad)'", 'git commit -m "fix the push logic"', "git log --grep push"):
            self.assertIsNone(self.bash(cmd), cmd)  # quoted text and other git commands are not publishing
        for mover in ("merge", "rebase", "reset", "checkout", "switch", "commit", "cherry-pick", "pull", "am", "revert",
                      "stash", "update-ref", "symbolic-ref", "branch", "tag", "restore", "worktree", "filter-branch",
                      "replace"):
            out = self.bash(f"git {mover} x y && git push")
            self.assertIn(f"{mover} and publishing in one command", reason(out), mover)
        for cmd in ("git branch -f main bad && git push", "git tag v1 && git push", "git stash && git push",
                    "git worktree add ../w && git push", "git symbolic-ref HEAD refs/heads/bad && git push",
                    "git branch --delete bad && git push"):
            self.assertTrue(self.denied(self.bash(cmd)), cmd)
        for cmd in ("git branch --show-current && git push", "git branch && git push", "git branch -a -v && git push",
                    "git branch --list 'b*' && git push", "git tag -l && git push", "git tag && git push",
                    "git branch --format '%(refname)' && git push", "git tag -n3 && git push",
                    "git stash list && git push", "git worktree list && git push", "git symbolic-ref HEAD && git push",
                    "git symbolic-ref --short -q HEAD && git push"):
            self.assertIsNone(self.bash(cmd), cmd)  # reading branches and refs moves nothing

    def test_publishing_is_recognised_through_options(self):
        self.python_repo()
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        self.git("commit", "-qam", "bad")
        for cmd in ("gh -R o/r pr create --fill", "gh --repo o/r pr create --fill", 'git -C "/tmp/my dir" push',
                    "git push origin main", "git push origin HEAD:refs/heads/main"):
            out = self.bash(cmd)
            self.assertTrue(self.denied(out), cmd)
            self.assertIn("[test] fail", out["hookSpecificOutput"]["permissionDecisionReason"], cmd)

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
        self.assertIsNone(self.bash("git push origin main\necho pushed"))  # the next line is not one of push's refs
        for cmd in ("git push origin main 2>&1", "git push -u origin main > push.log", "git push -o ci.skip origin main",
                    "git push --push-option ci.skip origin main", "git push origin @:main", "git push origin +main",
                    "git push origin refs/heads/main", "git push --receive-pack git-receive-pack origin main",
                    "git push --exec git-receive-pack origin main"):
            self.assertIsNone(self.bash(cmd), cmd)  # redirections, option values and @ are not refs
        self.assertEqual(self.log()[-1], {**self.log()[-1], "event": "pre-bash", "result": "pass"})

    def test_push_after_a_harness_change_is_denied(self):
        self.python_repo(config={"adapter": "python"})
        self.start()
        self.write(".aok/harness.json", json.dumps({"adapter": "python", "extra": []}))
        self.git("commit", "-qam", "tweak harness")
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn(".aok/harness.json", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_push_after_an_ignored_local_settings_change_is_denied(self):
        self.python_repo()
        self.write(".gitignore", ".claude/settings.local.json\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "ignore local settings")
        self.start()
        self.write(".claude/settings.local.json", json.dumps({"disableAllHooks": True}))
        self.assertEqual(self.git("status", "--porcelain"), "")  # git does not show it
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn(".claude/settings.local.json", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_hook_and_env_changes_in_local_settings_are_denied(self):
        for change in ({"hooks": {}}, {"env": {"PATH": "/tmp"}}, {"disableAllHooks": True}):
            with self.subTest(change=change):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                self.python_repo()
                self.write(".gitignore", ".claude/settings.local.json\n")
                self.write(".claude/settings.local.json", json.dumps({"permissions": {"allow": []}}))
                self.git("add", ".gitignore")
                self.git("commit", "-qm", "ignore local settings")
                self.start()
                self.write(".claude/settings.local.json", json.dumps({"permissions": {"allow": []}, **change}))
                self.assertTrue(self.denied(self.bash("git push")), change)

    def test_a_worktree_watches_the_main_checkouts_local_settings(self):
        self.python_repo()
        wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", str(wt), "-b", "feature")
        main, self.repo = self.repo, wt
        self.start()
        (main / ".claude").mkdir(exist_ok=True)
        (main / ".claude" / "settings.local.json").write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn("settings.local.json", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_permission_rules_claude_code_writes_are_not_tampering(self):
        self.python_repo()
        self.write(".gitignore", ".claude/settings.local.json\n")
        self.write(".claude/settings.local.json", json.dumps({"permissions": {"allow": []}}))
        self.stub("ruff", "exit 0\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore local settings")
        self.start()
        # the user chose "don't ask again": Claude Code adds the rule to the local settings
        self.write(".claude/settings.local.json", json.dumps({"permissions": {"allow": ["Bash(npm test)"]}}))
        self.assertIsNone(self.bash("git push"))

    def test_push_after_settings_ci_or_reviewer_changes_is_denied(self):
        for rel in (".claude/settings.json", ".github/workflows/ci.yml", ".claude/agents/aok-reviewer.md"):
            with self.subTest(rel=rel):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                self.python_repo()
                self.start()
                self.write(rel, json.dumps({"hooks": {}}) + "\n")
                self.git("add", "-A")
                self.git("commit", "-qm", "config")
                out = self.bash("git push")
                self.assertTrue(self.denied(out))
                self.assertIn(rel, out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_crash_denies_a_publishing_command(self):
        self.python_repo()
        self.start()
        core = self.faulty_core("changed_since")
        self.hook("session-start", {"source": "startup"}, core=core)  # its pre-push hook runs this copy
        out = self.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": "git push"}}, core=core)
        self.assertTrue(self.denied(out))
        self.assertIn("the harness crashed (RuntimeError('injected fault'))", out["hookSpecificOutput"]["permissionDecisionReason"])
        core = self.faulty_core("hooks_off")
        for cmd in ('sh -c "git push"', "gh pr new --fill", "git send-pack ../r main"):
            out = self.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": cmd}}, core=core)
            self.assertTrue(self.denied(out), cmd)  # recognised as publishing even when the crash came before the parsing
        self.assertIsNone(self.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, core=core))

    def test_a_shadowing_unittest_module_does_not_pass_the_gate(self):
        self.python_repo()
        self.start()
        self.write("unittest.py", "import sys\nsys.exit(0)\n")
        self.write("pkg/__init__.py", ADD_BAD)
        self.assertIn("[test] fail", self.stop()["reason"])

    def test_the_uncommitted_files_are_named(self):
        self.python_repo()
        self.start()
        self.write("report.xml", "<x/>\n")  # a file a check left behind
        reason = self.bash("git push")["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("uncommitted changes (report.xml)", reason)

    def test_permission_rules_in_any_settings_file_are_not_a_harness_change(self):
        self.python_repo()
        self.write(".claude/settings.json", json.dumps({"hooks": {"Stop": []}}))
        self.git("add", "-A")
        self.git("commit", "-qm", "settings")
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write(".claude/settings.json", json.dumps({"hooks": {"Stop": []}, "permissions": {"allow": ["Bash(npm test)"]}}))
        self.write(".claude/settings.local.json", json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))  # not ignored
        self.assertNotIn("harness file changed", (self.stop() or {}).get("systemMessage", ""))
        self.git("add", ".claude/settings.json")
        self.git("commit", "-qm", "allow npm test")
        (self.repo / ".claude/settings.local.json").unlink()
        self.assertIsNone(self.bash("git push"))

    def test_an_ignored_file_under_the_harness_directory_is_watched(self):
        self.python_repo()
        self.write(".gitignore", ".aok/local/\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "ignore")
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write(".aok/local/override.json", "{}\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn(".aok/local/override.json", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_ways_to_change_what_the_push_hook_sees_are_denied(self):
        self.python_repo()
        self.start()
        reason = lambda out: out["hookSpecificOutput"]["permissionDecisionReason"]  # noqa: E731
        for cmd, why in (("CLAUDECODE= sh push.sh", "CLAUDECODE"), ("env -u CLAUDECODE sh push.sh", "CLAUDECODE"),
                         ("unset CLAUDECODE; sh push.sh", "CLAUDECODE"), ('CLAUDE""CODE= sh push.sh', "CLAUDECODE"),
                         ("export CLAUDECODE=; sh push.sh", "CLAUDECODE"), ("env --unset=CLAUDECODE sh push.sh", "CLAUDECODE"),
                         ("env -i sh push.sh", "clears the environment"), ("env -i PATH=/bin sh push.sh", "clears the environment"),
                         ("env - sh push.sh", "clears the environment"), ("env --ignore-environment sh push.sh", "clears"),
                         ("env -u HOME -i sh push.sh", "clears the environment"), ("exec -c sh push.sh", "clears"),
                         ("echo exit 0 >.git/hooks/pre-push", "git hooks"), ("git -c 'alias.x=!sh x.sh' status", "shell alias"),
                         ("GIT_CONFIG_GLOBAL=x.cfg git p", "other configuration"), ("GIT_CONFIG_COUNT=1 sh push.sh", "other configuration"),
                         ("export GIT_CONFIG_PARAMETERS=x; sh push.sh", "other configuration"),
                         ("git --config-env=alias.p=P p", "other configuration"),
                         ("git config alias.p '!sh push.sh'", "shell alias"), ("git -c 'alias.p=!git push' p", "shell alias"),
                         ("git config --global alias.p '!git push'", "shell alias"),
                         ("rm .git/hooks/pre-push && sh push.sh", "git hooks"), ("cp x .git/hooks/pre-push", "git hooks"),
                         ("git send-pack ../remote.git main", "send-pack")):
            out = self.bash(cmd)
            self.assertTrue(self.denied(out), cmd)
            self.assertIn(why, reason(out), cmd)
        for cmd in ("echo $CLAUDECODE", "grep -rn CLAUDECODE docs", "git config alias.st status", "git st",
                    "git config --unset core.hooksPath", "env FOO=1 python3 -c pass", "env -u HOME python3 -c pass"):
            self.assertIsNone(self.bash(cmd), cmd)


class PushHookTests(Harness):
    """The git pre-push hook the session start installs: inside Claude Code only commits the gate passed go out,
    however the push names them."""

    def remote_repo(self):
        self.python_repo()
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], env=self.env, check=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "origin", "main")
        return remote

    def push(self, *args, claude=True):
        env = {**self.env, **({"CLAUDECODE": "1"} if claude else {})}
        return subprocess.run(["git", *args], cwd=self.repo, env=env, capture_output=True, text=True)

    def remote_head(self, remote):
        return subprocess.run(["git", "rev-parse", "main"], cwd=remote, env=self.env, capture_output=True, text=True).stdout.strip()

    def test_the_hook_is_installed_and_announced(self):
        self.python_repo()
        self.assertIn("git pre-push hook", self.start()["hookSpecificOutput"]["additionalContext"])
        self.assertIn("aok-harness pre-push", (self.repo / ".git" / "hooks" / "pre-push").read_text())
        self.start(source="compact")  # refreshing keeps one hook
        self.assertEqual((self.repo / ".git" / "hooks" / "pre-push").read_text().count("aok-harness pre-push"), 1)

    def test_only_commits_the_gate_passed_can_be_pushed(self):
        remote = self.remote_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        self.git("commit", "-qam", "good")
        self.assertIsNone(self.bash("git push origin main"))  # the gate passes and records the commit
        self.assertEqual(self.push("push", "-q", "origin", "main").returncode, 0)
        self.git("checkout", "-qb", "bad")
        self.write("pkg/__init__.py", ADD_BAD)
        self.git("commit", "-qam", "bad")
        self.git("checkout", "-q", "main")
        good = self.remote_head(remote)
        for args in (["-c", "remote.origin.push=refs/heads/bad:refs/heads/main", "push", "origin"],
                     ["push", "origin", "bad:main"]):
            p = self.push(*args)
            self.assertNotEqual(p.returncode, 0, args)
            self.assertIn("[aok-harness] push refused", p.stderr, args)
            self.assertEqual(self.remote_head(remote), good, args)
        self.git("update-ref", "refs/heads/main", "bad")  # moved in an earlier command
        self.assertIn("[aok-harness] push refused", self.push("push", "origin", "main").stderr)
        self.assertEqual(self.push("push", "-q", "origin", "main", claude=False).returncode, 0)  # a person may
        self.assertNotEqual(self.remote_head(remote), good)
        self.assertEqual(self.push("push", "-q", "origin", "bad:old", claude=False).returncode, 0)
        self.assertEqual(self.push("push", "-q", "origin", ":old").returncode, 0)  # a deletion sends no commit

    def test_a_pass_on_a_dirty_tree_does_not_clear_the_commit(self):
        remote = self.remote_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        self.git("commit", "-qam", "bad")
        self.write("pkg/__init__.py", ADD_OK)  # fixed in the work tree only: the gate passes, the commit is still bad
        self.assertIsNone(self.stop())
        self.assertIn("[aok-harness] push refused", self.push("push", "origin", "main").stderr)
        self.assertNotEqual(self.remote_head(remote), self.git("rev-parse", "HEAD").strip())

    def test_a_changed_hook_stops_publishing(self):
        self.python_repo()
        self.start()
        (self.repo / ".git" / "hooks" / "pre-push").write_text("#!/bin/sh\nexit 0\n")
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn("pre-push hook was removed or changed", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_changed_hook_is_restored_before_the_next_command(self):
        self.python_repo()
        self.start()
        hook = self.repo / ".git" / "hooks" / "pre-push"
        script = hook.read_text()
        hook.write_text("#!/bin/sh\nexit 0\n")  # by a script the command reading did not see
        self.assertIn("restored", self.bash("ls")["systemMessage"])
        self.assertEqual(hook.read_text(), script)
        hook.unlink()
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn("restored", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(hook.read_text(), script)
        self.assertTrue(os.access(hook, os.X_OK))
        fake = self.tmp / "fake" / "aok_harness.py"  # the harness's script, but running some other file
        fake.parent.mkdir()
        fake.write_text("import sys\nsys.exit(0)\n")
        hook.write_text(script.replace(str(CORE), str(fake)))
        self.assertIn("restored", self.bash("ls")["systemMessage"])
        self.assertEqual(hook.read_text(), script)
        self.stub("ruff", "exit 0\n")
        hook.write_text("#!/bin/sh\nexit 0\n")
        self.assertIn("restored", self.stop()["systemMessage"])  # the end of the turn looks too, changes or not
        hook.write_text("#!/bin/sh\nexit 0\n")
        self.write("pkg/__init__.py", ADD_OK + "\n")
        self.assertIn("restored", self.stop()["systemMessage"])

    def test_a_hook_that_cannot_be_restored_stops_publishing(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        shutil.rmtree(self.repo / ".git" / "hooks")
        (self.repo / ".git" / "hooks").write_text("not a directory\n")
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn("pre-push hook was removed or changed", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_hooks_path_set_in_the_session_is_caught(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.git("config", "core.hooksPath", "/dev/null")  # by a script the command reading did not see
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn("core.hooksPath", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("core.hooksPath", out["reason"])
        self.assertIsNone(self.bash("git config --unset core.hooksPath"))  # the way back is not refused
        self.git("config", "--unset", "core.hooksPath")
        self.assertIsNone(self.stop())

    def test_worktrees_share_the_hook_without_rewriting_it(self):
        self.python_repo()
        code, out = InstallTests.install(self, "--adapter", "python", "--yes")
        self.assertEqual(code, 0, out)
        self.git("add", "-A")
        self.git("commit", "-qm", "harness")
        wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", str(wt), "-b", "feature")
        self.hook("session-start", {"source": "startup"}, core=self.repo / ".aok/harness/aok_harness.py")
        hook = self.repo / ".git" / "hooks" / "pre-push"
        written = hook.read_text()
        self.repo = wt  # one hooks directory for both; each has its own copy of the core
        wt_core = wt / ".aok/harness/aok_harness.py"
        self.hook("session-start", {"source": "startup", "session_id": "w1"}, core=wt_core)
        self.assertIsNone(self.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "w1"},
                                    core=wt_core))
        self.assertEqual(hook.read_text(), written)

    def test_a_new_session_restores_a_changed_harness_hook(self):
        self.python_repo()
        self.start()
        hook = self.repo / ".git" / "hooks" / "pre-push"
        script = hook.read_text()
        hook.write_text(script.replace("exec python3", "exit 0 #"))  # still marked as the harness's
        self.start()
        self.assertEqual(hook.read_text(), script)

    def test_a_pass_at_the_end_of_the_turn_lets_the_commit_out(self):
        remote = self.remote_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.write("pkg/__init__.py", ADD_OK + "\n")
        self.git("commit", "-qam", "good")
        self.assertIsNone(self.stop())
        self.assertEqual(self.push("push", "-q", "origin", "main").returncode, 0)
        self.assertEqual(self.remote_head(remote), self.git("rev-parse", "HEAD").strip())

    def test_an_alias_is_read_as_what_it_runs(self):
        self.python_repo()
        self.git("config", "alias.p", "push")
        self.git("config", "alias.sp", "!git push origin main")
        self.git("config", "alias.lg", "!git log --oneline")
        self.start()
        self.write("pkg/__init__.py", ADD_BAD)
        self.git("commit", "-qam", "bad")
        out = self.bash("git p origin main")
        self.assertTrue(self.denied(out))
        self.assertIn("[test] fail", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIn("push the checked-out branch only", self.bash("git p origin HEAD~1:main")
                      ["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.bash("git sp")
        self.assertTrue(self.denied(out))
        self.assertIn("shell alias", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.bash("git lg"))  # a shell alias that does not publish

    def test_a_hook_that_is_not_the_harnesss_is_left_alone(self):
        self.python_repo()
        self.start()  # the harness's hook first; then a person puts their own in its place
        theirs = "#!/bin/sh\necho theirs\n"
        (self.repo / ".git" / "hooks").mkdir(exist_ok=True)
        (self.repo / ".git" / "hooks" / "pre-push").write_text(theirs)
        self.assertIn("not the harness's", self.start()["hookSpecificOutput"]["additionalContext"])
        self.assertEqual((self.repo / ".git" / "hooks" / "pre-push").read_text(), theirs)
        self.assertIsNone(self.bash("ls"))  # not restored over theirs either
        self.assertEqual((self.repo / ".git" / "hooks" / "pre-push").read_text(), theirs)

    def test_no_hook_when_core_hooks_path_is_set(self):
        self.python_repo()
        self.stub("ruff", "exit 0\n")
        self.start()
        self.git("config", "core.hooksPath", ".husky")  # set by a person between sessions
        self.assertIn("core.hooksPath is set", self.start()["hookSpecificOutput"]["additionalContext"])
        self.assertFalse((self.repo / ".husky").exists())
        self.assertIsNone(self.bash("git push"))  # read from the command only, as the session start said


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
        for bad, why in (({"gate": []}, "gate must be"), ({"budget": 5}, "budget must be"),
                         ({"budget": {"gate": "abc"}}, "budget.gate must be"), ({"budget": {"gate": 1800}}, "from 1 to 840"),
                         ({"budget": {"edit": 0}}, "budget.edit must be"), ({"budget": {"gate": True}}, "budget.gate"),
                         ({"edit": {"steps": [{"cmd": []}]}}, "edit.steps"), ({"extra": {}}, "extra must be"),
                         ({"budget": {"edit": 101}}, "from 1 to 100"),
                         ({"extra": [{"name": "x", "cmd": ["true"], "optional": "no"}]}, "optional true or false"),
                         ({"extra": [{"name": 3, "cmd": ["true"]}]}, "a name that is a string"),
                         ({"edit": {"match": [1], "steps": []}}, "edit.match must be"),
                         ({"adapter": 3}, "adapter must be")):
            self.write(".aok/harness.json", json.dumps({"adapter": "python", **bad}))
            code, out = self.verify()
            self.assertEqual(code, 2, bad)
            self.assertIn(why, out, bad)
            self.assertNotIn("Traceback", out, bad)
        self.write(".aok/harness.json", json.dumps({"adapter": "python", "gate": {"quick": []}}))
        code, out = self.verify("--mode", "quick")
        self.assertEqual(code, 2)
        self.assertIn("the adapter has no quick gate", out)
        self.write(".aok/harness.json", json.dumps({"adapter": "python"}))
        self.write(".aok/harness/adapters/python.json", "{broken")  # a committed adapter copy is what --base reads
        self.git("add", "-A")
        self.git("commit", "-qm", "broken adapter")
        code, out = self.verify("--base", "HEAD")
        self.assertEqual(code, 2)
        self.assertIn("an adapter is not valid JSON", out)
        self.assertNotIn("Traceback", out)

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

    def test_a_step_that_overruns_is_cannot_and_leaves_no_process(self):
        pidfile = self.tmp / "child.pid"
        # the grandchild lets go of the step's output pipes, so nothing waits for it: only killing the group ends it
        spawn = ("import subprocess, sys, time; c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                 "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                 f"open({str(pidfile)!r}, 'w').write(str(c.pid)); time.sleep(60)")
        self.python_repo(config={"adapter": "python", "extra": [{"name": "slow", "cmd": ["python3", "-c", spawn]}],
                                 "budget": {"gate": 4}})
        code, out = self.verify("--mode", "quick", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["steps"][-1]["status"], "cannot")
        pid = int(pidfile.read_text())
        time.sleep(0.2)
        alive = Path(f"/proc/{pid}").exists() and "zombie" not in Path(f"/proc/{pid}/status").read_text().lower()
        if alive:
            os.kill(pid, 9)  # do not leave it behind for the next test
        self.assertFalse(alive, "the step's own child must not outlive the timeout")

    def test_neither_gate_is_fooled_by_a_shadowing_unittest(self):
        self.python_repo()
        self.write("unittest.py", "import os\nos._exit(0)\n")
        self.write("pkg/__init__.py", ADD_BAD)
        for mode in ("quick", "full"):
            self.assertEqual(self.verify("--mode", mode)[0], 1, mode)

    def test_the_gate_budget_is_shared_by_the_steps(self):
        slow = {"name": "slow", "cmd": ["python3", "-c", "import time; time.sleep(2)"]}
        self.python_repo(config={"adapter": "python", "gate": {"quick": [slow, slow, slow]}, "budget": {"gate": 3}})
        code, out = self.verify("--mode", "quick", "--json")
        self.assertEqual(code, 2)
        self.assertIn("cannot", [s["status"] for s in json.loads(out)["steps"]][1:])

    def test_ci_runs_the_base_branch_harness(self):
        # what the runbook tells CI to do: the base branch's core, adapters and definition judge the pull request
        self.python_repo()
        code, out = InstallTests.install(self, "--adapter", "python", "--yes")
        self.assertEqual(code, 0, out)
        self.git("add", "-A")
        self.git("commit", "-qm", "harness")
        self.git("checkout", "-qb", "pr")
        loose = {"name": "python", "detect": ["pyproject.toml"], "gate": {"quick": [], "full": [{"name": "ok", "cmd": ["true"]}]}}
        self.write(".aok/harness/adapters/python.json", json.dumps(loose))
        core = self.repo / ".aok/harness/aok_harness.py"
        core.write_text(core.read_text().replace('return {"pass": 0, "fail": 1}.get(gate["verdict"], 2)', "return 0"))
        self.write("pkg/__init__.py", ADD_BAD)
        self.git("commit", "-qam", "loosen and break")
        base_core = self.tmp / "base-core.py"
        base_core.write_text(self.git("show", "main:.aok/harness/aok_harness.py"))
        p = subprocess.run([sys.executable, "-I", str(base_core), "verify", "--root", str(self.repo), "--base", "main"],
                           capture_output=True, text=True, env=self.env, timeout=300)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("[test] fail", p.stdout)


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
                           f'cat >> {record}\necho\necho \'{{"decision": "block", "reason": "from fsmap"}}\'\n')
        self.start()
        self.write("src/lib.rs", "pub fn f() { }\n")
        self.edit("src/lib.rs")
        self.hook("prompt", {"prompt": "go"})
        self.bash("cargo build")  # not publishing: fsmap still sees it
        self.bash("git push")
        self.assertEqual(self.stop(), {"decision": "block", "reason": "from fsmap"})
        calls = [ln for ln in record.read_text().splitlines() if ln.startswith("hook ") and ln != "hook --help"]
        self.assertEqual(calls, ["hook session-start", "hook post-edit", "hook prompt", "hook pre-bash", "hook pre-bash",
                                 "hook stop"])
        self.assertIn('"session_id": "s1"', record.read_text())

    def test_the_delegates_block_comes_back_with_its_reason(self):
        self.rust_repo()
        self.stub("fsmap", 'case "$*" in "hook --help") exit 0;; "hook stop") echo "fmt is off" >&2; exit 2;; esac\nexit 0\n')
        self.start()
        env = {**self.env, "CLAUDE_PROJECT_DIR": str(self.repo)}
        p = subprocess.run([sys.executable, "-I", str(CORE), "hook", "stop"], input='{"session_id": "s1"}',
                           capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(p.returncode, 2)  # Claude Code reads exit 2 as a block, with stderr as the reason
        self.assertIn("fmt is off", p.stderr)

    def test_a_delegate_that_hangs_is_cut_off(self):
        self.rust_repo()
        self.stub("fsmap", 'case "$*" in "hook --help") exit 0;; "hook prompt") sleep 40;; esac\nexit 0\n')
        self.start()
        out = self.hook("prompt", {"prompt": "go"})  # the prompt hook has 30 s; the delegate gets 24 of them
        self.assertIn("fsmap hook prompt did not run", out["systemMessage"])

    def test_a_failing_fsmap_is_not_a_pass(self):
        self.rust_repo()
        self.stub("fsmap", 'case "$*" in "hook --help") exit 0;; esac\necho "thread main panicked" >&2\nexit 101\n')
        self.assertIn("fsmap hook session-start failed (exit 101)", self.start()["systemMessage"])
        self.write("src/lib.rs", "pub fn f() { }\n")
        self.assertIn("fsmap hook post-edit failed (exit 101)", self.edit("src/lib.rs")["systemMessage"])
        self.assertIn("fsmap hook pre-bash failed (exit 101)", self.bash("cargo build")["systemMessage"])
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("fsmap hook stop failed (exit 101): thread main panicked", out["reason"])
        self.git("commit", "-qam", "x")
        out = self.bash("git push")
        self.assertTrue(self.denied(out))
        self.assertIn("nothing is published", out["hookSpecificOutput"]["permissionDecisionReason"])
        # answers the probe, then is gone when the hook itself starts
        self.stub("fsmap", 'case "$*" in "hook --help") rm -f "$0"; exit 0;; esac\nexit 0\n')
        self.write("src/lib.rs", "pub fn f() {  }\n")
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("fsmap hook stop did not run", out["reason"])

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

    def test_rust_quick_gate_runs_the_tests(self):
        self.rust_repo()
        self.stub("cargo", 'case "$1" in test) echo "test t ... FAILED"; exit 101;; esac\nexit 0\n')
        code, out = self.verify("--mode", "quick")
        self.assertEqual(code, 1, out)
        self.assertIn("fail  test", out)

    def test_fallback_checks_a_rust_edit(self):
        self.rust_repo()
        self.stub("cargo", 'case "$1" in check) echo "src/lib.rs:1:9: error: expected type"; exit 101;; esac\nexit 0\n')
        self.start()
        for rel, text in (("src/lib.rs", "pub fn f( {}\n"), ("Cargo.toml", '[package]\nname = "x"\nversion = "0.2.0"\n'),
                          ("build.rs", "fn main() {\n")):
            self.write(rel, text)
            out = self.edit(rel)
            self.assertTrue(self.blocked(out), rel)
            self.assertIn("[check] fail", out["reason"], rel)

    def test_rust_quick_gate_checks_format_and_denies_warnings(self):
        self.rust_repo()
        self.stub("cargo", 'case "$1" in fmt) echo "Diff in src/lib.rs"; exit 1;; clippy) case "$*" in *"-D warnings"*) '
                           'echo "error: unused"; exit 101;; esac;; esac\nexit 0\n')
        code, out = self.verify("--mode", "quick")
        self.assertEqual(code, 1)
        self.assertIn("fail  fmt", out)
        self.assertIn("fail  clippy", out)

    def test_more_rust_weakening_forms_are_reported(self):
        self.rust_repo()
        self.write("tests/t.rs", "#[test]\nfn t() {\n    assert!(true);\n    assert_ne!(1, 2);\n    debug_assert!(true);\n}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "tests")
        self.stub("cargo", "exit 0\n")
        self.start()
        self.write("src/lib.rs", "#![cfg_attr(all(), allow(warnings))]\n#![forbid(unsafe_code)]\npub fn f() {}\n"
                   "unsafe fn g() {}\nstruct S;\nunsafe impl Send for S {}\n#[cfg(false)]\nfn h() {}\n"
                   "#[cfg(not(all()))]\nfn i() {}\nunsafe trait T {}\nunsafe extern \"C\" {}\n#[unsafe(no_mangle)]\n"
                   "pub extern \"C\" fn k() {}\n")
        self.write("tests/t.rs", "#[test]\n#[cfg_attr(all(), ignore)]\nfn t() {\n}\n")
        self.write(".cargo/config.toml", "[target.'cfg(all())']\nrunner = \"true\"\n")
        msg = self.stop()["systemMessage"]
        for item in ("src/lib.rs: allow-attribute ×1", "src/lib.rs: unsafe ×5", "src/lib.rs: cfg-false ×2",
                     "tests/t.rs: ignored-test ×1", "tests/t.rs: assertion-removed ×3",
                     "check settings changed in this session, review what they switch off: .cargo/config.toml"):
            self.assertIn(item, msg)

    def test_fallback_reports_rust_weakening(self):
        self.rust_repo()
        self.write("tests/t.rs", "#[test]\nfn t() {\n    assert_eq!(1, 1);\n}\n#[test]\nfn u() {}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "tests")
        self.stub("cargo", "exit 0\n")
        self.start()
        self.write("src/lib.rs", "#[allow(dead_code)]\n#[expect(unused)]\npub fn f() { unsafe { } }\n#[cfg(any())]\nfn g() {}\n")
        self.write("tests/t.rs", "#[test]\n#[ignore]\nfn t() {\n}\n")
        msg = self.stop()["systemMessage"]
        for item in ("src/lib.rs: allow-attribute ×2", "src/lib.rs: unsafe ×1", "src/lib.rs: cfg-false ×1",
                     "tests/t.rs: ignored-test ×1", "tests/t.rs: assertion-removed ×1", "tests/t.rs: test-removed ×1"):
            self.assertIn(item, msg)


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
        self.write("src/lib.rs", "pub fn add(a: i32, b: i32) -> i32 {\n    b + a\n}\n")
        for answer in (self.edit("src/lib.rs"), self.hook("prompt", {"prompt": "go"}), self.bash("git status"), self.stop()):
            self.assertIsNone(answer)  # every event reaches fsmap and comes back as fsmap answered, not as a relay failure
        self.git("commit", "-qam", "swap")
        self.assertIsNone(self.bash("git push"))
        self.write("src/lib.rs", "pub fn add(a: i32, b: i32) -> i32 {\n    a +  b\n}\n")  # not rustfmt-clean
        out = self.stop()
        self.assertTrue(self.blocked(out))
        self.assertIn("fsmap verify failed", out["reason"])
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
        pre = s["hooks"]["PreToolUse"][0]
        self.assertEqual((pre["matcher"], pre["hooks"][0]["onFailure"]), ("Bash", "block"))
        self.assertGreaterEqual(s["hooks"]["Stop"][0]["hooks"][0]["timeout"], 840 + 60)  # the gate budget fits
        # a push runs the same gate in PreToolUse, so its hook gets as long as Stop's
        self.assertGreaterEqual(pre["hooks"][0]["timeout"], s["hooks"]["Stop"][0]["hooks"][0]["timeout"])
        self.assertNotIn("allow", s["permissions"])  # install never widens what runs without asking
        self.assertTrue((self.repo / ".aok/harness/aok_harness.py").is_file())
        self.assertTrue((self.repo / ".aok/harness/adapters/python.json").is_file())
        self.assertEqual(json.loads((self.repo / ".aok/harness.json").read_text()), {"adapter": "python"})
        self.assertTrue((self.repo / ".claude/agents/aok-reviewer.md").is_file())
        self.assertEqual(set(s["hooks"]), {"SessionStart", "UserPromptSubmit", "PostToolUse", "PreToolUse", "Stop"})
        for rule in ("Edit(/.aok/**)", "Write(/.aok/**)", "Edit(/.claude/**)", "Write(/.claude/**)"):
            self.assertIn(rule, s["permissions"]["ask"])

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

    def test_existing_hooks_are_kept_and_not_doubled(self):
        self.python_repo()
        mine = {"type": "command", "command": "echo mine"}
        self.write(".claude/settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [mine]}]}}))
        self.install("--adapter", "python", "--yes")
        s = json.loads((self.repo / ".claude/settings.json").read_text())
        stop = s["hooks"]["Stop"][1]["hooks"][0]
        self.assertEqual(s["hooks"]["Stop"][0]["hooks"], [mine])
        stop["command"] = stop["command"].replace("python3 -I", "python3.13 -I")  # a person changed the interpreter
        (self.repo / ".claude/settings.json").write_text(json.dumps(s))
        self.install("--adapter", "python", "--yes")
        self.assertEqual(len(json.loads((self.repo / ".claude/settings.json").read_text())["hooks"]["Stop"]), 2)

    def test_bad_inputs(self):
        self.python_repo()
        self.assertEqual(self.install("--adapter", "cobol")[0], 2)
        self.write(".claude/settings.json", "{oops")
        self.assertEqual(self.install("--adapter", "python", "--yes")[0], 2)
        for bad in ({"hooks": []}, {"hooks": {"Stop": {}}}, {"permissions": {"ask": "x"}}, []):
            self.write(".claude/settings.json", json.dumps(bad))
            code, out = self.install("--adapter", "python", "--yes")
            self.assertEqual(code, 2, bad)
            self.assertIn("merge by hand", out)
            self.assertNotIn("Traceback", out)
        (self.repo / ".claude" / "settings.json").write_bytes(b'{"model": "caf\xe9"}')  # not UTF-8
        code, out = self.install("--adapter", "python", "--yes")
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)
        self.write(".claude/settings.json", json.dumps({"disableAllHooks": True}))
        code, out = self.install("--adapter", "python", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("disableAllHooks", out)
        self.write(".claude/settings.json", "{}")
        (self.repo / ".aok").mkdir(exist_ok=True)
        (self.repo / ".aok" / "harness.json").write_bytes(b'{"adapter": "caf\xe9"}')  # not UTF-8
        code, out = self.install("--adapter", "python", "--yes")
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)
        self.write(".aok/harness.json", json.dumps({"adapter": "python"}))
        code, out = self.install("--adapter", "rust")
        self.assertEqual(code, 2)
        self.assertIn("already sets adapter 'python'", out)

    def test_an_existing_definition_and_reviewer_are_kept(self):
        self.python_repo()
        mine = {"adapter": "python", "extra": [{"name": "lint", "cmd": ["python3", "-c", "pass"]}]}
        self.write(".aok/harness.json", json.dumps(mine))
        self.write(".claude/agents/aok-reviewer.md", "my reviewer\n")
        code, out = self.install("--adapter", "python", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((self.repo / ".aok/harness.json").read_text()), mine)
        self.assertEqual((self.repo / ".claude/agents/aok-reviewer.md").read_text(), "my reviewer\n")

    def test_installing_again_from_the_installed_copy(self):
        self.python_repo()
        self.write(".claude/settings.local.json", json.dumps({"disableAllHooks": True}))
        code, out = self.install("--adapter", "python", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("warning: ", out)  # the local settings switch the hooks off
        p = subprocess.run([sys.executable, "-I", str(self.repo / ".aok/harness/aok_harness.py"), "install", "--repo",
                            str(self.repo), "--adapter", "python", "--yes"], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
