# ⑧ 작업 레포 하네스

| 항목 | 내용 |
|---|---|
| 실행 스킬 | `.claude/skills/aok-harness` (`/aok-harness`) |
| 코드 | `harness/aok_harness.py`(언어 중립 코어), `harness/adapters/*.json`(언어 어댑터), `harness/agents/aok-reviewer.md`(적대적 리뷰어) |
| 계약 | 게이트 종료 코드 0 통과 · 1 실패 · 2 실행 불가. 훅은 Claude Code 훅 JSON으로 답한다 |
| 상태 | v1 기능(D26, `docs/proposals/regression-harness-hub.md`) |
| 회차 | 1회차는 fsmap(`d-j-lee/filesystemMap`)의 Rust 하네스다. 이 코어가 2회차이고 정본이다. Rust 어댑터는 fsmap이 설치돼 있으면 훅 다섯 개와 `verify`를 모두 그것에 맡긴다 |

AOK의 다른 기능은 허브와 세션의 층에서 돈다. ⑧은 자식(dev)이 실제로 코드를 고치는 **작업 레포 안**에 둔다. 편집 직후 검사, 턴 종료 게이트, 푸시 전 게이트, 사람·에이전트·CI가 같이 쓰는 단일 진입점(`verify`)이 한 파일에서 나온다. 깨진 코드로 끝난 턴을 사람이 PR에서 처음 보는 일을 줄이는 것이 목적이다 [추론].

## 1. 무엇이 언제 도나

| 순간 | 훅 | 하는 일 |
|---|---|---|
| 세션 시작 | `SessionStart` → `hook session-start` | `startup`이거나 이 세션 id에 쓸 수 있는 상태가 없을 때 기준(HEAD, 이미 바뀐 파일의 해시, 하네스 파일의 해시)을 기록한다. 다른 세션을 이어받은 `resume`·`fork`인데 상태가 없으면 이미 바뀐 파일도 이 세션의 것으로 센다. 상태가 손상돼 있었으면 기준을 새로 잡고 그 사실을 알린다. 어댑터와 실제 도구 판을 Claude에게 알린다. 어댑터를 고를 수 없거나 정의를 읽을 수 없으면 "OFF"와 이유를 사용자에게 보인다 |
| 편집 직후 | `PostToolUse(Edit\|Write\|MultiEdit)` → `hook post-edit` | 어댑터가 맡는 파일이면 그 파일에 편집 검사(Python: 컴파일, ruff / Rust: cargo check)를 돌린다. 단계들은 `budget.edit` 하나를 나눠 쓴다. 실패와 실행 불가는 block 사유로 Claude에게 간다 |
| 턴 종료 | `Stop` → `hook stop` | 이 세션이 트리를 바꿨고 이미 통과·보고한 트리가 아니면 quick 게이트를 돌린다. 모든 단계가 통과해야 통과다. 실패, 실행 불가, 손상되거나 없는 세션 상태 모두 막는다. 연달아 최대 3번 막고, 세 번째 사유에 "마지막"이라고 적어 Claude가 보고할 수 있게 한 뒤 그다음 종료는 놓아 주며 "게이트가 통과하지 않았다"를 사용자에게 알린다. 통과하면(같은 턴에 푸시 전 게이트가 먼저 통과한 경우도) 이 세션이 약하게 만든 검사와 하네스 파일의 변경을 보고한다. 백그라운드 에이전트(서브에이전트, 팀원 등)가 아직 돌면 게이트를 진짜 끝으로 미룬다. 백그라운드 셸과 모니터는 미루는 이유가 아니다 |
| 사용자 입력 | `UserPromptSubmit` → `hook prompt` | 막힌 횟수를 0으로. 위임 중이면 fsmap에도 넘긴다 |
| 셸 명령 | `PreToolUse(Bash)` → `hook pre-bash` | git 명령이 훅을 끄면 거부한다(`--no-verify`, `commit -n`과 `-nm` 같은 묶음, `core.hooksPath`를 건드리는 모든 git 명령). `git push`·`gh pr create`이면 다음을 거부한다: 지금 체크아웃한 브랜치가 아닌 것의 게시(다른 ref, `--all`·`--mirror`·`--tags`, 다른 `--head`), 같은 명령 안에서 HEAD를 옮기는 일(commit·merge·rebase 등과 묶기), 이 세션의 하네스·설정·리뷰어·CI 워크플로 변경, 커밋하지 않은 변경(파일 이름을 적는다), quick 게이트 실패나 실행 불가 |

