# Changelog

버전은 SemVer를 따른다. 이력 근거는 `reports/`에 둔다.

## [Unreleased]

### 추가

- 관제실 콘솔 v13: 실제 모바일 런타임 기록(원장 `diag/`)에 맞춘 대응. 모바일 브라우저에서 `list_sessions`가 `blocked_by_policy`로 거부됐다.
  - 세션 정보를 읽지 못하는 보기에서 "당신 차례"에 원장 `handoff/current`의 사람 몫과 대기 중인 결정을 보여 준다. 커넥터 데이터가 아니므로 거부 상황에서도 보인다. 스냅숏은 계약대로 계속 숨긴다
  - `blocked_by_policy` 안내를 플랫폼 문구에 맞췄다("이 화면에서 쓸 수 없거나 조직 정책이 막음"). PC에서 열기를 권한다
  - 시험: 거부 모드(`policy`). 스냅숏이 거부 뒤에 도착해도 보이지 않는지, 사람 몫만 중요도 순으로 보이는지 확인한다. 변이 시험으로 스냅숏 누출을 잡는지도 확인했다
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
- ⑦ 회귀 고정(D25): 런북 `docs/patterns/regression.md`, 스킬 `.claude/skills/aok-regress`, 도구 두 개
  - `scripts/diffcheck.py`: 같은 입력을 기준·후보 명령에 넣어 종료 코드와 stdout(`--stderr`면 stderr도)을 바이트로 비교한다. 빈 출력(공백뿐이거나 정규화 뒤 `<N>`만 남은 것 포함, `--stderr`면 stdout과 stderr를 합쳐서)·0이 아닌 종료·시간 초과·실행마다 다른 결과(`--determinism`)·정규화를 쓸 때 출력에 글자 그대로 있는 `<N>`은 실패. 양쪽의 정규화 횟수가 다르면 `differs`. `--allow-fail`이어도 종료 코드는 비교한다. 정규화는 횟수 보고, `{nonce}`로 실행마다 새 키(`--same-nonce`는 한 쌍에 같은 키), `--timing`은 번갈아 측정. 실행할 수 없으면 종료 코드 2
  - `scripts/vacuity_check.py`: 레포 사본에서 보호 코드를 바꿔 테스트가 실패하는지 확인. 변이는 정확히 한 곳, 사본 안에만 쓴다(사본 밖으로 가는 심볼릭 링크는 거부). 컴파일되지 않는 `.py` 변이(`invalid`)·시간 초과(`timeout`)·`expect`(낱말 단위로 맞춤, 빈 문자열은 사용 오류)에 없는 실패(`other_failure`)는 통과가 아니다. `--jobs`로 병렬. 읽을 수 없는 입력과 범위 밖 `--timeout`은 종료 코드 2
  - CI 자기 시험: 변이 목록에 올린 장치를 하나씩 끈 변이 130개(diffcheck 33, vacuity_check 25, build_site 72), 변이마다 잡아야 할 시험 이름(`tests/mutations/*.json`)
