# ⑥ 예산 가드 (트리 상한)

| 항목 | 내용 |
|---|---|
| 실행 스킬 | `.claude/skills/aok-delegate` (`/aok-delegate`) — 모든 위임의 입구 |
| 템플릿 | `templates/system-prompts/child-base.md` + 역할 템플릿 |
| 계약 | `schemas/tree.schema.json`, `schemas/ledger.schema.json`(`trees`, `sessions`, `config_policy`), `schemas/aok1.schema.json`(`budget`) |
| 기본값 | `aok/budget.yaml` (D3. 모든 수치는 [추론] 기본값) |

## 1. 원칙

- 모든 위임은 하나의 트리에 속한다. **허브 비용도 트리에 포함한다.**
- 기본값은 **경보이지 감속이 아니다**(`on_overrun: "notify"`). 사용자가 품질을 우선한다.

## 2. 계약 — 트리 문서

```json
{"tree_id":"…","hub_session":"…","cap_usd":50,"soft":0.7,"max_depth":1,"max_concurrent":3,
 "on_overrun":"notify","hub_rotate":{"context_tokens":250000,"hub_cost_usd":25},
 "children":[{"session_id":"…","role":"reviewer","model":"…","cap_usd":10,"spent_usd":0,"read_at":"…"}]}
```

| 필드 | 의미 | 기본값(D3) |
|---|---|---|
| `cap_usd` | 트리 전체 상한. 허브 포함 | 50 |
| `soft` | 경고 비율 | 0.7 |
| `max_depth` | 허브 아래 위임 깊이 | 1 |
| `max_concurrent` | 동시에 살아 있는 자식 수 | 3 |
| `on_overrun` | `notify` 또는 `interrupt` | `notify` (D6: 자동 중단 끔) |
| `hub_rotate.context_tokens` | 허브 교대 맥락 기준 | 250000 |
| `hub_rotate.hub_cost_usd` | 허브 교대 비용 기준 | 25 |
| `children[].cap_usd` | 자식별 상한 | ≤15 |
| `children[].spent_usd`, `read_at` | 마지막으로 읽은 비용과 시각 | — |

## 3. 적용 지점

| # | 지점 | 동작 |
|---|---|---|
| 1 | 입장 검사 | 허브가 `create_session` 전에 `spent + child_cap ≤ cap`, 깊이, 동시 실행 수를 확인한다 |
| 2 | 차단(선택) | 작업 레포 settings에 `mcp__claude-code-remote__create_session` deny를 넣는다. 도구 이름 glob deny는 동작한다 [기록됨]. settings 변경이라 S4, D9 |
| 3 | 자기 점검 | 자식이 이정표마다 인자 없는 `get_session`으로 자기 비용을 확인한다 [문서] |
| 4 | 감시 | ②가 70%·100%를 본다 |
| 5 | 허브 교대 | 맥락 250k나 허브 비용 $25에 닿으면 HANDOFF를 쓰고 새 허브로 옮긴다 |
| 6 | 포크 | 허브 맥락이 100k 미만일 때만 쓰고, 세션당 횟수에 상한을 둔다 [추론] |

## 4. 절차 (런북: `/aok-delegate`)

1. **트리 열기.** 위임할 트리가 없으면 `tree_id`를 정하고 `ArtifactData`로 원장 `trees`에 `{tree_id, hub_session, cap_usd, spent_usd: 0, status: "active"}`을 쓴다. 트리 정의 전체(`schemas/tree.schema.json`)는 `aok/budget.yaml` 기본값에서 만든다.
2. **수단 선택.** §2.2 규칙으로 포크·서브에이전트·원격 자식 중 고른다. 원격 자식이 아니면 3–5단계를 건너뛴다(서브에이전트 토큰은 허브 쪽 비용으로 본다. 합산 여부는 [미확인]). 6단계(safety wake)는 위임한 뒤 턴을 끝낼 때 항상 적용한다.
3. **입장 검사.**
   - `spent` = 허브의 `get_session()`(인자 없음) `cost_usd` + 원장 `sessions`의 자식 `spent_usd` 합.
   - `spent + child_cap ≤ cap_usd`, 깊이 ≤ `max_depth`, 활성 자식 + 1 ≤ `max_concurrent`.
   - 하나라도 어기면 생성하지 않고 사용자에게 올린다.
