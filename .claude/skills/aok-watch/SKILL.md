---
name: aok-watch
description: AOK v1 정지·비용 감시기(§3②) 런북. A층은 허브가 깨어날 때마다(자식 메시지, safety wake, PR 이벤트, child-session-event, 사용자의 상태 확인 요청) 실행한다 — ReadNotifications를 0까지 비우고, 트리 자식 전부에 get_session, list_sessions의 parent_session_id로 원장 밖 자식을 찾고, 신호표(BLOCKED·failed·needs_action·WORKING인데 updated_at 30분 정지 의심·진전 없이 3 USD 넘게 증가한 루프 의심·상한 70%/100%·reply_by 초과)를 평가해 dedupe_key=<대상>:<kind>로 원장 alerts에 기록하고(열린 경보는 다시 알리지 않고, 해소 뒤 재발·확인 뒤 상태 변화 때만 다시 알림, 사라진 신호는 resolved_at) 사용자에게 알린다. B층(매시간 새 세션 예약 감시기)의 무장 조건(T1·T3 통과 + D7), 2회 연속 활성 0이면 자기 비활성화, list_triggers 고아 감사, 삭제 대신 비활성화 규칙과 컨트롤 콘솔에 어떻게 보이는지도 다룬다. 허브가 어떤 이유로든 깨어났을 때, 사용자가 "상태/감시/비용 확인"을 요청할 때, 긴 무인 트리에 예약 감시를 켤지 정할 때 로드한다.
---

# aok-watch — 정지·비용 감시 런북

A층은 v1 기본 기능(추가 인프라 0)이다. B층은 조건부(v1.x: T1·T3 통과, D7 사전 위임)다. 감시기는 **알리기만** 한다: 대리 승인, 보관, 기본 정책에서의 중단을 하지 않는다.

계약: `schemas/ledger.schema.json`(`sessions`, `alerts`, `triggers`, `audit_day`), `templates/system-prompts/watchdog.md`(B층). 원장 쓰기 규칙은 `aok-delegate` 부록 A.

## 1. 전제

- 이 세션이 허브이고 활성 트리가 있다(`aok-delegate` 3.2). 없으면 사용자 요청에 대한 단순 조회만 하고 경보는 쓰지 않는다.
- 운영 기준은 원장 `config/policy`(없으면 `aok/budget.yaml`): `stale_minutes` 30, `loop_cost_jump_usd` 3, `soft_ratio` 0.7, 허브 교대 25 USD·250k 토큰.
- A층 한 번의 비용을 작게 유지한다: 활성 자식당 `get_session` 1회, `list_sessions` 1회, `list_triggers` 1회, 원장 쓰기는 `batch` 1–2회.

## 2. 신호표

`status_bucket`은 `SESSION_STATUS_BUCKET_` 접두어를 떼고 소문자로 비교한다(`working`·`blocked`·`review_ready`·`completed`·`failed`). 비용 = `external_metadata.usage.cost_usd`. 활성 행 = status가 `spawned`·`working`·`idle`·`blocked`.