| 명령 | 쓰는 쪽 |
|---|---|
| `python3 -I .aok/harness/aok_harness.py verify --mode quick\|full [--json]` | 사람, 에이전트, CI. 훅과 같은 게이트 |
| `python3 -I harness/aok_harness.py install --repo <작업 레포> --adapter <이름> [--yes]` | 허브(사람 확인 뒤). `--yes` 없이는 계획만 보인다 |

## 2. 설계의 축

| 축 | 어떻게 |
|---|---|
| 규칙의 출처는 하나 | 훅·사람·CI가 같은 `verify`를 부른다. 게이트 단계는 어댑터(데이터)와 작업 레포의 `.aok/harness.json`에서 온다 |
| 언어 중립 | 코어는 언어를 모른다. 지문(HEAD + 바뀐 파일 내용 + 게이트 정의), 기준, 막힘 횟수, 약화 보고, 푸시 판단이 코어의 일이고, 무엇을 돌릴지는 어댑터가 정한다. 어댑터를 둘(Python, Rust) 두는 것은 이 분리가 실제로 언어와 무관한지 시험으로 보이기 위해서다 |
| 판정받는 쪽이 정의를 못 바꾼다 | 게이트 정의(`.aok/harness.json`)와 레포에 복사된 어댑터(`.aok/harness/adapters/`)는 세션이 시작된 커밋의 것을 쓴다. 작업 트리에서 바꾼 정의는 쓰이지 않고 "differs from the version in …"으로 보고된다. 기준 커밋에 정의가 없으면 작업 트리에 새로 만든 정의도 쓰지 않는다(커밋한 뒤 시작하는 세션부터 적용). 하네스 파일, `.claude/settings*.json`, 설치된 리뷰어, `.github/workflows/`를 바꾼 세션의 푸시는 막고 사람에게 넘긴다. 코어 코드 자체는 세션 안에서 고정하지 못한다(§5). CI는 기준 브랜치의 코어·어댑터·정의로 돈다(§3) |
| 닫힌 실패 | 단계를 실행할 수 없거나(필수 도구 없음, 시간 초과, 설정 오류), 세션 상태가 손상됐거나 없거나, 위임한 fsmap이 0·2가 아닌 코드로 끝나거나 시작하지 못하거나, 하네스가 죽으면 통과로 치지 않는다. 턴 종료는 막고 푸시는 거부한다. 상태를 저장하는 일까지 실패하면 그래도 block JSON을 낸다. PreToolUse 훅에는 `onFailure: "block"`을 달아, 코어가 시작조차 못 하거나 시간을 넘기면 명령을 막는다 [문서]. 선택 도구가 없으면 건너뛰되 세션에 한 번 알린다 |
| 출구가 있다 | 연달아 3번 막은 뒤 놓아 준다. 세 번째 사유가 "마지막"임을 Claude에게 알려, 그 턴 안에 무엇이 실패하는지 보고하게 한다(dev 자식은 부모에게 AOK/1 `blocked`). 막힌 자식이 예산을 태우며 맴돌지 않게 하기 위해서다. Claude Code에도 Stop 훅이 8번 연속 턴을 이어 가게 하면 다음 block을 무시하는 상한이 있고, 도구 호출마다 0으로 돌아가며 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`으로 바꾼다 [문서]. 하네스의 3번은 그보다 먼저 온다 |
| 무엇이 세션의 것인가 | 아무것도 바꾸지 않은 세션은 판정하지 않는다(기준 정의를 읽을 수 없을 때도). 시작할 때 이미 바뀌어 있던 파일은 그 뒤 내용이 바뀌거나 세션이 커밋하면 세션의 것이 된다. 커밋 전의 빈 레포에서 시작한 세션은 첫 커밋 전체가 자기 것이다. 세션의 커밋은 기준 이후 first-parent의 병합 아닌 커밋이다. 무엇이든 바꾼 세션은 트리 전체로 판정받으므로 원래 있던 실패에도 막힌다. 막는 사유가 "건드리지 않았고 원래 있던 실패면 고치지 말고 사용자에게 말하라"고 덧붙인다 |
| 한 번에 다 보고 | 실패한 단계 뒤에도 모든 단계를 돌려 한 번에 보고한다. 단계들은 `budget.gate` 하나를 나눠 쓰고, 시간을 넘긴 단계는 그 단계의 프로세스 그룹째 끝낸다 |

## 3. 작업 레포에 들이기

1. **사람 확인(S4).** `.claude/settings.json`을 바꾸는 일이다. 허브는 `install --repo <경로> --adapter <이름>`을 `--yes` 없이 돌려 계획을 보이고, 사용자 확인을 받은 뒤 `--yes`로 다시 돌린다.
2. 설치가 하는 일: `.aok/harness/`에 코어와 어댑터를 복사, `.aok/harness.json`(`{"adapter": "<이름>"}`)이 없으면 작성, `.claude/agents/aok-reviewer.md`(적대적 리뷰어)가 없으면 작성, `.claude/settings.json`에 훅 다섯 개와 `.aok/`·`.claude/` 편집 시 묻기(`ask`)를 더한다. `allow` 규칙은 더하지 않는다. 기존 설정과 다른 훅은 보존하고, 두 번 돌려도(인터프리터를 바꾼 뒤에도) 하네스 훅이 겹치지 않는다. 다음 경우에는 아무것도 쓰지 않고 2로 끝난다: 설정 파일이 JSON 객체가 아니거나 `hooks`·`permissions.ask`의 모양이 다름(손으로 합친다), 이미 있는 `.aok/harness.json`의 어댑터가 `--adapter`와 다름(손으로 바꾼다).
3. 커밋하고 PR로 올린다. 훅은 다음 세션부터 적용된다. 첫 세션의 맥락이 `[aok-harness] adapter …`(Rust를 fsmap에 맡기면 `[harness] fsmap … hooks are active`)로 시작하는지 본다.
4. CI에는 기존 단계를 그대로 두고(정본 게이트), **기준 브랜치의 코어**로 도는 잡을 더한다. PR 체크아웃의 코어·어댑터·정의는 그 PR이 고쳤을 수 있으므로, 코어를 기준 브랜치에서 꺼내 돌리고 `--base`로 어댑터와 정의도 기준 브랜치의 것을 쓰게 한다:
   ```bash
   git fetch origin main
   git show origin/main:.aok/harness/aok_harness.py > "$RUNNER_TEMP/aok_harness.py"
   python3 -I "$RUNNER_TEMP/aok_harness.py" verify --mode full --base origin/main
   ```
   하네스를 처음 들이는 PR은 기준 브랜치에 코어가 없으므로 이 잡이 실패한다. 그 PR은 사람이 검토해 병합한다.
5. 단계 덧붙이기·바꾸기는 `.aok/harness.json`에서 한다. 허용 키는 `adapter`(이름), `gate`(`quick`·`full` 단계 목록을 통째로 바꿈), `edit`(`match`·`steps`), `extra`(양쪽 게이트 끝에 덧붙임), `budget`(`gate` 1–840초, `edit` 1–100초. 훅 시간 제한 900초·120초에서 하네스의 git 호출 몫을 뺀 값)뿐이다. 단계는 `{"name": …, "cmd": [...], "optional": true|false}`이다. 모양이 틀리면 실행 불가(2)다.

## 4. 어댑터

| 어댑터 | 편집 검사 | quick 게이트 | full 게이트 | 약화 보고 |
|---|---|---|---|---|
| `python` | 컴파일(바이트코드를 쓰지 않음), ruff(선택) | ruff 버그 계열(선택), `python3 -I -B -m unittest discover -s tests -t .` | ruff(레포 설정), 시험 | `noqa`(파일 단위 `ruff: noqa`·`flake8: noqa` 포함), `type: ignore`, 건너뛴 시험(`skip`·`skipIf`·`skipUnless`·`expectedFailure`·`SkipTest`·`pytest.mark.skip/xfail`·`pytest.skip`), `load_tests` 정의, `pragma: no cover`, 지운 단언, 지우거나 이름을 바꾼 시험, 수집 경로 밖으로 옮기거나 지운 시험 파일 |
| `rust` | `cargo check` | fmt, clippy `-D warnings`, test | + cargo-deny(선택) | `#[allow(`·`#[expect(`, `#[ignore]`, `cfg(false)`·`cfg(any())`, `unsafe`, 지운 단언, 지운 `#[test]` |

