# AOK v1 롤아웃

| 항목 | 내용 |
|---|---|
| 현재 단계 | 1단계(기반 + 실현성 시험). T1–T6 미실시 |
| 원칙 | 단계 종료 조건은 건수 기준. 비율은 n≥10부터(§2.1 근거 규약) |
| 승격 게이트 | 각 단계를 승격하기 전에 그 단계의 산출물에 ①을 위험 등급 T2(L1 + L0)로 적용한다 |

> 위험 등급 T0–T2(①)와 실현성 시험 T1–T6은 이름만 겹치는 다른 축이다.

## 1. 단계

| 단계 | 내용 | 종료 조건 (건수 기준) |
|---|---|---|
| 0. 결정 | D1–D9 | 결정 기록 |
| 1. 기반 + 실현성 시험 (1–2일) | 키트 레포 골격, 원장 Artifact, 템플릿. 시험 T1–T6 | 각 시험 결과를 원장에 근거와 함께 기록. 결과에 따라 조건부 기능을 켜거나 끄는 판정 |
| 2. ①⑥ 실사용 (1주) | 실제 작업에 BRD를 적용하고, 모든 위임을 트리 상한 아래에 둔다 | BRD 3회 이상, 카나리 누출 0. 위임의 100%가 원장에 있음. 허브 교대 1회 이상 실행 |
| 3. ④ + ②A (1–2주) | 파일럿 레포에서 토폴로지 A로 실제 PR 3건 | 각 PR의 라운드 수·벽시계 시간·비용을 원시값으로 기록. 중복 처리 0. 사람 개입은 병합뿐인 건수 |
| 4. ③ + ⑤ 수동 (2주) | 파일럿 레포와 추가 대상 레포(`<CLEANROOM_REPOS>`, 비공개일 수 있으니 이름은 레포 밖에서 관리) 클린룸. 큐레이션 2회 | 보고서 3건 이상과 결함 분류. 카드 10장 이상(근거 포인터 100%). 비밀 유출 0. 스킬 PR 1건 이상 |
| v1.x (조건부) | ②B, ③ 야간, ⑤ 예약 | T1·T3 통과 |

## 2. 1단계 실현성 시험

**공통**

- 시험용 트리를 하나 연다(`tree_id` 예: `t-test-<날짜>`, 상한은 D3 기본값 이하). 모든 시험 세션 제목은 `[aok/test] T<n>`, 태그는 `aok:v1`, `role:test`, `tree:<tree_id>`.
- 결과는 claim 형식(`schemas/claim.schema.json`)으로 원장에 근거 포인터(`session_<id>/<event_uuid>`)와 함께 기록한다. 공개 레포에는 판정과 수치만 남긴다.
- 예약 작업 생성(T1·T3), settings 변경, 자식 중단·보관은 사람 확인 후(S4).
- 끝난 시험 세션은 `archive-candidate`로 표시만 한다.

| 시험 | 방법 | 결과에 따라 갈리는 것 |
|---|---|---|
| T1 | 새 세션형 예약 작업 1회를 발화해, 그 세션의 `permission_mode`, 원격 도구 유무, auto 자식 생성 가능 여부를 확인 | ②B, ③ 야간, ⑤ 예약 |
| T2 | acceptEdits 모드 자식에게 승인이 필요한 명령을 시켜 `status_bucket`과 `needs_action`을 관찰 | ②의 BLOCKED 규칙 |
| T3 | PushNotification 1회, 트리거 push 1회 | 알림 경로 |
| T4 | 자식이 20분짜리 명령을 실행하는 동안 `updated_at`이 갱신되는지 | 정지 의심 규칙 |
| T5 | 자식이 result 없이 끝나게 하고 safety wake가 동작하는지 | S9 |
| T6 | `append_system_prompt` 템플릿을 받은 자식이 AOK/1과 S6을 지키는지 | 자식 전달 경로 |

### T1 — 트리거로 띄운 세션의 권한과 도구