| kind | 대상(subject) | 조건 | 최대 감지 시간 | 조치 |
|---|---|---|---|---|
| `blocked` | session_id | status_bucket = blocked, 또는 `blocked` 봉투 수신 | A: 다음 기상 / B: 61분 | 사용자에게 즉시. 대리 승인 금지 |
| `failed` | session_id | status_bucket = failed, 또는 실패를 알리는 child-session-event | 같음 | 사용자에게 |
| `needs_action` | session_id | `post_turn_summary.needs_action`(external_metadata 아래 또는 최상위)이 비어 있지 않음 | 같음 | 사용자에게 |
| `stale` | session_id | status_bucket = working 이고 now − `updated_at` > 30분 | B: 91분 | "정지 의심"으로 알리기만. 긴 명령 중에도 `updated_at`이 갱신되는지는 [미확인](T4) |
| `loop_suspect` | session_id | 실시간 비용 − 원장 `spent_usd` > 3 USD (원장 `spent_usd`는 마지막 진전 시점 비용이다 — aok-delegate 3.8) | 61분 | 사용자에게. 커밋·봉투가 없는지 `list_events(limit=20)`로 한 번 확인 |
| `cap_soft` / `cap_over` | session_id | 비용 ≥ 0.7 × 행 `cap_usd` / ≥ `cap_usd` | 61분 | 경고 / 초과 알림. `on_overrun: interrupt`일 때만 `interrupt_session` |
| `cap_soft` / `cap_over` | `tree:<tree_id>` | 트리 합계(허브 포함, aok-delegate 3.3) ≥ 0.7 × / ≥ 1 × 트리 `cap_usd` | 61분 | 같음. 트리 `status`를 `soft` / `overrun`으로 |
| `hub_rotate` | 허브 session_id | 허브 비용 ≥ 25 또는 맥락 ≥ 250k | 다음 기상 | aok-delegate 3.12 교대 |
| `reply_overdue` | session_id | now > reply_by 이고 result 없음, 이미 재촉함 | safety wake 시각 | 첫 번째는 재촉 1회(aok-delegate 3.11, 경보 아님), 두 번째에 경보 |
| `orphan_child` | session_id | `list_sessions`의 `parent_session_id` = 허브인데 원장 행이 없음 | 다음 기상 | 원장에 등록 + 알림(위생 KPI) |
| `orphan_trigger` | `trig:<trigger_id>` | 켜진 예약 작업인데 원장 `triggers`에 없거나, 끝난 트리의 감시기 | 다음 기상 | 6.4 |
| `trigger_failed` | `trig:<trigger_id>` | 켜진 예약 작업의 `last_run.status`가 실패 | 다음 기상 | 사용자에게. 6.4 |

컨트롤 콘솔은 같은 기준을 실시간으로 직접 계산하고 kind 이름과 키(`<대상>:<kind>`)도 같다. 원장에는 표준 kind만 쓴다: `blocked`, `failed`, `needs_action`, `stale`, `loop_suspect`, `cap_soft`, `cap_over`, `reply_overdue`, `orphan_child`, `orphan_trigger`, `trigger_failed`, `hub_rotate`(`schemas/ledger.schema.json`).

## 3. A층 절차 (허브가 깨어날 때마다)

1. **큐 비우기.** `ReadNotifications`를 빈 결과가 나올 때까지 반복하고, 봉투를 `aok-delegate` 3.10의 2단계대로 처리한다(중복 제거, 진전 기록). `blocked` 봉투는 그 자리에서 `blocked` 신호로 표시한다.
2. **대상 수집.** `ArtifactData(action="query", url="{{LEDGER_URL}}", collection="sessions", query={"where":[["tree_id","==","<tree_id>"]],"limit":100})`.
3. **상태 조회.** 활성 행과 허브 행마다 `get_session(session_id=<id>)`(허브는 인자 없이) → status_bucket, updated_at, 비용, needs_action, `context_usage.used_tokens`(허브).
4. **원장 밖 자식.** `list_sessions(mine=true, limit=100)` → 레코드의 `parent_session_id`가 허브이고, 보관되지 않았고, 원장 행이 없는 것. 태그 필터 파라미터는 세션 안에서 오류가 나므로 쓰지 않고 레코드의 `tags`로 거른다. 찾으면 `sessions` 행을 등록한다(`tree_id`는 태그 `tree:<id>`가 있으면 그 값, 없으면 현재 트리; `role`은 태그 `role:<r>`나 `unknown`; `cap_usd`는 역할 기본값; status는 status_bucket에서: working→`working`, blocked→`blocked`, completed→`done`, 그 밖→`idle`).
5. **판정.** 2절 표를 적용한다.
6. **경보 기록·해소.** 4절(알릴 키는 쓰고, 이번에 평가한 대상에서 사라진 신호는 `resolved_at`).
7. **원장 갱신(batch 1회).** 상태가 바뀐 행의 `status`(status_bucket이 completed인데 원장이 working이면 `idle`로 두고 3.10 대조로 넘긴다), 트리 행 `spent_usd`·`status`. 진전(봉투·새 커밋)이 있던 행만 `spent_usd`를 실시간 비용으로 올린다.
8. **조치.** 2절 표의 조치 열. 승인 대기는 알리기만 한다. 보관하지 않는다. `interrupt_session`은 트리 정책이 `interrupt`일 때만.
9. **예약 작업 감사.** 6.4.
10. **사용자 알림.** 5절.
11. **다음 기상.** 활성 자식이 있으면 `aok-delegate` 3.9로 safety wake(가장 이른 reply_by). 기한이 2시간 넘게 남은 무인 자식이 있고 B층을 켤 수 없다면, 감지 지연이 그만큼 길어진다는 점을 사용자에게 한 줄로 알린다.

