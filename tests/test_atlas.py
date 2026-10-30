"""Tests for scripts/atlas_record.py (fsmap snapshot -> AOK atlas record).

The unit tests use a small hand-written `fsmap/1` snapshot. The integration test builds a
throwaway git repository with work that exists only locally and scans it with a real fsmap
binary; it is skipped when fsmap is not on PATH (or named by the FSMAP environment variable).

Run from the repo root:  python3 -m unittest tests.test_atlas -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "atlas_record.py"


def load(path=SCRIPT, name="atlas_record"):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def schema_errors(record) -> list[str]:
    """Validate against the ledger schema's #/$defs/atlas with the validator the schema tests use."""
    return load(ROOT / "tests" / "test_scripts.py", "aok_schema_tests").validate("ledger", record, "#/$defs/atlas")


def snapshot(repos, findings=None):
    return {"schema": "fsmap/1", "tool": "fsmap 0.1.0", "root": "/work/space", "generatedAt": 1793350000,
            "totals": {"files": 10, "dirs": 3, "bytes": 2048, "disk": 4096, "code": 120, "tokens": 900},
            "repos": repos, "findings": findings or []}


CLEAN = {"path": "kit", "head": "abc", "shallow": True, "untracked": 0,
         "hygiene": {"dirty": 0, "branch": "main", "upstream": "origin/main", "ahead": 0, "behind": 0,
                     "stashes": 0, "unpushedBranches": []}}
LOCAL = {"path": "draft", "head": "def", "untracked": 2,
         "hygiene": {"dirty": 3, "branch": "wip", "upstream": None, "ahead": 0, "behind": 0,
                     "stashes": 1, "unpushedBranches": ["wip", "main"]}}


class BuildRecord(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_clean_workspace_is_not_volatile(self):
        rec = self.m.build_record(snapshot([CLEAN]), "session_x")
        self.assertEqual(rec["schema"], "aok-atlas/1")
        self.assertFalse(rec["local_only"]["volatile"])
        self.assertEqual(rec["root"], "space")
        self.assertEqual(rec["generated_at"], "2026-10-30T08:46:40Z")
        self.assertEqual(rec["totals"], {"files": 10, "bytes": 2048, "disk": 4096, "code": 120, "tokens": 900})

    def test_local_only_work_is_volatile_and_listed_first(self):
        rec = self.m.build_record(snapshot([CLEAN, LOCAL]), "session_x")
        lo = rec["local_only"]
        self.assertTrue(lo["volatile"])
        self.assertEqual((lo["repos"], lo["dirty"], lo["untracked"], lo["stashes"], lo["unpushed_branches"]), (1, 3, 2, 1, 2))
        self.assertEqual(rec["repos"][0]["path"], "draft")
        self.assertEqual(rec["repos"][0]["unpushed_branches"], ["wip", "main"])

    def test_commits_ahead_alone_are_volatile(self):
        ahead = json.loads(json.dumps(CLEAN)); ahead["hygiene"]["ahead"] = 2
        self.assertTrue(self.m.build_record(snapshot([ahead]), "s")["local_only"]["volatile"])

    def test_findings_sorted_by_severity_and_capped(self):
        fs = [{"severity": "info", "title": f"i{i}"} for i in range(12)] + [{"severity": "high", "rule": "unpushed-work", "title": "h"}]
        rec = self.m.build_record(snapshot([CLEAN], fs), "session_x")
        self.assertEqual(rec["findings"][0], {"severity": "high", "rule": "unpushed-work", "title": "h"})
        self.assertEqual(len(rec["findings"]), 10)
        self.assertEqual(rec["findings_total"], 13)

    def test_unknown_severity_is_recorded_as_info(self):
        rec = self.m.build_record(snapshot([CLEAN], [{"severity": "critical", "title": "x"}]), "session_x")
        self.assertEqual(rec["findings"][0]["severity"], "info")

    def test_records_match_the_ledger_schema(self):
        for repos in ([], [CLEAN], [CLEAN, LOCAL]):
            with self.subTest(repos=len(repos)):
                rec = self.m.build_record(snapshot(repos, [{"severity": "low", "title": "t"}]), "session_x", "label", None)
                self.assertEqual(schema_errors(rec), [])
        bad = self.m.build_record(snapshot([LOCAL]), "session_x"); bad["local_only"]["volatile"] = "yes"
        self.assertNotEqual(schema_errors(bad), [])

    def test_rejects_other_schema(self):
        with self.assertRaises(ValueError):
            self.m.build_record({"schema": "fsmap/2"}, "s")

    def test_no_git_snapshot(self):
        snap = snapshot([]); del snap["repos"]
        rec = self.m.build_record(snap, "s")
        self.assertEqual((rec["repos"], rec["repos_total"], rec["local_only"]["volatile"]), ([], 0, False))

    def test_cli_round_trip_and_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "snap.json"; src.write_text(json.dumps(snapshot([LOCAL])), encoding="utf-8")
            out = subprocess.run([sys.executable, "-I", str(SCRIPT), str(src), "--session-id", "session_x", "--label", "t"],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(json.loads(out.stdout)["local_only"]["volatile"])
            src.write_text("not json", encoding="utf-8")
            bad = subprocess.run([sys.executable, "-I", str(SCRIPT), str(src), "--session-id", "s"], capture_output=True, text=True, timeout=60)
            self.assertEqual(bad.returncode, 2)


@unittest.skipUnless(os.environ.get("FSMAP") or shutil.which("fsmap"), "fsmap binary not available")
class WithRealFsmap(unittest.TestCase):
    def test_detects_work_that_exists_only_locally(self):
        fsmap = os.environ.get("FSMAP") or shutil.which("fsmap")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "ws" / "proj"; repo.mkdir(parents=True)
            git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
            git("init", "-q", "-b", "main")
            git("config", "user.email", "t@example.com"); git("config", "user.name", "t")
            (repo / "a.txt").write_text("a\n"); git("add", "a.txt"); git("commit", "-qm", "init")
            git("checkout", "-qb", "feature/local-only")
            (repo / "b.txt").write_text("b\n"); git("add", "b.txt"); git("commit", "-qm", "local")
            (repo / "a.txt").write_text("a\nchanged\n")
            snap = Path(tmp) / "snap.json"
            subprocess.run([fsmap, "scan", str(Path(tmp) / "ws"), "-o", str(snap), "-q", "--no-dupes"], check=True, timeout=120)
            rec = load().build_record(json.loads(snap.read_text(encoding="utf-8")), "session_x")
            self.assertEqual(schema_errors(rec), [])
            self.assertTrue(rec["local_only"]["volatile"])
            self.assertIn("feature/local-only", rec["repos"][0]["unpushed_branches"])
            self.assertGreaterEqual(rec["repos"][0]["dirty"], 1)


if __name__ == "__main__":
    unittest.main()
