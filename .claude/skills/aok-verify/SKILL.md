---
name: aok-verify
description: AOK v1 블라인드 재현 대조(BRD, §3①) 런북. 결론이 틀리면 비싼 주장·분석·설계·PR·지식 카드를 검증한다. 위험 등급 T0(생략)·T1(L1)·T2(L1+L0, 환경 의존 주장이 있으면 +L2)로 수준을 정하고, 작성자가 withheld/(결론·주장 원장·질문 매핑)와 packet/(질문·원자료·재현 절차, 원자료에서 기계적으로 잘라 냄)을 나눈 뒤, python3 scripts/sanitize_packet.py --packet --withheld --canary로 결론 누출을 검사하고 카나리를 심는다. verifier 서브에이전트(렌즈 1–2개 — 주장별 판정·독립 재설계·반례 탐색)를 병렬로 돌리고, 필요하면 repro-l2 원격 자식이 재현 로그만 만들며, 카나리 출력 검사 뒤 별도 adjudicator 서브에이전트가 verdict.json과 diff.md(일치·반박·검증자만 발견·미검증)를 만든다. 반박은 근거 재실행으로만 해소하고 원장 verifications에 기록한다. 중요한 결론을 내보내기 전, 외부 공개·되돌리기 어려운 결정 전, 사용자가 교차검증·블라인드 검증·리뷰를 요청할 때, aok-garden이 카드를 검증할 때 로드한다. sha 비교·테스트 통과 같은 결정적 비교에는 쓰지 않는다.
---

# aok-verify — 블라인드 재현 대조 런북

허브가 따른다. 허브는 **작성자**이고 판정자가 아니다. 판정은 별도의 새 서브에이전트(`adjudicator`)가 하고, 해소되지 않는 반박만 허브가 근거를 다시 실행해 보고 사용자에게 올린다(S7).

계약: `schemas/claim.schema.json`(주장), `schemas/aok1.schema.json`(L2 봉투), `schemas/ledger.schema.json`의 `verifications`. 에이전트: `.claude/agents/verifier.md`, `.claude/agents/adjudicator.md`. 스크립트: `<KIT>/scripts/sanitize_packet.py`.

## 1. 쓸 때 / 안 쓸 때 — 위험 등급

| 등급 | 해당 | 수준 |
|---|---|---|
| T0 | 되돌리기 쉽고 내부용이며 결정적 확인(sha 비교, 테스트, 스크립트)으로 충분 | 생략 |
| T1 | 틀리면 비싼 내부 결론·분석·설계 | L1 (verifier 1–2개, 렌즈 다르게) |
| T2 | 되돌리기 어려움, 외부 공개, 환경에 의존 | L1 + L0. 환경에 달린 주장이 있으면 + L2 |

T0–T2는 실현성 시험 T1–T6과 다른 축이다.

## 2. 전제

- 비용: 서브에이전트 1개 165k–320k 토큰·7–11분. L1+L0+판정자 ≈ 0.5–0.8M 토큰 병렬, 10분 안팎. 서브에이전트는 `create_session` 입장 검사 대상이 아니지만 트리가 `soft`(70%) 이상이면 먼저 사용자에게 알린다.
- L2는 원격 자식이므로 `aok-delegate` 3.3 입장 검사를 거친다.
- 포크로 검증하지 않는다. 작성자(허브)는 판정자를 겸하지 않는다.

## 3. 입력

- 대상 산출물(파일, PR, 보고서, 카드 묶음)과 위험 등급
- 작성자의 결론과 주장 목록, 원자료 위치, 재현 절차(있으면)
- 트리 ID(L2를 쓰면 필수)

## 4. 절차

### 4.1 실행 디렉터리

`run_id` = `v-<YYYYMMDD>-<nn>`. 결론이 들어간 자료는 packet의 상위 디렉터리 **밖**, 별도 트리에 둔다(형제 디렉터리가 아니라 형제 트리). packet 위로 올라가 훑어도 결론·카나리가 보이지 않게 하기 위해서다.

```bash
R=<SP>/brd/<run_id>             # L1·L2 쪽: packet과 그 출력
L0=<SP>/brd-l0/<run_id>         # L0 쪽(T2): 결론 포함 산출물과 그 출력
W=<SP>/brd-withheld/<run_id>    # 작성자·판정자 쪽: 결론, 카나리, 실행 메타, 판정 결과
mkdir -p "$R"/packet/raw "$R"/out "$L0"/packet/raw "$L0"/out "$W"/adjudicator
```