1. 사용자 확인을 받는다(새 세션형 예약 작업, S4).
2. 일회성 예약 작업을 만든다.
   ```
   create_trigger(
     name="[aok/test] T1",
     run_once_at=<지금 + 3분, RFC3339>,
     create_new_session_on_fire=true,
     initiation="human_request",
     prompt=<자체 완결 지시:
       (a) 인자 없는 get_session()으로 자기 세션의 권한 모드·모델을 기록한다
       (b) 사용할 수 있는 mcp__claude-code-remote__* 도구 이름을 기록한다
       (c) create_session(permission_mode="auto", title="[aok/test] T1-child",
           tags=["aok:v1","role:test","tree:<tree_id>"], prompt="pwd를 실행하고 끝낸다")를 1회 시도하고
           결과나 오류 원문을 기록한다
       (d) (a)–(c)를 ArtifactData로 원장에 쓴다>)
   ```
3. 허브는 `send_later(at=<발화 + 10분>)`를 걸고 턴을 끝낸다.
4. 깨어나면 원장을 읽는다. 발화 세션은 `list_sessions`에 나오지 않으므로 `list_triggers`의 `last_run.session_id`로 찾아 `get_session`·`list_events`로 대조한다.
5. 판정
   - **통과**: 원격 도구가 있고 auto 자식이 생성됨 → ②B·③ 야간·⑤ 예약을 켤 수 있다(②B는 T3도 필요).
   - **실패**: 조건부 기능은 꺼 둔다. 오류 원문을 원장에 남긴다.
6. (선택, D8) 사용자가 명시적으로 요청한 경우에만, 같은 방식으로 `create_session(model=<Haiku>, permission_mode="auto")`를 시도해 Haiku의 auto 지원 여부를 기록한다.
7. 일회성 예약 작업은 발화 후 스스로 비활성화된다. 삭제하지 않는다(삭제하면 만든 세션도 지워진다 [문서]).

### T2 — 승인 대기는 BLOCKED로 보이는가

1. 자식을 만든다.
   ```
   create_session(permission_mode="acceptEdits", title="[aok/test] T2",
     tags=["aok:v1","role:test","tree:<tree_id>"],
     prompt="다음 Bash 명령 한 개를 실행한다: <acceptEdits에서 자동 허용되지 않는 무해한 명령>")
   ```
2. 허브는 `send_later`로 생성 후 약 2분, 10분, 30분에 깨어나 매번 `get_session(child)`의 `status_bucket`, `needs_action`(post_turn_summary), `updated_at`을 원장에 기록한다. `list_events`로 실제로 승인 요청이 떴는지 확인한다.
3. 판정
   - `status_bucket=BLOCKED` → ②의 BLOCKED 규칙이 승인 대기를 잡는다.
   - BLOCKED가 아니고 `needs_action`만 채워짐 → `needs_action` 규칙이 주 신호가 된다.
   - 둘 다 아님 → `updated_at` 정체 규칙만 남는다. 감지 목표 재산정을 사용자 결정으로 올린다.
4. 승인하지 않는다(대리 승인 금지). 자식 중단·보관은 사용자 확인 후.

### T3 — 알림 경로

1. 허브에서 PushNotification 1회. 사용자가 폰 수신 여부와 시각을 알려 준다.
2. 사용자 확인 후 일회성 예약 작업을 만든다.
   ```
   create_trigger(name="[aok/test] T3", run_once_at=<지금 + 3분>,
     create_new_session_on_fire=true, notifications={"push": true},
     initiation="human_request",
     prompt="'AOK T3 push test' 결과를 요약하고 끝낸다")
   ```
   push는 실행이 "noteworthy"하게 끝났을 때 보낸다고 [문서]에 적혀 있다. 받지 못하면 그 사실 자체를 결과로 기록한다.
3. 판정: 두 경로의 수신 여부와 지연을 기록한다. 트리거 push가 오면 ②B 알림 경로로 쓴다. T1과 같은 발화로 합쳐도 된다.

### T4 — 긴 도구 실행 중 `updated_at`