- Python 시험은 `-I`로 돌아 레포 루트의 `unittest.py` 같은 파일이 표준 라이브러리를 가리지 못한다. pytest식 함수 시험은 `unittest`가 모으지 않는다. pytest를 쓰는 레포는 `.aok/harness.json`의 `gate`에서 시험 단계를 `python3 -I -m pytest -q`로 바꾼다.
- 제거 규칙(지운 단언 등)은 파일마다 지운 줄 수에서 더한 줄 수를 뺀 순수 감소로 센다. 단언에 메시지를 더하는 식으로 고쳐 쓴 것은 지운 것이 아니다.

Rust는 fsmap이 `PATH`에 있고 `fsmap hook --help`가 되면 훅 다섯 개와 `verify`를 fsmap에 넘긴다. fsmap은 cargo·rustfmt·clippy·툴체인 설정의 무결성 검사, 환경 변수 정리, git pre-push 검증과 감사까지 하는 1회차 구현이다. 없으면 위 기본 단계로 돌고, 세션 시작 때 그 차이를 알린다.

fsmap에 맡길 때 달라지는 것(fsmap 74b7ffb로 확인):
- 넘기기 전에 코어가 직접 보는 것: 훅을 끄는 git 옵션, 게시 대상(체크아웃한 브랜치만), 같은 명령 안의 HEAD 이동, 이 세션의 하네스·설정·CI 변경. 나머지(게이트, 커밋 안 된 변경의 처리)는 fsmap이 정한다.
- fsmap의 답은 종료 코드가 0이나 2일 때만 그대로 넘긴다. 그 밖의 코드(Rust 패닉의 101 등), 시작 실패, 시간 초과는 닫힌 실패다: 턴 종료는 막고, 푸시는 거부하고, 다른 훅은 사용자에게 알린다.
- fsmap의 세션 시작 훅은 레포의 git `pre-push` 훅을 설치하고 그 사실을 맥락에 알린다. 클린룸(③) 보고의 SessionStart 출력에 남는다.
- 게이트 정의는 fsmap이 작업 트리에서 읽고, 정의를 약하게 만드는 설정은 fsmap의 무결성 단계가 본다. `verify --base`는 어댑터를 고르는 데만 쓰이고, 그렇다고 stderr에 알린다.
- `verify --json`은 fsmap의 `--json` 보고를 그대로 낸다. 종료 코드는 fsmap도 0 통과 · 1 실패 · 2 실행 불가다.

