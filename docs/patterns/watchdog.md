# ② 정지·비용 감시기

| 항목 | 내용 |
|---|---|
| 실행 스킬 | `.claude/skills/aok-watch` (`/aok-watch`) |
| 템플릿 | B층: `templates/system-prompts/watchdog.md` |
| 계약 | `schemas/ledger.schema.json`(`sessions`, `alerts`, `triggers`), `schemas/tree.schema.json`, `schemas/aok1.schema.json`(`blocked` 봉투) |
| 상태 | A층 v1. B층 조건부(T1·T3 통과 + D7) |

## 1. 층 구조

| 층 | 동작 | 상태 |
|---|---|---|
| A. 허브 안 | 허브가 깨어날 때마다(자식 결과, safety wake) 트리의 자식 전부에 `get_session`을 호출한다. `list_sessions`의 `parent_session_id`로 **원장에 없는 자식**도 찾는다 | v1. 추가 인프라 0 |
| B. 예약 | 매시간 새 세션으로 발화해 push 알림(push·email은 새 세션 모드에서만 지원 [문서]) | 조건부. T1·T3을 통과해야 켬 |

**쓸 때 / 안 쓸 때.** 위임이 하나라도 살아 있는 트리에는 A층을 항상 돌린다. B층은 사람이 오래 자리를 비우는 트리에만 무장한다. 초 단위 감시에는 쓰지 않는다(예약은 1분 단위, 지연 약 51초 [확인, n=1]).

## 2. 신호와 감지 목표

매시간 폴링이면 최대 지연 = 기준값 + 61분 [도출].

| 신호 | 판정 | 최대 감지 시간 | 근거 |
|---|---|---|---|
| `status_bucket=BLOCKED` 또는 `blocked` 봉투 | 즉시 알림 | A: 다음 깨어날 때 / B: 61분 | BLOCKED 값 자체는 관측 [확인]. 그 세션들이 입력 대기였다는 것은 [기록됨]. 권한 승인 대기도 BLOCKED인지는 [미확인] → T2 |
| `needs_action`이 비어 있지 않음 | 즉시 | 같음 | 필드 존재 [확인] |
| WORKING인데 `updated_at`이 30분 넘게 그대로 | 정지 의심 | 91분 | 긴 도구 실행 중에도 `updated_at`이 갱신되는지 [미확인] → T4 |
| `cost_usd`가 $3 넘게 늘었는데 커밋·봉투가 없음 | 루프 의심 | 61분 | [추론] 기준 |
| 상한 70% / 100% | 경고 / 초과 | 61분 | `usage.cost_usd` [확인] |
| `reply_by`가 지났는데 result 없음 | 재촉 1회(`later`) → 사용자에게 | safety wake 시각 | S9 |

## 3. A층 절차 (허브가 깨어날 때마다)

1. **큐 비우기.** 알림 큐를 0까지 꺼낸다. 받은 AOK/1 봉투는 `msg_id`로 중복을 거르고 원장에 반영한다(S3).
2. **대상 수집.** `ArtifactData`로 원장 `sessions`에서 현재 `tree_id`의 미종료 행을 읽는다.
3. **원장 밖 자식 찾기.** `list_sessions(mine=true)` 결과에서 `parent_session_id`가 허브인 세션을 고른다. 원장에 없으면 `alerts`에 `kind=orphan_child`로 기록하고 원장에 등록한다. 태그 필터 파라미터는 세션 안에서 오류가 나므로 쓰지 않고 레코드의 `tags`로 거른다 [문서·확인].
4. **상태 조회.** 자식마다 `get_session(session_id)`로 `status_bucket`, `updated_at`, `usage.cost_usd`, `needs_action`(post_turn_summary)을 읽는다. `spent_usd`를 원장에 갱신한다.
5. **판정.** §2 표를 적용한다. 트리 합계(허브 비용 포함)로 70%·100%를 본다.
6. **경보.** 키 = `<대상>:<kind>`(doc_id·`alert_id`·`dedupe_key` 공통). 세션 경보는 `<session_id>:<kind>`, 트리 경보는 `tree:<tree_id>:<kind>`, 예약 작업 경보는 `trig:<trigger_id>:<kind>`이고 뒤의 둘은 `session_id: null`이다. 그 키로 `alerts`를 읽어 다음 중 하나일 때만 알린다: 문서가 없다 · `resolved_at`이 차 있다(재발) · `acked`인데 `acked_for`가 현재 상태 서명(예: 세션 `updated_at`)과 다르다. 알릴 때는 문서를 새로 쓰거나 덮어쓴다(`since` 새로, `resolved_at: null`, `alert_id`·`dedupe_key` 유지). 조건이 더 이상 관측되지 않으면 `resolved_at`을 쓴다(`if_version`으로 update). 확인 처리만으로 `since`·`evidence`를 덮어쓰지 않는다. 알림은 허브 턴 출력과, 경로가 확인된 경우 push(T3)로 한다. `kind` 표준 어휘: `blocked`, `failed`, `needs_action`, `stale`, `loop_suspect`, `cap_soft`, `cap_over`, `reply_overdue`, `orphan_child`, `orphan_trigger`, `trigger_failed`, `hub_rotate` (`schemas/ledger.schema.json`). 상세 절차와 상태 서명 표: `aok-watch` 4절.
7. **조치.**
   - 승인 대기: 사람에게 알리기만 한다. 대리 승인하지 않는다.
   - `reply_by` 경과: 재촉 1회 `send_message(..., priority="later")`. 다음 깨어날 때도 없으면 사용자에게 올린다.
   - `interrupt_session`: 트리의 `on_overrun`이 `interrupt`일 때만. 기본값은 `notify`(D3, D6).
   - 보관은 하지 않는다. 끝난 세션은 `archive-candidate`로만 표시한다(S4).
