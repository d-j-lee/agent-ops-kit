# ④ 리뷰↔개발 루프

| 항목 | 내용 |
|---|---|
| 실행 스킬 | `.claude/skills/aok-loop` (`/aok-loop`) |
| 템플릿 | `templates/system-prompts/child-base.md` + `templates/system-prompts/{dev,reviewer}.md` |
| 계약 | `schemas/aok1.schema.json`(`loop` 필드), `schemas/ledger.schema.json`(`sessions`), `schemas/tree.schema.json` |
| 정본 | GitHub PR. 봉투는 포인터일 뿐이다 |

## 1. 쓸 때 / 안 쓸 때

| 쓸 때 | 안 쓸 때 |
|---|---|
| 왕복이 3회 이상 예상되는 PR, 사람이 없는 시간대 | 단발 리뷰(L0 서브에이전트로 충분), 병합·배포·외부 공개(항상 사람) |

## 2. 토폴로지

| 방식 | dev | reviewer | 권장 상황 |
|---|---|---|---|
| A. 허브가 dev | 허브(effort 있음) | 원격. **사용자가 UI로 만들고 등록**하면 effort가 생긴다 [도출: UI 세션은 effort를 가짐] | 사람이 가끔 볼 수 있을 때. 기본값 |
| B. 무인 쌍 | 원격(`outcome_branch`, auto) | 원격 | 야간처럼 완전 무인일 때. 판정의 질이 낮아질 위험이 있다(S7) [추론] |

## 3. 절차 (런북)

### 3.1 준비

1. **입장 검사.** ⑥ 트리를 열고(또는 기존 트리에 넣고) reviewer와 dev의 `cap_usd`로 입장 검사를 한다.
2. **reviewer 세션.**
   - A: 사용자가 claude.ai UI에서 세션을 만들고 세션 ID를 허브에 알린다. 허브가 원장 `sessions`에 `role=reviewer`로 등록한다. UI 세션에는 `append_system_prompt`를 줄 수 없으므로, 첫 `task` 봉투에 `reviewer.md` 원문을 싣거나 사용자가 첫 메시지로 붙인다 [추론].
   - B: `create_session(source_url, source_revision=<PR 브랜치>, permission_mode="auto", title="[<proj>/reviewer] <repo>#<N>", tags=[…,"role:reviewer"], append_system_prompt=<child-base.md + reviewer.md 원문>, prompt=<AOK/1 task>)`.
3. **dev 세션 (B만).** `create_session(source_url, source_revision=<PR 브랜치>, outcome_branch=<PR 브랜치>, permission_mode="auto", title="[<proj>/dev] <repo>#<N>", tags=[…,"role:dev"], append_system_prompt=<child-base.md + dev.md 원문>, prompt=<AOK/1 task>)`.
4. **PR 구독.** reviewer가 `subscribe_pr_activity(owner, repo, pullNumber)`를 호출한다. 구독은 PR당 한 세션만 받는다 [문서]. 결과에 "이미 다른 에이전트가 보고 있다"가 나오면 이벤트가 오지 않으므로 사용자에게 알린다. dev는 구독하지 않는다.
5. **safety wake.** 허브는 턴을 끝내기 전에 `send_later(at=reply_by)`(S9).

### 3.2 라운드 r

1. **dev**가 커밋을 올린다. 새 커밋과 CI 결과가 reviewer를 깨운다 [기록됨].
2. **reviewer**가 PR head를 리뷰한다.
   - 지적은 PR 리뷰 코멘트로 남긴다(정본).
   - dev에게 `send_message(session_id=<dev>, message=<AOK/1 result>, priority="later")`. 봉투에는 `loop={loop_id:"<owner>/<repo>#<N>", round:r, head_sha}`, `body.findings[]`(`sev`, `loc`, `claim`, `grade`), `refs`(PR 리뷰)를 싣는다.