- ⑧ 작업 레포 하네스(D26): 런북 `docs/patterns/harness.md`, 스킬 `.claude/skills/aok-harness`, 코드 `harness/`
  - `harness/aok_harness.py`: 언어 중립 코어. Claude Code 훅 다섯 개(세션 시작 기준 기록, 편집 직후 검사, 턴 종료 게이트, 사용자 입력 시 막힘 횟수 초기화, 셸 명령 검사), 단일 진입점 `verify`(종료 0/1/2, `--base`로 기준 커밋의 정의와 어댑터), `install`(`--yes` 없이는 계획만, S4. 모양이 다른 설정·다른 어댑터는 쓰기 전에 거부, PreToolUse에 `onFailure: "block"`)
  - 게이트 정의와 어댑터 사본은 세션이 시작된 커밋에서 읽고(기준 커밋에 없는 정의는 쓰지 않음), 실행 불가·손상되거나 없는 세션 상태·fsmap의 0·2 아닌 종료·하네스 오류는 통과로 치지 않는다. 연달아 3번 막고 세 번째에 마지막임을 알린 뒤 놓아 준다. 게시는 체크아웃한 브랜치만, 같은 명령의 HEAD·브랜치 이동 없이, 하네스·설정·리뷰어·CI를 바꾸지 않은 세션에서만 허용하고, git 훅을 끄는 명령은 줄임·따옴표로 쪼갠 꼴까지 거부한다(래퍼·`$(…)`·`sh -c`·줄바꿈 안의 호출도 찾음). `git push`의 결정은 세션 시작 때 git 디렉터리에 까는 pre-push 훅이 한다: Claude Code 안에서는 보내는 ref의 끝 커밋이 게이트가 깨끗한 트리에서 통과시킨 커밋이어야 나간다. 그 훅이 보는 값을 바꾸는 명령(`CLAUDECODE`, `env -i`, `GIT_CONFIG_*`, 훅 파일, 셸 별칭)과 훅을 거치지 않는 `send-pack`은 거부하고, 바뀐 훅은 다음 명령 전에 되돌린다. 스크립트가 한 번 도는 동안 하는 일은 막지 못한다(런북 §5, CI가 정본). 통과 때(푸시 전 게이트가 먼저 통과했어도) 약해진 검사와 하네스 변경을 보고한다
  - 어댑터 `harness/adapters/{python,rust}.json`(데이터). Rust는 fsmap(1회차 구현)이 있으면 훅 다섯 개와 `verify`를 위임
  - 작업 레포 리뷰어 `harness/agents/aok-reviewer.md`: 작업 트리째 사본에서 원격을 지우고 실험, 푸시 없음
  - 병합 뒤 후속: pre-push 훅은 Claude Code가 자기가 띄운 프로세스에만 두는 `CLAUDE_CODE_CHILD_SESSION`이 있을 때 판정한다(IDE 통합 터미널에서 사람이 하는 푸시는 판정하지 않음, Claude Code v2.1.172+). 세션의 상태는 하나(프로젝트 디렉터리)이고, 세션이 일한 같은 레포의 worktree(명령이 들어가거나 경로를 가리키거나 만든 곳, 편집한 곳, 턴이 끝나는 곳)를 턴이 어디서 끝나든 모두 판정한다. worktree의 HEAD는 세션 시작 때와 비교하고, 게시는 게시가 도는 트리에서 판정한다. 하네스 파일을 바꾼 커밋은 턴 종료가 통과해도 pre-push 훅의 통과 목록에 적지 않는다. `declare +x`·간접 `unset`·`env -vu` 같은 꼴과 전개로 만든 이름도 거부한다. 앞선 코어가 깐 훅은 그대로 받고 새 세션이 바꾼다
  - 시험 `tests/test_harness.py`(129개, 실제 fsmap 1개는 `AOK_FSMAP`이 있을 때), 변이 334개(`tests/mutations/harness.json`), CI 자기 시험. Rust 위임은 고정 커밋(74b7ffb)으로 빌드한 실제 fsmap으로도 CI가 시험한다
  - 런북의 CI 예시는 기준 브랜치에서 꺼낸 코어로 돈다. 시험 T7(원격 자식에서 프로젝트 훅이 도는지, 미실시), dev 템플릿에 하네스 아래에서 일하는 법
  - 독립 검증 세 라운드(라운드마다 검증자 둘 + 판정자)와 그 반영: `reports/2026-10-30-verification-rounds.md` §7
