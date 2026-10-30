# AOK 관제실 (컨트롤·모니터링 콘솔)

AOK 운영을 한 화면에서 **보고(모니터링)** 바로 **조작(컨트롤)** 하는 claude.ai Artifact 페이지다. 소스는 `dashboard/console.html`이다. 게시된 콘솔의 URL은 이 공개 레포에 적지 않는다. 소유자의 개인 메모나 허브 세션에서 관리한다(`<CONSOLE_URL>`).

## 1. 화면 구성

| 영역 | 보여 주는 것 | 데이터원 |
|---|---|---|
| 상태 띠 | "이상 없음 / 확인 권장 N / 주의 필요 N · 긴급 M", 세션 기준 시각 | 계산값 |
| 요약 타일 | 당신 차례 · AI 작업 중(막힘 수 포함) · 사람 응답 대기 최장(중앙값) · 숨은 후속 · 켜진 예약 작업. 협업의 공이 누구에게 있는지를 보여 준다. 비용은 세션 표와 트리·예산의 참고 수치로만 둔다 | 커넥터 + 원장 |
| **당신 차례** | 모든 세션(사람이 연 세션 포함)에서 사람이 결정·처리할 일을 우선순위 순으로. 세션 열기(새 탭), 상세, 내일 다시, 처리함, 보관 후보 선택 | `list_sessions`(status_bucket, needs_action, status_detail, 비용·맥락) + `inbox_state` |
| 운영 경보 | AOK 트리·예산·예약 작업·허브 교대와 허브가 기록한 경보. 세션 단위 신호는 "당신 차례"로 옮겼다 | 계산값 + `alerts` |
| 세션 | 최근 100개 세션: 상태, 비용(상한 막대), 맥락, 마지막 변화, 요약. 범위(전체/AOK 관리/주의 필요), 검색, 보관 포함 | `list_sessions` + `sessions` |
| 세션 상세 | 상태 정보, 보관·되살리기·중단, 메시지 보내기(우선순위 선택), 태그 추가·제거, 원장 등록(트리·역할·상한), 최근 결과 3건 | 커넥터 + 원장 |
| 트리·예산 | 트리별 사용/상한 막대(70% 표시), 상한 수정, 닫기·다시 열기, 새 트리, 누적 비용 상위 세션 | `trees` + 계산값 |
| 예약 작업 | 이름, 일정, 상태, 다음·최근 실행, 켜기·끄기 | `list_triggers` + `triggers` |
| 검증 기록 | 검증 실행 수, 지적, 확정 오류, 검증 불가, 토큰(KPI)과 실행별 표 | `verifications` |
| 감사 기록 | 콘솔과 허브가 한 조작 최근 40건 | `audit` |
| 운영 기준 | 감지·예산 임계값 편집 | `config/policy` |

## 2. 런타임 권한 (게시 시 선언)

```json
{
  "mcp": {"servers": [{"server": "Claude Code Remote", "tools": [
    "list_sessions", "get_session", "list_triggers", "list_events",
    "archive_session", "unarchive_session", "interrupt_session",
    "send_message", "update_trigger", "set_session_tags"]}]},
  "db": {"rules": [{"path": "", "read": "admin", "write": "admin"}]},
  "user": {}
}
```

- **커넥터 호출:** 모든 호출은 **보는 사람의 권한**으로 실행된다. 첫 호출에서 claude.ai가 허용 여부를 묻는다.
  - 조회(`list_*`)는 `watchTool`로 1–2분마다 갱신한다.
  - 조작은 `callTool` 1회로 실행하고, 캐시하지 않는다.
- **원장 권한:** 읽기와 쓰기 모두 편집자 이상(`admin`)이다(v2 D15, 2026-10-30부터). 원장에는 경보 근거, 감사 기록, 인박스 상태처럼 다른 세션의 문구가 쌓이므로, 콘솔을 보기 전용으로 공유해도 원장은 보이지 않게 했다. 조작 버튼은 편집 권한이 있을 때만 켜진다.
- **세션 생성 제외:** `create_session`과 `create_trigger`는 일부러 넣지 않았다. 위임은 허브의 입장 검사(⑥)를 거쳐야 하기 때문이다.

## 3. 감지 신호 (`aok-watch` A층과 같은 기준)

종류(kind) 어휘는 `schemas/ledger.schema.json`의 `alerts.kind`가 기준이다. 콘솔은 그중 `loop_suspect`·`reply_overdue`·`orphan_child`를 직접 계산하지 않는다. 이 셋은 허브의 `aok-watch`가 원장 `alerts`에 쓴다.

