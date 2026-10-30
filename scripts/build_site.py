#!/usr/bin/env python3
"""Build the AOK reading hub (D21) from site/ into site/dist/.

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
  {{fig:ID}}     the figure's text, linked to its claim in the page's evidence
                 table. Numbers on hub pages come only from here (D22).
  {{doc:PATH}}   a link to a repository file on GitHub (PATH must exist).
  {{claim:ID}}   the claim sentence itself, linked like a figure.

Every page gets the site navigation, and a generated "이 페이지의 수치와 근거"
table listing each claim the page used: grade, sentence, scope and evidence.

Checks (any failure exits 1 and writes nothing)
  - unknown figure, claim or placeholder kind; malformed page metadata
  - a figure pointing at a missing claim; a claim with an unknown grade, or a
    [확인] claim without evidence (schemas/claim.schema.json rule 1)
  - duplicate claim ids; figures or claims no page uses (the registry stays
    exactly as large as the hub)
  - {{doc:}} paths that do not exist in the repository
  - published text containing an Artifact URL or a Claude session/trigger id
    (public-repo rule; the hub's own URL never goes into the repo)

Usage:
  build_site.py [--root REPO] [--out DIR] [--ref BRANCH] [--check]
    --ref    branch or tag used for GitHub links (default main)
    --check  validate only, write nothing

Exit codes: 0 ok, 1 check failed, 2 usage error. Stdlib only.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path

GRADES = ["확인", "도출", "추론", "미확인", "기록됨", "문서"]
GRADE_CLASS = {"확인": "g-ok", "도출": "g-der", "추론": "g-inf", "미확인": "g-unk", "기록됨": "g-rec", "문서": "g-doc"}
REPO_URL = "https://github.com/d-j-lee/agent-ops-kit"
PLACEHOLDER = re.compile(r"\{\{(\w+):([^}]+)\}\}")
META = re.compile(r"\A\s*<!--page\s*\n(.*?)\n-->\s*\n", re.S)
FORBIDDEN = [
    ("artifact url", re.compile(r"claude\.ai/(?:code/)?artifact/[0-9A-Za-z][0-9A-Za-z\-]{7,}")),
    ("session id", re.compile(r"\bsession_0[0-9A-Za-z]{20,}\b")),
    ("trigger id", re.compile(r"\btrig_[0-9A-Za-z]{16,}\b")),
]


class BuildError(Exception):
    pass


def load_registry(site: Path) -> tuple[dict, dict, list[str]]:
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
        if c.get("grade") == "확인" and not ev:
            errors.append(f"claim {cid}: [확인] needs at least one evidence item (claim.schema rule 1)")
        for e in ev:
            if not isinstance(e, dict) or e.get("kind") not in ("cmd", "event", "file", "url") or not e.get("ref"):
                errors.append(f"claim {cid}: evidence item needs kind in cmd|event|file|url and a ref")
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


def render_evidence(cids: list[str], claims: dict) -> str:
    if not cids:
        return ""
    rows = []
    for cid in cids:
        c = claims[cid]
        refs = []
        for e in c.get("evidence", []):
            ref = html.escape(e["ref"])
            if e["kind"] == "url":
                refs.append(f'<a href="{ref}" target="_blank" rel="noopener">{ref}</a>')
            elif e["kind"] == "file" and not e["ref"].startswith(("http://", "https://")):
                path = e["ref"].split("@", 1)[0]
                refs.append(f'<a href="{REPO_URL}/blob/{{ref}}/{html.escape(path)}" target="_blank" rel="noopener"><code>{ref}</code></a>')
            else:
                refs.append(f"<code>{ref}</code>")
        g = c["grade"]
        rows.append(
            f'<tr id="ev-{html.escape(cid)}"><td><span class="g {GRADE_CLASS.get(g, "g-unk")}">{html.escape(str(g))}</span></td>'
            f"<td>{html.escape(c['claim'])}</td><td>{html.escape(c.get('scope', '—'))}</td>"
            f"<td>{'<br>'.join(refs) or '—'}</td></tr>")
    return ('<section class="evidence" id="evidence"><h2>이 페이지의 수치와 근거</h2>'
            '<p class="note">본문의 밑줄 친 수치는 아래 주장에서 온다. 등급은 AOK 근거 규약(§2.1)의 여섯 등급이다.</p>'
            '<div class="table-scroll"><table class="ev-table"><thead><tr><th scope="col">등급</th><th scope="col">주장</th>'
            '<th scope="col">범위</th><th scope="col">근거</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></div></section>")


def build(root: Path, out: Path | None, ref: str) -> list[str]:
    site = root / "site"
    if not (site / "pages").is_dir():
        raise BuildError(f"no site/pages under {root}")
    claims, figures, errors = load_registry(site)
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
        page_claims: list[str] = []

        def note_claim(cid: str):
            if cid not in page_claims:
                page_claims.append(cid)
            used_claims.add(cid)

        def repl(m: re.Match) -> str:
            kind, arg = m.group(1), m.group(2).strip()
            if kind == "fig":
                f = figures.get(arg)
                if f is None or f.get("claim") not in claims:
                    errors.append(f"{name}: unknown figure {arg}")
                    return m.group(0)
                used_figs.add(arg)
                note_claim(f["claim"])
                c = claims[f["claim"]]
                return (f'<a class="fig" href="#ev-{html.escape(f["claim"])}" '
                        f'title="[{c["grade"]}] {html.escape(c.get("scope", ""))}">{html.escape(f["text"])}</a>')
            if kind == "claim":
                c = claims.get(arg)
                if c is None:
                    errors.append(f"{name}: unknown claim {arg}")
                    return m.group(0)
                note_claim(arg)
                return f'<a class="fig" href="#ev-{html.escape(arg)}">{html.escape(c["claim"])}</a>'
            if kind == "doc":
                if not (root / arg).exists():
                    errors.append(f"{name}: doc link to missing file {arg}")
                return f'<a href="{REPO_URL}/blob/{ref}/{html.escape(arg)}" target="_blank" rel="noopener"><code>{html.escape(arg)}</code></a>'
            errors.append(f"{name}: unknown placeholder kind {kind!r}")
            return m.group(0)

        body_out = PLACEHOLDER.sub(repl, body)
        evidence = render_evidence(page_claims, claims).replace("{ref}", html.escape(ref))
        rendered[name] = (meta, body_out, evidence)

    for fid in sorted(set(figures) - used_figs):
        errors.append(f"figure {fid} is not used by any page")
    fig_claims = {f.get("claim") for f in figures.values()}
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
        for label, rx in FORBIDDEN:
            if rx.search(page):
                errors.append(f"{name}: contains a {label} (public-repo rule)")
        outputs[name] = page

    if errors:
        return errors
    if out is not None:
        if out.exists():
            shutil.rmtree(out)
        (out / "assets").mkdir(parents=True)
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
