#!/usr/bin/env python3
"""Build the AOK reading hub (D27) from site/ into site/dist/.

Sources
  site/pages/*.html     page fragments. The first block is a metadata comment:
                          <!--page
                          title: 검증
                          nav: 검증
                          order: 2
                          description: one sentence
                          -->
                        `index.html` is the page published as the artifact; the
                        others are published next to it as supporting files.
  site/assets/*         shared CSS/JS, copied as is.
  site/claims.json      claim registry: a list of claims in the AOK claim
                        format (schemas/claim.schema.json).
  site/figures.json     {"<fig id>": {"text": "190→101ms", "claim": "<claim id>"}}

Placeholders in page fragments
  {{fig:ID}}     the figure's text, linked to its claim in the page's evidence table
  {{claim:ID}}   the claim sentence itself, linked the same way
  {{grade:ID}}   the claim's grade badge, linked the same way
  {{doc:PATH}}   a link to a repository file on GitHub (PATH must exist in the repo)
  {{docurl:PATH}} only the URL of that link, for href attributes

Every page gets the site navigation, and a generated "이 페이지의 수치와 근거"
table listing each claim the page used: grade, sentence, scope and evidence.

The figure rule (D28). Measured quantities on hub pages (numbers with %, 배,
ms/s/초/분/시간, KB/MB/GB) come from the registry. The build rejects such a
number written straight into page text. Exempt are <pre> and <code> blocks,
and <span class="ctx">…</span> for an explicitly marked context number (a size
or setting, not a result). Counts (건, 개, 회) cannot be told apart from
context mechanically and stay the author's responsibility.

Checks (any failure exits 1 and writes nothing)
  - unknown figure, claim or placeholder kind; malformed placeholders
    (anything left looking like {{ after substitution); malformed metadata
  - a figure pointing at a missing claim; a claim with an unknown grade or
    fields outside the claim schema; duplicate claim ids
  - evidence: kind cmd|event|file|url with a ref; a file ref must exist (the
    part before @sha); a url must be https; a cmd must be runnable as written
    (no <placeholder>; name inputs as $VARS defined in the procedure it cites)
  - a [확인] claim needs a cmd or url item: something to re-run or re-query
    (§2.1 rule 1). A file pointer alone is a record, not a re-run
  - figures or claims no page uses (the registry stays exactly as large as the hub)
  - a [확인] badge typed into a page (outside the grade legend): it has to come
    from the registry, {{grade:ID}} or {{claim:ID}}, so it carries evidence
  - a measured quantity outside the registry (the figure rule above)
  - a GitHub link to this repository typed as a URL instead of {{doc:}}/{{docurl:}}
  - {{doc:}} paths that do not exist or point outside the repository
  - <pre data-log="PATH"> excerpts: every line must appear in PATH verbatim
    and in order; lines may be left out only where the excerpt shows a line
    "…". Lines inside <span class="dim"> are annotations and are skipped
  - published text that scripts/secret_scan.py flags (artifact URLs,
    session/trigger ids, emails, tokens), or any UUID (public-repo rule; the
    hub's own URL never goes into the repo)

Usage:
  build_site.py [--root REPO] [--out DIR] [--ref BRANCH] [--check]
    --ref    branch or tag used for GitHub links (default main)
    --check  validate only, write nothing
  An existing --out directory is replaced only if an earlier build made it
  (it holds the marker file .aok-site-build).

Exit codes: 0 ok, 1 check failed, 2 usage error. Stdlib only.
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

GRADES = ["확인", "도출", "추론", "미확인", "기록됨", "문서"]
GRADE_CLASS = {"확인": "g-ok", "도출": "g-der", "추론": "g-inf", "미확인": "g-unk", "기록됨": "g-rec", "문서": "g-doc"}
REPO_URL = "https://github.com/d-j-lee/agent-ops-kit"
MARKER = ".aok-site-build"
PLACEHOLDER = re.compile(r"\{\{(\w+):([^{}]+)\}\}")
META = re.compile(r"\A\s*<!--page\s*\n(.*?)\n-->\s*\n", re.S)
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
OWN_LINK = re.compile(re.escape(REPO_URL.split("//", 1)[1]) + r"/(?:blob|tree)/")
CONFIRMED_BADGE = re.compile(r'<(?:span|a)\b[^>]*\bclass="g g-ok"')
MEASURE = re.compile(r"(?<![\w.])[-+−~]?\d[\d,.]*\s*(?:%p|%|배|ms|초|분|시간|s|KB|MB|GB)(?!\w)")
EXEMPT = [re.compile(p, re.S | re.I) for p in (
    r"<!--.*?-->", r"<pre\b.*?</pre>", r"<code\b.*?</code>", r"<script\b.*?</script>", r"<style\b.*?</style>",
    r'<span class="ctx">.*?</span>', r'<ul class="legend".*?</ul>')]
PRE_LOG = re.compile(r'<pre\b([^>]*)\bdata-log="([^"]+)"([^>]*)>(.*?)</pre>', re.S)
TAG = re.compile(r"<[^>]+>")


class BuildError(Exception):
    pass


def load_secret_scan():
    path = Path(__file__).resolve().parent / "secret_scan.py"
    spec = importlib.util.spec_from_file_location("aok_secret_scan", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def inside(root: Path, rel: str) -> Path | None:
    """root/rel if it stays inside root, else None."""
    p = (root / rel).resolve()
    r = root.resolve()
    return p if p == r or r in p.parents else None


def load_registry(root: Path, site: Path) -> tuple[dict, dict, list[str]]:
    errors: list[str] = []
    try:
        claims_list = json.loads((site / "claims.json").read_text(encoding="utf-8"))
        figures = json.loads((site / "figures.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise BuildError(f"cannot read registry: {e}") from e
    if not isinstance(claims_list, list) or not isinstance(figures, dict):
        raise BuildError("claims.json must be a list and figures.json an object")
    claims: dict[str, dict] = {}
    allowed = {"id", "claim", "grade", "evidence", "scope"}
    for c in claims_list:
        cid = c.get("id") if isinstance(c, dict) else None
        if not cid or not isinstance(c.get("claim"), str) or not c["claim"]:
            errors.append(f"claim without id or sentence: {c!r:.80}")
            continue
        if cid in claims:
            errors.append(f"duplicate claim id {cid}")
        extra = set(c) - allowed
        if extra:
            errors.append(f"claim {cid}: fields outside the claim schema: {sorted(extra)}")
        if c.get("grade") not in GRADES:
            errors.append(f"claim {cid}: grade {c.get('grade')!r} is not one of {GRADES}")
        ev = c.get("evidence", [])
        if not isinstance(ev, list):
            errors.append(f"claim {cid}: evidence must be a list")
            ev = []
        kinds = set()
        for e in ev:
            if not isinstance(e, dict) or e.get("kind") not in ("cmd", "event", "file", "url") \
                    or not isinstance(e.get("ref"), str) or not e["ref"].strip():
                errors.append(f"claim {cid}: evidence item needs kind in cmd|event|file|url and a ref")
                continue
            kinds.add(e["kind"])
            ref = e["ref"]
            if e["kind"] == "file" and not inside(root, ref.split("@", 1)[0]):
                errors.append(f"claim {cid}: file ref outside the repository: {ref}")
            elif e["kind"] == "file" and not (root / ref.split("@", 1)[0]).exists():
                errors.append(f"claim {cid}: file ref to a missing file: {ref}")
            if e["kind"] == "url" and not ref.startswith("https://"):
                errors.append(f"claim {cid}: url ref must start with https://: {ref[:60]}")
            if e["kind"] == "cmd" and re.search(r"<[^<>\s][^<>]*>", ref):
                errors.append(f"claim {cid}: cmd ref has a <placeholder>; name inputs as $VARS from a procedure: {ref[:80]}")
        if c.get("grade") == "확인" and not kinds & {"cmd", "url"}:
            errors.append(f"claim {cid}: [확인] needs a cmd or url evidence item to re-run or re-query "
                          "(claim.schema rule 1; a file pointer alone is a record)")
        claims[cid] = c
    for fid, f in figures.items():
        if not isinstance(f, dict) or not isinstance(f.get("text"), str) or not f.get("text"):
            errors.append(f"figure {fid}: needs a text")
        elif f.get("claim") not in claims:
            errors.append(f"figure {fid}: claim {f.get('claim')!r} not in claims.json")
    return claims, figures, errors


def parse_page(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    m = META.match(raw)
    if not m:
        raise BuildError(f"{path.name}: missing <!--page ... --> metadata block")
    meta = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise BuildError(f"{path.name}: bad metadata line {line!r}")
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    for k in ("title", "nav", "order", "description"):
        if not meta.get(k):
            raise BuildError(f"{path.name}: metadata needs {k}")
    try:
        meta["order"] = int(meta["order"])
    except ValueError as e:
        raise BuildError(f"{path.name}: order must be an integer") from e
    return meta, raw[m.end():]


def lint_source(name: str, body: str) -> list[str]:
    """Checks on the page source before substitution: typed [확인] badges, raw figures, own-repo URLs."""
    errors = []
    text = PLACEHOLDER.sub(" ", body)
    for rx in EXEMPT:
        text = rx.sub(" ", text)
    if CONFIRMED_BADGE.search(text):
        errors.append(f"{name}: a [확인] badge is typed into the page; use {{{{grade:ID}}}} or {{{{claim:ID}}}} "
                      "so it carries evidence")
    plain = html.unescape(TAG.sub(" ", text))
    found = MEASURE.findall(plain)
    if found:
        shown = ", ".join(dict.fromkeys(f.strip() for f in found))
        errors.append(f"{name}: {len(found)} measured quantity(ies) outside the registry: {shown[:200]} "
                      "(use {{fig:ID}}, or <span class=\"ctx\"> for a context number)")
    if OWN_LINK.search(PLACEHOLDER.sub(" ", body)):
        errors.append(f"{name}: a link to this repository is typed as a URL; use {{{{doc:PATH}}}} or {{{{docurl:PATH}}}}")
    return errors


def check_logs(name: str, body: str, root: Path) -> list[str]:
    errors = []
    for m in PRE_LOG.finditer(body):
        rel, content = m.group(2), m.group(4)
        path = inside(root, rel)
        if not path or not path.is_file():
            errors.append(f"{name}: data-log points at a missing file {rel}")
            continue
        log = [ln.rstrip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()]
        content = re.sub(r'<span class="dim">.*?</span>', "", content, flags=re.S)
        lines = [ln.rstrip() for ln in html.unescape(TAG.sub("", content)).splitlines()]
        blocks, cur = [], []
        for ln in lines:
            if ln.strip() == "…":
                blocks.append(cur)
                cur = []
            elif ln.strip():
                cur.append(ln)
        blocks.append(cur)
        pos = 0
        for block in (b for b in blocks if b):
            at = next((i for i in range(pos, len(log) - len(block) + 1) if log[i:i + len(block)] == block), None)
            if at is None:
                errors.append(f"{name}: excerpt from {rel} is not verbatim (or skips lines without a '…' line): "
                              f"{block[0][:60]!r}")
                break
            pos = at + len(block)
    return errors


def render_evidence(cids: list[str], claims: dict, ref: str) -> str:
    if not cids:
        return ""
    rows = []
    for cid in cids:
        c = claims[cid]
        refs = []
        for e in c.get("evidence", []):
            r = html.escape(e["ref"])
            if e["kind"] == "url":
                refs.append(f'<a href="{r}" target="_blank" rel="noopener">{r}</a>')
            elif e["kind"] == "file":
                path, _, sha = e["ref"].partition("@")
                refs.append(f'<a href="{REPO_URL}/blob/{html.escape(sha or ref)}/{html.escape(path)}" '
                            f'target="_blank" rel="noopener"><code>{r}</code></a>')
            else:
                refs.append(f"<code>{r}</code>")
        g = c["grade"]
        rows.append(
            f'<tr id="ev-{html.escape(cid)}"><td><span class="g {GRADE_CLASS.get(g, "g-unk")}">{html.escape(str(g))}</span></td>'
            f"<td>{html.escape(c['claim'])}</td><td>{html.escape(c.get('scope', '—'))}</td>"
            f"<td>{'<br>'.join(refs) or '—'}</td></tr>")
    return ('<section class="evidence" id="evidence"><h2>이 페이지의 수치와 근거</h2>'
            '<p class="note">본문의 밑줄 친 수치와 등급 배지는 아래 주장에서 온다. 등급은 AOK 근거 규약(§2.1)의 여섯 등급이다.</p>'
            '<div class="table-scroll"><table class="ev-table"><thead><tr><th scope="col">등급</th><th scope="col">주장</th>'
            '<th scope="col">범위</th><th scope="col">근거</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></div></section>")


def build(root: Path, out: Path | None, ref: str) -> list[str]:
    site = root / "site"
    if not (site / "pages").is_dir():
        raise BuildError(f"no site/pages under {root}")
    claims, figures, errors = load_registry(root, site)
    scanner = load_secret_scan()
    pages = []
    for p in sorted((site / "pages").glob("*.html")):
        try:
            meta, body = parse_page(p)
        except BuildError as e:
            errors.append(str(e))
            continue
        pages.append((p.name, meta, body))
    if not any(name == "index.html" for name, _, _ in pages):
        errors.append("site/pages/index.html is required (the published page)")
    pages.sort(key=lambda t: t[1]["order"])

    used_figs: set[str] = set()
    used_claims: set[str] = set()
    rendered = {}
    for name, meta, body in pages:
        errors += lint_source(name, body)
        errors += check_logs(name, body, root)
        page_claims: list[str] = []

        def note_claim(cid: str):
            if cid not in page_claims:
                page_claims.append(cid)
            used_claims.add(cid)

        def doc_url(arg: str) -> str:
            if not inside(root, arg):
                errors.append(f"{name}: doc link outside the repository {arg}")
            elif not (root / arg).exists():
                errors.append(f"{name}: doc link to missing file {arg}")
            return f"{REPO_URL}/blob/{ref}/{html.escape(arg)}"

        def repl(m: re.Match) -> str:
            kind, arg = m.group(1), m.group(2).strip()
            if kind == "fig":
                f = figures.get(arg)
                if f is None or f.get("claim") not in claims:
                    errors.append(f"{name}: unknown figure {arg}")
                    return ""
                used_figs.add(arg)
                note_claim(f["claim"])
                c = claims[f["claim"]]
                return (f'<a class="fig" href="#ev-{html.escape(f["claim"])}" '
                        f'title="[{c["grade"]}] {html.escape(c.get("scope", ""))}">{html.escape(f["text"])}</a>')
            if kind in ("claim", "grade"):
                c = claims.get(arg)
                if c is None:
                    errors.append(f"{name}: unknown claim {arg}")
                    return ""
                note_claim(arg)
                if kind == "grade":
                    return (f'<a class="g {GRADE_CLASS.get(c["grade"], "g-unk")}" href="#ev-{html.escape(arg)}" '
                            f'title="{html.escape(c["claim"])}">{html.escape(c["grade"])}</a>')
                return f'<a class="fig" href="#ev-{html.escape(arg)}">{html.escape(c["claim"])}</a>'
            if kind == "doc":
                return f'<a href="{doc_url(arg)}" target="_blank" rel="noopener"><code>{html.escape(arg)}</code></a>'
            if kind == "docurl":
                return doc_url(arg)
            errors.append(f"{name}: unknown placeholder kind {kind!r}")
            return ""

        body_out = PLACEHOLDER.sub(repl, body)
        if "{{" in body_out or "}}" in body_out:
            at = body_out.find("{{") if "{{" in body_out else body_out.find("}}")
            errors.append(f"{name}: malformed placeholder near {body_out[at:at + 40]!r}")
        rendered[name] = (meta, body_out, render_evidence(page_claims, claims, ref))

    for fid in sorted(set(figures) - used_figs):
        errors.append(f"figure {fid} is not used by any page")
    fig_claims = {f.get("claim") for f in figures.values() if isinstance(f, dict)}
    for cid in sorted(set(claims) - used_claims - fig_claims):
        errors.append(f"claim {cid} is not used by any page or figure")

    nav_items = [(name, meta) for name, meta, _ in pages]
    outputs = {}
    for name, (meta, body_out, evidence) in rendered.items():
        nav = "".join(
            '<a href="{}"{}>{}</a>'.format(n, ' aria-current="page"' if n == name else "", html.escape(m["nav"]))
            for n, m in nav_items)
        head = (f'<title>{html.escape(meta["title"])}</title>\n'
                '<meta name="claude:viewport-fit-sides" content="cover">\n'
                f'<meta name="description" content="{html.escape(meta["description"])}">\n'
                '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
                '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
                '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500'
                '&family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap">\n'
                '<link rel="stylesheet" href="assets/site.css">\n')
        page = (head + f'<header class="sitebar"><div class="sitebar-in"><a class="brand" href="index.html">AOK <span>안내서</span></a>'
                f'<nav class="sitenav" aria-label="안내서 페이지">{nav}</nav></div></header>\n'
                f'{body_out}\n{evidence}\n'
                '<footer class="sitefoot"><p>규범은 저장소의 <code>docs/</code>가 정본이다. 이 안내서는 그 설명이며, '
                f'수치는 <code>site/claims.json</code>에서 온다. 원본: <a href="{REPO_URL}/tree/{html.escape(ref)}/site" '
                'target="_blank" rel="noopener">site/</a></p></footer>\n'
                '<script src="assets/site.js"></script>\n')
        if name != "index.html":
            # supporting pages are served without the artifact skeleton, so they carry their own
            page = ('<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
                    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                    + head + '</head>\n<body>\n' + page[len(head):] + '</body>\n</html>\n')
        hits = sorted({rule for _, _, rule, _, _ in scanner.scan_text(page)})
        if UUID.search(page):
            hits.append("uuid")
        for rule in hits:
            errors.append(f"{name}: contains a {rule} (public-repo rule)")
        outputs[name] = page

    if errors:
        return errors
    if out is not None:
        if out.exists():
            if not (out / MARKER).is_file():
                return [f"--out {out} exists and was not made by this build (no {MARKER}); refusing to replace it"]
            shutil.rmtree(out)
        (out / "assets").mkdir(parents=True)
        (out / MARKER).write_text("made by scripts/build_site.py; safe to delete\n", encoding="utf-8")
        for name, page in outputs.items():
            (out / name).write_text(page, encoding="utf-8")
        for a in (site / "assets").iterdir():
            if a.is_file():
                shutil.copy2(a, out / "assets" / a.name)
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the AOK reading hub from site/ into site/dist/.")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent), help="repository root")
    ap.add_argument("--out", help="output directory (default <root>/site/dist)")
    ap.add_argument("--ref", default="main", help="git ref used in GitHub links (default main)")
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: root not found: {root}", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[A-Za-z0-9._/\-]+", args.ref):
        print(f"error: bad --ref {args.ref!r}", file=sys.stderr)
        return 2
    out = None if args.check else Path(args.out) if args.out else root / "site" / "dist"
    if out is not None and (out.resolve() == root.resolve() or out.resolve() in root.resolve().parents):
        print(f"error: --out must not be the repository or one of its parents: {out}", file=sys.stderr)
        return 2
    try:
        errors = build(root, out, args.ref)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print("site: ok" + ("" if out is None else f" → {out}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
