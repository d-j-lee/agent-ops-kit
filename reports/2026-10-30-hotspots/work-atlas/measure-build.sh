#!/usr/bin/env bash
# work-atlas 전체 빌드: 원본·패치를 같은 --as-of로 번갈아 지어 시간과 모델 파일 동일성을 보고, --as-of 없이 두 번 지어 비결정성을 본다.
# 입력은 measure.sh와 같다(PY, WA_BASE, WA_CAND, INST, OUT). 인스턴스 사본을 $OUT/build/ 아래에 만든다(원본 인스턴스는 바꾸지 않는다).
# 실행: bash reports/2026-10-30-hotspots/work-atlas/measure-build.sh
set -euo pipefail
: "${PY:?}" "${WA_BASE:?}" "${WA_CAND:?}" "${INST:?}" "${OUT:?}"
ASOF=2026-10-29T00:00:00+00:00
W="$OUT/build"; rm -rf "$W"; mkdir -p "$W"
log() { tee -a "$OUT/build.txt"; }
atlas() { local code=$1; shift; "$PY" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from atlas.__main__ import main; raise SystemExit(main(sys.argv[2:]))' "$code" "$@"; }
# a failed build must show up as FAIL, not as a fast time
secs() { local s; s=$(date +%s%N); if "$@" > "$W/last.log" 2>&1; then awk -v d=$(( $(date +%s%N) - s )) 'BEGIN{printf "%.1f", d/1e9}'; else echo "FAIL($?)"; fi; }
echo "# $(date -u +%FT%TZ) $(nproc) vCPU · base $(git -C "$WA_BASE" rev-parse --short HEAD) · cand $(git -C "$WA_CAND" rev-parse --short HEAD) · zk, --as-of $ASOF --no-llm" | log
for side in base cand; do cp -r "$INST/zk" "$W/zk-$side"; done
for i in 1 2 3; do
  tb=$(secs atlas "$WA_BASE" build --root "$W/zk-base" --as-of "$ASOF" --no-llm)
  tc=$(secs atlas "$WA_CAND" build --root "$W/zk-cand" --as-of "$ASOF" --no-llm)
  echo "build $i: base ${tb}s · cand ${tc}s" | log
done
same=0 diff=0
for f in categories.json claims.jsonl generation.json paths.json text_index.json; do
  if cmp -s "$W/zk-base/data/model/$f" "$W/zk-cand/data/model/$f"; then same=$((same+1)); else diff=$((diff+1)); echo "model $f differs" | log; fi
done
echo "model files byte-identical: $same of $((same+diff)) (build.json and catalog.json carry paths and build times by design)" | log

echo "## trap: the same build twice without --as-of" | log
cp -r "$INST/zk" "$W/zk-noasof"
atlas "$WA_BASE" build --root "$W/zk-noasof" --no-llm > "$W/last.log" 2>&1; cp "$W/zk-noasof/data/model/categories.json" "$W/categories-1.json"
sleep 2
atlas "$WA_BASE" build --root "$W/zk-noasof" --no-llm > "$W/last.log" 2>&1; cp "$W/zk-noasof/data/model/categories.json" "$W/categories-2.json"
"$PY" -I - "$W/categories-1.json" "$W/categories-2.json" <<'PY' | log
import json, sys
a, b = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
def walk(x, y, path=""):
    if type(x) is not type(y):
        yield path; return
    if isinstance(x, dict):
        for k in sorted(set(x) | set(y)):
            if k not in x or k not in y: yield f"{path}.{k}"
            else: yield from walk(x[k], y[k], f"{path}.{k}")
    elif isinstance(x, list):
        if len(x) != len(y): yield path
        else:
            for i, (u, v) in enumerate(zip(x, y)): yield from walk(u, v, f"{path}[]")
    elif x != y:
        yield path
keys = sorted({p.rsplit(".", 1)[-1] for p in walk(a, b)})
print("categories.json differs in fields:", ", ".join(keys) if keys else "(none)")
PY
echo "# done $(date -u +%FT%TZ)" | log
