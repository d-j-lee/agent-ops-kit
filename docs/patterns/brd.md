# ① 블라인드 재현 대조 (BRD)

| 항목 | 내용 |
|---|---|
| 실행 스킬 | `.claude/skills/aok-verify` (`/aok-verify`) |
| 서브에이전트 | `.claude/agents/verifier.md`, `.claude/agents/adjudicator.md` (읽기 전용 도구) |
| 템플릿 | L2: `templates/system-prompts/child-base.md` + `templates/system-prompts/repro-l2.md` |
| 계약 | `schemas/claim.schema.json`(주장), `schemas/aok1.schema.json`(L2 봉투), `schemas/ledger.schema.json`(`verifications`) |
| 스크립트 | `scripts/sanitize_packet.py` |

## 1. 쓸 때 / 안 쓸 때

| 쓸 때 | 안 쓸 때 |
|---|---|
| 결론이 틀리면 비싼 주장·분석·설계·PR | sha 비교, 테스트 통과처럼 결정적인 비교. 이것은 ⑦ 회귀 고정(`docs/patterns/regression.md`)으로 한다. 결정적 비교도 빈 출력·비결정성·좁은 비교 범위로 스스로 속기 때문이다 |

## 2. 검증 수준과 위험 등급

| 수준 | 검증자가 보는 것 | 수단 | 역할 |
|---|---|---|---|
| L0 검토 | 결론 포함 산출물 | 새 서브에이전트 | 오류, 과장, 수치 착오 |
| L1 블라인드 | 질문 + 원자료 | 새 서브에이전트(렌즈 1–2개) | 누락과 대안 |
| L2 재현 | 재현 절차만 | 원격 자식 | **재현 로그만** 만든다. 판정은 L0·L1과 판정자가 한다(S7) |

| 위험 등급 | 조건 | 적용 |
|---|---|---|
| T0 | — | 생략 |
| T1 | — | L1 |
| T2 | 되돌리기 어려움, 외부 공개, 환경 의존 | L1 + L0. 환경에 달린 주장이 있으면 L2 추가 |

> 위험 등급 T0–T2는 `docs/rollout.md`의 실현성 시험 T1–T6과 다른 축이다.

## 3. 절차 (런북)

1. **등급 판정.** 산출물의 위험 등급을 정한다. L2를 쓰면 ⑥ 트리를 열고 입장 검사를 한다(`docs/patterns/budget.md`).
2. **패킷 분리.** 결론이 든 쪽은 packet의 상위 디렉터리 밖, 별도 트리에 둔다(형제 디렉터리가 아니라 형제 트리). 검증자가 packet 위로 올라가 훑어도 보이지 않게 하기 위해서다.
   - `withheld/` = `<SP>/brd-withheld/<run_id>/`: 결론, 주장 원장(`schemas/claim.schema.json` 형식의 주장 목록), 카나리, 실행 메타, 판정 결과.
   - `packet/` = `<SP>/brd/<run_id>/packet/`: 질문, 원자료, 재현 절차. **원자료에서 기계적으로 잘라** 만든다. 작성자의 요약·해석을 넣지 않는다.
   - L0용 결론 포함 산출물은 `<SP>/brd-l0/<run_id>/packet/`(L1 packet의 상위에 두지 않는다). 상세: `aok-verify` 4.1.
3. **카나리와 정화.** 한 번의 호출로 카나리를 심고 `packet/`을 검사한다.
   ```
   python3 -I scripts/sanitize_packet.py --packet <SP>/brd/<run_id>/packet --withheld <SP>/brd-withheld/<run_id> --canary [--terms <SP>/brd-withheld/<run_id>/terms.txt]
   ```
   - `--canary`: `withheld/CANARY-<hex>.txt`를 만들고 토큰을 출력한다.
   - `withheld/`의 특징적인 줄과 `--terms`(결론 어휘·권고 절 목록)를 `packet/`에서 찾아 `path:line: term`으로 출력한다.
   - 종료 코드 0 = 깨끗함, 1 = 누출(패킷을 다시 자른다), 2 = 사용법 오류(통과가 아니다. 고치지 못하면 멈추고 보고한다).