새 언어는 `harness/adapters/<이름>.json` 하나로 더한다: `detect`(이 파일이 있으면 그 언어), `versions`, `env`, `edit.match`·`edit.steps`, `gate.quick`·`gate.full`, `test_files`(시험 파일 glob), `weakening`(`added` 또는 `removed` 정규식), 선택적으로 `delegate`.

## 5. 한계

- **가드레일이지 보안 경계가 아니다.** 에이전트는 코어 파일, 상태 디렉터리(`<git-dir>/aok-harness/`), 로컬 설정을 고칠 수 있다. 손상되거나 지워진 상태는 막지만, 하네스가 쓴 것처럼 꾸민 상태는 알아보지 못한다. 세션이 고친 코어는 그 세션에서 그대로 돈다(보고되고 푸시는 거부된다). `ask` 규칙, 푸시 거부, 통과 보고가 그런 변경을 사람 앞에 드러내고, 기준 브랜치의 코어로 도는 CI가 정본 게이트다.
- **설정 파일은 하네스에 닿는 키만 본다.** `.claude/settings*.json`은 `hooks`·`disableAllHooks`·`env`의 변경만 하네스 변경으로 친다. Claude Code는 사용자가 "다시 묻지 않기"를 고르면 `.claude/settings.local.json`에 권한 규칙을 스스로 쓰므로, 권한 규칙의 변경은 막지 않는다.
- **게시 명령 읽기는 휴리스틱이다.** 명령을 셸 단어로 나눠(`bash -c "…"`는 열어 본다) `git … push`와 `gh … pr create`를 찾고, 나눌 수 없으면 글자로 찾는다. 별칭, 스크립트 파일, `git subtree push`, `gh api`·`gh pr merge`·MCP 도구로 하는 게시는 보지 못한다. 반대로 `--no-verify`나 `core.hooksPath`라는 글자만 들어가도(커밋 메시지 안이라도) git 명령은 거부된다. git 자체의 pre-push 훅으로 보는 방식은 fsmap(1회차)에 있다.
- **커밋과 푸시를 한 명령에 묶으면** 거부된다. 커밋하고 다음 명령에서 푸시한다. 게이트 단계가 무시되지 않는 파일(시험 보고서 등)을 남기면 그 뒤 푸시가 "커밋하지 않은 변경"으로 거부된다. 거부 사유에 파일 이름이 있으니 `.gitignore`에 넣는다.
- **세션의 것을 가르는 일의 한계.** 세션 중 기준 브랜치로 리베이스하면 위로 올라온 남의 커밋이 세션의 것으로 센다. 약화는 세션이 건드린 파일에서 기준 커밋과의 차이로 세므로, 그 파일에 세션 전부터 있던 커밋 안 된 억제 주석이나 같은 파일에 병합으로 들어온 남의 변경이 세션의 약화로 보고될 수 있다. 순수 감소로 세므로 한 파일에서 시험 하나를 지우고 다른 하나를 더하면 보이지 않는다. 시험 첫 줄의 `return`처럼 정규식으로 잡을 수 없는 약화는 보지 못한다. 상태 없이 이어받은 `fork`·`resume`은 이미 바뀐 파일을 모두 세션의 것으로 세지만, 갈라지기 전에 부모가 만든 커밋은 세지 않는다.
- **훅이 도는 곳.** 클라우드 세션에서 레포의 `.claude/settings.json` 훅은 레포 하나로 연 세션(프로젝트 스레드 밖)에서 적용된다 [문서]. 원격 자식 세션에서 실제로 도는지는 시험하지 않았다 [미확인]. `docs/rollout.md`의 T7이 그 시험이다.
- **훅이 시작하지 못할 때.** `onFailure`는 Claude Code v2.1.295 이상에서 동작하고 Stop 훅에는 효과가 없다 [문서]. 그래서 코어가 시작조차 못 하면(인터프리터 없음, 코어 파일 손상) Stop은 막히지 않는다. PreToolUse는 막히므로 그동안에는 모든 Bash 명령이 거부되고, 사람이 고치거나 훅을 뺀다. 그보다 오래된 판에서 이 필드가 어떻게 다뤄지는지는 확인하지 않았다 [미확인].
- **백그라운드 작업.** Stop 입력의 `background_tasks`는 문서에 정의된 필드이고 셸·서브에이전트·모니터 같은 유형을 가진다 [문서]. 하네스는 그중 에이전트 유형(subagent, workflow, teammate, cloud session)이 있을 때만 미룬다. 미룬 게이트는 다음 Stop에서 돈다. 에이전트가 끝난 뒤 Claude Code가 그 턴에 Stop을 다시 보내는지는 Claude Code를 돌려 확인하지 않았다 [미확인]. 푸시 전 게이트는 미루지 않는다.
- **작업량.** quick 게이트는 턴마다 돈다. 시험이 오래 걸리면 `.aok/harness.json`의 `gate.quick`을 줄이고 full은 CI에 둔다.
- **레포 코드를 실행한다.** 시험과 빌드 스크립트가 돈다. 믿지 않는 레포에는 켜지 않는다.

