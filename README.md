# AOK — Agent Ops Kit v1

AOK는 claude.ai 클라우드 세션으로 멀티에이전트 작업을 돌릴 때 쓰는 운영 키트다. 병렬 세션을 "많이" 쓰는 단계에서 "체계적이고 안전하게, 스스로 나아지게" 쓰는 단계로 옮기는 것이 목적이다. 얇은 허브 세션 하나가 위임·감시·예산·판정을 맡고, 자식 세션은 템플릿과 AOK/1 메일박스로 통신하며, 모든 위임은 원장(Artifact DB)에 남는다. 특정 프로젝트와 무관하게 재사용한다.

## 구성

**기능 6개**

| # | 기능 | 하는 일 | 스킬 | 런북 | v1 상태 |
|---|---|---|---|---|---|
| ① | 블라인드 재현 대조 (BRD) | 결론을 숨긴 검증자(L0·L1·L2)와 별도 판정자로 대조 | `/aok-verify` | [brd.md](docs/patterns/brd.md) | v1 |
| ② | 정지·비용 감시기 | 허브가 깨어날 때마다 자식 점검(A층), 예약 감시와 push(B층) | `/aok-watch` | [watchdog.md](docs/patterns/watchdog.md) | A층 v1 · B층 조건부(T1·T3) |
| ③ | 클린룸 재현 게이트 | 새 컨테이너에서 문서의 명령을 글자 그대로 실행 | `/aok-cleanroom` | [cleanroom.md](docs/patterns/cleanroom.md) | 요청 시 v1 · 야간 조건부(T1) |
| ④ | 리뷰↔개발 루프 | PR을 정본으로 dev·reviewer가 최대 3라운드 왕복 | `/aok-loop` | [loop.md](docs/patterns/loop.md) | v1 |
| ⑤ | 큐레이터 (정원사) | 세션 기록 → 카드·씨앗 → 사람 검토 → 스킬 갱신 | `/aok-garden` | [curator.md](docs/patterns/curator.md) | 수동 v1 · 예약 조건부(T1) |
| ⑥ | 예산 가드 (트리 상한) | 허브 포함 트리 상한, 입장 검사, 허브 교대 | `/aok-delegate` | [budget.md](docs/patterns/budget.md) | v1 |

**공통 기반 5개**

