#!/usr/bin/env bash
# work-atlas 훅 지연 패치의 전후 비교를 ⑦ 도구로 다시 잰다. 공개 로그($OUT/hooks.txt)에는 판정·바이트 수·시간만 남는다.
# diffcheck가 차이를 보일 때 찍는 diff 머리(훅 출력 원문)는 $OUT/hooks-raw.txt에만 간다. 그 파일은 레포에 올리지 않는다.
# 원본과 패치는 인스턴스를 공유하지 않는다. 훅이 인스턴스에 세션 상태와 쓰임 기록을 남기므로, 실행할 때마다
# $INST/<이름>을 쪽별 임시 사본(a: 원본, b: 패치, A/A는 a와 b 모두 원본)으로 새로 복사해 쓴다.
#
# 입력(환경 변수). 만드는 법은 ../README.md "work-atlas" 절.
#   AOK      이 저장소(agent-ops-kit) 경로           PY       orjson 3.13.0·pyyaml·jsonschema가 있는 python3
#   WA_BASE  work-atlas 원본(10011b6)                WA_CAND  패치 적용본(work-atlas PR #1, 49e4fbe)
#   INST     인스턴스 루트(아래에 lang/, zk/)          REPO_LANG, REPO_ZK  commons-lang·zookeeper 클론
#   OUT      로그 디렉터리                             N        번갈아 잴 횟수(기본 21)
# 실행: bash reports/2026-10-30-hotspots/work-atlas/measure.sh
set -euo pipefail
: "${AOK:?}" "${PY:?}" "${WA_BASE:?}" "${WA_CAND:?}" "${INST:?}" "${REPO_LANG:?}" "${REPO_ZK:?}" "${OUT:?}"
N="${N:-21}"
mkdir -p "$OUT"
DC=(python3 -I "$AOK/scripts/diffcheck.py")
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
fresh() { # fresh <인스턴스 이름>: 쪽별 사본 a·b를 원본에서 새로 만든다
  rm -rf "$WORK/a" "$WORK/b"; mkdir -p "$WORK/a" "$WORK/b"
  cp -a "$INST/$1" "$WORK/a/$1"; cp -a "$INST/$1" "$WORK/b/$1"
}
hook() { printf '%s -I -c %q %s %s' "$PY" \
  'import sys; sys.path.insert(0, sys.argv[1]); import atlas; assert atlas.__file__.startswith(sys.argv[1]), atlas.__file__; from atlas.hook import main; raise SystemExit(main(["--root", sys.argv[2]]))' \
  "$1" "$2"; }
event() { # event <edit|prompt> <repo> <session id>
  if [[ $1 == edit ]]; then
    local f
    [[ $2 == "$REPO_ZK" ]] && f=zookeeper-server/src/main/java/org/apache/zookeeper/server/quorum/Leader.java \
                           || f=src/main/java/org/apache/commons/lang3/StringUtils.java
    printf '{"hook_event_name":"PostToolUse","session_id":"%s","cwd":"%s","tool_name":"Edit","tool_input":{"file_path":"%s/%s"}}\n' "$3" "$2" "$2" "$f"
  else
    local k; [[ $2 == "$REPO_ZK" ]] && k=ZOOKEEPER-4990 || k=LANG-1700
    printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","cwd":"%s","prompt":"%s 고쳐줘"}\n' "$3" "$2" "$k"
  fi
}
: > "$OUT/hooks.txt"; : > "$OUT/hooks-raw.txt"
log() { tee -a "$OUT/hooks-raw.txt" | { grep -v '^  ' || true; } | tee -a "$OUT/hooks.txt"; }   # diff 머리는 두 칸 들여쓴 줄이다
{
  echo "# $(date -u +%FT%TZ) $(uname -m) $(nproc) vCPU · $("$PY" -c 'import sys,orjson; print("python", sys.version.split()[0], "orjson", orjson.__version__)')"
  echo "# base $(git -C "$WA_BASE" rev-parse --short HEAD) · cand $(git -C "$WA_CAND" rev-parse --short HEAD) · N=$N · 쪽별 인스턴스 사본 · diff 머리는 이 로그에서 뺐다"
} | log

for inst in lang zk; do
  repo=$REPO_LANG; [[ $inst == zk ]] && repo=$REPO_ZK
  for kind in edit prompt; do
    event "$kind" "$repo" "dc-{nonce}" > "$OUT/$inst-$kind.json"
    for run in 1 2; do
      fresh "$inst"
      echo "## $inst $kind run $run: diffcheck --determinism --timing $N" | log
      "${DC[@]}" --base "$(hook "$WA_BASE" "$WORK/a/$inst")" --cand "$(hook "$WA_CAND" "$WORK/b/$inst")" \
        --stdin "$OUT/$inst-$kind.json" --determinism --timing "$N" | log || true
    done
  done
done

fresh zk
echo "## zk edit A/A (base vs base, separate copies): noise floor, diffcheck --timing $N" | log
"${DC[@]}" --base "$(hook "$WA_BASE" "$WORK/a/zk")" --cand "$(hook "$WA_BASE" "$WORK/b/zk")" \
  --stdin "$OUT/zk-edit.json" --timing "$N" | log || true

# 함정: 세션 ID를 고정하면 훅은 그 세션에 이미 알린 내용을 다시 내지 않는다. 예전 측정 스크립트가 그랬다.
fresh zk
echo "## trap: one fixed session id (no {nonce}), separate copies per side" | log
event edit "$REPO_ZK" "trap-$(date +%s)" > "$OUT/zk-edit-fixed.json"
for run in 1 2; do
  echo "### run $run: diffcheck" | log
  "${DC[@]}" --base "$(hook "$WA_BASE" "$WORK/a/zk")" --cand "$(hook "$WA_CAND" "$WORK/b/zk")" \
    --stdin "$OUT/zk-edit-fixed.json" | log || true
done
fresh zk
echo "### run 3: fresh copies, the same fixed id, diffcheck --determinism" | log
"${DC[@]}" --base "$(hook "$WA_BASE" "$WORK/a/zk")" --cand "$(hook "$WA_CAND" "$WORK/b/zk")" \
  --stdin "$OUT/zk-edit-fixed.json" --determinism | log || true
echo "# done $(date -u +%FT%TZ)" | log
