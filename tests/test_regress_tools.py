"""Tests for the regression-pinning tools of feature ⑦ (stdlib unittest only).

scripts/diffcheck.py      differential check: empty, failed, nondeterministic, differs, identical
scripts/vacuity_check.py  proves a test fails when the code it guards is mutated (on a copy)

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
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


def py(code: str) -> str:
    """A command string that runs `code` with this interpreter (for --base/--cand/--test)."""
    return shlex.join([PY, "-I", "-c", code])


class DiffcheckTests(unittest.TestCase):
    def diff(self, *args):
        code, out, err = run_script("diffcheck", *args, "--json")
        report = json.loads(out) if out.strip().startswith("{") else None
        return code, report, err

    def test_identical(self):
        code, r, _ = self.diff("--base", py("print('a')"), "--cand", py("print('a')"))
        self.assertEqual((code, r["verdict"]), (0, "identical"))
        self.assertEqual(r["base"]["bytes"], 2)

    def test_differs_shows_diff_head(self):
        code, r, _ = self.diff("--base", py("print('a')"), "--cand", py("print('b')"))
        self.assertEqual((code, r["verdict"]), (1, "differs"))
        self.assertIn("-a", r["diff_head"])
        self.assertIn("+b", r["diff_head"])

    def test_empty_outputs_are_not_identical(self):
        # the trap that motivated ⑦: both sides silent, a naive compare says "same"
        code, r, _ = self.diff("--base", py("pass"), "--cand", py("pass"))
        self.assertEqual((code, r["verdict"]), (1, "empty"))
        code, r, _ = self.diff("--base", py("pass"), "--cand", py("pass"), "--allow-empty")
        self.assertEqual((code, r["verdict"]), (0, "identical"))

    def test_one_side_empty(self):
        code, r, _ = self.diff("--base", py("print('x')"), "--cand", py("pass"))
        self.assertEqual(r["verdict"], "empty")
        self.assertTrue(any("cand" in p for p in r["problems"]))

    def test_failed_exit_code(self):
        code, r, _ = self.diff("--base", py("print('a'); raise SystemExit(3)"), "--cand", py("print('a')"))
        self.assertEqual((code, r["verdict"]), (1, "failed"))
        self.assertEqual(r["base"]["exit"], 3)
        code, r, _ = self.diff("--base", py("print('a'); raise SystemExit(3)"), "--cand", py("print('a')"),
                               "--allow-fail")
        self.assertEqual(r["verdict"], "identical")

    def test_nondeterminism_detected_and_normalized(self):
        rnd = py("import secrets; print('id', secrets.token_hex(4))")
        code, r, _ = self.diff("--base", rnd, "--cand", rnd, "--determinism")
        self.assertEqual((code, r["verdict"]), (1, "nondeterministic"))
        code, r, _ = self.diff("--base", rnd, "--cand", rnd, "--determinism", "--normalize", "[0-9a-f]{8}")
        self.assertEqual((code, r["verdict"]), (0, "identical"))
        self.assertEqual(r["base"]["normalized_counts"], [1])

    def test_normalizing_everything_is_visible(self):
        # a regex that eats the whole output still "matches"; the counts make that visible
        code, r, _ = self.diff("--base", py("print('a')"), "--cand", py("print('b')"), "--normalize", "[ab]")
        self.assertEqual(r["verdict"], "identical")
        self.assertEqual((r["base"]["normalized_counts"], r["cand"]["normalized_counts"]), ([1], [1]))

    def test_nonce_is_fresh_per_invocation(self):
        echo = py("import sys; print(sys.argv[1])") + " {nonce}"
        code, r, _ = self.diff("--base", echo, "--cand", echo)
        self.assertEqual(r["verdict"], "differs")  # each invocation got its own token
        code, r, _ = self.diff("--base", echo, "--cand", echo, "--normalize", "[0-9a-f]{16}")
        self.assertEqual(r["verdict"], "identical")

    def test_stdin_with_nonce(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.txt"
            f.write_text("hello {nonce}\n", encoding="utf-8")
            cat = py("import sys; print(sys.stdin.read().split()[0])")
            code, r, _ = self.diff("--base", cat, "--cand", cat, "--stdin", str(f))
            self.assertEqual((code, r["verdict"]), (0, "identical"))

    def test_timing_is_interleaved(self):
        code, r, _ = self.diff("--base", py("print(1)"), "--cand", py("print(1)"), "--timing", "3")
        self.assertEqual(r["verdict"], "identical")
        t = r["timing"]
        self.assertTrue(t["interleaved"])
        self.assertEqual((t["base"]["n"], t["cand"]["n"]), (3, 3))
        self.assertLessEqual(t["base"]["min_ms"], t["base"]["median_ms"])
        self.assertIn("median_change_pct", t)

    def test_human_output(self):
        code, out, _ = run_script("diffcheck", "--base", py("print('a')"), "--cand", py("print('a')"))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("verdict: identical"))
        self.assertIn("base: exit 0 · 2 bytes", out)

    def test_usage_errors(self):
        ok = py("print(1)")
        self.assertEqual(run_script("diffcheck", "--base", ok, "--cand", ok, "--normalize", "(")[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", ok, "--cand", ok, "--stdin", "/no/such/file")[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", "no-such-command-xyz", "--cand", ok)[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", "", "--cand", ok)[0], 2)
        self.assertEqual(run_script("diffcheck", "--base", ok, "--cand", ok, "--timing", "-1")[0], 2)
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
    assert safe_div(1, 0) is None
    print("ok")
    ''')
TEST_VACUOUS = textwrap.dedent('''\
    from calc import safe_div
    assert safe_div(4, 2) == 2
    print("ok")
    ''')


class VacuityCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
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

    def test_original_untouched(self):
        before = (self.repo / "calc.py").read_text(encoding="utf-8")
        self.vac("test_guard.py", "--mutate", "calc.py", self.guard_old, "")
        self.assertEqual((self.repo / "calc.py").read_text(encoding="utf-8"), before)

    def test_mutations_file_mixed_results(self):
        muts = [{"file": "calc.py", "old": self.guard_old, "new": "", "label": "drop zero guard"},
                {"file": "calc.py", "old": "return a / b", "new": "return a // b", "label": "floor div"}]
        mf = Path(self._tmp.name) / "muts.json"
        mf.write_text(json.dumps(muts), encoding="utf-8")
        code, r, _ = self.vac("test_guard.py", "--mutations", mf)
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
        bad = Path(self._tmp.name) / "bad.json"
        bad.write_text('{"not": "a list"}', encoding="utf-8")
        self.assertEqual(run_script("vacuity_check", "--repo", self.repo, "--test", t, "--mutations", bad)[0], 2)

    def test_help(self):
        code, out, _ = run_script("vacuity_check", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--mutations", out)


if __name__ == "__main__":
    unittest.main()
