# 재현 자료 — 공개 레포 핫스팟 (2026-10-29~31)

[`../2026-10-30-hotspots.md`](../2026-10-30-hotspots.md)의 수치를 다시 재는 자료다. 키트 기능이 아니라 이력의 일부다. 새 비교는 이 폴더의 측정 스크립트보다 `scripts/diffcheck.py`(⑦)를 쓴다.

| 폴더 | 무엇 |
|---|---|
| `work-atlas/` | 훅 지연 패치(`hook-latency.patch`, 같은 내용이 work-atlas PR #1) · 처음 쓴 측정 스크립트(`bench-hooks.sh`, 번갈아 측정 · 새 세션 ID · 빈 출력 검사) |
| `atlas-sim/` | work-atlas 유사도 질의의 Rust 이식과 Python 기준(`py_sim.py`), 비교 스크립트(`bench-sim.sh`) |
| `docbench-diff/` | docbench `lineDiff`의 Rust 이식(네이티브 + WASM)과 jsdiff 비교 벤치 |
| `tooling/` | ruff · oxlint · uv 도입 설정과 측정 |

## work-atlas

```bash
git clone https://github.com/apache/commons-lang.git && git clone https://github.com/apache/zookeeper.git
python -m venv .venv && . .venv/bin/activate && pip install -e <work-atlas> orjson
# inst/zk/atlas.toml 은 work-atlas EVALUATION.md "재현" 절의 형식 그대로
atlas collect all --root inst/zk
atlas build --root inst/zk --as-of 2026-10-29T00:00:00+00:00 --no-llm     # 기준 시각을 고정해야 바이트 비교가 된다
cp -r <work-atlas> /tmp/wa-patched && git -C /tmp/wa-patched apply $PWD/work-atlas/hook-latency.patch
```

⑦ 도구로 비교한다. 이벤트 파일의 `session_id`에 `{nonce}`를 넣으면 실행마다 새 세션이 된다(같은 ID를 다시 쓰면 편집 훅이 아무것도 내지 않는다).

```bash
R=<zookeeper 경로>
printf '{"hook_event_name":"PostToolUse","session_id":"dc-{nonce}","cwd":"%s","tool_name":"Edit","tool_input":{"file_path":"%s/zookeeper-server/src/main/java/org/apache/zookeeper/server/quorum/Leader.java"}}' $R $R > edit.json
hook() { printf '%s -I -c "import sys; sys.path.insert(0, sys.argv[1]); from atlas.hook import main; raise SystemExit(main([\\"--root\\", sys.argv[2]]))" %s inst/zk' .venv/bin/python3 "$1"; }
python3 -I <agent-ops-kit>/scripts/diffcheck.py --base "$(hook <work-atlas>)" --cand "$(hook /tmp/wa-patched)" \
  --stdin edit.json --determinism --timing 11
```

프롬프트 훅은 `{"hook_event_name":"UserPromptSubmit","session_id":"dc-{nonce}","cwd":"<R>","prompt":"ZOOKEEPER-4990 고쳐줘"}`. 빌드 비교는 원본·패치로 인스턴스 사본을 하나씩 같은 `--as-of`로 지은 뒤 `data/model/` 파일을 `cmp`한다. `build.json`(경로·시간)과 `catalog.json`(`built_at`)은 원래 실행마다 다르다.

## atlas-sim

```bash
cd atlas-sim && cargo build --release
WORK_ATLAS=<work-atlas> PY=<venv>/bin/python3 ./bench-sim.sh <inst>/data/model/text_index.json wu:issue:ZOOKEEPER-4990 query.txt 11
```

첫 줄은 Python(표준 json · orjson)과 Rust의 상위 200개가 같은지, 다음 두 줄은 프로세스 전체와 프로세스 안(색인 읽기 + postings + 질의) 시간의 중앙값이다. 두 시간의 차이가 Python 기동·import 몫이다. 점수는 Python `round(x, 4)`와 같은 방식(정확한 10진 반올림)으로, JSON 실수는 serde_json `float_roundtrip`으로 읽는다. `WORK_ATLAS`가 없거나 틀리면 설치된 다른 판을 쓰지 않고 멈춘다.

## docbench-diff

```bash
cd docbench-diff && npm init -y && npm i diff@9.0.0
cat <work-atlas>/*.md <docbench>/*.md <docbench>/docs/*.md <dj-assis>/*.md > base_all.md   # 4,334줄
node mkcases.mjs && node bench.mjs 1000 2000 4334            # jsdiff (docbench lineDiff 그대로)
cargo run --release --bin docdiff-bench -- . 1000 2000 4334  # Rust 네이티브 + 원문 복원 검증
cargo build --release --lib --target wasm32-unknown-unknown
node bench-wasm.mjs target/wasm32-unknown-unknown/release/docdiff.wasm 1000 2000 4334
node split.mjs 4334 && node caps.mjs                          # 시간 구성, jsdiff timeout·maxEditLength
```

"heavy" 시나리오는 빈 줄이 아닌 줄의 약 40%를 글자 순서를 뒤집은 글로 바꾼다(`mkcases.mjs`, 시드 고정). 영어 낱말은 거의 겹치지 않고, 한글은 같은 글자 토큰이 순서만 뒤집혀 남는다. `bench-wasm.mjs`가 비교하는 것은 더한·지운 줄 수와 줄 목록(종류·글자)까지다. 낱말 강조 위치는 비교하지 않는다.