## 4. 경보 수명주기와 중복 제거

키 `<key>` = `<subject>:<kind>`이고 doc_id·`alert_id`·`dedupe_key`가 모두 이 값이다. `subject`는 session_id, `tree:<tree_id>`, `trig:<trigger_id>` 중 하나다. 세션 경보는 `session_id`에 그 ID를, 트리·예약 작업 경보는 `session_id: null`을 쓴다(대상은 키에 들어 있다. 예: `tree:<tree_id>:cap_soft`, `trig:<trigger_id>:trigger_failed`).

1. **읽기.** 점검마다 `ArtifactData(action="list", url="{{LEDGER_URL}}", collection="alerts")`로 한 번 읽는다. 쓰기 직전에는 `get`으로 `version`을 다시 읽는다(부록 A.1).
2. **알릴지(NOTIFY).** 이번에 신호가 관측된 키마다 다음 중 하나면 알린다:
   - 문서가 없다
   - `resolved_at`이 차 있다(해소 뒤 재발)
   - `acked`가 참이고 `acked_for` ≠ 현재 상태 서명(아래 표 — 확인한 뒤 상태가 바뀜)

   그 밖(열려 있고 미확인, 또는 같은 서명으로 확인됨)은 다시 알리지 않고 **아무것도 쓰지 않는다**. `since`·`evidence`를 덮어쓰지 않는다.
3. **알릴 때 쓰기.** 문서를 새로 쓰거나 통째로 덮어쓴다. 새 문서는 `if_version` 없이, 기존 문서는 `get`의 `version`으로 `if_version`을 준다. `alert_id`·`dedupe_key`는 그대로 `<key>`이고 `since`는 새로 잡는다.
   ```
   ArtifactData(action="set", url="{{LEDGER_URL}}", collection="alerts", doc_id="<key>", if_version=<기존 문서일 때만>,
     data={"alert_id":"<key>","session_id":"<session_id 또는 null>","kind":"<kind>","since":"<now>",
           "evidence":"<한 줄: 관측값과 출처, 예: updated_at 42분 그대로 (get_session)>",
           "dedupe_key":"<key>","acked":false,"resolved_at":null})
   ```
   새 문서 쓰기가 "이미 있음"으로, 덮어쓰기가 버전 충돌로 거부되면 다른 쪽(콘솔·B층)이 먼저 쓴 것이다. 다시 읽고 2단계를 한 번 더 적용한다.
4. **해소.** 열린 경보(`resolved_at`이 없거나 null)인데 이번 점검에서 그 대상을 평가했고 조건이 더 이상 관측되지 않으면 `ArtifactData(action="update", url="{{LEDGER_URL}}", collection="alerts", doc_id="<key>", if_version=<version>, data={"resolved_at":"<now>"})`. 평가하지 못한 대상(다른 트리, 조회 실패)의 경보는 건드리지 않는다.

**상태 서명** — 콘솔이 확인 처리 때 `acked_for`에 쓰는 값과 같다:

| kind | 서명 |
|---|---|
| `blocked`·`failed`·`needs_action`·`stale` | 세션 `updated_at` |
| `cap_soft`·`cap_over`(세션·트리) | 그 행 `cap_usd`의 문자열(예 `"15"`) |
| `hub_rotate` | `"rotate"` |
| `orphan_trigger` | `"orphan"` |
| `trigger_failed` | `last_run.fired_at` |
| `loop_suspect`·`reply_overdue`·`orphan_child` | `""`(콘솔이 계산하지 않는 신호. 확인되면 해소될 때까지 다시 알리지 않는다) |