| 신호 | 등급 | 조건 |
|---|---|---|
| `blocked` | 긴급 | `status_bucket=BLOCKED` |
| `failed` | 긴급 | `status_bucket=FAILED` |
| `needs_action` | 경고 | `post_turn_summary.needs_action`이 비어 있지 않음 |
| `stale` | 경고 | WORKING인데 `updated_at`이 `stale_minutes`(기본 30) 넘게 그대로. 긴 명령 실행 중에도 갱신되는지는 [미확인](T4) |
| `cap_over` / `cap_soft` | 긴급 / 경고 | 세션: 원장에 등록된 세션 상한 대비 100% / `soft_ratio`(0.7). 트리(대상 `tree:<tree_id>`): 허브를 포함한 등록 세션의 누적 비용 합 대비 |
| `hub_rotate` | 경고 | 역할 hub이고 맥락 ≥ 250k 또는 비용 ≥ $25 |
| `orphan_trigger` | 경고 | 이름이 `aok-watch`로 시작하고(`aok-watch <tree_id>`) 켜진 반복 작업인데 활성 AOK 세션이 0 |
| `trigger_failed` | 경고 | 마지막 실행이 FAILED |

- **중복 제거:** 같은 신호는 `dedupe_key = <대상>:<종류>`로 한 번만 표시한다.
- **확인 처리:** `alerts/<dedupe_key>`에 `acked: true, acked_at, acked_for: <당시 상태 서명>`을 쓴다. `since`와 `evidence`는 바꾸지 않는다. 상태가 바뀌거나(`updated_at`, 상한) 허브가 `resolved_at`을 기록한 뒤 다시 발생하면 새 회차로 열린다. 허브가 기록한 경보(`loop_suspect`·`reply_overdue`·`orphan_child` 등)도 확인·해소 전까지 "허브 기록"으로 표시한다.

## 3a. "당신 차례" 규칙 (관제탑 v2 MVP-1.2, 규칙층)

대화 기록을 읽지 않고 세션 메타데이터만으로 계산한다. 설계 근거는 `docs/proposals/control-tower-v2.md` §6이다. 문구 판정은 모두 키워드 추정이며, 작업 카드가 생기면 그쪽이 우선한다.

| 종류(kind) | 조건 | 점수 |
|---|---|---|
| 권한 승인 대기(`approval`) | BLOCKED이거나 `needs_action`이 있고, `session_status`가 REQUIRES_ACTION이거나 문구가 "Approve or deny…", "Waiting on permission…", 권한 요청 표현에 맞음. PR·시안의 "승인 대기"는 결정으로 본다 | 95 + 대기 가산 − 감쇠 |
| 병합 대기(`merge`) | 위와 같은 조건에서 `needs_action`이 PR·버튼·커밋과 함께 merge·병합을 말함 | 90 + 대기 가산 − 감쇠 |
| 결정 대기(`decision`) | BLOCKED이거나, `needs_action`이 있거나, `status_category`가 `need_input`임(위 두 종류 제외) | 85 + 대기 가산 − 감쇠 |
| 미저장 산출물(`volatile`) | COMPLETED, 30일 이내, `needs_action`·`status_detail`·`recent_action`이 산출물이 컨테이너에만 있다고 말함("휘발", "컨테이너에만", "로컬 커밋", "not pushed", "unpushed" 등). "no unpushed commits", "pushed to origin" 같은 부정·완료 표현은 먼저 지운다. 결정류 항목에 이 표현이 있으면 그 항목에 +10과 경고를 붙인다 | 75 |
| 마지막 턴 실패(`failed`) | FAILED | 80 |
| 정지 의심(`stall`) | WORKING인데 `stale_minutes` 넘게 그대로 | 70 |
| 사람 후속(`followup`) | COMPLETED이고 `status_detail` 또는 `recent_action`이 사람 몫을 말함("사용자 몫·확인·설치", "남음", "결정 N건", "확인 필요", "새 세션에서 확인", "left to …", "your review" 등). "병합 완료", "Nothing left to do" 같은 완료 표현은 먼저 지운다 | 14일 이내 60, 그 뒤 40("놓친 후속") |
| 안 읽은 결과(`unread`) | COMPLETED, 14일 이내, 세션 레코드의 `unread`가 참이고 후속 표시는 없음 | 55. 필드 의미는 추정 |
| 검토 대기 방치(`review_stale`) | REVIEW_READY이고 72시간 초과 | 50 + 대기 가산/3 |
| 맥락 포화(`context`) | 맥락 600k 이상(완료 세션 제외). 100k 늘 때마다 다시 연다 | 45 |
| 같은 레포 동시 작업(`same_repo`) | 같은 레포에서 48시간 안에 움직인 WORKING·BLOCKED 세션이 2개 이상 | 35 |
| 보관 후보(`hygiene`) | 끝난 지 7일 넘음, 후속 표시 없음, `unread` 아님, 미저장 표현 없음 | 10 |

