"""Replays synthetic sessions through the console's inbox rules (dashboard/console.html).

The cases in tests/console/inbox_cases.json were written from the rule spec (docs/dashboard.md §3a) by an agent
that did not read the console code, so they act as a holdout set. Cases marked "ambiguous" are reported, not asserted.
tests/console/atlas_cases.json adds fsmap measurements (ledger atlas/<session_id>, §3b) and checks that a measurement
outranks the wording guess without changing the cases that have none.

Needs node, the `playwright` package and a Chromium build; skipped when any is missing.
Run from the repo root:  python3 -m unittest tests.test_console_inbox -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tests" / "console" / "run_inbox_cases.cjs"
CASES = ROOT / "tests" / "console" / "inbox_cases.json"
ATLAS_CASES = ROOT / "tests" / "console" / "atlas_cases.json"
GROUP_KINDS = {"same_repo", "hygiene"}


def _node_env() -> dict | None:
    node = shutil.which("node")
    if not node:
        return None
    env = dict(os.environ)
    npm = shutil.which("npm")
    if npm and "NODE_PATH" not in env:
        root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True).stdout.strip()
        if root:
            env["NODE_PATH"] = root
    ok = subprocess.run([node, "-e", "require.resolve('playwright')"], env=env, capture_output=True).returncode == 0
    return env if ok else None


def _run(mode: str, env: dict, cases: Path = CASES) -> dict:
    out = subprocess.run(["node", str(RUNNER), str(cases), mode], env=env, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise AssertionError(f"runner failed ({mode}): {out.stderr.strip()[-800:]}")
    return json.loads(out.stdout)


def _main_kinds(result: dict) -> dict:
    best: dict[str, dict] = {}
    for item in result["inbox"]:
        sid = item.get("sid")
        if not sid or item["kind"] in GROUP_KINDS:
            continue
        if sid not in best or item["prio"] > best[sid]["prio"]:
            best[sid] = item
    return best


class ConsoleInboxCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = _node_env()
        if cls.env is None:
            raise unittest.SkipTest("node + playwright not available")
        cls.cases = json.loads(CASES.read_text(encoding="utf-8"))
        cls.full = _run("full", cls.env)

    def test_no_page_errors(self):
        self.assertEqual(self.full["errors"], [])

    def test_holdout_cases(self):
        best = _main_kinds(self.full)
        hygiene = {sid for item in self.full["inbox"] if item["kind"] == "hygiene" for sid in item["n"]}
        failures, ambiguous = [], []
        for sid, case in zip(self.full["ids"], self.cases):
            item = best.get(sid)
            got = {
                "kind": item["kind"] if item else "none",
                "settings_note": bool(item and "설정·권한 변경" in item["note"]),
                "superseded": bool(item and item["superseded"]),
                "in_hygiene": sid in hygiene,
            }
            want = {
                "kind": case["expect_kind"],
                "settings_note": case.get("expect_settings_note", False),
                "superseded": case.get("expect_superseded", False),
                "in_hygiene": case.get("expect_in_hygiene", False),
            }
            if got != want:
                diff = {k: (want[k], got[k]) for k in want if want[k] != got[k]}
                (ambiguous if case.get("ambiguous") else failures).append(f"{sid} {case['case']}: {diff}")
        if ambiguous:
            print("\nambiguous cases that differ (not asserted):\n  " + "\n  ".join(ambiguous))
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_slim_list_with_get_session_matches_full(self):
        slim = _run("slim", self.env)
        self.assertEqual(slim["errors"], [])
        self.assertTrue(slim["fetched"], "no get_session calls in slim mode")
        full_best, slim_best = _main_kinds(self.full), _main_kinds(slim)
        for sid in slim["fetched"]:
            self.assertEqual((full_best.get(sid) or {}).get("kind"), (slim_best.get(sid) or {}).get("kind"), sid)

    def test_snapshot_fallback_without_connector(self):
        snap = _run("snapshot", self.env)
        self.assertEqual(snap["errors"], [])
        self.assertTrue(any("스냅숏" in t for t in snap["notices"]), snap["notices"])
        full_best, snap_best = _main_kinds(self.full), _main_kinds(snap)
        self.assertEqual({k: v["kind"] for k, v in full_best.items()}, {k: v["kind"] for k, v in snap_best.items()})


    def test_atlas_measurements(self):
        cases = json.loads(ATLAS_CASES.read_text(encoding="utf-8"))
        res = _run("full", self.env, ATLAS_CASES)
        self.assertEqual(res["errors"], [])
        best = _main_kinds(res)
        hygiene = {sid for item in res["inbox"] if item["kind"] == "hygiene" for sid in item["n"]}
        failures = []
        for sid, case in zip(res["ids"], cases):
            item = best.get(sid)
            got = {"kind": item["kind"] if item else "none", "in_hygiene": sid in hygiene}
            want = {"kind": case["expect_kind"], "in_hygiene": case.get("expect_in_hygiene", False)}
            if "expect_measured" in case:
                got["measured"], want["measured"] = bool(item and item["measured"]), case["expect_measured"]
            if "expect_prio" in case:
                got["prio"], want["prio"] = item["prio"] if item else None, case["expect_prio"]
            if "expect_note" in case:
                got["note"], want["note"] = bool(item and case["expect_note"] in item["note"]), True
            if got != want:
                failures.append(f"{sid} {case['case']}: {({k: (want[k], got[k]) for k in want if want[k] != got[k]})}")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))
        measured = [c for c in cases if "atlas" in c]
        self.assertEqual(len(res["atlasRows"]), len(measured))
        self.assertTrue(any(t.startswith("컨테이너에만 있는 작업") and "측정 세션 " + str(len(measured)) + "개 중" in t for t in res["tiles"]), res["tiles"])
        self.assertIn("작업공간 지도 (fsmap 측정)", res["atlasBox"] or [])


if __name__ == "__main__":
    unittest.main()