4. **검증자 실행.** 수준별로 병렬로 띄운다.
   - L1: `Agent(subagent_type="verifier")`에 `packet/` 경로·출력 경로와 렌즈만 준다(withheld 경로는 주지 않는다). 렌즈는 셋 중 1–2개: 주장별 판정 / 독립 재설계 / 반례 탐색.
   - L0: `Agent(subagent_type="verifier")`에 결론 포함 산출물 전체를 준다.
   - L2: 원격 자식.
     ```
     create_session(
       source_url=<repo>, source_revision=<sha>,
       permission_mode="auto",
       title="[<proj>/repro-l2] <task>",
       tags=["aok:v1","proj:<p>","role:repro-l2","tree:<tree_id>"],
       append_system_prompt=<child-base.md 원문 + repro-l2.md 원문>,
       prompt=<AOK/1 task 봉투: packet/의 재현 절차, budget.cap_usd, reply_by>)
     ```
     원장 `sessions`에 행을 쓰고, 턴을 끝내기 전에 `send_later(at=reply_by)`(S9).
   - 검증자는 실행할 수 있으면 실행한다. 실행이 막히면 `unverifiable`로 보고한다.
5. **카나리 검사.** 검증자 출력을 파일로 저장하고 검사한다.
   ```
   python3 -I scripts/sanitize_packet.py --withheld <SP>/brd-withheld/<run_id> --check-output <존재하는 검증자 출력 파일·디렉터리…>
   ```
   존재하는 출력 경로만 넘긴다. 종료 코드 1이면 그 실행은 무효다. 패킷을 다시 만들고 `verifications.canary_leak=true`로 남긴다. 종료 코드 2(사용법 오류)는 통과가 아니다. 판정으로 넘어가지 않고 멈추고 보고한다.
6. **판정.** 별도의 새 서브에이전트 `Agent(subagent_type="adjudicator")`가 `withheld/`와 검증 결과를 대조한다. 작성자는 판정자가 될 수 없다.
   - 갈래: 일치 / 반박 / 검증자만 발견 / 미검증
   - 출력: `verdict.json`(주장별 갈래와 근거), `diff.md`(사람이 읽는 차이)
7. **해소.** 반박 항목은 근거를 다시 실행해서만 해소한다. 해소하지 못하면 사용자에게 올린다.
8. **기록.** `ArtifactData`로 원장 `verifications`에 `{run_id, level, at, target, claims, disagreements, confirmed_errors, unverifiable, canary_leak, tokens, cost, note}`를 쓴다. `cost`는 USD 숫자 또는 `null`(객체가 아니다), `tokens`는 정수, `note`는 한국어 한 줄이다. `level`은 `L1`, `L1+L0`, `L1+L0+L2` 형식이다. `unverifiable`은 통과로 세지 않고 따로 집계한다.

## 4. 계약

| 산출물 | 형식 |
|---|---|
| `withheld/` 주장 원장, 검증자 주장 | `schemas/claim.schema.json` (§2.1 근거 규약: [확인]에는 재실행 가능한 `ref`) |
| L2 task / result | `schemas/aok1.schema.json` |
| 원장 기록 | `schemas/ledger.schema.json`의 `verifications` |
| `verdict.json` | 주장 `id`별 갈래(일치·반박·검증자만 발견·미검증)와 근거 포인터 |

## 5. 실패 모드와 대응

| 실패 | 대응 |
|---|---|
| 결론 누출 | 출력에 카나리가 나오면 그 실행은 무효 |
| 같은 모델의 상관 오류 | 렌즈를 다르게 하고, 필요하면 `model`을 바꾼다. 모델을 바꿔도 독립이 되지는 않는다(§8). 결정적인 판단은 실행 결과와 사람에게 기댄다 |
| 실행 불가를 통과로 셈 | `unverifiable`을 따로 집계 |
| L2 자식이 판정까지 함 | 판정 부분은 버리고 재현 로그만 쓴다(S7) |
| 작성자가 판정 | 판정자는 항상 별도 새 서브에이전트 |
| 포크로 검증 | 금지. 포크는 맥락과 편향을 상속한다 [추론] (§2.2) |