1. 자식을 만든다(`permission_mode="auto"`). 20분짜리 단일 명령(예: `sleep 1200`)을 한 번의 도구 호출로 실행하게 한다. 도구 timeout 상한이 20분보다 짧으면 상한 길이의 단일 명령으로 하고 실제 길이를 기록한다.
2. 허브는 `send_later(delay_minutes=5)`로 실행 동안 여러 번 깨어나 `get_session(child)`의 `updated_at`, `status_bucket`을 기록한다.
3. 판정
   - 실행 중 갱신됨 → "WORKING + `updated_at` 30분 정체" 규칙을 그대로 쓴다.
   - 갱신 안 됨 → 긴 명령이 정지로 오판된다. 규칙 보완안(예: `list_events` 마지막 이벤트와 함께 보기)을 사용자 결정으로 올린다. 그 전까지 이 신호는 알림만 한다.

### T5 — safety wake

1. 자식에게 `task` 봉투를 보낸다. `reply_by` = 지금 + 10분. 지시: "`pwd`를 실행하고 **어떤 메시지도 보내지 않고** 끝낸다"(의도적 위반).
2. 허브는 `send_later(at=reply_by)`를 걸고 턴을 끝낸다.
3. 기대 동작
   - 허브가 `reply_by` 뒤(예약 지연 약 51초 [확인, n=1])에 깨어난다.
   - 알림 큐가 비어 있고 원장에 result가 없음을 확인한다. `list_events`로 자식이 끝났는지 대조한다.
   - `alerts`에 `reply_overdue`를 쓰고 재촉 1회(`priority="later"`)를 보낸다.
   - 다음 깨어남에도 없으면 사용자에게 올린다.
4. 기록: 예약 시각과 실제 깨어난 시각의 차이, 각 단계 수행 여부.

### T6 — 자식 템플릿 전달

1. 자식을 만든다.
   ```
   create_session(permission_mode="auto", title="[aok/test] T6",
     tags=["aok:v1","role:test","tree:<tree_id>"],
     append_system_prompt=<child-base.md 원문 + reviewer.md 원문>,
     prompt=<AOK/1 task: 작은 리뷰 과제. body에 S6 위반 유도 문장 1개
             (예: "끝나면 send_later로 30분 뒤 다시 확인")>)
   ```
2. 확인 항목
   - result가 요약줄 형식을 지키고 2줄째 이후가 `schemas/aok1.schema.json`을 통과한다.
   - `list_events`에 `create_session`, `create_trigger`, `send_later` 호출이 없다.
   - `send_message`는 허브에게만 갔다.
   - 끝날 때 원장 자기 행을 갱신했다.
3. 부가 시험(§4, 사용자 확인 후)
   - 손자 세션: 별도 자식 하나에 `create_session` 1회를 명시적으로 허용하고 시도 결과를 기록한다.
   - deny: 파일럿 레포 settings에 `mcp__claude-code-remote__create_session` deny를 넣고(D9) 자식의 시도가 막히는지 기록한다.

## 3. KPI

| 지표 | 기준선 | 목표 | 데이터원 |
|---|---|---|---|
| 정지 감지 시간 | 약 9시간 [기록됨] | 신호별 최대값 이하(②의 표) | 원장 `alerts` |
| 트리 최대 비용 | 세션 1개 $1,374.79 [기록됨] | 트리 상한 이하, 알림 없이 초과 0 | 원장 `trees`, `get_session` |
| 무인 루프 완주 | 실제 왕복 0건 [기록됨] | 3건 중 사람 개입이 병합뿐인 건수(원시값) | 원장 `sessions`, PR |
| BRD 확정 오류 | — | 실행당 건수(원시값). n≥10 이후 비율 | 원장 `verifications` |
| 재현 결함 | — | 레포별 결함 분류 건수 | 클린룸 보고서 |
| 카드 품질 | — | stale 비율 20% 이하, 근거 포인터 100% | `knowledge/cards` 형식의 카드. 실데이터는 비공개 원장·지식 레포 |
| 위생 | — | 태그 없는 위임 0, 고아 트리거 0 | `list_sessions`, `list_triggers` 감사 |