- **용어와 판정 순서:**
  - 결정류는 approval·merge·decision이다.
  - 한 세션은 approval → merge → decision 순서로 처음 맞는 하나가 된다. `needs_action`이 있으면 상태가 COMPLETED여도 결정류가 먼저다. 그때 미저장 표현은 경고와 +10으로만 붙는다.
  - "끝난 세션"은 COMPLETED만 뜻한다. REVIEW_READY와 FAILED는 끝난 것으로 보지 않고, 보관 후보에도 들지 않는다. FAILED는 감쇠 없이 80점을 유지한다. 실패는 사람이 확인하기 전까지 남겨 둔다.
  - 후속 표시는 사람 후속과 미저장 산출물을 함께 뜻한다.
  - 정지 기준 `stale_minutes`의 기본값은 30분이며 운영 기준(`config/policy`)에서 바꾼다.
- **대기 가산:** `min(15, 3·log2(대기 시간 + 1))`. 대기 시간은 시간 단위이며 `updated_at`부터 잰다. AI가 사람의 답을 오래 기다릴수록 위로 올린다. 비용은 점수에 넣지 않는다(참고 수치).
- **감쇠:** 7일이 지나면 하루에 2점씩, 최대 40점 내린다. 7일 경계에서 점수가 갑자기 떨어지지 않게 하려는 것이다.
- **대체 추정:** 72시간 넘게 기다린 결정류 항목이 있는데, 같은 레포에 그 마지막 변화 이후 만든 세션(보관된 세션은 근거로 쓰지 않는다)이 있으면 "대체됐을 수 있음"으로 표시하고 30점 내린다. 이런 항목은 "사람 응답 대기" 타일에서 뺀다.
- **보관된 세션은 제외한다.** 보관하면 버킷이 COMPLETED로 바뀌어도 예전 `needs_action`이 남는다 [기록됨: 비공개 스냅숏의 보관 세션 3건].
- **보관의 손실:** 보관하면 컨테이너가 반납된다. 되살리면 새 컨테이너가 붙는다 [문서: 도구 설명]. 푸시·업로드하지 않은 파일은 사라질 수 있으니 보관 후보에 이 경고를 붙인다.
- **설정·권한 변경 요청 표시:** `needs_action`에 `/config`, `/permissions`, allowlist, settings.json, permission rule·mode, auto mode, 권한 모드·규칙, "설정을 변경·꺼" 같은 표현이 있으면 문구를 붙인다. 콘솔은 조치하지 않는다(S4·S8).
- **출처 칩:** 부모가 있거나 `origin`이 MCP 생성이면 "자식", 아니면 "사람".
- **처리 상태:** 처리함·무시는 그 항목의 상태 서명에 묶인다. 서명은 세션 항목이면 `updated_at`, 묶음 항목이면 세션 ID 목록의 해시, 맥락 항목이면 100k 단위다. 서명이 바뀌면 다시 열린다. 내일 다시는 24시간 동안 숨긴다. 무시할 때는 이유(과민·이미 처리·무관)를 남기고, 이 기록은 임계값 조정을 **제안**하는 데만 쓴다.
- **데이터 원천과 보강:** 2026-10 말부터 `list_sessions`는 가벼운 레코드만 준다. `session_context`(레포·모델), `external_metadata`(맥락·사용량), `status_detail`, `status_category`가 빠졌다 [확인: 2026-10-30, limit 3·100]. `get_session`은 전체 레코드를 준다 [확인]. 그래서 콘솔은 인박스에 들 수 있는 세션(보관 제외, 대기·작업 중·검토·실패 상태, `needs_action`, `unread`, 14일 안에 움직임)을 최근순으로 최대 30개 골라 `get_session`으로 보강하고, `updated_at`이 바뀔 때까지 캐시한다. 동시 호출은 3개다. 보강이 실패하면 레포·맥락에 기대는 신호(IN09·IN11·IN23)가 꺼지고, 인박스 도구 줄에 그 사실을 표시한다. 보강 대상이 아닌 세션(14일이 지난 완료 세션 등)은 `status_detail`·`status_category` 없이 `needs_action`·`recent_action`만으로 판정한다. 그래서 14일이 지난 "놓친 후속"과 15–30일 된 미저장 산출물은 `recent_action`에 그 표현이 있을 때만 잡힌다. 보강되지 않은 세션은 레포가 없으므로 대체 추정의 근거가 되지 못한다.
- **보는 범위:** `list_sessions` 최근 100개. Cowork 세션과 예약 작업이 띄운 세션은 기본 목록에 나오지 않는다 [문서]. 100개를 넘는 오래된 세션은 보이지 않는다.
- **세션 열기:** `https://claude.ai/code/<session_id>` 링크다. 형식은 플랫폼이 세션 링크로 쓰는 표기를 따랐다 [문서]. 다른 세션에서도 열리는지는 첫 사용 때 확인한다 [미확인].
- **결정 위치:** 사람이 연 세션의 결정은 그 세션에서 사람이 직접 한다. 콘솔은 안내만 한다.