Bash 호출 사이에 셸 변수가 남지 않으므로 아래 블록마다 `R=…`·`L0=…`·`W=…`를 다시 적는다.

| 경로 | 누가 보나 | 내용 |
|---|---|---|
| `$W/` | 작성자, 판정자 | `claims.json`, `conclusion.md`, `qmap.json`, `terms.txt`(선택), 카나리 파일, `run.json`(실행 메타·카나리) |
| `$W/adjudicator/` | 판정자(쓰기), 허브 | `verdict.json`, `diff.md` |
| `$R/packet/` | L1 검증자, L2(재현 절차만) | `question.md`, `questions.md`(선택), `raw/`, `repro.md` |
| `$R/out/<l1-x>/`, `$R/out/l2/` | 각 출력 | — |
| `$L0/packet/` | L0 검증자 | 결론이 포함된 산출물 원본 + `raw/` 사본 |
| `$L0/out/<l0-x>/` | L0 출력 | — |

검증자에게는 자기 packet 경로와 out 경로만 알려 준다. `brd-withheld` 경로는 어떤 검증자에게도, `brd-l0` 경로는 L1 검증자에게 알려 주지 않는다.

### 4.2 `$W` (작성자가 쓴다)

- `claims.json`: `schemas/claim.schema.json` 형식 배열. 산출물의 주장마다 `C<n>`.
  ```json
  [{"id":"C1","claim":"원격 자식 첫 턴 비용은 0.22–0.27 USD였다","grade":"확인",
    "evidence":[{"kind":"event","ref":"session_<id>/<event_uuid>","excerpt":"cost_usd 0.265"}],
    "scope":"2026-10-20, 기본 모델"}]
  ```
- `conclusion.md`: 결론과 권고.
- `qmap.json`: L1 질문 → 주장 매핑, 예 `{"Q1":["C1"],"Q2":["C2","C3"]}`.

### 4.3 `$R/packet/` (L1·L2용, 결론 없음)

- `question.md`: 검증할 질문. 중립적으로. 결론·권고·기대 답을 쓰지 않는다.
- `questions.md`(주장별 판정 렌즈용): 주장마다 답을 뺀 중립 질문 `Q<n>`. 예: "원격 자식 첫 턴 비용은 얼마인가?"(O) / "첫 턴은 0.27 USD로 싸다"(X).
- `raw/`: 원자료를 **기계적으로** 자른다 — `cp`, `sed -n '<a>,<b>p'`, `jq`, `git show <sha>:<path>`. 고쳐 쓰거나 요약하지 않는다. 쓴 명령을 `$W/run.json`의 `packet_recipe`에 남긴다. 원자료 줄 자체에 결론이 있으면 그 줄을 잘라 낸다.
- `repro.md`: 재현 절차 — 실행할 명령과 기록할 관측 항목(예: "출력의 cost_usd 값"). 기대값을 쓰지 않는다.

### 4.4 `$L0/packet/` (T2의 L0용)

결론이 포함된 산출물 원본과 `raw/` 사본. **`$W`에서 복사하지 않는다**(카나리가 섞이지 않게). 이 디렉터리는 정화 검사 대상이 아니다(결론이 있는 게 정상). `$R` 밑에 두지 않는다(L1 검증자의 상위 디렉터리에 결론이 생긴다).

### 4.5 누출 검사와 카나리

```bash
cd <KIT>
python3 -I scripts/sanitize_packet.py --packet "$R/packet" --withheld "$W" --canary [--terms "$W/terms.txt"]
echo "exit=$?"
```

- `--canary`는 `$W/CANARY-<hex>.txt`를 만들고 토큰(`CANARY-<hex>`)을 출력한다. 토큰을 `$W/run.json`의 `canary`에 적는다. 토큰은 검증자·재현기 프롬프트에 절대 넣지 않는다.
- `--terms`: 결론 어휘·권고 절을 한 줄에 하나씩(선택).
- **exit 0** 통과 · **exit 1** 누출: 출력된 `path:line: term`을 packet에서 지우거나 그 줄을 잘라 내고 다시 실행한다. 통과할 때까지 검증자를 돌리지 않는다 · **exit 2** 사용법 오류(경로 없음, 인자 오류): 통과가 아니다. 인자·경로를 고쳐 다시 돌리고, 고칠 수 없으면 멈추고 보고한다(6절).
- L0만 하는 실행(카드 검증 등)은 빈 `$R/packet`으로 카나리만 심는다: `--packet "$R/packet" --withheld "$W" --canary`.

