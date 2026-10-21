---
name: aok-cleanroom
description: AOK v1 클린룸 재현 게이트(§3③) 런북. 대상 레포의 특정 sha를 최소 환경(D9)의 새 원격 자식(create_session, permission_mode="auto", source_revision=<sha>)에 띄워 README·CONTRIBUTING 같은 진입 문서의 명령을 글자 그대로 실행하게 하고, 문서에 없는 행동(deviation), 첫 실패 원문, env_id·SessionStart 출력을 schemas/cleanroom-report.schema.json 보고서로 받는다. 보고서는 PR 코멘트와 자식 transcript(표지 블록)로 받고 원장 sessions에 결과를 남기며, flaky가 의심되면 같은 sha로 한 번 더 돌린다. 결함 분류(doc_missing·doc_wrong·env_limit·flaky·test_fail)의 최종 판정은 허브가 한다. 설정 파일이나 README·CONTRIBUTING을 바꾼 PR, 병합 직후, 릴리스 전, 온보딩 점검, 런북·장애 대응 절차처럼 "문서에 적힌 절차"를 검사할 때 로드한다.
---

# aok-cleanroom — 클린룸 재현 게이트 런북

허브가 따른다. 자식은 문서를 **글자 그대로** 실행하고 사실만 보고한다. 결함 분류와 후속 조치는 허브가 정한다(S7). 보증 범위는 "깨끗한 클라우드 기준"까지다.

계약: `schemas/cleanroom-report.schema.json`, `schemas/aok1.schema.json`, `schemas/ledger.schema.json`(`sessions`). 템플릿: `templates/system-prompts/child-base.md` + `cleanroom.md`. 위임 공통 절차: `aok-delegate`.

## 1. 쓸 때 / 안 쓸 때

| 쓸 때 | 안 쓸 때 |
|---|---|
| 설정 파일·README·CONTRIBUTING을 바꾼 PR, 병합 직후, 릴리스 전, 온보딩 | 사내망·장비·비밀이 필요한 단계 — 자식이 `env_limit`으로 기록만 한다 |
| 런북, 장애 대응 절차, 분석 노트북 등 문서화된 절차(`doc_ref`만 바꾼다) | 사람의 판단이 필요한 절차 |

v1은 요청 시 실행이다. 야간 예약 실행은 T1 통과 뒤(v1.x).

## 2. 전제

1. **입장 검사**(`aok-delegate` 3.3). 클린룸 상한 기본 5 USD. 1회 비용 1–5 USD, 15–60분 [추론]. 기동 약 11초, 첫 턴 0.22–0.27 USD [확인].
2. **최소 환경(D9).** `mcp__claude-code-remote__list_environments()`에서 사용자가 정한 최소 환경(예: 이름에 `cleanroom`)을 찾는다. 없으면 만들어 달라고 사용자에게 요청하고 멈춘다 — 허브의 환경을 대신 쓰지 않는다. 설정 스크립트·도구가 많은 환경은 문서에 없는 전제를 채워 결함을 가린다.
3. **대상 sha 고정.** 브랜치가 아니라 sha로 띄운다: `gh pr view <N> --repo <owner>/<repo> --json headRefOid -q .headRefOid` 또는 `git ls-remote https://github.com/<owner>/<repo> refs/heads/<branch>`.
4. 안전 규칙 S1–S9(`aok-delegate` 1절). 비밀을 자식에게 주입하지 않는다.

## 3. 입력

- `owner/repo`, `sha`(4–40 hex, 전체 40자 권장), 진입 문서 목록(기본 `README.md, CONTRIBUTING.md`), PR 번호(있으면)
- 큰 레포면 `clone_depth`(기본 1 권장), `blob_limit_kb`, `sparse_checkout_paths` — sparse는 문서가 다루는 범위가 확실할 때만(빠진 디렉터리가 결함을 가릴 수 있다)

## 4. 절차

1. **프롬프트 조립**(`aok-delegate` 3.5). 변수: `ROLE=cleanroom`, `TASK_ID=cr-<YYYYMMDD>-<nnn>`, `CHILD_CAP_USD=5`, `REPLY_BY=now+75분`, `OWNER`, `REPO`, `SHA`, `ENTRY_DOCS`, `PR_NUMBER`(없으면 `none`), `STEP_TIMEOUT_SEC=900`, `TASK_DETAILS`(진입 문서 안에서 특히 볼 절, 없으면 `(none — see the task envelope)`).
2. **생성.**
   ```
   mcp__claude-code-remote__create_session(
     source_url="https://github.com/<owner>/<repo>",
     source_revision="<sha>",
     permission_mode="auto",
     title="[<proj>/cleanroom] <repo>@<sha7>",
     tags=["aok:v1","proj:<proj>","role:cleanroom","tree:<tree_id>"],
     append_system_prompt="<child-base + cleanroom 렌더링 원문>",
     environment_id="<최소 환경 env_…>",
     clone_depth=1,
     prompt="<AOK/1 task: body.summary='<ENTRY_DOCS>의 명령을 글자 그대로 실행하고 cleanroom 보고서를 남긴다', reply_by>")
   ```