| 기반 | 요지 | 정의 |
|---|---|---|
| 근거 규약 | 등급 6종. [확인]에는 재실행 가능한 ref | [architecture §2.1](docs/architecture.md#21-근거-규약), `schemas/claim.schema.json` |
| 수단 선택 규칙 | 맥락은 포크, 판단은 새 서브에이전트, 환경은 원격 자식, 로컬 CLI 금지, 대기는 `send_later` | [architecture §2.2](docs/architecture.md#22-수단-선택-규칙) |
| 안전 규칙 S1–S9 | auto 자식, 사람 확인 지점, 기록은 데이터, safety wake | [architecture §2.3](docs/architecture.md#23-안전-규칙) |
| 이름·태그·원장 | `[<proj>/<role>] <task>`, `aok:v1`·`tree:<id>` 태그, AOK Ledger | [architecture §2.4](docs/architecture.md#24-이름-태그-원장), `schemas/ledger.schema.json` |
| 메일박스 AOK/1 | 요약 1줄 + JSON, ack 없음, 멱등, `reply_by` | [protocol-aok1.md](docs/protocol-aok1.md), `schemas/aok1.schema.json` |

## 빠른 시작 (허브 세션)

**1. 키트 붙이기**

```
add_repo(owner="d-j-lee", repo="agent-ops-kit")
# 결과로 받은 clone 명령을 실행한 뒤
register_repo_root(owner="d-j-lee", repo="agent-ops-kit", directory="<clone 경로>")
```

다음 턴부터 `.claude/skills/aok-*`와 `CLAUDE.md`가 실린다. 원격 자식에게는 플러그인과 스킬이 닿지 않으므로, 자식에게는 `append_system_prompt`로 `templates/system-prompts/` 원문을 넣는다([architecture §3](docs/architecture.md#3-키트-전달-경로)).

**2. 관제실 열기**

관제실 URL: <CONSOLE_URL>

모든 세션(사람이 연 세션 포함)에서 사람이 처리할 일을 모은 "당신 차례" 인박스가 첫 화면이다. 원장(트리·세션·경보·검증·예약 작업)도 여기서 보고 조작한다. 화면과 권한은 [docs/dashboard.md](docs/dashboard.md)에 있다.

**3. 첫 명령**

| 명령 | 언제 |
|---|---|
| `/aok-delegate` | 위임할 때 항상. 트리 개설, 입장 검사, 자식 생성, 원장 기록, safety wake |
| `/aok-verify` | 결론이 틀리면 비싼 주장·분석·설계·PR |
| `/aok-watch` | 허브가 깨어날 때마다(자식 결과, safety wake) |
| `/aok-cleanroom` | README·설정을 바꾼 PR, 병합 직후, 릴리스 전 |
| `/aok-loop` | 왕복이 3회 이상 예상되는 PR |
| `/aok-garden` | 주 1회, 또는 끝난 세션이 20개 쌓였을 때 |

## 레포 지도

```
agent-ops-kit/
├─ README.md · CHANGELOG.md
├─ CLAUDE.md                       # 모든 세션의 운영 규칙(수단 선택, S1–S9)
├─ docs/
│  ├─ architecture.md              # 규범: 원칙, 구조, 공통 기반, 전달 경로
│  ├─ protocol-aok1.md             # 메일박스 규격과 예시
│  ├─ patterns/{brd,watchdog,cleanroom,loop,curator,budget}.md   # 기능별 런북
│  ├─ rollout.md                   # 단계, 시험 T1–T6, KPI, 결정 D1–D9
│  ├─ dashboard.md                 # 관제실 콘솔
│  └─ proposals/{control-tower-v2,workspace-atlas}.md   # 제안: 모든 세션의 중간관리자·비서, 다음 과제 작업공간 지도(합의 전)
├─ schemas/{aok1,tree,ledger,claim,card,seed,cleanroom-report}.schema.json
├─ .claude/skills/aok-{delegate,verify,cleanroom,loop,watch,garden}/SKILL.md
├─ .claude/agents/{verifier,adjudicator}.md                      # 읽기 전용 도구
├─ templates/system-prompts/{child-base,dev,reviewer,cleanroom,repro-l2,watchdog,curator}.md
├─ scripts/{sanitize_packet,harvest_events,secret_scan}.py
├─ aok/budget.yaml                 # 예산 기본값(D3)
├─ dashboard/console.html          # 관제실 소스
├─ knowledge/                      # 카드·씨앗 형식과 템플릿만
├─ tests/
└─ reports/                        # 이력: 실측, 검증 기록
```

## 상태

| 항목 | 내용 |
|---|---|
| 버전 | v1.0.0 ([CHANGELOG](CHANGELOG.md)) |
| 단계 | 1단계(기반 + 실현성 시험). **T1–T6 미실시** — 결과 전까지 ②B, ③ 야간, ⑤ 예약은 꺼 둔다 |
| 결정 | D1(키트 위치 = 이 레포)만 확정. D2–D9는 [rollout.md §4](docs/rollout.md#4-결정-사항) |
| 다음 | 관제탑 v2 [제안](docs/proposals/control-tower-v2.md). MVP-1.1 인박스는 콘솔에 들어갔고, 나머지는 결정 D10–D18을 받은 뒤 진행한다 |
| 근거 | [실측 기록](reports/2026-10-20-field-test.md), [검증 기록](reports/2026-10-20-plan-review.md), [v2 설계 대조](reports/2026-10-21-v2-design-crosscheck.md) |

## 공개 레포 주의

- 이 레포는 공개다. 세션 ID, 컨테이너 ID, 트리거 ID, 계정 UUID, 이메일, Artifact URL, 다른 세션 기록 발췌를 쓰지 않는다. `session_<id>` 같은 자리표시자를 쓴다.
- **실제 지식 카드·씨앗·원장 데이터는 여기에 커밋하지 않는다.** 원장은 Artifact DB에, 실제 카드와 씨앗은 비공개 원장이나 비공개 레포에 둔다. `knowledge/`에는 형식과 템플릿만 있다.
- 커밋 전 `python3 -I scripts/secret_scan.py .`