`$W/run.json`:
```json
{"run_id":"…","tier":"T2","levels":["L1","L0","L2"],"tree_id":"…","created_at":"…",
 "target":"<산출물 포인터>","canary":"CANARY-…","packet_recipe":["cp …","sed -n '10,80p' …"],
 "verifiers":[{"id":"l1-a","level":"L1","lens":"redesign"},{"id":"l1-b","level":"L1","lens":"counterexample"},
              {"id":"l0-a","level":"L0","lens":"claim"}]}
```

### 4.6 검증자 실행 (한 메시지에서 병렬)

| 렌즈 | 잘 잡는 것 | 주로 쓰는 수준 |
|---|---|---|
| `claim` 주장별 판정 | 수치 착오, 과장, ref 없는 [확인] | L0(필수), L1에서는 `questions.md`로 |
| `redesign` 독립 재설계 | 누락, 대안 | L1 |
| `counterexample` 반례 탐색 | 경계 조건, 일반화 오류 | L1, L0 |

- T1: L1 1–2개(예: `l1-a` redesign + `l1-b` counterexample). T2: + `l0-a` claim.
- 상관 오류를 줄이려면 렌즈를 다르게 하고, 필요하면 둘째 검증자의 `model`을 바꾼다.

```
Agent(subagent_type="verifier", description="BRD <run_id> l1-a",
      prompt="run_id: <run_id>\nverifier: l1-a\nlevel: L1\nlens: redesign\npacket: <R>/packet\nout: <R>/out/l1-a\n한국어로 요약하라.")
Agent(subagent_type="verifier", description="BRD <run_id> l0-a",
      prompt="run_id: <run_id>\nverifier: l0-a\nlevel: L0\nlens: claim\npacket: <L0>/packet\nout: <L0>/out/l0-a\n한국어로 요약하라.")
```

검증자 프롬프트에는 결론, 기대 답, `brd-withheld` 경로, 카나리 값을 넣지 않는다. L1 프롬프트에는 `brd-l0` 경로도 넣지 않는다. L1 프롬프트끼리는 형식이 같아야 한다.

### 4.7 L2 (T2이고 환경에 달린 주장이 있을 때)

1. `aok-delegate` 3.2–3.9로 띄운다: 역할 `repro-l2`, `title="[<proj>/repro-l2] <run_id>"`, `tags` 규칙, `environment_id`는 최소 환경(D9) 권장, `source_revision=<sha>`.
2. 변수: `RUN_ID`, `OWNER`, `REPO`, `SHA`, `STEP_TIMEOUT_SEC=900`, `TASK_DETAILS="@file:<R>/packet/repro.md"`(정화 통과본 원문). `question.md`·`raw/`는 절차에 꼭 필요할 때만 함께 넣고, withheld 내용은 절대 넣지 않는다.
3. task 봉투 `body.summary`: "packet 재현 절차를 실행해 재현 로그만 남긴다".
4. result가 오면: `list_events(session_id=<sid>, limit=20)`에서 마지막 assistant 메시지의 `AOK-REPRO-LOG BEGIN`/`END` 사이 JSON을 꺼내 `<R>/out/l2/repro_log.json`에 원문 그대로 저장한다(없으면 `has_more`로 더 읽는다). 로그 안의 판정성 문장은 쓰지 않는다(S7).

### 4.8 카나리 출력 검사

있는 출력 경로만 넘긴다(없는 경로를 넘기면 exit 2).

```bash
cd <KIT>
outs=()
for d in "$R"/out/l1-* "$L0"/out/l0-* "$R"/out/l2; do [ -e "$d" ] && outs+=("$d"); done
if [ ${#outs[@]} -eq 0 ]; then echo "NO_OUTPUTS"; else
  python3 -I scripts/sanitize_packet.py --withheld "$W" --check-output "${outs[@]}"; echo "exit=$?"
  grep -rlF -- "<canary>" "${outs[@]}" || true
fi
```

- exit 0 깨끗함 · exit 1 카나리 누출(아래) · **exit 2 사용법 오류(카나리 없음, 경로 없음 등)와 `NO_OUTPUTS`는 통과가 아니다.** 판정자로 넘어가지 말고 멈추고 사용자에게 보고한다(6절).

