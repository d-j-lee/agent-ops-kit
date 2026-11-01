# 재현 자료 — 공개 레포 핫스팟 (2026-10-29~11-01)

[`../2026-10-30-hotspots.md`](../2026-10-30-hotspots.md)의 수치를 다시 재는 자료다. 키트 기능이 아니라 이력의 일부다. 새 비교는 이 폴더의 측정 스크립트보다 `scripts/diffcheck.py`(⑦)를 쓴다.

| 경로 | 무엇 |
|---|---|
| `logs/` | 2026-11-01에 아래 절차로 다시 잰 원자료. 허브 수치 레지스트리(`site/claims.json`)의 [확인]이 가리키는 곳이다 |
| `work-atlas/` | 훅 지연 패치(`hook-latency.patch`, 같은 내용이 work-atlas PR #1) · `measure.sh`(훅, ⑦ 도구로) · `measure-build.sh`(전체 빌드) · 처음 쓴 측정 스크립트 `bench-hooks.sh`(이력) |
| `atlas-sim/` | work-atlas 유사도 질의의 Rust 이식과 Python 기준(`py_sim.py`), 비교 스크립트(`bench-sim.sh`), 저장한 질의(`queries/`) |
| `docbench-diff/` | docbench `lineDiff`의 Rust 이식(네이티브 + WASM)과 jsdiff 비교 벤치 |
| `tooling/` | ruff · oxlint · uv 도입 설정과 측정, `measure-ruff.sh` |

## 공통 준비

아래 변수 이름을 각 절의 명령과 허브 레지스트리의 근거 명령이 그대로 쓴다.

```bash
git clone https://github.com/d-j-lee/agent-ops-kit.git                                              # 이 저장소($AOK)
git clone https://github.com/apache/commons-lang.git && git -C commons-lang checkout --detach 5ebe419f3   # 수집 때의 판
git clone https://github.com/apache/zookeeper.git && git -C zookeeper checkout --detach 858c8d770
git clone https://github.com/d-j-lee/work-atlas.git wa && git -C wa fetch origin pull/1/head:pr1
git -C wa worktree add --detach ../wa-base 10011b6 && git -C wa worktree add --detach ../wa-cand 49e4fbe
git clone https://github.com/d-j-lee/docbench.git && git -C docbench checkout --detach 881dd6f       # docbench-diff 입력
git clone https://github.com/d-j-lee/dj-assis.git && git -C dj-assis checkout --detach 9acecc9       # docbench-diff 입력
python3 -m venv venv && venv/bin/pip install orjson==3.13.0 pyyaml jsonschema
export SRC=$PWD AOK=$PWD/agent-ops-kit PY=$PWD/venv/bin/python3 WA_BASE=$PWD/wa-base WA_CAND=$PWD/wa-cand \
       INST=$PWD/inst REPO_LANG=$PWD/commons-lang REPO_ZK=$PWD/zookeeper OUT=$PWD/logs
# 인스턴스 inst/lang, inst/zk 의 atlas.toml 은 work-atlas EVALUATION.md "재현" 절의 형식 그대로(저장소 경로는 위 클론).
# collect 는 Apache Jira 전체 이력을 받는다. 2026-10-29 수집본이라 다시 받으면 이슈 수가 달라질 수 있다
for i in lang zk; do $PY -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from atlas.__main__ import main; raise SystemExit(main(sys.argv[2:]))' \
  $WA_BASE collect all --root $INST/$i; done
for i in lang zk; do $PY -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from atlas.__main__ import main; raise SystemExit(main(sys.argv[2:]))' \
  $WA_BASE build --root $INST/$i --as-of 2026-10-29T00:00:00+00:00 --no-llm; done   # 기준 시각을 고정해야 바이트 비교가 된다(같은 atlas.toml끼리: 모델의 generation 값이 설정을 해시한다)
```

## work-atlas

```bash
bash $AOK/reports/2026-10-30-hotspots/work-atlas/measure.sh         # → $OUT/hooks.txt
bash $AOK/reports/2026-10-30-hotspots/work-atlas/measure-build.sh   # → $OUT/build.txt
```

- `measure.sh`는 훅 4종(편집·프롬프트 × LANG·ZK)을 `diffcheck --determinism --timing 21`로 두 번씩 재고, 기준끼리의 A/A 한 번, 같은 세션 ID를 다시 쓰는 함정 세 번을 남긴다. 이벤트의 `session_id`는 `dc-{nonce}`라 실행마다 새 세션이다.
- 훅은 인스턴스에 세션 상태와 쓰임 기록을 남긴다. 그래서 비교마다 인스턴스를 쪽별 임시 사본으로 새로 복사해, 원본과 패치(A/A에서는 두 원본)가 상태를 나누지 않게 한다.
- 공개 로그에는 판정·바이트 수·시간만 남는다. diff 머리(훅 출력 원문)는 `$OUT/hooks-raw.txt`에만 쓰고 레포에 올리지 않는다.
- 훅 명령은 `$PY -I -c "<atlas.hook main>" <코드 경로> <인스턴스>` 꼴이고, 시작할 때 import한 `atlas`가 그 코드 경로의 것인지 단언한다(설치된 다른 판을 조용히 쓰지 않게).
- `measure-build.sh`는 인스턴스 사본 두 개를 원본·패치로 같은 `--as-of`에 번갈아 3회 지어 시간과 `data/model/` 파일 5개의 바이트 동일성을 보고, `--as-of` 없이 두 번 지어 `categories.json`이 어느 필드에서 달라지는지 본다. `build.json`(경로·시간)과 `catalog.json`(`built_at`)은 원래 실행마다 다르다. 실패한 빌드는 시간 대신 `FAIL`로 찍힌다.

## atlas-sim

```bash
cd $AOK/reports/2026-10-30-hotspots/atlas-sim && cargo build --release
{ echo "# $(date -u +%FT%TZ) $(nproc) vCPU · WORK_ATLAS=work-atlas@$(git -C $WA_CAND rev-parse --short HEAD) · $($PY -c 'import sys,orjson; print("python", sys.version.split()[0], "orjson", orjson.__version__)') · atlas-sim release build · load1 $(cut -d' ' -f1 /proc/loadavg)"
  for i in lang zk; do echo "## $i: bench-sim.sh \$INST/$i/data/model/text_index.json - queries/$i.txt 11"
    WORK_ATLAS=$WA_CAND ./bench-sim.sh $INST/$i/data/model/text_index.json - queries/$i.txt 11; done; } | tee $OUT/sim.txt
```

첫 줄은 Python(표준 json · orjson)과 Rust의 상위 200개가 같은지, 다음 두 줄은 프로세스 전체와 프로세스 안(색인 읽기 + postings + 질의) 시간의 중앙값이다. 두 시간의 차이가 Python 기동·import 몫이다. 점수는 Python `round(x, 4)`와 같은 방식(정확한 10진 반올림)으로, JSON 실수는 serde_json `float_roundtrip`으로 읽는다. `WORK_ATLAS`가 없거나 틀리면 설치된 다른 판을 쓰지 않고 멈춘다. 10-30의 합성 다국어 질의 13종과 반올림 경계 값 색인은 남기지 않았다.

## docbench-diff

```bash
```bash
cd $AOK/reports/2026-10-30-hotspots/docbench-diff && npm init -y && npm i diff@9.0.0
(export LC_ALL=C; cat $WA_BASE/*.md $SRC/docbench/*.md $SRC/docbench/docs/*.md $SRC/dj-assis/*.md > base_all.md)   # 공개 레포 3곳, 4,334줄. 글롭은 C 로캘 순서로
sha256sum base_all.md   # acf740366394… 이어야 로그와 같은 입력이다
cargo build --release --lib --target wasm32-unknown-unknown
W=target/wasm32-unknown-unknown/release/docdiff.wasm
{ echo "# $(date -u +%FT%TZ) $(nproc) vCPU · node $(node --version) · diff@9.0.0 · base_all.md $(wc -l < base_all.md) newlines · sha256 $(sha256sum base_all.md | cut -c1-12)"
  echo "## node mkcases.mjs && node bench.mjs 1000 2000 4334"; node mkcases.mjs && node bench.mjs 1000 2000 4334   # jsdiff (docbench lineDiff 그대로)
  echo "## node bench-wasm.mjs docdiff.wasm 1000 2000 4334 (wasm $(wc -c < $W) bytes)"; node bench-wasm.mjs $W 1000 2000 4334
  echo "## node caps.mjs"; node caps.mjs; } | tee $OUT/docbench.txt                                            # jsdiff timeout·maxEditLength
cargo run --release --bin docdiff-bench -- . 1000 2000 4334  # Rust 네이티브 + 원문 복원 검증(로그에는 남기지 않았다)
node split.mjs 4334                                          # 시간 구성(로그에는 남기지 않았다)
```

"heavy" 시나리오는 빈 줄이 아닌 줄의 약 40%를 글자 순서를 뒤집은 글로 바꾼다(`mkcases.mjs`, 시드 고정). 영어 낱말은 거의 겹치지 않고, 한글은 같은 글자 토큰이 순서만 뒤집혀 남는다. `bench-wasm.mjs`가 비교하는 것은 더한·지운 줄 수와 줄 목록(종류·글자)까지다. 낱말 강조 위치는 비교하지 않는다. 11-01 로그의 `base_all.md`는 10-30에 이어 붙인 파일 그대로다(sha256 앞 12자리가 로그 머리에 있다). 11-01에 위 고정 커밋 세 개로 다시 이어 붙여 같은 해시가 나오는 것을 확인했다. 로그 머리 줄은 위 묶음의 `echo`가 쓴다. 가벼운 수정의 jsdiff 시간은 실행마다 두 값(약 19ms, 약 35ms) 사이를 오가 그 배수는 측정마다 크게 다르다(3회차 검증자 재현: WASM/jsdiff 0.96~1.83배).

## tooling

```bash
bash $AOK/reports/2026-10-30-hotspots/tooling/measure-ruff.sh   # → $OUT/ruff.txt
```

`uvx ruff@0.17.3 check --no-cache --select F,B,E9,PLE --statistics atlas tests`를 한 번 돌려 지적 수를 남기고, 같은 명령의 시간(uvx 기동 포함)과 uvx가 받아 둔 같은 판 실행 파일만의 시간을 21회씩 잰다. `PATH`의 다른 판 ruff는 쓰지 않는다.