- 읽기 허브 「AOK 안내서」(D27): 원본 `site/pages`, 수치 레지스트리 `site/claims.json`·`site/figures.json`, 빌드 `scripts/build_site.py`. 수치 규칙(D28): HTML 파서로 브라우저가 보이는 대로 읽어, 페이지 본문·title 속성·메타데이터의 날 측정 수치(조사가 붙어도, 엔티티·폭 없는 문자·전각으로 써도)·어떤 꼴로든 손으로 단 [확인] 배지와 등급 범례(범례는 규범 문구로 생성)·cmd·url 근거가 없는 [확인]·셸 문법에 맞지 않거나 자리표시가 든 근거 명령·git에 없는 파일이나 저장소 밖 링크·원자료(git이 추적하는 site/ 밖 파일)와 다른 발췌·근거 없는 터미널 발췌나 측정값이 든 `<pre>`([기록됨] 배지 없는 data-unverified 포함)·렌더된 글자에서도 공개 레포 금지 항목은 빌드 실패. 작성 실수를 막는 가드레일이며 의도적 우회(CSS가 만든 글자, 말로 쓴 숫자, 목록 밖 단위)는 막지 않는다. 게시 URL은 레포에 두지 않는다
- [문서] 등급 범위(D29): 외부 원문(도구·제품 문서, 논문·공식 보고서)에 그렇게 적혀 있음. `docs/architecture.md`·`schemas/claim.schema.json`·verifier 정의
- 시험: `tests/test_regress_tools.py`, `tests/test_site.py`
- 제안서 `docs/proposals/regression-harness-hub.md`(⑦, ⑧ 작업 레포 하네스 제안 D26, 읽기 허브, 결정 D25–D29, ⑧에 넘기는 요구)
- 이력: `reports/2026-10-30-verification-rounds.md`(독립 검증 라운드와 이 PR의 ① T2), `reports/2026-10-30-hotspots.md`와 재현 자료·원자료 `logs/`(공개 레포 핫스팟 실측, 2026-11-01 재측정. 측정 스크립트는 쪽별 인스턴스 사본, 측정 중 실패 검사, 입력 판 고정을 갖추고, 공개 로그에는 판정·수치만 남긴다)
- `docs/patterns/brd.md` §1 ⑦과의 분담, §8 외부 근거(자기 수정, 오류 찾기, 자기 편애, 상관 오류, 아첨)
- `.gitignore`(허브 빌드 결과, 재현 절차가 만드는 빌드 결과)
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

- 기능 표를 7개로(⑦ 제안). ①은 결정적 비교를 ⑦로 넘긴다(`brd.md` §1, `aok-verify` T0). `brd.md` §5 상관 오류 대응에 "모델을 바꿔도 독립은 아니다"를 더했다
- `CLAUDE.md` 문서 목록에 `regression.md`(S4: 이 변경의 병합이 사람 확인이다)
- 관제실 "주의" 섹션을 "운영 경보"(트리·예산·예약 작업·허브 기록)로 좁히고, 세션 단위 신호는 인박스로 옮겼다
- 판단 축을 협업 효용으로 바꿨다. "24시간 비용" 타일을 없애고 비용은 참고 수치로만 표시한다

### 고침 (이 PR의 독립 검증이 찾은 기존 결함)

- ③ 클린룸의 작업 트리 검사: `git diff --exit-code`는 스테이징·커밋·새 파일·ignore된 파일을 놓친다. HEAD가 시작 sha인지와 `git status --porcelain --ignored`, `git diff --stat <sha>`로 본다(`docs/patterns/cleanroom.md`, `templates/system-prompts/cleanroom.md`)
- `docs/patterns/brd.md` §6·`aok-verify`: L1+L0+판정자 비용의 [도출] 값이 전제에서 나오지 않았다. 3개 495k–960k 토큰, 벽시계 14–22분으로 고침. 작업 완료 알림에서 온 값은 [기록됨]
- `README.md`: 커밋 전 스캔에 `--strict`, 검증자·판정자 도구 설명(읽기·실행, 쓰기 없음)
- `scripts/secret_scan.py`: 공유 링크 형식(`claude.ai/public/artifacts/<id>`)의 Artifact URL도 잡는다

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