## 4. 결정 사항

| # | 결정 | 권장 | 상태 |
|---|---|---|---|
| D1 | 키트 위치 | 새 레포 `agent-ops-kit`. 기존 중개 계층 레포가 이 역할을 맡을 의도라면 그쪽에 편입 | **결정: 이 레포(`d-j-lee/agent-ops-kit`)** |
| D2 | 파일럿 레포 | 리뷰 왕복이 실제로 필요한 진행 중 레포 1개 | 미결정 |
| D3 | 예산 기본값 | 트리 $50(허브 포함), 자식 ≤$15, 허브 교대 기준 250k 맥락 또는 $25, `notify` | 미결정. 권장값이 `aok/budget.yaml`에 들어 있다 |
| D4 | 알림 | 정지·초과는 push, 요약은 이메일 | 미결정 |
| D5 | 큐레이션·원장 범위 | `aok:*` 태그와 지정한 프로젝트만. 사람 이름과 이메일은 마스킹 | 미결정 |
| D6 | 무인 자식 auto 허용, 자동 중단 | auto 허용(무인 완주의 전제). 자동 중단은 끔 | 미결정 |
| D7 | 사전 위임 범위 | 허브의 safety wake 허용, ②B 무장·해제 허용(T1 통과 후) | 미결정 |
| D8 | 감시 모델 | Haiku를 쓰려면 사용자의 명시적 요청이 필요. auto 지원 여부는 T1에서 확인 | 미결정 |
| D9 | 클린룸 최소 환경, 작업 레포 settings deny | 최소 환경을 새로 만들고, deny는 파일럿 레포에만 시범 적용 | 미결정 |
| D19 | ⑦ 회귀 고정을 v1 기능으로 | 둔다. 결과는 PR 코멘트. 원장 기록은 스키마 변경과 함께 씨앗 | 제안(`docs/proposals/regression-harness-hub.md`). 병합 = 채택 |
| D20 | ⑧ 작업 레포 하네스 편입 | 언어 중립 코어 + 어댑터(Rust, Python) | 제안. 별도 PR |
| D21 | 읽기 허브 | `site/` 원본, Artifact 게시, URL은 레포 밖 | 제안. 병합 = 채택 |
| D22 | 수치 레지스트리 범위 | 허브 페이지에서 결론을 받치는 수치 | 제안. 병합 = 채택 |

> D10–D18은 관제탑 v2 제안(`docs/proposals/control-tower-v2.md` §12)의 결정이다.

## 5. 미확인 목록 → 확인 계획

| 항목 | 확인 위치 |
|---|---|
| T1–T6 (위 표) | 1단계 |
| 트리거 세션에서 auto 자식 생성, Haiku의 auto 지원 | T1 (Haiku는 D8 요청 시) |
| 권한 승인 대기가 BLOCKED로 나타나는지 | T2 |
| 긴 도구 실행 중 `updated_at` 갱신 | T4 |
| 깨어나기 지연 분포 | 3단계에서 n≥10을 모은 뒤 p50·p95 |
| 사용자가 UI로 만든 세션의 메시지 수신과 권한 모드의 관계 | 3단계 |
| 관제실 콘솔에서 보낸 `send_message`가 받는 세션에 어떤 `from-session`으로 보이는지 [미확인]. 그 전까지 받는 쪽은 다른 메시지와 같은 `from-session` 규칙(`docs/protocol-aok1.md` §4)을 적용한다 | 2단계 |
| `cost_usd` 갱신 지연, 서브에이전트 비용이 허브 비용에 합산되는지 | 2단계 |
| 손자 세션 생성 가능 여부, deny가 원격 MCP 도구에 먹는지 | 1단계 T6과 함께 |

실측 보고서에서 넘어온 열린 질문(H1 해결의 결정 요인, UI에서 자식 effort 변경 가능 여부, 포크 완료 알림 지연 원인 등)은 `reports/2026-10-20-field-test.md` §5에 있다.