- 확인 처리(`acked`·`acked_at`·`acked_for`)는 콘솔에서 사람이 한다. 확인 처리만 할 때는 `since`·`evidence`를 덮어쓰지 않는다.
- 감지 시간 KPI는 `alerts.since`와 알림 시각의 차로 원시 기록한다.

## 5. 사용자 알림

- 허브 응답 맨 위에 `AOK 경보 (새 N건)` 절: 줄마다 kind · 세션 제목/ID · 증거 · 권하는 다음 행동. 새 경보가 없으면 한 줄 `감시: 활성 n, 새 경보 없음`.
- 열려 있고(`resolved_at` 없음) `acked:false`인 이전 경보는 끝에 `미확인 경보 m건 (콘솔 참조)` 한 줄로만 다시 언급한다. 다시 push하지 않는다.
- D4(정지·초과는 push): `blocked`·`failed`·`stale`·`cap_over`는 `PushNotification` 도구가 있고 T3 통과 기록(aok-delegate A.4)이 있을 때만 push도 보낸다.

## 6. B층 — 예약 감시기 (조건부)

### 6.1 무장 조건 (모두 참)

1. D7 사전 위임 기록(감사 `decision`/`D7`/`granted`)이 있거나, 사용자가 이 대화에서 B층 무장을 승인했다(그때는 D7 기록도 남긴다).
2. T1·T3 통과 기록(감사 `feasibility`)이 있다.
3. 사람이 1시간 넘게 자리를 비우는 무인 트리다(예: 야간 토폴로지 B 루프).
4. 이 트리에 이미 켜진 감시기가 없다(`triggers` 중 purpose `watch:<tree_id>`, `armed:true`).

### 6.2 무장

1. 프롬프트 조립: `aok-delegate` 3.5로 `child-base.md` + `watchdog.md`. 변수: `ROLE=watchdog`, `TASK_ID=wd-<YYYYMMDD>-<nnn>`, `CHILD_CAP_USD=1`, `REPLY_BY=n/a`, `TASK_DETAILS=(none)`, `TRIGGER_ID=PENDING`, `TREE_ID`, `HUB_SESSION_ID`, `LEDGER_URL`, `STALE_MINUTES=30`, `LOOP_COST_JUMP_USD=3`, `SOFT_RATIO=0.7`.
2. 생성:
   ```
   mcp__claude-code-remote__create_trigger(
     name="aok-watch <tree_id>",            # 콘솔의 고아 판정이 이름 접두어 aok-watch를 본다
     cron_expression="0 * * * *",           # 매시간. 서버가 생성 시각의 분으로 고정한다
     create_new_session_on_fire=true,       # push·email은 이 모드에서만
     notifications={"push": true},
     connectors=[],
     prompt="<조립한 프롬프트 원문>",
     initiation="human_request")            # 이 대화에서 사용자가 승인했을 때. D7 사전 위임만으로 켰으면 "own_followup"
   ```
   모델은 바꾸지 않는다(D8: Haiku는 사용자의 명시적 요청이 있을 때만). 결과에 "커넥터를 저장하지 않는다"는 경고가 있으면 사용자에게 그대로 알린다.
3. 받은 ID로 프롬프트를 다시 렌더링(`TRIGGER_ID=<trig_…>`)해 `update_trigger(trigger_id="<trig_…>", prompt="<새 원문>")`.
4. 원장: `triggers/<trig_…>` = `{"trigger_id":"<trig_…>","purpose":"watch:<tree_id>","armed":true,"last_audit":"<now>"}`, 감사 `create_trigger`.
5. 사용자에게: 매시간 실행, 활성 자식 0이 2회 연속이면 스스로 꺼짐, 콘솔 "예약 작업"에 보임.

### 6.3 해제