## 4. 조작의 안전장치

- **두 번 확인:** 모든 조작은 두 번 눌러야 실행된다. 첫 클릭으로 무장하고, 5초 안에 다시 누르면 실행한다. 페이지에서는 `confirm()`을 쓸 수 없어 이렇게 만들었다.
- **결과가 불확실할 때:** 쓰기 조작이 `server_unavailable` / `upstream_error`로 실패하면 **실행됐는지 불확실하다고 표시**하고 다시 시도하지 않는다. 새로고침으로 상태를 확인한다.
- **감사 기록:** 모든 조작과 원장 변경은 `audit/<YYYY-MM-DD>.console` 문서(`{day, actor: "console", entries: [{at, actor, action, target, result, detail}]}`)에 남는다. 허브는 `audit/<YYYY-MM-DD>.hub`에 쓴다. 주체마다 문서를 나눠 서로 덮어쓰지 않는다.
- **일괄 보관:** 작업 중(WORKING)이 아니고 보관되지 않은 세션만 선택할 수 있다.
- **오작동 방지:** 무장 후 400ms 안의 두 번째 클릭(더블클릭)은 무시한다. 포인터를 누르고 있거나 입력하는 동안에는 화면을 다시 그리지 않고, 입력값은 다시 그려도 유지된다. 태그 추가·제거도 두 번 눌러야 실행된다.
- **원장 등록:** 등록할 때 트리를 반드시 고른다(모든 위임은 트리에 속한다). 콘솔에서 만든 트리는 `hub_session: null`(허브 미지정)로 시작하고, 허브가 인수하면 채운다.
- **메시지:** 우선순위는 `later`(기본)와 `now`(중단 목적)만 고를 수 있다. 콘솔에서 보낸 메시지가 자식에게 어떤 발신자로 보이는지는 [미확인]이다. 자식은 이를 데이터로만 취급할 수 있으니(S5), 멈추려면 중단 버튼을 쓴다.
- **최근 결과:** 다른 세션이 쓴 텍스트이므로 `textContent`로만 표시하고 데이터로만 읽는다(S5).

## 5. 원장 (이 콘솔의 db = AOK 원장)

컬렉션과 필드는 `schemas/ledger.schema.json`을 따른다.

| 경로 | 쓰는 쪽 | 내용 |
|---|---|---|
| `trees/<tree_id>` | 허브(`aok-delegate`), 콘솔 | 트리 상한과 상태 |
| `sessions/<session_id>` | 허브, 자식(종료 시), 콘솔(등록) | 역할, 트리, 상한, 판정 |
| `alerts/<dedupe_key>` | 허브(`aok-watch`), 콘솔(확인 처리) | 경보와 확인 상태 |
| `verifications/<run_id>` | 허브(`aok-verify`) | 검증 실행 결과 |
| `triggers/<trigger_id>` | 허브 | 예약 작업의 목적과 무장 상태 |
| `audit/<YYYY-MM-DD>.<actor>` | 허브(`.hub`), 콘솔(`.console`), 감시기(`.watchdog`) | 하루·주체별 조작 기록(문서당 최대 500건) |
| `inbox_state/<item_key>` | 콘솔 | "당신 차례" 항목에 대한 사람의 처리 상태(미룸·처리함·무시) |
| `config/policy` | 콘솔 | 운영 기준(임계값) |

허브나 자식 세션은 `ArtifactData` 도구에 `url=<CONSOLE_URL>`을 넘겨 쓴다. 기존 문서를 고칠 때는 읽은 `version`을 `if_version`으로 넘긴다.

## 6. 재배포

- **같은 대화에서:** `Artifact(action: "publish", file_path: "dashboard/console.html")`. URL이 유지되고 원장도 보존된다.
- **다른 세션에서:** 먼저 `Artifact(action: "read", url: <CONSOLE_URL>)`로 읽은 뒤 같은 `url`로 게시한다. `capabilities`를 생략하면 기존 선언이 유지된다.
- **커넥터 도구를 바꿀 때:** `capabilities`를 전체 집합으로 다시 넘긴다. 일부만 넘기면 빠진 것은 철회된다.

## 7. 미확인 (첫 사용 때 확인)

- 페이지에서 보낸 `send_message`가 받는 세션에 어떤 발신자로 보이는지. 호출 세션이 없는 경로다.
- `list_sessions` 응답에 다른 세션 데이터의 신뢰 경계 표식이 붙는지 여부. 콘솔은 JSON 객체와 감싼 텍스트를 모두 해석한다.
- 권한 승인 대기 세션이 BLOCKED로 표시되는지(T2).
- `updated_at`이 긴 명령 실행 중에 갱신되는지(T4).
