#!/usr/bin/env bash
# work-atlas 훅 지연 측정: 원본 코드와 패치 코드를 같은 인스턴스·같은 사건으로 번갈아 N회(기본 21)씩 돌려 중앙값을 낸다.
# 측정마다 새 세션 ID 를 쓴다(훅은 세션별로 이미 알려 준 파일을 기억해 두 번째부터 아무것도 출력하지 않는다).
# 마지막에 원본·패치 출력을 비교하고, 출력이 비어 있으면 비교가 무의미하다고 알린다.
#   bench-hooks.sh <원본 work-atlas> <패치한 work-atlas> <인스턴스 루트> <대상 저장소 경로> <파일(저장소 기준)> <이슈 키>
# 예: bench-hooks.sh ~/work-atlas /tmp/wa-patched inst/zk ~/src/zookeeper \
#       zookeeper-server/src/main/java/org/apache/zookeeper/server/quorum/Leader.java ZOOKEEPER-4990
set -uo pipefail
orig="$1" patched="$2" inst="$3" repo="$4" file="$5" key="$6"
PY="${PY:-python3}"
HOOK='import sys; sys.path.insert(0, sys.argv[1]); from atlas.hook import main; raise SystemExit(main(["--root", sys.argv[2]]))'
ev() { # ev <edit|prompt> <세션 id>
  if [[ $1 == edit ]]; then
    printf '{"hook_event_name":"PostToolUse","session_id":"%s","cwd":"%s","tool_name":"Edit","tool_input":{"file_path":"%s/%s"}}' "$2" "$repo" "$repo" "$file"
  else
    printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","cwd":"%s","prompt":"%s 고쳐줘"}' "$2" "$repo" "$key"
  fi
}
N="${N:-21}"
med() { sort -n | awk '{a[NR]=$1} END{print a[int((NR+1)/2)]}'; }
for kind in edit prompt; do
  # 원본과 패치를 번갈아 실행한다 — 시간에 따른 잡음(다른 작업·CPU 클록)이 양쪽에 고르게 들어가게
  to=() tp=()
  for i in $(seq 1 "$N"); do
    for v in o p; do
      [[ $v == o ]] && code="$orig" || code="$patched"
      s=$(date +%s%N)
      ev "$kind" "b$$-$RANDOM-$v$i" | "$PY" -c "$HOOK" "$code" "$inst" >/dev/null
      t=$(( ($(date +%s%N) - s) / 1000000 ))
      [[ $v == o ]] && to+=("$t") || tp+=("$t")
    done
  done
  mo=$(printf '%s\n' "${to[@]}" | med); mp=$(printf '%s\n' "${tp[@]}" | med)
  printf '%-6s orig median %4sms · patched median %4sms · %s%%  (N=%s, interleaved)\n' "$kind" "$mo" "$mp" "$(awk -v o="$mo" -v p="$mp" 'BEGIN{printf "%+.0f", (p-o)*100/o}')" "$N"
  oa=$(ev "$kind" "cmpA-$$-$RANDOM" | "$PY" -c "$HOOK" "$orig" "$inst")
  ob=$(ev "$kind" "cmpB-$$-$RANDOM" | "$PY" -c "$HOOK" "$patched" "$inst")
  if [[ -z "$oa" ]]; then echo "       EMPTY OUTPUT — 비교가 의미 없다(파일·이슈 키가 모델에 있는지 확인)"
  elif [[ "$oa" == *"옛 형식"* ]]; then echo "       STALE MODEL — 모델이 옛 형식이라 두 버전 모두 같은 안내만 낸다(atlas build 후 다시)"
  elif [[ "$oa" == "$ob" ]]; then echo "       output identical ($(printf %s "$oa" | wc -c) bytes)"
  else echo "       OUTPUT DIFFERS"; fi
done
