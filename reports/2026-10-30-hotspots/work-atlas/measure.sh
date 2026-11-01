#!/usr/bin/env bash
# work-atlas 훅 지연 패치의 전후 비교를 ⑦ 도구로 다시 잰다. 로그에는 판정·바이트 수·시간이 남는다. 훅 출력은 diffcheck가
# 차이를 보일 때 diff 머리로만 남는다(입력이 공개 Apache 레포라 공개해도 되는 내용이다. 사내 인스턴스에 쓸 때는 로그를 밖에 두지 않는다).
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
log() { tee -a "$OUT/hooks.txt"; }
{
  echo "# $(date -u +%FT%TZ) $(uname -m) $(nproc) vCPU · $("$PY" -c 'import sys,orjson; print("python", sys.version.split()[0], "orjson", orjson.__version__)')"
  echo "# base $(git -C "$WA_BASE" rev-parse --short HEAD) · cand $(git -C "$WA_CAND" rev-parse --short HEAD) · N=$N"
} | log

for inst in lang zk; do
  repo=$REPO_LANG; [[ $inst == zk ]] && repo=$REPO_ZK
  for kind in edit prompt; do
    event "$kind" "$repo" "dc-{nonce}" > "$OUT/$inst-$kind.json"
    for run in 1 2; do
      echo "## $inst $kind run $run: diffcheck --determinism --timing $N" | log
      "${DC[@]}" --base "$(hook "$WA_BASE" "$INST/$inst")" --cand "$(hook "$WA_CAND" "$INST/$inst")" \
        --stdin "$OUT/$inst-$kind.json" --determinism --timing "$N" | log || true
    done
  done
done

echo "## zk edit A/A (base vs base): noise floor, diffcheck --timing $N" | log
"${DC[@]}" --base "$(hook "$WA_BASE" "$INST/zk")" --cand "$(hook "$WA_BASE" "$INST/zk")" \
  --stdin "$OUT/zk-edit.json" --timing "$N" | log || true

echo "## trap: one fixed session id for both sides (no {nonce})" | log
event edit "$REPO_ZK" "trap-$(date +%s)" > "$OUT/zk-edit-fixed.json"
for run in 1 2; do
  echo "### run $run" | log
  "${DC[@]}" --base "$(hook "$WA_BASE" "$INST/zk")" --cand "$(hook "$WA_CAND" "$INST/zk")" \
    --stdin "$OUT/zk-edit-fixed.json" | log || true
done
echo "# done $(date -u +%FT%TZ)" | log
