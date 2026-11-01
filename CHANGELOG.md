# Changelog

버전은 SemVer를 따른다. 이력 근거는 `reports/`에 둔다.

## [Unreleased]

### 추가

- 작업공간 측정(관제탑 v2 Rust 적용 R1). 엔진은 사용자의 Rust CLI fsmap(`d-j-lee/filesystemMap`)이다.
  - `scripts/atlas_record.py`: fsmap 스냅숏(`fsmap/1`)을 원장 레코드 `atlas/<session_id>`(`aok-atlas/1`)로 줄인다. 표준 라이브러리만 쓴다. fsmap이 놓치는 원격에 없는 커밋(detached HEAD, 업스트림이 지워진 브랜치)을 git으로 대조하고, 측정의 빈틈을 `coverage`로 남긴다
  - 스킬 `aok-atlas`: 고정 커밋 설치, 스캔, 변환, 원장 쓰기, 결과별 조치, 해석의 한계
  - 원장 스키마 `#/$defs/atlas`
  - 관제실 콘솔 v12:
    - 미저장 산출물을 측정으로 판정한다. 측정 78, 문구 추정 75, 빈틈 없는 깨끗한 측정과 어긋나는 문구 50. 측정은 세션의 마지막 변화보다 10분 넘게 앞서면 깨끗함의 근거로 쓰지 않는다
    - 한 세션에 항목 하나: 실패에는 측정 근거를 붙이고, 사람 후속·안 읽음은 측정 항목의 메모로 남긴다
    - 원장 행을 정규화한다(타입 강제, ID 불일치·형식 오류 행은 버리고 진단에 기록). 깨진 행 하나가 인박스를 지우던 문제를 막는다
    - 측정한 휘발 세션은 보관 후보에서 빼고, 현재 측정이 깨끗한 세션 수를 보관 후보에 적는다
    - 작업공간 카드(세션별 저장소 띠), 세션 상세의 작업공간 지도, "컨테이너에만 있는 작업" 타일
  - 시험: `tests/test_atlas.py`(변환·스키마·빈틈, 실제 fsmap으로 로컬 전용 작업 종류별), `tests/console/atlas_cases.json` 19건과 변이 7종
  - CI `atlas` 잡: 고정 커밋 fsmap을 빌드해 통합 시험을 돌린다
- 관제탑 v2 §4a Rust 적용 계획(R1 구현, R2 하네스 게이트 제안, R3 규칙 코어 씨앗)과 결정 D22–D24. 작업공간 지도 §3a 엔진 결정
- 관제실 콘솔 v11. 실제 런타임에서 연결이 막히던 문제를 고쳤다.
  - 연결: 내장 `permissions`로 `mcp:Claude Code Remote` 상태 확인 → 필요하면 시작 때 한 번 `request` → `callTool`로 첫 조회 → 그다음 `watchTool`. watch는 동의를 묻지 않기 때문이다
  - 오류 코드별 안내와 버튼: 연결 허용, 권한 설정 열기(`permissions.manage`), 다시 시도
  - 커넥터를 실행할 수 없는 보기(모바일 앱, 미리보기)에서는 보는 사람 전용 원장 경로 `data/users/<id>/snapshot`을 읽기 전용으로 보여 준다. 권한 거부·정책 차단 때는 보여 주지 않는다. 조작과 세션 등록 변경은 막힌다
  - 첫 조회의 성패와 상관없이 watch를 시작해 일시 오류에서 스스로 복구한다. 동의는 시작 때 한 번만 묻고, 버튼은 `callTool`을 정확히 한 번 한다
  - `get_session` 보강은 일시 오류에서 그 회차를 멈추고 나중에 다시 한다(반복 호출 없음). 원장 구독은 `unavailable`에서 다시 구독한다
  - 인박스 항목에 원장 검증 기록 배지(예: V1 일치)
  - 세션 등록을 갱신할 때 보강되지 않은 비용(null)이나 허브가 정한 보관 상태를 덮어쓰지 않는다
  - 연결 진단: 화면 맨 아래에 표시하고, 열고 20초 뒤 원장 `diag/<날짜>.<보기 종류>`에 저장
  - 좁은 화면: 640px 이하는 띠 고정 해제와 두 열 타일, 760px 이하는 인박스 버튼을 본문 아래로. 버튼 열은 폭의 42% 이하
  - `get_session` 응답이 예상과 다르면 그 결과도 캐시해 반복 호출을 막는다