4. **생성.**
   ```
   create_session(
     permission_mode="auto",                                   # S1
     title="[<proj>/<role>] <task>",
     tags=["aok:v1","proj:<p>","role:<role>","tree:<tree_id>"],
     append_system_prompt=<child-base.md 원문 + <role>.md 원문>,
     prompt=<AOK/1 task 봉투: budget.cap_usd, reply_by>,
     source_url=…, source_revision=…, model=…)                 # 필요할 때
   ```
5. **원장 기록.** `sessions`에 `{session_id, tree_id, role, model, cap_usd, spent_usd: 0, status: "spawned", created_at}`.
6. **safety wake.** 턴을 끝내기 전에 `send_later(at=reply_by)`(S9).
7. **깨어날 때마다.** ② A층 점검으로 `spent_usd`를 갱신하고 70%·100%를 본다.
8. **허브 교대.** 자기 `get_session()`의 맥락이 250k 또는 비용이 $25에 닿으면:
   - `HANDOFF.md`(열린 트리, 활성 자식, 대기 중인 `reply_by`, 다음 행동)를 쓴다. 세션 식별자는 원장에만 두고 공개 레포에 쓰지 않는다.
   - 사용자가 새 허브 세션을 열고 키트를 붙인다(README 빠른 시작). 새 허브가 원장 `trees.hub_session`을 갱신한다.
9. **종료.** 트리의 모든 자식이 `done`이면 `trees.status`를 `closed`로 두고, 끝난 자식을 `archive-candidate`로 표시한다. 보관은 사용자 확인 후(S4).

**선택: settings deny (적용 지점 2)**

```json
{"permissions": {"deny": ["mcp__claude-code-remote__create_session"]}}
```

작업 레포의 `.claude/settings.json`에 넣는다. 사람 확인 후, 파일럿 레포에만 시범 적용한다(D9). deny가 원격 MCP 도구에 실제로 먹는지는 [미확인]이며 T6과 함께 확인한다.

## 5. 단가 참고

| 수단 | 단가 |
|---|---|
| 원격 첫 턴 | $0.22–0.27 |
| 원격 후속 턴 | 약 $0.09 |
| 포크 | 호출당 약 142k 맥락 토큰 |
| 측정 시점 허브 | 누적 $14.59 / 맥락 300k |

모두 [확인]이다. Haiku 자식 $0.016은 [기록됨]이다.

## 6. 한계

- 기술적으로 강제하는 수단은 적용 지점 2(settings deny)와 `interrupt_session`뿐이다. 나머지는 정책과 감시다.
- 금액은 list 가격 기준이다. 구독 사용량 한도(5시간·7일)가 따로 있다.
- `cost_usd` 갱신 지연, 서브에이전트 비용이 허브 비용에 합산되는지는 [미확인]이다(2단계에서 확인).

## 7. 실패 모드와 대응

| 실패 | 대응 |
|---|---|
| 자식의 재귀 생성 | S6, 선택적 settings deny, `max_depth` |
| 허브 비용 누락 | 트리에 허브 포함, 교대 기준 |
| 원장 밖 위임 | ② A층의 `parent_session_id` 수집 |
| 상한 초과를 늦게 앎 | 자식 자기 점검 + ② 70% 경고 |
| 포크 남용으로 맥락 재독 비용 증가 | 허브 맥락 100k 미만에서만, 횟수 상한 |

## 8. 첫 롤아웃 점검 (2단계)

- [ ] 위임의 100%가 원장에 있음
- [ ] 허브 교대 1회 이상 실행
- [ ] KPI: 트리 최대 비용 ≤ 상한, 알림 없이 초과 0
- [ ] `cost_usd` 갱신 지연과 서브에이전트 비용 합산 여부 기록