## 6. 근거와 시험

| 근거 | 내용 | 등급 |
|---|---|---|
| 1회차 | 출발점 세션의 Rust 하네스와 fsmap이 독립 검증 라운드마다 게이트 우회(턴 중 커밋, 첫 커밋, 검사 도구 부재 시 꺼짐)와 공허한 훅 시험을 찾았다 | [기록됨] `reports/2026-10-30-verification-rounds.md`, fsmap `docs/agent-harness.md` |
| 2회차 검증 | 독립 검증자 둘과 판정자가 이 코어에서 우회·문서 불일치를 찾았고, 고친 뒤 반례마다 시험과 변이를 더했다 | [기록됨] `reports/2026-10-30-verification-rounds.md` §7 |
| 훅 계약 | 블록·거부·맥락의 JSON 필드(`decision: "block"`·`reason`, `hookSpecificOutput.permissionDecision: "deny"`, `additionalContext`, `systemMessage`), 명령 훅 시간 초과 기본값 600초, `onFailure`(v2.1.295+, Stop에는 효과 없음), Stop 입력의 `background_tasks`, 8번 연속 상한 | [문서] https://code.claude.com/docs/en/hooks (2026-11-01 조회) |
| 시험 | `tests/test_harness.py`가 훅 다섯 개, `verify`, `install`, 두 어댑터의 동작을 시험한다. `tests/mutations/harness.json`이 코어와 어댑터의 장치를 하나씩 끄고, 변이마다 지정한 시험이 실패한다. CI가 돌린다 | [확인] `python3 -I scripts/vacuity_check.py --repo . --test "python3 -m unittest tests.test_harness" --mutations tests/mutations/harness.json --jobs 4 --timeout 600` |
| 실제 fsmap | 가짜 실행 파일이 아니라 빌드한 fsmap으로 훅 다섯 개와 `verify`가 넘어가고, 막힘·통과와 종료 코드가 그대로 돌아온다. CI의 harness 작업이 고정 커밋을 빌드해 돌린다. `AOK_FSMAP`이 없으면 이 시험은 건너뛰므로 결과에 `skipped`가 없어야 한다 | [확인] `cargo install --git https://github.com/d-j-lee/filesystemMap --rev 74b7ffb --locked && AOK_FSMAP="${CARGO_HOME:-$HOME/.cargo}/bin/fsmap" python3 -m unittest tests.test_harness.RealFsmapTests -v` |