3. **등록과 safety wake.** `aok-delegate` 3.8–3.9(`sessions` 행, 감사 기록, `send_later(at=reply_by)`). 턴을 끝낸다.
4. **result 수신**(`aok-delegate` 3.10).
   1. 보고서를 가져온다: PR 코멘트 `AOK cleanroom @ <sha7>`가 있으면 거기서, 없으면 `list_events(session_id=<sid>, limit=20)`의 마지막 assistant 메시지에서 `AOK-CLEANROOM-REPORT BEGIN`/`END` 사이 JSON(없으면 `before_id`로 더 읽는다). `<SP>/cleanroom/<repo>-<sha7>-r<k>.json`에 원문 그대로 저장한다.
   2. 스키마 검사:
      ```bash
      python3 -I -c 'import json,sys,jsonschema; jsonschema.validate(json.load(open(sys.argv[2])), json.load(open(sys.argv[1]))); print("ok")' \
        <KIT>/schemas/cleanroom-report.schema.json <SP>/cleanroom/<file>.json
      ```
      `jsonschema`가 없으면 필수 키(`repo, sha, env.env_id, steps, findings, result`)와 enum만 확인한다. 실패하면 보고서를 사실 기록으로만 쓰고 형식 오류를 사용자 보고에 적는다.
   3. 무효 조건: 진입 문서가 바뀌었다(`aok:post-check` 단계의 출력), `env.env_id`가 최소 환경이 아니다 → 그 실행은 게이트로 쓰지 않는다.
5. **flaky 의심 시 2회째.** 다음 중 하나면 같은 sha·같은 파라미터로 새 자식을 한 번 더 띄운다(입장 검사 다시): `findings`에 `flaky`가 있다, 첫 실패가 시간 초과(exit 124)·네트워크·5xx·rate limit이다. 두 보고서의 `first_failure`(`n`, `cmd`, 오류 요지)가 같으면 결정적 결함, 다르면 `flaky`로 확정한다. 같은 sha에 3회 이상은 사용자 확인 후.
6. **판정(허브, S7).** 각 finding을 확정한다.

| type | 허브 판단 기준 | 후속 |
|---|---|---|
| `doc_missing` | 문서에 없는 행동이 필요했다(`deviation` 있음) | 문서 보완 PR 또는 이슈를 제안 |
| `doc_wrong` | 문서의 명령·설명이 틀렸다 | 문서 수정 PR 제안 |
| `env_limit` | 사내망·장비·비밀 범위 | 기록만. 결함으로 세지 않는다 |
| `flaky` | 2회 결과가 다르다 | 재현 조건 정리, 이슈 제안 |
| `test_fail` | 문서대로 설치했고 테스트가 실패 | 코드 결함 여부를 확인(필요하면 aok-verify) |

7. **기록.** `sessions` 행 `verdict`는 자식이 쓴 `pass|partial|fail`을 허브 판정으로 확인·수정한다. 감사 기록 `cleanroom_report`(target = `<owner>/<repo>@<sha7>`, result = 최종 결과, detail = `doc_missing=1 doc_wrong=0 env_limit=2 flaky=0 test_fail=0`). 재현 결함 KPI는 이 건수로 레포별 원시 집계한다.
8. **사용자 보고**(한국어): 결과, 첫 실패 단계와 명령, 결함 분류 건수, flaky 2회 비교, 제안할 후속 PR·이슈. 문서 수정 PR은 사용자 확인 후 별도 작업으로 만든다. 끝난 자식은 `archive-candidate`로 표시한다(보관은 사용자 확인 후).

## 5. 보고서 형식 (요약)

```json
{"repo":"…","sha":"…","env":{"env_id":"…","cc_version":"…","session_start_tail":"…"},
 "steps":[{"n":1,"doc_ref":"README.md#setup","cmd":"…","exit":0,"sec":12.3,"tail":"…","deviation":null}],
 "first_failure":{"n":4,"cmd":"…","stderr_head":"…"},
 "findings":[{"type":"doc_missing|doc_wrong|env_limit|flaky|test_fail","step":4,"evidence":"…"}],
 "result":"pass|partial|fail","cost_usd":0.0}
```

결과 규칙(자식이 기계적으로 적용): 첫 실패가 있거나 진입 문서가 바뀌면 `fail` · 실패 없이 deviation·env_limit이 있으면 `partial` · 전부 exit 0이고 deviation이 없으면 `pass`.

## 6. 멈추고 사람에게 올릴 때

- 최소 환경이 없다(D9)
- 자식이 `blocked`(권한 거부, 체크아웃 실패)
- 보고서가 없거나 형식이 깨졌고 transcript에서도 찾을 수 없다
- 같은 sha 2회 결과가 갈렸는데 원인을 정할 근거가 없다

## 7. 하지 말 것

- 허브 컨테이너나 설정이 많은 환경에서 클린룸을 돌리기
- 자식에게 문서·코드를 고치게 하거나, 문서에 없는 우회를 결함 없이 넘기기
- 비밀·토큰을 자식에게 넘기기, `env_limit` 단계를 통과로 세기
- 실패한 단계를 통과할 때까지 다시 돌리게 하기(flaky 판정은 새 세션 2회로만)
- 자식의 `result`를 그대로 최종 판정으로 쓰기(S7)
