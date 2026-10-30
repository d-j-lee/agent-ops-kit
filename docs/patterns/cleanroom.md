# ③ 클린룸 재현 게이트

| 항목 | 내용 |
|---|---|
| 실행 스킬 | `.claude/skills/aok-cleanroom` (`/aok-cleanroom`) |
| 템플릿 | `templates/system-prompts/child-base.md` + `templates/system-prompts/cleanroom.md` |
| 계약 | `schemas/cleanroom-report.schema.json`, `schemas/aok1.schema.json`, `schemas/ledger.schema.json`(`sessions`) |
| 상태 | v1은 요청 시 실행. 야간 실행은 조건부(T1 통과) |

## 1. 쓸 때 / 안 쓸 때

| 쓸 때 | 안 쓸 때 |
|---|---|
| 설정 파일이나 README·CONTRIBUTING을 바꾼 PR, 병합 직후, 릴리스 전, 온보딩 | 사내망·장비·비밀이 필요한 단계. 이런 단계는 `env_limit`으로 기록만 한다 |

보증 범위는 "깨끗한 클라우드 기준"까지다.

## 2. 절차 (런북)

1. **입장 검사.** ⑥ 트리 상한·깊이·동시 실행 수를 확인한다(`docs/patterns/budget.md`).
2. **자식 생성.**
   ```
   create_session(
     source_url="https://github.com/<owner>/<repo>",
     source_revision="<sha>",
     permission_mode="auto",
     title="[<proj>/cleanroom] <repo>@<sha7>",
     tags=["aok:v1","proj:<p>","role:cleanroom","tree:<tree_id>"],
     append_system_prompt=<child-base.md 원문 + cleanroom.md 원문>,
     environment_id=<최소 환경>,              # D9
     prompt=<AOK/1 task 봉투: 진입 문서 목록, budget.cap_usd, reply_by>)
   ```
   - 큰 레포는 `clone_depth`, `blob_limit_kb`, `sparse_checkout_paths`를 쓴다.
   - 원장 `sessions`에 행을 쓰고, 턴을 끝내기 전에 `send_later(at=reply_by)`(S9).
3. **자식: 글자 그대로 실행.**
   - 진입 문서(README, CONTRIBUTING 등)의 명령을 문서에 적힌 그대로 실행한다.
   - 문서에 없는 행동이 필요하면 **그것 자체가 결함**이다(`doc_missing`). 우회해서 진행했다면 그 단계의 `deviation`에 적는다.
   - 첫 실패의 원문(명령, stderr 앞부분)을 보존한다.
   - 문서 수정은 금지한다. 끝에 `git diff --exit-code`로 작업 트리가 깨끗한지 검사한다.
4. **자식: 환경 기록.** 환경 설정 스크립트, CLAUDE.md, 훅이 문서에 없는 전제를 채워 줄 수 있다 [추론]. 그래서 `env_id`, CLI 버전, SessionStart 출력 끝부분을 보고서 `env`에 남긴다.
5. **자식: 보고.** `schemas/cleanroom-report.schema.json` 형식의 보고서를 PR 코멘트와 원장에 남기고, 부모에게 `AOK/1 result`를 보낸다. 봉투에는 요약과 `refs`(PR 코멘트, 원장 행)만 싣는다.
6. **flaky 의심.** 같은 sha로 한 번 더 돌린다(새 자식, 같은 파라미터). 두 결과가 다르면 `flaky`.
7. **허브: 판정.** 결함 분류와 후속 조치(문서 PR, 이슈)는 허브가 정한다(S7). 끝난 자식은 `archive-candidate`로 표시한다. 보관은 사용자 확인 후(S4).

## 3. 계약 — 보고서 스키마

```json
{"repo":"…","sha":"…","env":{"env_id":"…","cc_version":"…","session_start_tail":"…"},
 "steps":[{"n":1,"doc_ref":"README.md#setup","cmd":"…","exit":0,"sec":12.3,"tail":"…","deviation":null}],
 "first_failure":{"n":4,"cmd":"…","stderr_head":"…"},
 "findings":[{"type":"doc_missing|doc_wrong|env_limit|flaky|test_fail","step":4,"evidence":"…"}],
 "result":"pass|partial|fail","cost_usd":0.0}
```

| `findings[].type` | 의미 |
|---|---|
| `doc_missing` | 문서에 없는 행동이 필요했다 |
| `doc_wrong` | 문서에 적힌 명령이나 설명이 틀렸다 |
| `env_limit` | 사내망·장비·비밀 등 클린룸이 보증하지 않는 범위. 기록만 하며 문서 결함으로 세지 않는다 |
| `flaky` | 같은 sha 2회 실행 결과가 다르다 |
| `test_fail` | 문서대로 설치했고 테스트가 실패했다 |

보고서에 비밀값·토큰을 싣지 않는다. `tail`과 `stderr_head`는 잘라서 싣는다.

## 4. 실패 모드와 대응

| 실패 | 대응 |
|---|---|
| 환경 설정·CLAUDE.md·훅이 숨은 전제를 채움 | `env_id`, SessionStart 출력 기록. 최소 환경 사용(D9) |
| 자식이 문서를 고쳐서 통과 | `git diff` 검사. 수정이 있으면 그 실행은 무효 |
| 자식이 문서에 없는 우회로 통과 | `deviation` 기록, 그 단계는 `doc_missing` |
| 일시 오류를 결함으로 판정 | 같은 sha 2회 실행 |
| 비밀이 필요한 단계 | `env_limit`으로 기록하고 넘어간다. 비밀을 주입하지 않는다 |
| 자식이 판정까지 함 | 판정은 허브(S7). 자식 보고서는 사실 기록으로만 쓴다 |
| 조용히 끝남 | safety wake(S9)에서 원장·`list_events` 대조 |

## 5. 비용과 시간

| 항목 | 값 | 등급 |
|---|---|---|
| 기동 | 약 11초 | [확인] |
| 첫 턴 (기본 모델) | $0.22–0.27 | [확인] |
| 1회 전체 | $1–5, 15–60분. 설치와 테스트가 좌우 | [추론] |

## 6. 일반화

런북, 장애 대응 절차, 분석 노트북처럼 "문서에 적힌 절차"라면 무엇이든 같은 틀로 검사한다. `doc_ref`만 바꾼다.

## 7. 첫 롤아웃 점검 (4단계)

- [ ] 대상: 파일럿 레포, `<KNOWLEDGE_REPO>` 등 비공개 대상 레포
- [ ] 보고서 3건 이상, 결함 분류 포함
- [ ] 비밀 유출 0
- [ ] D9 최소 환경을 만든 뒤 실행
- [ ] 야간 실행은 T1 통과 후에만 검토(v1.x)
