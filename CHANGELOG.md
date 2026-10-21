# Changelog

버전은 SemVer를 따른다. 이력 근거는 `reports/`에 둔다.

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
