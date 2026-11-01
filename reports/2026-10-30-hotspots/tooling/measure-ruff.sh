#!/usr/bin/env bash
# ruff를 work-atlas 원본에 돌려 버그 계열 지적 수와 시간을 남긴다. uvx 기동을 포함한 시간과 ruff 실행 파일만의 시간을 따로 잰다.
#   WA_BASE  work-atlas 원본(10011b6)    OUT  로그 디렉터리    N  반복 횟수(기본 21)
# 실행: bash reports/2026-10-30-hotspots/tooling/measure-ruff.sh   # → $OUT/ruff.txt
set -euo pipefail
: "${WA_BASE:?}" "${OUT:?}"
V=0.17.3 SEL=F,B,E9,PLE N="${N:-21}"
mkdir -p "$OUT"
cd "$WA_BASE"
# uvx가 받아 둔 같은 판의 실행 파일. 설치된 다른 판(PATH의 ruff)을 쓰지 않는다
RUFF=$(uvx --from "ruff@$V" python -c 'from ruff.__main__ import find_ruff_bin; print(find_ruff_bin())' 2>/dev/null)
[[ $("$RUFF" --version) == "ruff $V" ]] || { echo "not ruff $V: $RUFF" >&2; exit 2; }
ms() { # ms <명령…>: 밀리초. ruff는 지적이 있으면 1로 끝나므로 1까지는 정상이다
  local s rc=0
  s=$(date +%s%N); "$@" > /dev/null 2>&1 || rc=$?
  (( rc <= 1 )) || { echo "FAILED (exit $rc): $*" >&2; exit 1; }
  echo $(( ($(date +%s%N) - s) / 1000000 ))
}
lines=$(find atlas tests -name '*.py' -print0 | xargs -0 cat | wc -l)
{
  echo "# $(date -u +%FT%TZ) $(nproc) vCPU · work-atlas@$(git rev-parse --short HEAD) · $lines lines in atlas/ tests/ · ruff $V"
  echo "## uvx ruff@$V check --no-cache --select $SEL --statistics atlas tests"
  rc=0; uvx "ruff@$V" check --no-cache --select "$SEL" --statistics atlas tests 2>/dev/null || rc=$?
  (( rc <= 1 )) || { echo "FAILED (exit $rc)"; exit 1; }
  echo "## the same, wall time of $N runs (ms)"
  for _ in $(seq 1 "$N"); do t=$(ms uvx "ruff@$V" check --no-cache --select "$SEL" --statistics atlas tests); printf '%s ' "$t"; done; echo
  echo "## ruff binary directly (no uvx start-up), $N runs (ms): \$RUFF check --no-cache --select $SEL --statistics atlas tests"
  for _ in $(seq 1 "$N"); do t=$(ms "$RUFF" check --no-cache --select "$SEL" --statistics atlas tests); printf '%s ' "$t"; done; echo
} | tee "$OUT/ruff.txt"
