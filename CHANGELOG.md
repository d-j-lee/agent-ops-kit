# Changelog

버전은 SemVer를 따른다. 이력 근거는 `reports/`에 둔다.

## [Unreleased]

### 추가

- 관제실 "당신 차례" 인박스(관제탑 v2 MVP-1.1): 모든 세션의 메타데이터로 권한 승인·병합·결정 대기, 실패, 정지, 사람 후속, 검토 방치, 맥락 포화, 같은 레포 동시 작업, 보관 후보를 우선순위대로 모은다. 처리 상태는 원장 `inbox_state`
- 관제실 협업 타일: AI 작업 중, 사람 응답 대기 최장(중앙값), 숨은 후속
- 인박스 MVP-1.2(적대 검토 반영): 사람 후속을 `status_detail`·`recent_action` 두 필드로 판정하고 완료 표현을 먼저 제거, 14일 뒤 "놓친 후속", 안 읽은 결과(`unread`), REQUIRES_ACTION 승인, 대체 추정, 7일 감쇠, 같은 레포는 48시간 안 세션만, 보관 후보에서 안 읽은 세션 제외와 손실 경고, 무시 사유(과민·이미 처리·무관), 세션 목록 100개
- `inbox_state.dismiss_reason`
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
