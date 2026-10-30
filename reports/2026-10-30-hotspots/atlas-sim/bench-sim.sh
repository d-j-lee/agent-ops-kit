#!/usr/bin/env bash
# atlas-sim 대 Python(work-atlas 의 실제 Index 코드) 비교: 결과 동일성 + 시간.
#   bench-sim.sh <text_index.json> <제외할 wu|-> <질의 파일> [N=9]
# 출력: 상위 200개가 같은지(비어 있으면 그렇다고), 그리고 번갈아 N회 돌린 중앙값 두 가지
#   - 프로세스 전체: 훅처럼 매번 새 프로세스로 띄울 때의 체감 시간(Python 기동·import 포함)
#   - 프로세스 안: 색인 읽기 + postings + 질의만(PyO3 처럼 이미 떠 있는 Python 에 붙일 때 줄어드는 몫)
set -uo pipefail
idx="$1" ex="$2" q="$3" N="${4:-9}"
here="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-python3}" RS="${RS:-$here/target/release/atlas-sim}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

run() { "$@" 2>"$tmp/err" || { { echo "FAILED: $*"; cat "$tmp/err"; } >&2; exit 1; }; }   # 실패를 "결과 없음"으로 오해하지 않게
run "$PY" "$here/py_sim.py" "$idx" "$ex" 200 json   < "$q" > "$tmp/py.json"
run "$PY" "$here/py_sim.py" "$idx" "$ex" 200 orjson < "$q" > "$tmp/py.orjson"
run "$RS" "$idx" "$ex" 200 < "$q" > "$tmp/rs"
if [[ ! -s "$tmp/py.json" ]]; then echo "EMPTY OUTPUT — 질의가 아무것도 찾지 못해 비교가 의미 없다"
elif cmp -s "$tmp/py.json" "$tmp/rs" && cmp -s "$tmp/py.orjson" "$tmp/rs"; then echo "identical ($(wc -l < "$tmp/rs") lines, json·orjson·rust)"
else echo "OUTPUT DIFFERS"; diff "$tmp/py.orjson" "$tmp/rs" | head -6; fi

stage() { awk '{for(i=1;i<=NF;i++){split($i,a,"=");v[a[1]]=a[2]}} END{printf "%.0f", v["load_ms"]+v["postings_ms"]+v["query_ms"]}'; }
med() { printf '%s\n' "$@" | sort -n | awk '{a[NR]=$1} END{print a[int((NR+1)/2)]}'; }
pw=() pi=() rw=() ri=()
for _ in $(seq 1 "$N"); do
  s=$(date +%s%N); e=$("$PY" "$here/py_sim.py" "$idx" "$ex" 200 orjson < "$q" 2>&1 >/dev/null)
  pw+=($(( ($(date +%s%N) - s) / 1000000 ))); pi+=("$(stage <<<"$e")")
  s=$(date +%s%N); e=$("$RS" "$idx" "$ex" 200 < "$q" 2>&1 >/dev/null)
  rw+=($(( ($(date +%s%N) - s) / 1000000 ))); ri+=("$(stage <<<"$e")")
done
printf 'whole process  python(orjson) %4sms · rust %4sms   (N=%s, interleaved, median)\n' "$(med "${pw[@]}")" "$(med "${rw[@]}")" "$N"
printf 'in process     python(orjson) %4sms · rust %4sms   (load + postings + query)\n' "$(med "${pi[@]}")" "$(med "${ri[@]}")"