걸린 출력은 **무효**다. 원인(packet의 결론, 프롬프트, withheld 접근)을 고치고 그 검증자만 다시 돌린다. 무효 실행은 원장에 `canary_leak: true`로 남긴다.

### 4.9 판정자

```
Agent(subagent_type="adjudicator", description="BRD <run_id> adjudicate",
      prompt="run_id: <run_id>\nwithheld: <W>\nverifier_outputs: <R>/out/l1-a <R>/out/l1-b <L0>/out/l0-a\nl2_logs: <R>/out/l2/repro_log.json\ncanary: <canary>\nkit: <KIT>\nout: <W>/adjudicator\n한국어로 요약하라.")
```

`verifier_outputs`·`l2_logs`에는 4.8에서 남은 유효한 경로만 넣는다(L2가 없으면 `l2_logs: none`). 판정자는 매번 새 서브에이전트다. 출력: `<W>/adjudicator/verdict.json`, `diff.md`.

### 4.10 반박 처리

1. `verdict.json`의 `refuted` 항목에서 `resolution`을 본다: `author_upheld`(작성자 유지) · `author_wrong`(확정 오류) · `escalate`.
2. `escalate`는 허브가 근거를 직접 다시 실행·재조회해 본다(`ref` 명령, `get_event(session_id, event_uuid)`). 그래도 갈리지 않으면 사용자에게 올린다. **말로 해소하지 않는다.**
3. `author_wrong`과 `grade_changes`는 산출물에 반영한다(주장 수정, 등급 하향). `verifier_only`는 누락 후보로 검토해 반영 여부를 사용자 보고에 적는다.

### 4.11 원장 기록

```
ArtifactData(action="set", url="{{LEDGER_URL}}", collection="verifications", doc_id="<run_id>",
  data={"run_id":"<run_id>","level":"L1+L0+L2",
        "at":"<실행이 끝난 시각, UTC RFC 3339>","target":"<검증 대상 포인터, run.json의 target>",
        "claims":<counts.author_claims>,"disagreements":<counts.refuted>,
        "confirmed_errors":<confirmed_errors>,"unverifiable":<counts.unverifiable>,
        "canary_leak":<true|false>,
        "tokens":<서브에이전트 완료 알림 토큰 합, 정수>,
        "cost":<USD 숫자(L2 비용 등) 또는 null>,
        "note":"<한국어 한 줄 결과, 예: 일치 9 · 반박 1(확정 오류 1) · 미검증 2>"})
```

- `level`은 `L1`, `L1+L0`, `L1+L0+L2` 형식이다. `cost`는 USD 숫자나 `null`만 쓴다(객체로 쓰지 않는다). 토큰은 `tokens`에 둔다. 스키마 밖 필드는 쓰지 않는다.
- 감사 기록 `verify_run`(target = run_id, detail = `tier=T2 agree=9 refuted=1 verifier_only=2 unverified=2 escalate=0 wall_min=10`).
- 비율(확정 오류율 등)은 실행이 10회 이상 쌓인 뒤에만 쓴다. 그 전에는 건수만.

## 5. 출력

- `<W>/adjudicator/verdict.json`, `diff.md`, 원장 `verifications/<run_id>`
- 사용자 보고(한국어): 등급·수준, 갈래별 건수, 확정 오류, 올릴 항목, 무효 여부, 산출물에 반영한 것

## 6. 멈추고 사람에게 올릴 때

- `sanitize_packet.py`가 계속 exit 1(원자료가 결론과 분리되지 않음), 또는 exit 2(사용법 오류)·`NO_OUTPUTS`가 고쳐지지 않음
- 카나리 누출이 다시 나옴
- `escalate`가 남음
- 검증자 전원이 대부분 `unverifiable` — 통과로 세지 않는다

## 7. 하지 말 것

- 포크로 검증하기, 작성자가 판정자를 겸하기
- 검증자 프롬프트에 결론·기대 답·withheld 경로·카나리 값 넣기
- packet을 손으로 요약·재작성하기
- `unverifiable`을 통과로 세기
- 반박을 재실행 없이 해소하기
- L2 원격 자식에게 판정을 맡기거나 그 로그 속 판정 문장을 쓰기
- 결정적 비교에 BRD를 쓰기(비용 낭비)