## 6. 비용과 시간

| 항목 | 값 | 등급 |
|---|---|---|
| 새 서브에이전트 1개 | 165k–320k 토큰, 7–11분 | [확인: 블라인드 설계자 165k·7.9분, L0 검토 165k·6.9분, V 221k·10.6분, C 318k·8.2분. 출처는 각 작업 완료 알림] |
| L1 + L0 + 판정자 | 약 0.5–0.8M 토큰 병렬, 10분 안팎 | [도출] |
| L2 원격 자식 | 첫 턴 $0.22–0.27 + 재현 작업량 | [확인] |

**사례 (n=1, 확정 일반화 아님)**

| 검증 | 잡은 것 |
|---|---|
| 실측 보고서 L0(V) | 비용 오류 1건, 범위 축소 1건, 위험 경고 누락 1건 [확인] |
| L2(R) | H9 발견. 동시에 H1 재현은 검증 불가 [확인] |
| 설계 문서 L1(블라인드 설계) | 트리 상한, 카나리, PR 정본, 카드 수명주기를 더함 |
| 설계 문서 L0 | 트리거 제약, 교착, KPI 데이터원 문제를 지적 |

수준마다 잡는 것이 달랐다는 해석은 [추론]이다. 이력: `reports/2026-10-20-plan-review.md`.

## 7. 첫 롤아웃 점검 (2단계)

- [ ] 실제 작업에 BRD 3회 이상
- [ ] 카나리 누출 0
- [ ] 모든 실행이 `verifications`에 있음. `unverifiable` 건수가 따로 집계됨
- [ ] 확정 오류는 실행당 원시 건수로 보고(n≥10 전에는 비율 금지)

## 8. 외부 근거 — 왜 결론을 숨기고 맥락을 나누나

| 근거 | 내용 | 등급 |
|---|---|---|
| 스스로 고치기 | 외부 피드백 없는 자기 수정은 추론 성능을 개선하지 못했고 때로는 악화시켰다(Huang 외, 2023) | [문서] https://arxiv.org/abs/2310.01798v2 |
| 찾기가 병목 | 추론 과정의 첫 오류 위치를 찾는 정확도는 가장 좋은 모델(GPT-4)도 52.87%였다. 오류 위치를 알려 주면 스스로 고쳐 전체 성능이 올랐다(Tyen 외, 2023) | [문서] https://arxiv.org/html/2311.08516v3 |
| 자기 편애 | LLM 평가자는 자기 출력을 알아보고 더 높게 평가했고, 알아보는 능력과 편애의 강도가 선형으로 함께 움직였다(Panickssery 외, NeurIPS 2024) | [문서] https://papers.nips.cc/paper_files/paper/2024/hash/7f1f0218e45f5414c79c0679633e47bc-Abstract-Conference.html |
| 함께 틀림 | HELM 리더보드(모델 71개, 4지선다)에서 두 모델이 모두 틀린 문제의 평균 60%에서 같은 오답을 골랐다(무작위라면 33%). 350개가 넘는 모델 전체에서, 크고 정확한 모델일수록 제공사와 구조가 달라도 오류가 강하게 상관됐다(Kim 외, ICML 2025) | [문서] https://arxiv.org/abs/2506.07962v1 |
| 동의 쪽으로 기욺 | 사용자 견해와 맞는 응답이 사람 평가에서 선호되며, 이 선호가 RLHF 모델에서 흔한 아첨의 원인 중 하나일 가능성이 높다고 보고됐다(Sharma 외, 2023) | [문서] https://www.anthropic.com/research/towards-understanding-sycophancy-in-language-models |

이 근거들이 L1의 결론 숨기기, 작성자와 판정자의 분리, 포크 금지를 뒷받침한다는 해석은 [추론]이다.

