"""Tests for the reading hub (D21) and its figure registry (D22). Stdlib unittest only.

- the real site builds, every registry claim is a valid AOK claim, every figure points at one
- the build refuses unknown figures, unused registry entries, [확인] without evidence,
  missing {{doc:}} files, Artifact URLs and session ids (public-repo rule)
- supporting pages carry their own document skeleton; the published index does not

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

from test_scripts import validate  # noqa: E402  (minimal JSON Schema validator shared with the schema tests)

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
BUILD = ROOT / "scripts" / "build_site.py"


def run_build(*args: str):
    proc = subprocess.run([sys.executable, "-I", str(BUILD), *map(str, args)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


PAGE = """<!--page
title: 시험
nav: 시험
order: 1
description: 시험용 페이지
-->
<main><p>값 {{fig:f1}} · 문서 {{doc:README.md}}</p>BODY</main>
"""


class RealSiteTests(unittest.TestCase):
    def test_real_site_builds(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = run_build("--out", Path(d) / "dist")
            self.assertEqual(code, 0, err)
            built = sorted(p.name for p in (Path(d) / "dist").glob("*.html"))
            self.assertIn("index.html", built)
            self.assertEqual(built, sorted(p.name for p in (SITE / "pages").glob("*.html")))

    def test_check_mode_writes_nothing(self):
        before = (SITE / "dist").exists()
        code, _, err = run_build("--check")
        self.assertEqual(code, 0, err)
        self.assertEqual((SITE / "dist").exists(), before)

    def test_registry_claims_valid(self):
        claims = json.loads((SITE / "claims.json").read_text(encoding="utf-8"))
        self.assertGreater(len(claims), 0)
        ids = [c["id"] for c in claims]
        self.assertEqual(len(ids), len(set(ids)), "duplicate claim ids")
        for c in claims:
            self.assertEqual(validate("claim", c), [], c["id"])

    def test_figures_point_at_claims(self):
        claims = {c["id"] for c in json.loads((SITE / "claims.json").read_text(encoding="utf-8"))}
        figures = json.loads((SITE / "figures.json").read_text(encoding="utf-8"))
        for fid, f in figures.items():
            self.assertIn(f["claim"], claims, fid)
            self.assertTrue(f["text"].strip(), fid)

    def test_skeletons(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_build("--out", Path(d) / "dist")[0], 0)
            index = (Path(d) / "dist" / "index.html").read_text(encoding="utf-8")
            self.assertFalse(index.lstrip().lower().startswith("<!doctype"), "index is wrapped at publish time")
            self.assertTrue(index.lstrip().startswith("<title>"))
            for p in (Path(d) / "dist").glob("*.html"):
                if p.name != "index.html":
                    self.assertTrue(p.read_text(encoding="utf-8").startswith("<!doctype html>"), p.name)

    def test_built_pages_have_no_placeholders_or_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_build("--out", Path(d) / "dist")[0], 0)
            for p in (Path(d) / "dist").glob("*.html"):
                text = p.read_text(encoding="utf-8")
                self.assertNotIn("{{", text, p.name)
                self.assertNotIn("claude.ai/artifact/", text, p.name)
            scan = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "secret_scan.py"), "--strict",
                                   str(Path(d) / "dist")], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(scan.returncode, 0, scan.stdout + scan.stderr)


class BuildCheckTests(unittest.TestCase):
    """Each check on a minimal synthetic site."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "README.md").write_text("# x\n", encoding="utf-8")
        site = self.root / "site"
        (site / "pages").mkdir(parents=True)
        (site / "assets").mkdir()
        (site / "assets" / "site.css").write_text("body{}\n", encoding="utf-8")
        self.claims = [{"id": "C1", "claim": "값은 1이다.", "grade": "확인",
                        "evidence": [{"kind": "cmd", "ref": "echo 1"}], "scope": "시험"}]
        self.figures = {"f1": {"text": "1", "claim": "C1"}}
        self.page = PAGE.replace("BODY", "")
        self.write()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self):
        site = self.root / "site"
        (site / "claims.json").write_text(json.dumps(self.claims, ensure_ascii=False), encoding="utf-8")
        (site / "figures.json").write_text(json.dumps(self.figures, ensure_ascii=False), encoding="utf-8")
        (site / "pages" / "index.html").write_text(self.page, encoding="utf-8")

    def build(self):
        return run_build("--root", self.root, "--out", self.root / "out")

    def test_minimal_site_builds_with_evidence_table(self):
        code, _, err = self.build()
        self.assertEqual(code, 0, err)
        html = (self.root / "out" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="#ev-C1"', html)
        self.assertIn('id="ev-C1"', html)
        self.assertIn("값은 1이다.", html)
        self.assertIn("/blob/main/README.md", html)

    def test_unknown_figure(self):
        self.page = PAGE.replace("BODY", "{{fig:nope}}")
        self.write()
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("unknown figure nope", err)
        self.assertFalse((self.root / "out").exists(), "nothing is written when a check fails")

    def test_unused_figure_and_claim(self):
        self.claims.append({"id": "C2", "claim": "안 쓰인다.", "grade": "추론"})
        self.figures["f2"] = {"text": "2", "claim": "C1"}
        self.write()
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("figure f2 is not used", err)
        self.assertIn("claim C2 is not used", err)

    def test_confirmed_needs_evidence(self):
        self.claims[0].pop("evidence")
        self.write()
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("[확인] needs at least one evidence item", err)

    def test_bad_grade_and_extra_field(self):
        self.claims[0]["grade"] = "실측"
        self.claims[0]["value"] = 1
        self.write()
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("grade '실측'", err)
        self.assertIn("fields outside the claim schema", err)

    def test_missing_doc(self):
        self.page = PAGE.replace("BODY", "{{doc:docs/none.md}}")
        self.write()
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("missing file docs/none.md", err)

    def test_public_repo_rule(self):
        # assembled at runtime so this file stays clean under secret_scan.py (same convention as test_scripts.py)
        for bad in ("claude.ai/" + "artifact/" + "abcdef1234567890", "session_" + "01" + "ABCDEFGHIJKLMNOPQRSTUV"):
            self.page = PAGE.replace("BODY", bad)
            self.write()
            code, _, err = self.build()
            self.assertEqual(code, 1, bad)
            self.assertIn("public-repo rule", err)

    def test_metadata_required(self):
        self.page = "<main>{{fig:f1}}</main>\n"
        self.write()
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("missing <!--page", err)

    def test_usage_errors(self):
        self.assertEqual(run_build("--root", "/no/such/dir")[0], 2)
        self.assertEqual(run_build("--ref", "bad ref;x")[0], 2)


if __name__ == "__main__":
    unittest.main()
