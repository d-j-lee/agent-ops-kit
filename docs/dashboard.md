# AOK 관제실 (컨트롤·모니터링 콘솔)

AOK 운영을 한 화면에서 **보고(모니터링)** 바로 **조작(컨트롤)** 하는 claude.ai Artifact 페이지다. 소스는 `dashboard/console.html`이다. 게시된 콘솔의 URL은 이 공개 레포에 적지 않는다. 소유자의 개인 메모나 허브 세션에서 관리한다(`<CONSOLE_URL>`).

## 1. 화면 구성

| 영역 | 보여 주는 것 | 데이터원 |
|---|---|---|
| 상태 띠 | "이상 없음 / 확인 권장 N / 주의 필요 N · 긴급 M", 세션 기준 시각 | 계산값 |
| 요약 타일 | 주의 필요 · 작업 중 · 막힘 · 24시간 활동 세션의 누적 비용 합 · 켜진 예약 작업 | 커넥터 + 원장 |
| 주의 | 감지 신호 목록(아래 §3). 열기, 중단, 확인 처리 | 계산값 + `alerts` |
| 세션 | 최근 60개 세션: 상태, 비용(상한 막대), 맥락, 마지막 변화, 요약. 범위(전체/AOK 관리/주의 필요), 검색, 보관 포함 | `list_sessions` + `sessions` |
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
    "list_sessions", "list_triggers", "list_events",
    "archive_session", "unarchive_session", "interrupt_session",
    "send_message", "update_trigger", "set_session_tags"]}]},
  "db": {"rules": [{"path": "", "read": "view", "write": "admin"}]},
  "user": {}
}
```

- **커넥터 호출:** 모든 호출은 **보는 사람의 권한**으로 실행된다. 첫 호출에서 claude.ai가 허용 여부를 묻는다.
  - 조회(`list_*`)는 `watchTool`로 1–2분마다 갱신한다.
  - 조작은 `callTool` 1회로 실행하고, 캐시하지 않는다.
- **원장 권한:** 읽기는 보는 사람 누구나(`view`), 쓰기는 편집자 이상(`admin`)이다. 조작 버튼은 편집 권한이 있을 때만 켜진다.
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
| `audit/<YYYY-MM-DD>.<actor>` | 허브(`.hub`), 콘솔(`.console`) | 하루·주체별 조작 기록(문서당 최대 500건) |
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