| 상황 | 동작 |
|---|---|
| 활성 자식이 2회 연속 0 | 감시기 세션이 스스로 `update_trigger(enabled=false)`(watchdog.md 3절 9단계) |
| 트리 종료 | 허브가 `update_trigger(trigger_id=<id>, enabled=false)`, 원장 `armed:false`, 감사 기록 |
| 사용자 요청 | 같음 |

**삭제하지 않는다.** 새 세션형 예약 작업을 지우면 그것이 만든 세션도 지워져 증거가 사라진다.

### 6.4 예약 작업 감사 (A층 9단계, 매 기상)

1. `list_triggers(enabled=true, limit=100)`, 원장 `triggers` 목록(`ArtifactData list`).
2. 각 켜진 예약 작업:
   - 원장 행이 있고 트리가 활성 → `last_audit` 갱신.
   - purpose `watch:<tree_id>`인데 그 트리가 `closed` → 6.3대로 비활성화(D7 범위), `orphan_trigger` 경보.
   - 원장에 없음 → `orphan_trigger` 경보만 쓰고 건드리지 않는다(사용자의 다른 작업일 수 있다). 사용자에게 끌지 묻는다.
   - `last_run.status`가 실패 → `trig:<trigger_id>:trigger_failed` 경보(4절)를 쓰고 사용자에게 알린다.
3. 원장에서 `armed:true`인데 목록에 없는 행(자기 비활성화, 발화한 일회성 wake) → `armed:false`.

## 7. 컨트롤 콘솔에 보이는 방식

`{{LEDGER_URL}}`의 콘솔 페이지(`dashboard/console.html`, `docs/dashboard.md`)는 원장을 실시간으로 읽고, 세션·예약 작업 상태는 보는 사람의 커넥터로 직접 조회한다.

| 콘솔 영역 | 이 스킬이 쓰는 것 |
|---|---|
| 주의 | 콘솔이 같은 신호를 직접 계산한다. 경보 키가 `<대상>:<종류>`로 같아서, 사용자가 콘솔에서 확인 처리하면 `alerts/<key>.acked`가 참이 되고(`acked_for` = 그때의 상태 서명) 허브는 상태가 바뀌거나 해소 뒤 재발할 때까지 다시 알리지 않는다. `resolved_at`이 찬 경보는 주의 목록에서 빠진다 |
| 세션 | 원장 `sessions`(역할·트리·상한)로 상한 막대를 그린다 → 모든 위임을 원장에 등록해야 보인다 |
| 트리·예산 | `trees`와 등록 세션의 비용 합. 사용자가 콘솔에서 상한을 바꾸거나 트리를 닫을 수 있다 → 허브는 매 기상 `trees` 행을 다시 읽고 따른다 |
| 예약 작업 | `list_triggers` + `triggers`. 이름이 `aok-watch`로 시작하는 켜진 작업은 활성 AOK 세션이 0이면 고아 의심으로 표시된다 |
| 감사 기록 | `audit/<YYYY-MM-DD>.hub`·`.console`(B층 감시기는 `.watchdog`) 문서의 최근 40건 |

허브는 `acked`를 직접 참으로 바꾸지 않는다. 콘솔 수치는 실시간 조회이고, 원장 `spent_usd`는 마지막 진전 시점 값이다.

## 8. 하지 말 것

- 폴링 루프로 감시하기(감시는 기상 시점과 B층에서만)
- 열린 경보를 다시 보내기, `acked`를 허브가 바꾸기, 확인 처리만으로 `since`·`evidence` 덮어쓰기
- 승인 대기를 대리 승인하기, 세션 보관하기, 기본 정책(`notify`)에서 중단하기
- 감시 예약 작업 삭제하기, 원장에 없는 예약 작업을 임의로 끄기
- D7·T1·T3 없이 B층 켜기, 사용자 요청 없이 감시 모델 바꾸기
- `list_sessions`에 태그 필터 파라미터 넘기기, `list_events`에 `kinds` 필터를 쓰고 빈 페이지에서 멈추기
