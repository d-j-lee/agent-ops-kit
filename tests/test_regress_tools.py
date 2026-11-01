"""Tests for the regression-pinning tools of feature ⑦ (stdlib unittest only).

scripts/diffcheck.py      differential check: failed, empty, nondeterministic, differs, identical
scripts/vacuity_check.py  shows a test fails when the code it guards is mutated (on a copy)

The guards listed in tests/mutations/diffcheck.json and vacuity_check.json each have a
test here that fails when the guard is removed; CI runs those removals through
vacuity_check.py itself, with the catching test named in "expect".

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = sys.executable


def run_script(name: str, *args: str, timeout: int = 120):
    proc = subprocess.run([PY, "-I", str(SCRIPTS / f"{name}.py"), *map(str, args)],
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def py(code: str, *extra: str) -> str:
    """A command string that runs `code` with this interpreter (for --base/--cand/--test)."""
    return shlex.join([PY, "-I", "-c", code, *extra])


# A command that exits 0 on its first call and 1 on every later call (state in a counter file).
FLAKY = "import pathlib, sys; p = pathlib.Path(sys.argv[1]); n = int(p.read_text() or 0) if p.exists() else 0; " \
        "p.write_text(str(n + 1)); print('out'); sys.exit(0 if n == 0 else 1)"


class DiffcheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def diff(self, *args):
        code, out, err = run_script("diffcheck", *args, "--json")
        report = json.loads(out) if out.strip().startswith("{") else None
        return code, report, err

    def test_identical(self):
        code, r, _ = self.diff("--base", py("print('a')"), "--cand", py("print('a')"))
        self.assertEqual((code, r["verdict"]), (0, "identical"))
        self.assertEqual(r["base"]["bytes"], 2)
        self.assertEqual(r["compared"], ["exit", "stdout"])

    def test_differs_shows_diff_head(self):
        code, r, _ = self.diff("--base", py("print('a')"), "--cand", py("print('b')"))
        self.assertEqual((code, r["verdict"]), (1, "differs"))
        self.assertIn("-a", r["diff_head"])
        self.assertIn("+b", r["diff_head"])

    def test_bytes_not_decoded_text(self):
        # two different invalid UTF-8 bytes must not collapse into one replacement character
        w = "import sys; sys.stdout.buffer.write(bytes([int(sys.argv[1])]) + b'\\n')"
        code, r, _ = self.diff("--base", py(w, "255"), "--cand", py(w, "254"))
        self.assertEqual((code, r["verdict"]), (1, "differs"))
        self.assertIn("-\\xff", r["diff_head"])
        # and line endings are compared as written
        crlf = "import sys; sys.stdout.buffer.write(b'a' + sys.argv[1].encode().replace(b'N', b'\\n').replace(b'R', b'\\r'))"
        code, r, _ = self.diff("--base", py(crlf, "RN"), "--cand", py(crlf, "N"))
        self.assertEqual(r["verdict"], "differs")

    def test_empty_outputs_are_not_identical(self):
        # the trap that motivated ⑦: both sides silent, a naive compare says "same"
        code, r, _ = self.diff("--base", py("pass"), "--cand", py("pass"))
        self.assertEqual((code, r["verdict"]), (1, "empty"))
        code, r, _ = self.diff("--base", py("pass"), "--cand", py("pass"), "--allow-empty")
        self.assertEqual((code, r["verdict"]), (0, "identical"))

    def test_whitespace_only_is_empty(self):
        code, r, _ = self.diff("--base", py("print()"), "--cand", py("print('  ')"))
        self.assertEqual((code, r["verdict"]), (1, "empty"))
        self.assertEqual(r["problems"], [f"empty output: {s} printed nothing or only whitespace (stdout)" for s in ("base", "cand")])

    def test_one_side_empty(self):
        code, r, _ = self.diff("--base", py("print('x')"), "--cand", py("pass"))
        self.assertEqual(r["verdict"], "empty")
        self.assertEqual(r["problems"], ["empty output: cand printed nothing or only whitespace (stdout)"])

    def test_normalizing_everything_is_empty(self):
        # a regex that eats the whole output must not turn 'apple' vs 'banana' into "identical"
        code, r, _ = self.diff("--base", py("print('apple')"), "--cand", py("print('banana')"),
                               "--normalize", "(?s).+")
        self.assertEqual((code, r["verdict"]), (1, "empty"))
        self.assertIn("nothing left after normalization", r["problems"][0])
        # only placeholders left counts too: nothing real was compared
        code, r, _ = self.diff("--base", py("print('a1 a2')"), "--cand", py("print('b1 b2')"), "--normalize", "\\w+")
        self.assertEqual(r["verdict"], "empty")

    def test_failed_exit_code(self):
        code, r, _ = self.diff("--base", py("print('a'); raise SystemExit(3)"), "--cand", py("print('a')"))
        self.assertEqual((code, r["verdict"]), (1, "failed"))
        self.assertEqual(r["base"]["exit"], 3)
        self.assertEqual(r["problems"], ["base exited 3"])

    def test_allow_fail_still_compares_exit_codes(self):
        code, r, _ = self.diff("--base", py("print('a'); raise SystemExit(3)"), "--cand", py("print('a')"),
                               "--allow-fail")
        self.assertEqual((code, r["verdict"]), (1, "differs"))
        self.assertIn("exit code: base 3 · cand 0", r["diff_head"])
        both = py("print('a'); raise SystemExit(3)")
        code, r, _ = self.diff("--base", both, "--cand", both, "--allow-fail")
        self.assertEqual((code, r["verdict"]), (0, "identical"))

    def test_stderr_compared_only_on_request(self):
        warn = py("import sys; print('a'); print('warning', file=sys.stderr)")
        code, r, _ = self.diff("--base", py("print('a')"), "--cand", warn)
        self.assertEqual(r["verdict"], "identical")
        self.assertEqual(r["cand"]["stderr_bytes"], 8)
        code, r, _ = self.diff("--base", py("print('a')"), "--cand", warn, "--stderr")
        self.assertEqual((code, r["verdict"]), (1, "differs"))
        self.assertEqual(r["compared"], ["exit", "stdout", "stderr"])

    def test_timeout_always_fails(self):
        slow = py("import time; time.sleep(5)")
        code, r, _ = self.diff("--base", slow, "--cand", slow, "--timeout", "0.5", "--allow-fail", "--allow-empty")
        self.assertEqual((code, r["verdict"]), (1, "failed"))
        self.assertTrue(r["base"]["timeout"] and r["cand"]["timeout"])

    def test_nondeterminism_detected_and_normalized(self):
        rnd = py("import secrets; print('id', secrets.token_hex(4))")
        code, r, _ = self.diff("--base", rnd, "--cand", rnd, "--determinism")
        self.assertEqual((code, r["verdict"]), (1, "nondeterministic"))
        code, r, _ = self.diff("--base", rnd, "--cand", rnd, "--determinism", "--normalize", "[0-9a-f]{8}")
        self.assertEqual((code, r["verdict"]), (0, "identical"))
        self.assertEqual(r["base"]["normalized_counts"], [1])

    def test_candidate_side_nondeterminism_beats_differs(self):
        rnd = py("import secrets; print(secrets.token_hex(4))")
        code, r, _ = self.diff("--base", py("print('fixed')"), "--cand", rnd, "--determinism")
        self.assertEqual((code, r["verdict"]), (1, "nondeterministic"))
        self.assertEqual([p.split()[0] for p in r["problems"]], ["cand"])

    def test_determinism_compares_exit_codes(self):
        flaky = py(FLAKY, str(self.tmp / "count"))
        code, r, _ = self.diff("--base", py("print('out')"), "--cand", flaky, "--determinism")
        self.assertEqual((code, r["verdict"]), (1, "nondeterministic"))
        self.assertIn("exit code", r["problems"][0])

    def test_determinism_compares_stderr_under_stderr(self):
        noisy = py("import secrets, sys; print('same'); print(secrets.token_hex(4), file=sys.stderr)")
        code, r, _ = self.diff("--base", noisy, "--cand", noisy, "--stderr", "--determinism")
        self.assertEqual((code, r["verdict"]), (1, "nondeterministic"))
        self.assertIn("second run differs in stderr", r["problems"][0])

    def test_normalize_counts_every_match(self):
        three = py("print('ids t1 t2 t3')")
        code, r, _ = self.diff("--base", three, "--cand", py("print('ids t9 t8 t7')"), "--normalize", "t[0-9]")
        self.assertEqual((code, r["verdict"]), (0, "identical"))
        self.assertEqual((r["base"]["normalized_counts"], r["cand"]["normalized_counts"]), ([3], [3]))

    def test_nonce_is_fresh_per_invocation(self):
        echo = py("import sys; print('id', sys.argv[1])", "{nonce}")
        code, r, _ = self.diff("--base", echo, "--cand", echo)
        self.assertEqual(r["verdict"], "differs")  # each invocation got its own token
        code, r, _ = self.diff("--base", echo, "--cand", echo, "--normalize", "[0-9a-f]{16}")
        self.assertEqual(r["verdict"], "identical")

    def test_same_nonce_shares_the_token_within_a_pair(self):
        echo = py("import sys; print('id', sys.argv[1])", "{nonce}")
        code, r, _ = self.diff("--base", echo, "--cand", echo, "--same-nonce", "--determinism")
        # same token on both sides of a pair, but a new one for the second pair: base varies between runs
        self.assertEqual(r["verdict"], "nondeterministic")
        code, r, _ = self.diff("--base", echo, "--cand", echo, "--same-nonce")
        self.assertEqual((code, r["verdict"]), (0, "identical"))

    def test_same_nonce_with_determinism_is_noted(self):
        echo = py("print('ok')")
        code, r, _ = self.diff("--base", echo, "--cand", echo, "--same-nonce", "--determinism")
        self.assertIn("--same-nonce with --determinism", r["notes"][0])
        code, r, _ = self.diff("--base", echo, "--cand", echo, "--same-nonce")
        self.assertNotIn("notes", r)

    def test_stdin_nonce_is_substituted(self):
        f = self.tmp / "in.txt"
        f.write_text("hello {nonce}\n", encoding="utf-8")
        # the marker is split so that diffcheck does not substitute it inside this probe's own code
        probe = py("import sys; s = sys.stdin.read(); print('raw' if '{' + 'nonce}' in s else 'fresh', len(s.split()[1]))")
        code, r, _ = self.diff("--base", probe, "--cand", py("print('fresh 16')"), "--stdin", f)
        self.assertEqual((code, r["verdict"]), (0, "identical"))
        cat = py("import sys; print(sys.stdin.read())")
        code, r, _ = self.diff("--base", cat, "--cand", cat, "--stdin", f, "--determinism")
        self.assertEqual(r["verdict"], "nondeterministic")  # every run saw a new token

    def test_stdin_is_bytes(self):
        f = self.tmp / "in.bin"
        f.write_bytes(b"\xff\xfe {nonce}\n")
        size = py("import sys; print(len(sys.stdin.buffer.read()))")
        code, r, err = self.diff("--base", size, "--cand", size, "--stdin", f)
        self.assertEqual((code, r["verdict"]), (0, "identical"), err)

    def test_timing_is_interleaved(self):
        log = self.tmp / "order.log"
        mark = "import sys; open(sys.argv[1], 'a').write(sys.argv[2]); print(1)"
        code, r, _ = self.diff("--base", py(mark, str(log), "b"), "--cand", py(mark, str(log), "c"), "--timing", "3")
        self.assertEqual(r["verdict"], "identical")
        # one comparison run per side, then base, cand, base, cand, ...
        self.assertEqual(log.read_text(), "bc" + "bc" * 3)
        t = r["timing"]
        self.assertEqual((t["base"]["n"], t["cand"]["n"]), (3, 3))
        self.assertLessEqual(t["base"]["min_ms"], t["base"]["median_ms"])
        self.assertLessEqual(t["base"]["median_ms"], t["base"]["max_ms"])

    def test_timing_change_sign(self):
        code, r, _ = self.diff("--base", py("import time; time.sleep(0.3); print(1)"), "--cand", py("print(1)"),
                               "--timing", "3")
        self.assertLess(r["timing"]["median_change_pct"], -50)  # candidate is faster: negative change

    def test_failure_during_timing_fails(self):
        flaky = py(FLAKY, str(self.tmp / "count"))
        code, r, _ = self.diff("--base", py("print('out')"), "--cand", flaky, "--timing", "2")
        self.assertEqual((code, r["verdict"]), (1, "failed"))
        self.assertIn("cand exited 1 during timing", r["problems"])

    def test_stderr_normalized_away_is_empty(self):
        e1 = py("import sys; print('a'); print('warn 1', file=sys.stderr)")
        e2 = py("import sys; print('a'); print('other 2', file=sys.stderr)")
        code, r, _ = self.diff("--base", e1, "--cand", e2, "--stderr", "--normalize", "(?s).+")
        self.assertEqual((code, r["verdict"]), (1, "empty"))
        self.assertIn("nothing left after normalization, only <N> (stdout and stderr)", " ".join(r["problems"]))

    def test_stderr_counts_as_output_under_stderr(self):
        # a hook that blocks answers on stderr only and exits 2: with --stderr that is output, not emptiness
        block = py("import sys; print('blocked: tests fail', file=sys.stderr); sys.exit(2)")
        code, r, _ = self.diff("--base", block, "--cand", block, "--stderr", "--allow-fail")
        self.assertEqual((code, r["verdict"]), (0, "identical"), r["problems"])
        code, r, _ = self.diff("--base", block, "--cand", block, "--allow-fail")
        self.assertEqual((code, r["verdict"]), (1, "empty"))  # stdout only: nothing to compare
        warn = py("import sys; print('a'); print('warn 1', file=sys.stderr)")
        code, r, _ = self.diff("--base", warn, "--cand", warn, "--stderr", "--normalize", "(?m)^warn.*$")
        self.assertEqual((code, r["verdict"]), (0, "identical"), r["problems"])  # stdout still says something

    def test_literal_placeholder_is_ambiguous(self):
        code, r, _ = self.diff("--base", py("print('x=1 y=<N>')"), "--cand", py("print('x=<N> y=2')"), "--normalize", "[0-9]")
        self.assertEqual((code, r["verdict"]), (1, "failed"))
        self.assertIn("printed the placeholder <N> itself", " ".join(r["problems"]))
        code, r, _ = self.diff("--base", py("print('<N>')"), "--cand", py("print('<N>')"))
        self.assertEqual((code, r["verdict"]), (0, "identical"), r["problems"])  # without --normalize it is just text

    def test_normalization_counts_must_match(self):
        code, r, _ = self.diff("--base", py("print('xa')"), "--cand", py("print('xb')"), "--normalize", "a", "--normalize", "b")
        self.assertEqual((code, r["verdict"]), (1, "differs"))  # both read x<N>, but a different pattern matched
        self.assertIn("normalization counts: base [1, 0] · cand [0, 1]", r["diff_head"])
        e1 = py("import sys; print('same'); print('a', file=sys.stderr)")
        e2 = py("import sys; print('same'); print('b', file=sys.stderr)")
        code, r, _ = self.diff("--base", e1, "--cand", e2, "--stderr", "--normalize", "a", "--normalize", "b")
        self.assertEqual((code, r["verdict"]), (1, "differs"))
        self.assertIn("(stderr: base [1, 0] · cand [0, 1])", r["diff_head"][0])

    def test_timing_uses_a_fresh_nonce_each_run(self):
        seen = self.tmp / "seen"
        once = py("import pathlib, sys; p = pathlib.Path(sys.argv[1]); s = p.read_text().split() if p.exists() else []; "
                  "p.write_text(' '.join(s + [sys.argv[2]])); print('ok'); sys.exit(1 if sys.argv[2] in s else 0)",
                  str(seen), "{nonce}")
        code, r, _ = self.diff("--base", once, "--cand", once, "--timing", "3")
        self.assertEqual((code, r["verdict"]), (0, "identical"), r["problems"])
        self.assertEqual(len(seen.read_text().split()), 8)  # 2 comparison runs + 3 timed pairs, all different

    def test_timeout_during_timing_fails_even_with_allow_fail(self):
        count = self.tmp / "n"
        slow_later = py("import pathlib, sys, time; p = pathlib.Path(sys.argv[1]); n = int(p.read_text() or 0) if p.exists() else 0; "
                        "p.write_text(str(n + 1)); print('x'); time.sleep(5 if n else 0)", str(count))
        code, r, _ = self.diff("--base", py("print('x')"), "--cand", slow_later, "--timing", "1", "--timeout", "1.5",
                               "--allow-fail")
        self.assertEqual((code, r["verdict"]), (1, "failed"))
        self.assertIn("cand timed out during timing", r["problems"])

    def test_human_output(self):
        code, out, _ = run_script("diffcheck", "--base", py("print('a')"), "--cand", py("print('a')"))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("verdict: identical\ncompared: exit, stdout\n"))
        self.assertIn("base: exit 0 · 2 bytes", out)

    def test_tool_errors_exit_2(self):
        ok = py("print(1)")
        notexec = self.tmp / "notexec.sh"
        notexec.write_text("#!/bin/sh\necho x\n", encoding="utf-8")
        os.chmod(notexec, 0o644)
        code, _, err = run_script("diffcheck", "--base", str(notexec), "--cand", ok)
        self.assertEqual(code, 2)
        self.assertIn("cannot run", err)
        for cwd in (notexec, self.tmp / "none"):  # a file, a missing dir: say so instead of "command not found"
            code, _, err = run_script("diffcheck", "--base", ok, "--cand", ok, "--cwd", str(cwd))
            self.assertEqual(code, 2)
            self.assertIn("--cwd is not a directory", err)

    def test_usage_errors(self):
        ok = py("print(1)")
        self.assertEqual(run_script("diffcheck", "--base", ok, "--cand", ok, "--normalize", "(")[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", ok, "--cand", ok, "--stdin", "/no/such/file")[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", "no-such-command-xyz", "--cand", ok)[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", "", "--cand", ok)[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", ok, "--cand", ok, "--timing", "-1")[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", ok, "--cand", ok, "--timeout", "0")[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", ok, "--cand", ok, "--timeout", "nan")[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", ok, "--cand", ok, "--timeout", "1e10")[0], 2)
        if Path("/proc/self/mem").is_file():  # a file that exists but cannot be read
            code, _, err = run_script("diffcheck", "--base", ok, "--cand", ok, "--stdin", "/proc/self/mem")
            self.assertEqual(code, 2, err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run_script("diffcheck", "--base", "'unclosed", "--cand", ok)[0], 2)
        self.assertEqual(run_script("diffcheck")[0], 2)

    def test_help(self):
        code, out, _ = run_script("diffcheck", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--determinism", out)


CALC = textwrap.dedent('''\
    def safe_div(a, b):
        if b == 0:
            return None
        return a / b
    ''')
TEST_GUARD = textwrap.dedent('''\
    from calc import safe_div
    assert safe_div(4, 2) == 2
    assert safe_div(1, 0) is None, "zero guard"
    print("ok")
    ''')
TEST_VACUOUS = textwrap.dedent('''\
    from calc import safe_div
    assert safe_div(4, 2) == 2
    print("ok")
    ''')
TEST_NOFLAG = textwrap.dedent('''\
    import pathlib, sys
    sys.exit(1 if pathlib.Path("skipme/flag.txt").exists() else 0)
    ''')
TEST_BYTES = textwrap.dedent('''\
    import pathlib, sys
    sys.exit(0 if b"value=1" in pathlib.Path("data.bin").read_bytes() else 1)
    ''')


class VacuityCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "calc.py").write_text(CALC, encoding="utf-8")
        (self.repo / "test_guard.py").write_text(TEST_GUARD, encoding="utf-8")
        (self.repo / "test_vacuous.py").write_text(TEST_VACUOUS, encoding="utf-8")
        (self.repo / "test_broken.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
        self.guard_old = "    if b == 0:\n        return None\n"

    def tearDown(self):
        self._tmp.cleanup()

    def vac(self, test_file: str, *mut_args: str):
        code, out, err = run_script("vacuity_check", "--repo", self.repo, "--test",
                                    shlex.join([PY, test_file]), *mut_args, "--json")
        return code, (json.loads(out) if out.strip().startswith("{") else None), err

    def muts_file(self, muts: list[dict]) -> Path:
        mf = self.tmp / "muts.json"
        mf.write_text(json.dumps(muts), encoding="utf-8")
        return mf

    def test_guarded(self):
        code, r, _ = self.vac("test_guard.py", "--mutate", "calc.py", self.guard_old, "")
        self.assertEqual((code, r["verdict"]), (0, "guarded"))
        self.assertTrue(r["baseline"]["passed"])
        self.assertEqual(r["mutations"][0]["status"], "guarded")

    def test_vacuous(self):
        code, r, _ = self.vac("test_vacuous.py", "--mutate", "calc.py", self.guard_old, "")
        self.assertEqual((code, r["verdict"]), (1, "vacuous"))
        self.assertEqual(r["mutations"][0]["status"], "vacuous")

    def test_baseline_must_pass(self):
        code, r, _ = self.vac("test_broken.py", "--mutate", "calc.py", self.guard_old, "")
        self.assertEqual((code, r["verdict"]), (1, "baseline_fails"))
        self.assertEqual(r["mutations"], [])

    def test_mutation_must_match_exactly_once(self):
        code, r, _ = self.vac("test_guard.py", "--mutate", "calc.py", "no such text", "x")
        self.assertEqual((code, r["verdict"]), (1, "mutation_error"))
        self.assertIn("0 times", r["mutations"][0]["detail"])
        (self.repo / "calc.py").write_text(CALC + "\n# return None\n", encoding="utf-8")
        code, r, _ = self.vac("test_guard.py", "--mutate", "calc.py", "return None", "return 0")
        self.assertEqual(r["verdict"], "mutation_error")
        self.assertIn("2 times", r["mutations"][0]["detail"])

    def test_overlapping_matches_count(self):
        (self.repo / "calc.py").write_text(CALC + "# aaa\n", encoding="utf-8")
        code, r, _ = self.vac("test_guard.py", "--mutate", "calc.py", "aa", "x")
        self.assertEqual(r["verdict"], "mutation_error")
        self.assertIn("2 times", r["mutations"][0]["detail"])

    def test_original_untouched(self):
        before = (self.repo / "calc.py").read_text(encoding="utf-8")
        self.vac("test_guard.py", "--mutate", "calc.py", self.guard_old, "")
        self.assertEqual((self.repo / "calc.py").read_text(encoding="utf-8"), before)

    def test_symlink_out_of_the_copy_is_not_written(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "calc.py").write_text(CALC, encoding="utf-8")
        (self.repo / "lib").symlink_to(outside.resolve(), target_is_directory=True)  # absolute link
        code, r, _ = self.vac("test_guard.py", "--mutate", "lib/calc.py", self.guard_old, "")
        self.assertEqual((code, r["verdict"]), (1, "mutation_error"))
        self.assertEqual(r["mutations"][0]["status"], "unsafe")
        self.assertEqual((outside / "calc.py").read_text(encoding="utf-8"), CALC)

    def test_symlink_inside_the_copy_is_fine(self):
        (self.repo / "alias.py").symlink_to("calc.py")  # relative link, stays in the copy
        code, r, _ = self.vac("test_guard.py", "--mutate", "alias.py", self.guard_old, "")
        self.assertEqual((code, r["verdict"]), (0, "guarded"))
        self.assertEqual((self.repo / "calc.py").read_text(encoding="utf-8"), CALC)

    def test_syntax_breaking_mutation_is_invalid_not_guarded(self):
        # breaks the file, so even the vacuous test "fails": that must not count
        code, r, _ = self.vac("test_vacuous.py", "--mutate", "calc.py", "return None", "return None)")
        self.assertEqual((code, r["verdict"]), (1, "mutation_error"))
        self.assertEqual(r["mutations"][0]["status"], "invalid")

    def test_timeout_is_inconclusive(self):
        code, r, _ = self.vac("test_guard.py", "--mutate", "calc.py", "    return a / b", "    while True: pass",
                              "--timeout", "3")
        self.assertEqual((code, r["verdict"]), (1, "inconclusive"))
        self.assertEqual(r["mutations"][0]["status"], "timeout")

    def test_expect_is_a_whole_word(self):
        code, r, _ = self.vac("test_guard.py", "--mutate", "calc.py", self.guard_old, "", "--expect", "zero")
        self.assertEqual(r["verdict"], "guarded")
        code, r, _ = self.vac("test_guard.py", "--mutate", "calc.py", self.guard_old, "", "--expect", "zer")
        self.assertEqual(r["mutations"][0]["status"], "other_failure")  # a prefix of a word is not a match

    def test_global_expect_applies_to_a_mutations_file(self):
        mf = self.muts_file([{"file": "calc.py", "old": self.guard_old, "new": "", "label": "drop zero guard"}])
        code, r, _ = self.vac("test_guard.py", "--mutations", mf, "--expect", "not_in_the_output")
        self.assertEqual(r["mutations"][0]["status"], "other_failure")

    def test_baseline_timeout_is_baseline_fails(self):
        (self.repo / "test_slow.py").write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
        code, r, _ = self.vac("test_slow.py", "--mutate", "calc.py", self.guard_old, "", "--timeout", "1")
        self.assertEqual((code, r["verdict"]), (1, "baseline_fails"))
        self.assertTrue(r["baseline"]["timeout"])

    def test_uncopyable_repository_is_a_tool_error(self):
        os.mkfifo(self.repo / "pipe")
        code, _, err = run_script("vacuity_check", "--repo", self.repo, "--test", shlex.join([PY, "test_guard.py"]),
                                  "--mutate", "calc.py", self.guard_old, "", timeout=60)
        self.assertEqual(code, 2)
        self.assertIn("cannot copy", err)

    def test_expect_separates_the_right_failure(self):
        code, r, _ = self.vac("test_guard.py", "--mutate", "calc.py", self.guard_old, "", "--expect", "zero guard")
        self.assertEqual((code, r["verdict"]), (0, "guarded"))
        code, r, _ = self.vac("test_guard.py", "--mutate", "calc.py", self.guard_old, "", "--expect", "other test")
        self.assertEqual((code, r["verdict"]), (1, "inconclusive"))
        self.assertEqual(r["mutations"][0]["status"], "other_failure")

    def test_verdict_precedence(self):
        guard = {"file": "calc.py", "old": self.guard_old, "new": "", "label": "drop zero guard"}
        floor = {"file": "calc.py", "old": "return a / b", "new": "return a // b", "label": "floor div"}
        broken = {"file": "calc.py", "old": "return None", "new": "return None)", "label": "syntax"}
        hang = {"file": "calc.py", "old": "    return a / b", "new": "    while True: pass", "label": "hang"}
        cases = [([guard, floor], "vacuous", ["guarded", "vacuous"]),
                 ([floor, broken], "mutation_error", ["vacuous", "invalid"]),
                 ([hang, floor], "vacuous", ["timeout", "vacuous"]),
                 ([guard, hang], "inconclusive", ["guarded", "timeout"])]
        for muts, verdict, statuses in cases:
            code, r, _ = self.vac("test_guard.py", "--mutations", self.muts_file(muts), "--timeout", "3")
            self.assertEqual((code, r["verdict"]), (1, verdict), statuses)
            self.assertEqual([m["status"] for m in r["mutations"]], statuses)
        self.assertEqual(r["mutations"][1]["label"], "hang")

    def test_jobs_keep_input_order(self):
        guard = {"file": "calc.py", "old": self.guard_old, "new": "", "label": "drop zero guard"}
        floor = {"file": "calc.py", "old": "return a / b", "new": "return a // b", "label": "floor div"}
        code, r, _ = self.vac("test_guard.py", "--mutations", self.muts_file([guard, guard, floor]), "--jobs", "3")
        self.assertEqual([m["status"] for m in r["mutations"]], ["guarded", "guarded", "vacuous"])
        self.assertEqual(run_script("vacuity_check", "--repo", self.repo, "--test", "true", "--mutate", "calc.py",
                                    "a", "b", "--jobs", "0")[0], 2)

    def test_ignore_adds_to_the_defaults(self):
        for d in ("skipme", "node_modules"):
            (self.repo / d).mkdir()
            (self.repo / d / "flag.txt").write_text("x", encoding="utf-8")
        (self.repo / "test_noflag.py").write_text(TEST_NOFLAG.replace('"skipme/flag.txt"', "sys.argv[1]"), encoding="utf-8")

        def verdict(flag, *extra):
            _, out, err = run_script("vacuity_check", "--repo", self.repo, "--test", shlex.join([PY, "test_noflag.py", flag]),
                                     "--mutate", "calc.py", self.guard_old, "", *extra, "--json")
            return json.loads(out)["verdict"]
        self.assertEqual(verdict("skipme/flag.txt"), "baseline_fails")  # copied by default
        self.assertEqual(verdict("node_modules/flag.txt"), "vacuous")  # skipped by default
        self.assertEqual(verdict("skipme/flag.txt", "--ignore", "skipme"), "vacuous")
        self.assertEqual(verdict("node_modules/flag.txt", "--ignore", "skipme"), "vacuous")  # the defaults stay
        self.assertEqual(verdict("node_modules/flag.txt", "--ignore", "none"), "baseline_fails")
        self.assertEqual(verdict("node_modules/flag.txt", "--ignore", "none", "--ignore", "skipme"), "baseline_fails")
        self.assertEqual(verdict("skipme/flag.txt", "--ignore", "none", "--ignore", "skipme"), "vacuous")

    def test_any_file_encoding(self):
        (self.repo / "data.bin").write_bytes(b"\xff\xfe value=1\n")
        (self.repo / "test_bytes.py").write_text(TEST_BYTES, encoding="utf-8")
        code, r, err = self.vac("test_bytes.py", "--mutate", "data.bin", "value=1", "value=2")
        self.assertEqual((code, r["verdict"]), (0, "guarded"), err)

    def test_mutations_file_mixed_results(self):
        muts = [{"file": "calc.py", "old": self.guard_old, "new": "", "label": "drop zero guard"},
                {"file": "calc.py", "old": "return a / b", "new": "return a // b", "label": "floor div"}]
        code, r, _ = self.vac("test_guard.py", "--mutations", self.muts_file(muts))
        # 4 // 2 == 2 too, so the second mutation is not caught: the report must say so
        self.assertEqual((code, r["verdict"]), (1, "vacuous"))
        self.assertEqual([m["status"] for m in r["mutations"]], ["guarded", "vacuous"])
        self.assertEqual(r["mutations"][1]["label"], "floor div")

    def test_human_output(self):
        code, out, _ = run_script("vacuity_check", "--repo", self.repo, "--test",
                                  shlex.join([PY, "test_guard.py"]), "--mutate", "calc.py", self.guard_old, "")
        self.assertEqual(code, 0)
        self.assertIn("verdict: guarded", out)
        self.assertIn("baseline: exit 0 (pass)", out)

    def test_tool_errors_exit_2(self):
        notexec = self.repo / "run.sh"
        notexec.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(notexec, 0o644)
        code, _, err = run_script("vacuity_check", "--repo", self.repo, "--test", "./run.sh",
                                  "--mutate", "calc.py", self.guard_old, "")
        self.assertEqual(code, 2)
        self.assertIn("cannot run", err)

    def test_usage_errors(self):
        t = shlex.join([PY, "test_guard.py"])
        self.assertEqual(run_script("vacuity_check", "--repo", self.repo, "--test", t)[0], 2)
        self.assertEqual(run_script("vacuity_check", "--repo", "/no/such/dir", "--test", t,
                                    "--mutate", "calc.py", "a", "b")[0], 2)
        self.assertEqual(run_script("vacuity_check", "--repo", self.repo, "--test", t,
                                    "--mutate", "/etc/passwd", "a", "b")[0], 2)
        self.assertEqual(run_script("vacuity_check", "--repo", self.repo, "--test", t,
                                    "--mutate", "../x.py", "a", "b")[0], 2)
        self.assertEqual(run_script("vacuity_check", "--repo", self.repo, "--test", t,
                                    "--mutate", "calc.py", "", "b")[0], 2)
        bad = self.tmp / "bad.json"
        bad.write_text('{"not": "a list"}', encoding="utf-8")
        self.assertEqual(run_script("vacuity_check", "--repo", self.repo, "--test", t, "--mutations", bad)[0], 2)
        self.assertEqual(run_script("vacuity_check", "--repo", self.repo, "--test", t, "--mutate", "calc.py", "a", "b",
                                    "--timeout", "nan")[0], 2)
        for entry, message in (({"file": "calc.py", "old": 5, "new": "x"}, "needs string fields"),
                               ({"file": "calc.py", "old": "a", "new": "b", "expect": 5}, "expect must be a non-empty string"),
                               ({"file": "calc.py", "old": "a", "new": "b", "expect": ""}, "expect must be a non-empty string")):
            bad.write_text(json.dumps([entry]), encoding="utf-8")
            code, _, err = run_script("vacuity_check", "--repo", self.repo, "--test", t, "--mutations", bad)
            self.assertEqual(code, 2, err)
            self.assertIn(message, err)
        self.assertEqual(run_script("vacuity_check", "--repo", self.repo, "--test", t, "--mutate", "calc.py", "a", "b",
                                    "--timeout", "1e308")[0], 2)
        if Path("/proc/self/mem").is_file():  # a file that exists but cannot be read
            code, _, err = run_script("vacuity_check", "--repo", self.repo, "--test", t, "--mutations", "/proc/self/mem")
            self.assertEqual(code, 2, err)
            self.assertNotIn("Traceback", err)

    def test_help(self):
        code, out, _ = run_script("vacuity_check", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--mutations", out)


if __name__ == "__main__":
    unittest.main()