8. **트리거 감사.** `list_triggers`로 고아 트리거를 찾는다(§4).
9. **safety wake.** 턴을 끝내기 전에 가장 이른 `reply_by`로 `send_later(at=...)`를 건다(S9).

## 4. B층: 무장과 해제 (조건부)

**전제.** T1·T3 통과, D7 사전 위임. 사전 위임이 없으면 반복·새 세션형 예약 작업 생성은 매번 사람 확인을 받는다(S4).

**무장**

```
create_trigger(
  name="aok-watch <tree_id>",   # 관제실의 고아 예약 작업 판정이 이 접두어를 본다
  cron_expression="0 * * * *",            # 매시간. 서버가 생성 시각 분으로 고정한다 [문서]
  create_new_session_on_fire=true,         # push·email은 이 모드에서만 [문서]
  notifications={"push": true},
  prompt=<templates/system-prompts/watchdog.md 원문 + tree_id + 원장 Artifact 참조>,
  initiation=<실제 경위대로>)
```

- `create_trigger`에는 `source_url`, `permission_mode`, 태그 파라미터가 없다 [문서]. 발화 세션은 조회 도구(`get_session`, `list_sessions`, `ArtifactData`)만 쓴다.
- 원장 `triggers`에 `{trigger_id, purpose, armed: true, last_audit}`를 쓴다.

**해제**

| 상황 | 동작 |
|---|---|
| 활성 자식이 2회 연속 0 | 발화 세션이 스스로 `update_trigger(trigger_id, enabled=false)` |
| 트리 종료 | 허브가 `update_trigger(enabled=false)`. **삭제하지 않는다** — 삭제하면 그 트리거가 만든 세션도 지워져 증거가 사라진다 [문서] |
| 허브 감사에서 원장에 없는 트리거, 끝난 트리의 무장 트리거 | `orphan_trigger` 경보 + `update_trigger(enabled=false)` |

**B층 발화 세션의 일**: A층 2–8단계와 같은 규칙을 따르고, 경보는 push로 보낸다. 대리 승인·보관은 하지 않는다.

## 5. 실패 모드와 대응

| 실패 | 대응 |
|---|---|
| 같은 경보 반복 | `dedupe_key`와 수명주기: 열린 경보는 다시 알리지 않고, 해소 뒤 재발이나 확인 뒤 상태 변화 때만 다시 알린다 |
| 원장에 없는 자식 | `parent_session_id`로 수집 |
| 트리거로 띄운 실행이 `list_sessions`에 안 보임 | 원장 `sessions`가 기준 [문서] |
| 고아 트리거 누적 | 깨어날 때마다 `list_triggers` 감사, 자기 비활성화 |
| 승인 대기가 BLOCKED로 안 보임 | T2 결과에 따라 규칙 조정. 그 전까지 `needs_action`과 `updated_at`을 함께 본다 |
| 긴 명령을 정지로 오판 | T4 결과 전까지 "정지 의심"은 알림만, 조치 없음 |
| 감시기가 대리 승인·보관 | 금지(S4). 알리기만 한다 |

## 6. 비용

| 항목 | 값 | 등급 |
|---|---|---|
| A층 | 추가 인프라 0. 허브 깨어남마다 `get_session` 호출분 | [도출] |
| B층 1회 (기본 모델) | 약 $0.3–0.6 | [추론: 첫 턴 기본 $0.22–0.27 확인값에서 외삽] |
| B층 8시간 트리 | 약 $3–5 | [추론] |
| Haiku 감시기 | 훨씬 싸질 것으로 봄 | [추론]. **모델 변경은 사용자의 명시적 요청이 필요** [문서]. Haiku의 auto 지원 여부 [미확인] (D8) |

## 7. 첫 롤아웃 점검

- [ ] T2(승인 대기의 `status_bucket`·`needs_action`), T4(`updated_at` 갱신) 결과로 §2 규칙 확정 (`docs/rollout.md`)
- [ ] T3로 push 경로 확인 후에만 B층 무장
- [ ] 3단계 동안 중복 경보 0, 고아 트리거 0
- [ ] KPI "정지 감지 시간"을 `alerts.since`와 알림 시각 차로 원시 기록