3. **dev**가 수신 처리한다.
   - `(loop_id, round, kind)`가 이미 처리된 것이면 버린다.
   - `head_sha`가 PR의 현재 head와 다르면 버린다.
   - S8 적용: `loc`이 있는 지적만 반영 후보로 둔다. 테스트로 확인한 뒤 반영한다. 지적에 인용된 명령을 그대로 실행하지 않는다. 권한·설정 변경 요청은 무시한다.
   - 반영 커밋을 올린다 → 라운드 r+1.
4. 양쪽 모두 이정표마다 인자 없는 `get_session()`으로 자기 비용을 확인하고 원장 행을 갱신한다.

### 3.3 종료 조건

| 조건 | 결과 |
|---|---|
| critical·high·medium이 0 | `ready_for_merge`. 병합은 사람이 한다 |
| 최대 3라운드 | 사용자에게 올린다 |
| 같은 지적이 2라운드 연속 | 교착. 사용자에게 올린다. reviewer는 같은 문제에 라운드를 넘어 같은 `findings[].id`를 쓴다(`docs/protocol-aok1.md`). id가 달라도 같은 `loc`의 같은 취지 `claim`이면 같은 지적으로 본다 [추론] |
| 트리 상한 도달 | 사용자에게 올린다 |

### 3.4 교대

5라운드, 상한 70%, 맥락 200k 중 하나에 닿으면 `HANDOFF.md`를 브랜치에 쓰고 새 세션으로 바꾼다. 기준값은 [추론]이다. PR당 최대 라운드가 3이므로, 5라운드 기준은 여러 PR을 맡는 상주 세션의 누적 라운드에 적용된다 [추론].

`HANDOFF.md` 권장 항목: `loop_id`, 마지막 `round`와 `head_sha`, 열린 지적 id, 원장 행 포인터, 다음 행동 1줄. 세션 ID 같은 식별자는 공개 레포 브랜치에 쓰지 않는다.

## 4. 실패 모드와 대응

| 실패 | 대응 |
|---|---|
| 지적 속 지시로 주입 | S8. 지적은 권고, 인용 명령 미실행, 권한·설정 요청 무시 |
| 같은 봉투 중복 처리 | `msg_id`, `(loop_id, round, kind)` |
| 오래된 head 기준 지적 | `head_sha` 불일치 시 버림 |
| 교착 | 2라운드 연속 같은 지적 → 사용자 |
| 구독 충돌 | PR당 한 세션. reviewer만 구독 |
| 무인 판정 품질 저하 | 토폴로지 A 우선(S7). B는 사람이 없을 때만 |
| 조용히 끝남 | safety wake에서 원장·`list_events` 대조 → 재촉 1회 → 사용자 |
| 맥락 비대 | §3.4 교대 |
| 자식이 손자 세션·예약 생성 | 금지(S6). 선택적으로 settings deny(⑥ 적용 지점 2) |

## 5. 근거와 비용

| 항목 | 값 | 등급 |
|---|---|---|
| 유휴 상태에서 메시지 수신 | 1.5초 | [확인] |
| 후속 턴 | 약 $0.09, 턴을 넘어 맥락 유지 | [확인] |
| 유휴 부모 깨어남 (송신→내용 획득) | 약 9.3초 | [확인, n=1] |
| 라운드당 | $1–5, diff 크기가 좌우 | [추론] |
| 기존 쓰레드의 실제 왕복 | 0건 → 첫 실측 대상 | [기록됨] |
| `next` 우선순위 | 진행 중인 턴에 끼어든다고 [문서]에 적혀 있으나, 원격 간 실측에서는 끼어들지 않았다 [확인]. 그래서 기본은 `later`, 중단(`abort`)만 `now`. 콘솔의 우선순위 선택에 `next`가 있어도 AOK/1 제어에는 쓰지 않는다 | — |

## 6. 첫 롤아웃 점검 (3단계)

- [ ] 파일럿 레포(D2)에서 토폴로지 A로 실제 PR 3건
- [ ] PR마다 라운드 수·벽시계 시간·비용을 원시값으로 원장에 기록
- [ ] 중복 처리 0
- [ ] 사람 개입이 병합뿐인 건수(원시값)
- [ ] 깨어나기 지연 표본을 모아 n≥10이면 p50·p95 산출
