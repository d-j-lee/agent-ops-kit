"""Tests for the reading hub (D27) and its figure registry (D28). Stdlib unittest only.

- the real site builds, every registry claim is a valid AOK claim, every figure points at one
- each check that build_site.py documents refuses a minimal synthetic site that breaks it;
  tests/mutations/build_site.json removes the checks one by one and CI shows these tests catch it
- supporting pages carry their own document skeleton; the published index does not

Run from the repo root:  python3 -m unittest discover -s tests -v
                     or  python3 -m unittest tests.test_site
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
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `-m unittest tests.test_site` finds test_scripts too

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
OTHER = PAGE.replace("order: 1", "order: 2").replace("값 {{fig:f1}} · 문서 {{doc:README.md}}", "다른 쪽")


class RealSiteTests(unittest.TestCase):
    def test_real_site_builds(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = run_build("--out", Path(d) / "dist")
            self.assertEqual(code, 0, err)
            built = sorted(p.name for p in (Path(d) / "dist").glob("*.html"))
            self.assertIn("index.html", built)
            self.assertEqual(built, sorted(p.name for p in (SITE / "pages").glob("*.html")))

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

    def test_legend_words_match_the_norm(self):
        import re as _re
        src = (ROOT / "scripts" / "build_site.py").read_text(encoding="utf-8")
        block = src[src.index("GRADE_DEF = {"):src.index("}", src.index("GRADE_DEF = {"))]
        norm = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        words = _re.findall(r'"(\S+)": "([^"]+)"', block)
        self.assertEqual(len(words), 6)
        for grade, text in words:
            self.assertIn(f"[{grade}] {text}", norm, grade)

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
    """Each check on a minimal synthetic site. Every failing case also asserts that nothing was written."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "README.md").write_text("# x\n", encoding="utf-8")
        (self.root / "logs").mkdir()
        (self.root / "logs" / "run.txt").write_text("verdict: identical\nbase: exit 0\ncand: exit 0\ntiming 1 ms\n",
                                                   encoding="utf-8")
        site = self.root / "site"
        (site / "pages").mkdir(parents=True)
        (site / "assets").mkdir()
        (site / "assets" / "site.css").write_text("body{}\n", encoding="utf-8")
        self.claims = [{"id": "C1", "claim": "값은 1이다.", "grade": "확인",
                        "evidence": [{"kind": "cmd", "ref": "echo 1"}], "scope": "시험"}]
        self.figures = {"f1": {"text": "1", "claim": "C1"}}
        self.pages = {"index.html": PAGE.replace("BODY", "")}
        self.write()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self):
        site = self.root / "site"
        (site / "claims.json").write_text(json.dumps(self.claims, ensure_ascii=False), encoding="utf-8")
        (site / "figures.json").write_text(json.dumps(self.figures, ensure_ascii=False), encoding="utf-8")
        for old in (site / "pages").glob("*.html"):
            old.unlink()
        for name, text in self.pages.items():
            (site / "pages" / name).write_text(text, encoding="utf-8")

    def build(self):
        return run_build("--root", self.root, "--out", self.root / "out")

    def body(self, html: str):
        self.pages["index.html"] = PAGE.replace("BODY", html)
        self.write()

    def ok(self, msg: str = ""):
        code, _, err = self.build()
        self.assertEqual(code, 0, msg + err)
        shutil.rmtree(self.root / "out")

    def refused(self, *expected: str):
        code, _, err = self.build()
        self.assertEqual(code, 1, err)
        self.assertNotIn("Traceback", err, "a refusal is a reported error, not a crash")
        for e in expected:
            self.assertIn(e, err)
        self.assertFalse((self.root / "out").exists(), "nothing is written when a check fails")
        return err

    def test_minimal_site_builds_with_evidence_table(self):
        code, _, err = self.build()
        self.assertEqual(code, 0, err)
        html = (self.root / "out" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="#ev-C1"', html)
        self.assertIn('id="ev-C1"', html)
        self.assertIn("값은 1이다.", html)
        self.assertIn("/blob/main/README.md", html)

    def test_check_mode_writes_nothing(self):
        code, _, err = run_build("--root", self.root, "--check")
        self.assertEqual(code, 0, err)
        self.assertFalse((self.root / "site" / "dist").exists())
        self.assertFalse((self.root / "out").exists())

    def test_unknown_figure(self):
        self.body("{{fig:nope}}")
        self.refused("unknown figure nope")

    def test_unknown_claim_and_placeholder_kind(self):
        self.body("{{claim:nope}} {{grade:nope2}} {{chart:x}}")
        self.refused("unknown claim nope", "unknown claim nope2", "unknown placeholder kind 'chart'")

    def test_malformed_placeholder(self):
        self.body("{{ fig:f1 }} and {{fig:f1")
        self.refused("malformed placeholder")

    def test_unused_figure_and_claim(self):
        self.claims.append({"id": "C2", "claim": "안 쓰인다.", "grade": "추론"})
        self.figures["f2"] = {"text": "2", "claim": "C1"}
        self.write()
        self.refused("figure f2 is not used", "claim C2 is not used")

    def test_figure_to_missing_claim(self):
        self.figures["f1"]["claim"] = "C9"
        self.write()
        self.refused("figure f1: claim 'C9' not in claims.json")

    def test_duplicate_claim_id(self):
        self.claims.append(dict(self.claims[0]))
        self.write()
        self.refused("duplicate claim id C1")

    def test_confirmed_needs_evidence(self):
        self.claims[0].pop("evidence")
        self.write()
        self.refused("[확인] needs a cmd or url evidence item")

    def test_confirmed_needs_something_to_rerun(self):
        self.claims[0]["evidence"] = [{"kind": "file", "ref": "README.md"}]
        self.write()
        self.refused("[확인] needs a cmd or url evidence item")
        self.claims[0]["grade"] = "기록됨"  # a record is fine with a pointer
        self.write()
        self.ok()

    def test_evidence_items_are_checked(self):
        self.claims[0]["evidence"] = [{"kind": "note", "ref": "x"}, {"kind": "cmd", "ref": "run <input file>"},
                                      {"kind": "file", "ref": "reports/none.md@abc1234"},
                                      {"kind": "url", "ref": "javascript:alert(1)"},
                                      {"kind": "file", "ref": "../../etc/hostname"}]
        self.write()
        self.refused("evidence item needs kind", "cmd ref has a <placeholder>", "file ref to a missing file",
                     "url ref must start with https://", "file ref outside the repository")

    def test_file_ref_at_sha_links_that_sha(self):
        self.claims.append({"id": "C2", "claim": "기록.", "grade": "기록됨",
                            "evidence": [{"kind": "file", "ref": "README.md@abc1234"}]})
        self.body("{{claim:C2}}")
        self.assertEqual(self.build()[0], 0)
        self.assertIn("/blob/abc1234/README.md", (self.root / "out" / "index.html").read_text(encoding="utf-8"))

    def test_bad_grade_and_extra_field(self):
        self.claims[0]["grade"] = "실측"
        self.claims[0]["value"] = 1
        self.write()
        self.refused("grade '실측'", "fields outside the claim schema")

    def test_registry_entries_need_their_fields(self):
        self.claims.append({"id": "C2", "claim": "", "grade": "추론"})
        self.write()
        self.refused("claim without id or sentence")
        self.claims.pop()
        self.figures["f1"]["text"] = ""
        self.write()
        self.refused("figure f1: needs a text")
        self.figures["f1"]["text"] = "1"
        self.claims[0].update(grade="기록됨", evidence="echo 1")
        self.write()
        self.refused("claim C1: evidence must be a list")
        self.claims[0]["evidence"] = ["echo 1"]
        self.write()
        self.refused("claim C1: evidence item needs kind")

    def test_order_must_be_an_integer(self):
        self.pages["index.html"] = PAGE.replace("order: 1", "order: first").replace("BODY", "")
        self.write()
        self.refused("order must be an integer")

    def test_typed_confirmed_badge(self):
        for badge in ('<span class="g g-ok">확인</span>', '<span class="g-ok g">확인</span>',
                      "<span class='g g-ok'>확인</span>", '<b class="g g-ok">확인</b>'):
            self.body(badge)
            self.refused("a [확인] badge is typed into the page")

    def test_grade_placeholder_and_legend(self):
        self.body("{{grade:C1}} {{legend:all}}")
        code, _, err = self.build()
        self.assertEqual(code, 0, err)
        html = (self.root / "out" / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="g g-ok" href="#ev-C1"', html)
        self.assertIn("외부 원문(도구·제품 문서, 논문·공식 보고서)에 그렇게 적혀 있음", html)
        shutil.rmtree(self.root / "out")
        self.body('<ul class="legend"><li>확인: 파일 하나로 충분</li></ul>')
        self.refused("a grade legend is typed into the page")

    def test_terminal_excerpt_needs_a_log(self):
        self.body('<pre class="term">$ bench\nmedian 101 ms (-46.8%)</pre>')
        self.refused("needs data-log")
        self.body('<pre class="term" data-unverified>$ cargo check\nerror</pre>')
        self.ok()
        self.body('<pre data-log="logs/run.txt"><span class="dim"># 99% faster</span>verdict: identical</pre>')
        self.refused("measured quantity(ies) outside the registry: 99%")

    def test_cmd_must_be_valid_shell(self):
        self.claims[0]["evidence"] = [{"kind": "cmd", "ref": "python -m unittest discover -s tests (other repo, patched)"}]
        self.write()
        self.refused("cmd ref is not a valid shell command")

    def test_evidence_fields_and_scope_type(self):
        self.claims[0]["evidence"][0]["note"] = "x"
        self.claims[0]["scope"] = 3
        self.write()
        self.refused("evidence item has fields outside the claim schema", "scope must be a string")

    def test_own_repo_url_variants(self):
        for url in ("https://github.com/D-J-Lee/agent-ops-kit/blob/main/README.md", "https://github.com/d-j-lee/agent-ops-kit/pull/4",
                    "https://raw.githubusercontent.com/d-j-lee/agent-ops-kit/main/README.md"):
            self.body(f'<a href="{url}">x</a>')
            self.refused("a link to this repository is typed as a URL")

    def test_allow_marker_does_not_exempt_pages(self):
        self.body("claude.ai/" + "public/artifacts/" + "abcdef12-3456-7890 <!-- secret-scan: allow -->")
        self.refused("public-repo rule")

    def test_git_tracked_links(self):
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        (self.root / "notes.md").write_text("untracked\n", encoding="utf-8")
        self.body("{{doc:notes.md}}")
        self.refused("doc link to a file git does not track")
        self.body("")
        self.claims.append({"id": "C2", "claim": "기록.", "grade": "기록됨", "evidence": [{"kind": "file", "ref": "README.md@0000000"}]})
        self.figures["f2"] = {"text": "2", "claim": "C2"}
        self.body("{{fig:f2}}")
        self.refused("file ref not in git at 0000000")

    def test_check_writes_no_bytecode(self):
        cache = ROOT / "scripts" / "__pycache__"
        for old in cache.glob("secret_scan*.pyc") if cache.is_dir() else []:
            old.unlink()
        self.assertEqual(run_build("--root", self.root, "--check")[0], 0)
        self.assertEqual(list(cache.glob("secret_scan*.pyc")) if cache.is_dir() else [], [])

    def test_measured_quantity_outside_registry(self):
        self.body("<p>훅이 -46% 빨라졌고 2.2초 걸렸다</p>")
        self.refused("2 measured quantity(ies) outside the registry: -46%, 2.2초")

    def test_measured_quantity_with_particles_and_other_units(self):
        self.body("<p>30%가 줄었고 190ms에서 101ms로, 약3배, 9.5× 빨라졌다. 1~3분기 자료.</p>"
                  "<p>53분에서 3시간으로, 2.5초, 40µs, 7밀리초, 1.2s, 16KB·3MB·2GB였다.</p>")
        err = self.refused("measured quantity(ies) outside the registry")
        for q in ("30%", "190ms", "101ms", "3배", "9.5×", "53분", "3시간", "2.5초", "40µs", "7밀리초", "1.2s",
                  "16KB", "3MB", "2GB"):
            self.assertIn(q, err)
        self.assertIn(": 14 measured quantity", err, "1~3분기 is a quarter, not a duration")

    def test_context_and_code_numbers_are_exempt(self):
        self.body('<p><span class="ctx">2 vCPU, 4,334줄, 16KB</span> <code>--timeout 30s</code> 1~3분기</p>'
                  '<pre>12 ms</pre>')
        code, _, err = self.build()
        self.assertEqual(code, 0, err)

    def test_own_repo_url_must_use_doc(self):
        self.body('<a href="https://github.com/d-j-lee/agent-ops-kit/blob/main/README.md">x</a>')
        self.refused("a link to this repository is typed as a URL")
        self.body('<a href="{{docurl:README.md}}">x</a>')
        self.ok()

    def test_missing_doc(self):
        self.body("{{doc:docs/none.md}} {{docurl:docs/none2.md}}")
        self.refused("missing file docs/none.md", "missing file docs/none2.md")

    def test_doc_outside_repo(self):
        self.body("{{doc:../../../../../../etc/hostname}}")
        self.refused("doc link outside the repository")

    def test_log_excerpt_must_be_verbatim(self):
        self.body('<pre data-log="logs/run.txt">verdict: identical\nbase: exit 0\ncand: exit 0</pre>')
        self.ok("a verbatim excerpt builds")
        self.body('<pre data-log="logs/run.txt">verdict: identical\ncand: exit 0</pre>')
        self.refused("is not verbatim")  # a line was dropped without a … marker
        self.body('<pre data-log="logs/run.txt">verdict: identical\n…\ncand: exit 0\n<span class="dim"># note</span></pre>')
        self.ok("an elision shown as … and annotations are fine")
        self.body('<pre data-log="logs/run.txt">verdict: differs</pre>')
        self.refused("is not verbatim")
        self.body('<pre data-log="logs/none.txt">x</pre>')
        self.refused("data-log points at a missing file")

    def test_public_repo_rule(self):
        # assembled at runtime so this file stays clean under secret_scan.py (same convention as test_scripts.py)
        for bad, rule in (("claude.ai/" + "artifact/" + "abcdef1234567890", "claude_artifact_url"),
                          ("claude.ai/" + "public/artifacts/" + "abcdef12-3456-7890", "claude_artifact_url"),
                          ("session_" + "01" + "ABCDEFGHIJKLMNOPQRSTUV", "claude_session_id"),
                          ("trig_" + "01" + "ABCDEFGHIJKLMNOPQR", "claude_trigger_id"),
                          ("3f2a9c1e-77aa-4b1c-9d0e-" + "1a2b3c4d5e6f", "uuid")):
            self.body(bad)
            self.refused("public-repo rule", rule)

    def test_public_repo_rule_covers_supporting_pages(self):
        self.pages["other.html"] = OTHER.replace("BODY", "trig_" + "01" + "ABCDEFGHIJKLMNOPQR")
        self.write()
        self.refused("other.html: contains a claude_trigger_id")

    def test_index_required(self):
        self.pages = {"other.html": OTHER.replace("BODY", "{{fig:f1}} {{doc:README.md}}")}
        self.write()
        self.refused("site/pages/index.html is required")

    def test_metadata_required(self):
        self.pages["index.html"] = "<main>{{fig:f1}}</main>\n"
        self.write()
        self.refused("missing <!--page")

    def test_out_dir_not_made_by_the_build_is_kept(self):
        keep = self.root / "out"
        keep.mkdir()
        (keep / "precious.txt").write_text("x", encoding="utf-8")
        code, _, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn("was not made by this build", err)
        self.assertTrue((keep / "precious.txt").exists())
        keep_file = keep / "precious.txt"
        keep_file.unlink()
        keep.rmdir()
        self.assertEqual(self.build()[0], 0)
        self.assertEqual(self.build()[0], 0, "a directory the build made is replaced")
        self.assertEqual(run_build("--root", self.root, "--out", self.root)[0], 2)
        link = self.root / "linked-out"
        link.symlink_to(self.root / "out")
        code, _, err = run_build("--root", self.root, "--out", link)
        self.assertEqual(code, 2)
        self.assertIn("must not be a symbolic link", err)  # refused up front, not by rmtree failing later
        self.assertTrue((self.root / "out" / "index.html").exists())

    def test_usage_errors(self):
        self.assertEqual(run_build("--root", "/no/such/dir")[0], 2)
        self.assertEqual(run_build("--ref", "bad ref;x")[0], 2)


if __name__ == "__main__":
    unittest.main()