- 원장 스키마: `snapshot_sessions`, `diag`, `handoff`. `verifications`에 v2 사다리 수준(V0–V5)과 `session_id`·`verdict`·`ref`
- 시험: 스냅숏 대체 화면이 실시간과 같은 분류를 내는지(`test_snapshot_fallback_without_connector`)
- CI(`.github/workflows/ci.yml`): PR과 main에서 단위 시험·공개 레포 스캔(`checks`), 헤드리스 Chromium 콘솔 시험(`console`)
- 관제실 "당신 차례" 인박스(관제탑 v2 MVP-1.1): 모든 세션의 메타데이터로 권한 승인·병합·결정 대기, 실패, 정지, 사람 후속, 검토 방치, 맥락 포화, 같은 레포 동시 작업, 보관 후보를 우선순위대로 모은다. 처리 상태는 원장 `inbox_state`
- 관제실 협업 타일: AI 작업 중, 사람 응답 대기 최장(중앙값), 숨은 후속
- 인박스 MVP-1.2(적대 검토 반영): 사람 후속을 `status_detail`·`recent_action` 두 필드로 판정하고 완료 표현을 먼저 제거, 14일 뒤 "놓친 후속", 안 읽은 결과(`unread`), REQUIRES_ACTION 승인, 대체 추정, 7일 감쇠, 같은 레포는 48시간 안 세션만, 보관 후보에서 안 읽은 세션 제외와 손실 경고, 무시 사유(과민·이미 처리·무관), 세션 목록 100개
- `inbox_state.dismiss_reason`
- 인박스 MVP-1.3: `list_sessions`가 가벼운 레코드만 주게 된 변화(2026-10 말)에 맞춰 인박스 후보를 `get_session`으로 보강(최대 30개, 동시 3개, `updated_at` 캐시). 보강이 실패하면 꺼진 신호를 표시한다. `status_category=need_input`을 결정 대기로, 미저장 산출물(IN24, `volatile`)을 새 종류로 추가
- 관제실 권한: `get_session` 추가, 원장 읽기를 `admin`으로(v2 D15)
- 홀드아웃 시험 `tests/test_console_inbox.py`, 실행기 `tests/console/run_inbox_cases.cjs`, 합성 사례 `tests/console/inbox_cases.json`. node·playwright가 없으면 건너뛴다
- `schemas/ledger.schema.json`: `inbox_state` 정의
- 제안서 `docs/proposals/control-tower-v2.md`, 다음 과제 초안 `docs/proposals/workspace-atlas.md`, 이력 `reports/2026-10-21-v2-design-crosscheck.md`

### 변경

- 관제실 "주의" 섹션을 "운영 경보"(트리·예산·예약 작업·허브 기록)로 좁히고, 세션 단위 신호는 인박스로 옮겼다
- 판단 축을 협업 효용으로 바꿨다. "24시간 비용" 타일을 없애고 비용은 참고 수치로만 표시한다

## [1.0.0] — 2026-10-21

설계 확정 2026-10-20, 키트 골격 2026-10-21. 첫 릴리스.

### 추가

- 규범 문서: `docs/architecture.md`, `docs/protocol-aok1.md`, `docs/patterns/{brd,watchdog,cleanroom,loop,curator,budget}.md`, `docs/rollout.md`, `docs/dashboard.md`
- `CLAUDE.md`: 수단 선택 규칙, 안전 규칙 S1–S9, 공개 레포 규칙
- 계약: `schemas/{aok1,tree,ledger,claim,card,seed,cleanroom-report}.schema.json`
- 스킬 `.claude/skills/aok-{delegate,verify,cleanroom,loop,watch,garden}`, 서브에이전트 `.claude/agents/{verifier,adjudicator}`
- 자식 템플릿 `templates/system-prompts/{child-base,dev,reviewer,cleanroom,repro-l2,watchdog,curator}.md`
- 스크립트 `scripts/{sanitize_packet,harvest_events,secret_scan}.py`
- 예산 기본값 `aok/budget.yaml`(D3 권장값, 모두 [추론])
- 관제실 콘솔 `dashboard/console.html`
- `knowledge/` 형식·템플릿(실제 카드 없음)
- 이력: `reports/2026-10-20-field-test.md`, `reports/2026-10-20-plan-review.md`

### 상태

- v1 범위: ①, ⑥, ② A층, ④, ③(요청 시), ⑤(수동)
- 조건부(꺼짐): ② B층, ③ 야간, ⑤ 예약. 1단계 실현성 시험 T1–T6 미실시
- 결정: D1(키트 위치 = 이 레포)만 확정

### 근거

- 실측: [reports/2026-10-20-field-test.md](reports/2026-10-20-field-test.md)
- 확정안 검증(L1 블라인드 설계 대조, L0 적대적 검토): [reports/2026-10-20-plan-review.md](reports/2026-10-20-plan-review.md)
