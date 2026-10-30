"""Tests for scripts/atlas_record.py (fsmap snapshot -> AOK atlas record).

The unit tests use a small hand-written `fsmap/1` snapshot. The integration tests build throwaway git
repositories with work that exists only locally (each kind fsmap reports, plus a detached-HEAD commit and a
branch whose upstream is gone, which only the local-commit check sees) and scan them with a real fsmap
binary; they are skipped when fsmap is not on PATH (or named by the FSMAP environment variable).

Run from the repo root:  python3 -I -m unittest discover -s tests -p test_atlas.py -v
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


def snapshot(repos, findings=None, **extra):
    return {"schema": "fsmap/1", "tool": "fsmap 0.1.0", "root": "/work/space", "generatedAt": 1793350000,
            "totals": {"files": 10, "dirs": 3, "bytes": 2048, "disk": 4096, "code": 120, "tokens": 900},
            "repos": repos, "findings": findings or [], **extra}


HYG = {"dirty": 0, "branch": "main", "upstream": "origin/main", "ahead": 0, "behind": 0, "stashes": 0, "unpushedBranches": []}
CLEAN = {"path": "kit", "head": "abc", "shallow": True, "untracked": 0, "hygiene": dict(HYG)}
LOCAL = {"path": "draft", "head": "def", "untracked": 2,
         "hygiene": dict(HYG, dirty=3, branch="wip", upstream=None, stashes=1, unpushedBranches=["wip", "main"])}


def codes(rec):
    return [g["code"] for g in rec["coverage"]["gaps"]]


class BuildRecord(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_clean_snapshot_without_git_check_is_not_volatile_but_incomplete(self):
        rec = self.m.build_record(snapshot([CLEAN]), "session_x")
        self.assertEqual(rec["schema"], "aok-atlas/1")
        self.assertFalse(rec["local_only"]["volatile"])
        self.assertEqual((rec["coverage"]["complete"], codes(rec)), (False, ["local_commits_unchecked"]))
        self.assertIsNone(rec["repos"][0]["local_commits"])
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
        ahead = json.loads(json.dumps(CLEAN))
        ahead["hygiene"]["ahead"] = 2
        self.assertTrue(self.m.build_record(snapshot([ahead]), "session_x")["local_only"]["volatile"])

    def test_unpushed_branches_are_counted_before_the_cap(self):
        many = json.loads(json.dumps(LOCAL))
        many["hygiene"]["unpushedBranches"] = [f"b{i}" for i in range(12)]
        rec = self.m.build_record(snapshot([many]), "session_x")
        self.assertEqual((len(rec["repos"][0]["unpushed_branches"]), rec["local_only"]["unpushed_branches"]), (10, 12))

    def test_gaps_mark_the_record_incomplete(self):
        broken = {"path": "odd", "head": "abc", "untracked": 0, "hygiene": None, "error": "git failed"}
        partial = {"path": "new", "head": "abc", "untracked": 0, "hygiene": {"dirty": 0, "branch": "main"}}
        rec = self.m.build_record(snapshot([broken, partial], reposNotAnalysed=3), "session_x")
        for code in ("repo_error", "hygiene_fields_missing", "repos_not_analysed"):
            self.assertIn(code, codes(rec))
        self.assertEqual(rec["repos_total"], 5)
        self.assertFalse(rec["coverage"]["complete"])

    def test_no_git_snapshot(self):
        snap = snapshot([])
        del snap["repos"]
        rec = self.m.build_record(snap, "session_x")
        self.assertEqual((rec["repos"], rec["repos_total"], rec["local_only"]["volatile"]), ([], 0, False))
        self.assertEqual(codes(rec), ["no_repos"])

    def test_repo_without_commits_needs_no_commit_check(self):
        empty = {"path": "fresh", "head": None, "untracked": 1, "hygiene": None}
        rec = self.m.build_record(snapshot([empty]), "session_x")
        self.assertEqual(codes(rec), [])
        self.assertTrue(rec["local_only"]["volatile"])

    def test_findings_sorted_by_severity_and_capped(self):
        fs = [{"severity": "info", "title": f"i{i}"} for i in range(12)] + [{"severity": "high", "rule": "unpushed-work", "title": "h"}]
        rec = self.m.build_record(snapshot([CLEAN], fs), "session_x")
        self.assertEqual(rec["findings"][0], {"severity": "high", "rule": "unpushed-work", "title": "h"})
        self.assertEqual(len(rec["findings"]), 10)
        self.assertEqual(rec["findings_total"], 13)

    def test_unknown_severity_is_recorded_as_info(self):
        rec = self.m.build_record(snapshot([CLEAN], [{"severity": "critical", "title": "x"}]), "session_x")
        self.assertEqual(rec["findings"][0]["severity"], "info")

    def test_bad_input_is_rejected(self):
        for snap, sid in (({"schema": "fsmap/2"}, "session_x"), (["not", "a", "dict"], "session_x"), (snapshot([CLEAN]), "s")):
            with self.subTest(sid=sid, snap=str(snap)[:30]), self.assertRaises(ValueError):
                self.m.build_record(snap, sid)
        self.assertIsNone(self.m.build_record(snapshot([CLEAN], generatedAt=10 ** 20), "session_x")["generated_at"])

    def test_records_match_the_ledger_schema(self):
        for repos in ([], [CLEAN], [CLEAN, LOCAL]):
            with self.subTest(repos=len(repos)):
                rec = self.m.build_record(snapshot(repos, [{"severity": "low", "title": "t"}]), "session_x", "label", None)
                self.assertEqual(schema_errors(rec), [])
        bad = self.m.build_record(snapshot([LOCAL]), "session_x")
        bad["local_only"]["volatile"] = "yes"
        self.assertNotEqual(schema_errors(bad), [])
        bad = self.m.build_record(snapshot([LOCAL]), "session_x")
        bad["coverage"]["gaps"] = [{"code": "other", "detail": None}]
        self.assertNotEqual(schema_errors(bad), [])

    def test_cli_round_trip_and_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "snap.json"
            src.write_text(json.dumps(snapshot([LOCAL])), encoding="utf-8")

            def run(*args):
                return subprocess.run([sys.executable, "-I", str(SCRIPT), str(src), *args], capture_output=True, text=True, timeout=60)

            out = run("--session-id", "session_x", "--label", "t")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(json.loads(out.stdout)["local_only"]["volatile"])
            self.assertEqual(run("--session-id", "s").returncode, 2)
            for text in ("not json", "[1, 2]"):
                src.write_text(text, encoding="utf-8")
                self.assertEqual(run("--session-id", "session_x").returncode, 2, text)


def _git(repo, *args):
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def _pushed_repo(tmp: Path, name: str) -> Path:
    """A repository with one commit pushed to its own bare remote, upstream set."""
    remote = tmp / (name + ".git")
    repo = tmp / "ws" / name
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("1\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


@unittest.skipUnless(os.environ.get("FSMAP") or shutil.which("fsmap"), "fsmap binary not available")
class WithRealFsmap(unittest.TestCase):
    def scan(self, ws: Path) -> dict:
        fsmap = os.environ.get("FSMAP") or shutil.which("fsmap")
        snap = ws.parent / "snap.json"
        subprocess.run([fsmap, "scan", str(ws), "-o", str(snap), "-q", "--no-dupes"], check=True, timeout=120)
        rec = load().build_record(json.loads(snap.read_text(encoding="utf-8")), "session_x", git_root=str(ws))
        self.assertEqual(schema_errors(rec), [])
        return rec

    def test_each_kind_of_local_only_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _pushed_repo(tmp, "clean")
            misc = _pushed_repo(tmp, "misc")
            (misc / "f").write_text("stash me\n")
            _git(misc, "stash", "-q")
            (misc / "f").write_text("ahead\n")
            _git(misc, "commit", "-qam", "ahead")
            (misc / "f").write_text("dirty\n")
            (misc / "new.txt").write_text("untracked\n")
            _git(misc, "branch", "local-only")
            detached = _pushed_repo(tmp, "detached")
            _git(detached, "checkout", "-q", "--detach")
            (detached / "f").write_text("d\n")
            _git(detached, "commit", "-qam", "d")
            gone = _pushed_repo(tmp, "gone")
            _git(gone, "checkout", "-q", "-b", "feat")
            _git(gone, "push", "-q", "-u", "origin", "feat")
            _git(gone, "push", "-q", "origin", "--delete", "feat")
            _git(gone, "fetch", "-q", "--prune")
            (gone / "f").write_text("g\n")
            _git(gone, "commit", "-qam", "g")
            rec = self.scan(tmp / "ws")
            by = {r["path"]: r for r in rec["repos"]}
            self.assertTrue(rec["coverage"]["complete"], rec["coverage"])
            self.assertEqual((by["clean"]["dirty"], by["clean"]["local_commits"]), (0, 0))
            m = by["misc"]
            self.assertEqual((m["dirty"], m["untracked"], m["stashes"], m["ahead"], m["local_commits"]), (1, 1, 1, 1, 1))
            self.assertIn("local-only", m["unpushed_branches"])
            # fsmap's hygiene alone sees nothing in these two; the local-commit check does.
            for name in ("detached", "gone"):
                self.assertEqual((by[name]["ahead"], by[name]["unpushed_branches"], by[name]["local_commits"]), (0, [], 1), name)
            self.assertTrue(rec["local_only"]["volatile"])
            self.assertEqual(rec["local_only"]["repos"], 3)
            self.assertEqual(rec["repos"][-1]["path"], "clean")

    def test_clean_workspace_is_complete_and_not_volatile(self):
        with tempfile.TemporaryDirectory() as tmp:
            _pushed_repo(Path(tmp), "a")
            rec = self.scan(Path(tmp) / "ws")
            self.assertEqual((rec["local_only"]["volatile"], rec["coverage"]["complete"]), (False, True))


if __name__ == "__main__":
    unittest.main()
