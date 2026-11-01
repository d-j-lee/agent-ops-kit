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
| 세션 시작 | `SessionStart` → `hook session-start` | `startup`이거나 이 세션 id에 쓸 수 있는 상태가 없을 때 기준(HEAD, 이미 바뀐 파일의 해시, 하네스 파일의 해시)을 기록한다. 같은 레포의 다른 worktree가 그때 가리키던 HEAD도 적는다. 다른 세션을 이어받은 `resume`·`fork`인데 상태가 없으면 이미 바뀐 파일도 이 세션의 것으로 센다. 상태가 손상돼 있었으면 기준을 새로 잡고 그 사실을 알린다. 그때의 ref들도 적어 두어, 그 뒤에 생긴 커밋만 세션의 것으로 센다. git 디렉터리(작업 트리 밖)에 git `pre-push` 훅을 깔고 그 사실을 알린다(아래 "푸시"). 어댑터와 실제 도구 판을 Claude에게 알린다. 어댑터를 고를 수 없거나 정의를 읽을 수 없으면 "OFF"와 이유를 사용자에게 보인다 |
| 편집 직후 | `PostToolUse(Edit\|Write\|MultiEdit)` → `hook post-edit` | 어댑터가 맡는 파일이면 그 파일에 편집 검사(Python: 컴파일, ruff / Rust: cargo check)를 돌린다. 단계들은 `budget.edit` 하나를 나눠 쓴다. 단계의 실패와 실행 불가는 block 사유로 Claude에게 간다. 같은 레포의 다른 worktree에 있는 파일이면 그 worktree에서 검사를 돌리고, 그 worktree를 세션이 일한 곳으로 적는다(§2). 하네스가 세션 상태나 정의를 읽지 못하면 편집 검사를 건너뛰고 그 사실을 알린다(그때 턴 종료와 푸시는 막힌다) |
| 턴 종료 | `Stop` → `hook stop` | 프로젝트 디렉터리와 세션이 일한 worktree마다(§2), 이 세션이 그 트리를 바꿨고 이미 통과·보고한 트리가 아니면 quick 게이트를 돌린다. 턴이 어느 디렉터리에서 끝나든 같다. 트리들은 `budget.gate` 하나를 나눠 쓰고, 실패는 한 번의 막음에 모아 트리 경로를 붙여 적는다. 모든 단계가 통과해야 통과다. 실패, 실행 불가, 손상되거나 없는 세션 상태 모두 막는다. 연달아 최대 3번 막고, 세 번째 사유에 "마지막"이라고 적어 Claude가 보고할 수 있게 한 뒤 그다음 종료는 놓아 주며 "게이트가 통과하지 않았다"를 사용자에게 알린다. 통과하면(같은 턴에 푸시 전 게이트가 먼저 통과한 경우도) 이 세션이 약하게 만든 검사와 하네스 파일의 변경을 보고한다. 다른 트리가 실패해 막았으면 통과한 트리의 보고는 모두 통과한 다음 종료 때 한다. 하네스 파일을 바꾼 커밋은 통과해도 pre-push 훅의 통과 목록에 적지 않는다(사람이 게시한다). 바뀐 pre-push 훅은 되돌리고 알리며, 세션 중에 `core.hooksPath`가 생겨 그 훅이 돌지 않게 됐으면 바뀐 것이 없어도 막는다. 백그라운드 에이전트(서브에이전트, 팀원 등)가 아직 돌면 게이트를 진짜 끝으로 미룬다. 백그라운드 셸과 모니터는 미루는 이유가 아니다 |
| 사용자 입력 | `UserPromptSubmit` → `hook prompt` | 막힌 횟수를 0으로. 위임 중이면 fsmap에도 넘긴다 |
| 푸시 | git `pre-push` 훅 → `git-pre-push` | Claude Code가 띄운 프로세스에서(`CLAUDE_CODE_CHILD_SESSION`이 있을 때) 보내는 ref마다 그 끝 커밋이 게이트가 깨끗한 트리에서 통과시킨 커밋인지 본다. 아니면 푸시를 거부한다. `git push`가 이 훅을 부르는 한, 명령을 어떻게 썼든(설정으로 바꾼 refspec, 먼저 옮겨 둔 ref, 별칭, 백그라운드에서 늦게 도는 푸시, 스크립트 안의 푸시) git이 실제로 보내는 커밋을 본다. 끝 커밋 아래의 커밋은 따로 보지 않는다(원격에 남는 트리는 끝 커밋의 것이다. CI도 끝 커밋을 본다). 통과 목록은 worktree마다 그 worktree의 git 디렉터리에 있다. 사람이 터미널이나 IDE의 통합 터미널에서 하는 푸시는 막지 않는다(앞선 판 코어의 훅이 깔려 있는 동안은 예외, §5) |
| 셸 명령 | `PreToolUse(Bash)` → `hook pre-bash` | 모든 명령에서: git 명령이 훅을 끄면 거부한다(`--no-verify`와 git이 받아 주는 줄임 `--no-veri`, 따옴표·역슬래시로 쪼갠 꼴, `commit -n`과 `-nm` 같은 묶음, `core.hooksPath`를 건드리는 git 명령. 되돌리는 `git config --unset core.hooksPath`만 받는다). pre-push 훅이 보는 것을 바꾸는 명령도 거부한다: `CLAUDE_CODE_CHILD_SESSION`·`CLAUDECODE`를 바꾸거나 지우거나 내보내기를 끄는 일(대입, GNU env가 읽는 대로의 `env -u`: `-vu 이름` 같은 묶음, `--un` 같은 긴 옵션 줄임, `-S`의 단어. 그리고 변수를 바꿀 수 있는 명령, 곧 `unset`·`export`·`declare`·`typeset`·`local`·`readonly`, `env -u`, 이름을 전개로 만든 명령(`${u}set`)이 있을 때, 두 이름이나 그 조각(`CLAUDECODE`·`CLAUDE_CODE_`·`CHILD_SESSION`, 다른 변수의 온전한 이름은 빼고)이 어디든 들어 있으면: `declare +x`, `v=이름; unset $v`, `x=CHILD_SESSION; unset CLAUDE_CODE_$x` 같은 간접형까지), 환경을 비우는 `env -i`·`exec -c`, git이 다른 설정을 읽게 하는 `GIT_CONFIG_*`·`--config-env`, `.git/hooks` 아래 파일을 건드리는 일, 셸 별칭(`alias.*=!…`) 정의. 깔아 둔 pre-push 훅이 지워지거나 바뀌었으면 명령 전에 되돌리고 알린다(앞선 코어가 깐 훅도 하네스의 것으로 받는다. 새 세션의 시작이 지금 판으로 바꾼다). 명령이 들어가는 worktree(`cd X`·`pushd X`, `git -C X`), 명령이 경로를 가리키는 worktree(인자, 리다이렉션 대상, `--opt=값`: `sed -i X/f`, `cp a X/`, `> X/f`, `ln -s X l`), 만드는 worktree(`git worktree add`)를 명령이 돌기 전에 적는다. 게시(`git push`, `gh pr create|new`, 별칭이면 그것이 펼쳐지는 명령, `git send-pack`·`http-push`는 그 자체로 거부)이면, 게시가 도는 트리에서 다음을 거부한다: 한 명령에서 두 worktree 이상의 게시, 지금 체크아웃한 브랜치가 아닌 것의 게시(다른 ref, `--all`·`--branches`·`--mirror`·`--tags`, `-c remote.*`·`push.*`·`branch.*`, 다른 `--head`·`--head=`·`-H`), 같은 명령 안에서 HEAD나 브랜치를 옮기는 일(commit·merge·rebase·update-ref·branch 등과 묶기. 브랜치·태그·stash·worktree 목록과 `symbolic-ref` 읽기는 옮기지 않는다), 게시하는 셸 별칭, 바로 앞에서 되돌린 훅, 쓸 수 없게 된 훅이나 세션 중에 생긴 `core.hooksPath`, 이 세션의 하네스·설정(하네스에 닿는 키)·리뷰어·CI 워크플로 변경, 커밋하지 않은 변경(파일 이름을 적는다), quick 게이트 실패나 실행 불가. 통과하면 그 커밋을 pre-push 훅이 보낼 수 있는 커밋으로 적는다. 리다이렉션(`2>&1`, `> log`)과 옵션 값(`-o ci.skip`, `--receive-pack …`)은 ref로 읽지 않는다. 다른 레포에서 도는 게시(`git -C ../other push`)는 이 하네스가 판정하지 않는다. fsmap에 맡기면 게시가 아닌 명령도 fsmap에 넘긴다 |

| 명령 | 쓰는 쪽 |
|---|---|
| `python3 -I .aok/harness/aok_harness.py verify --mode quick\|full [--json]` | 사람, 에이전트, CI. 훅과 같은 게이트 |
| `python3 -I harness/aok_harness.py install --repo <작업 레포> --adapter <이름> [--yes]` | 허브(사람 확인 뒤). `--yes` 없이는 계획만 보인다 |

## 2. 설계의 축

| 축 | 어떻게 |
|---|---|
| 규칙의 출처는 하나 | 훅·사람·CI가 같은 `verify`를 부른다. 게이트 단계는 어댑터(데이터)와 작업 레포의 `.aok/harness.json`에서 온다 |
| 언어 중립 | 코어는 언어를 모른다. 지문(HEAD + 바뀐 파일 내용 + 게이트 정의), 기준, 막힘 횟수, 약화 보고, 푸시 판단이 코어의 일이고, 무엇을 돌릴지는 어댑터가 정한다. 어댑터를 둘(Python, Rust) 두는 것은 이 분리가 실제로 언어와 무관한지 시험으로 보이기 위해서다 |
| 판정받는 쪽이 정의를 못 바꾼다 | 게이트 정의(`.aok/harness.json`)와 레포에 복사된 어댑터(`.aok/harness/adapters/`)는 세션이 시작된 커밋의 것을 쓴다. 작업 트리에서 바꾼 정의는 쓰이지 않고 "differs from the version in …"으로 보고된다. 기준 커밋에 정의가 없으면 작업 트리에 새로 만든 정의도 쓰지 않는다(커밋한 뒤 시작하는 세션부터 적용). 하네스 파일, `.claude/settings*.json`의 하네스에 닿는 키, 설치된 리뷰어, `.github/workflows/`를 바꾼 세션의 푸시는 막고 사람에게 넘긴다. 코어 코드 자체는 세션 안에서 고정하지 못한다(§5). CI는 기준 브랜치의 코어·어댑터·정의로 돈다(§3) |
| 게시는 보내는 커밋으로 판정 | 명령 읽기는 빠른 안내이고, `git push`의 결정은 git `pre-push` 훅이 보내는 ref의 끝 커밋으로 한다. 그 길로는 게이트가 깨끗한 트리에서 통과시킨 커밋만 Claude Code 안에서 나간다. 훅을 끄거나 훅이 보는 값을 바꾸는 길(`--no-verify`, `core.hooksPath`, `CLAUDE_CODE_CHILD_SESSION`, `env -i`, `GIT_CONFIG_*`, 셸 별칭, 훅 파일, 훅을 부르지 않는 `send-pack`)은 명령 읽기가 거부하고, 바뀐 훅 파일은 다음 명령 전에 되돌린다. 그 일을 스크립트 안에서 한 번에 하면 막지 못한다(§5). 그래서 정본 게이트는 CI다 |
| 닫힌 실패 | 단계를 실행할 수 없거나(필수 도구 없음, 시간 초과, 설정 오류), 세션 상태가 손상됐거나 없거나, 위임한 fsmap이 0·2가 아닌 코드로 끝나거나 시작하지 못하거나, 하네스가 죽으면 통과로 치지 않는다. 턴 종료는 막고 푸시는 거부한다. 상태를 저장하는 일까지 실패하면 그래도 block JSON을 낸다. PreToolUse 훅에는 `onFailure: "block"`을 달아, 코어가 시작조차 못 하거나 시간을 넘기면 명령을 막는다 [문서]. 선택 도구가 없으면 건너뛰되 세션에 한 번 알린다 |
| 출구가 있다 | 연달아 3번 막은 뒤 놓아 준다. 세 번째 사유가 "마지막"임을 Claude에게 알려, 그 턴 안에 무엇이 실패하는지 보고하게 한다(dev 자식은 부모에게 AOK/1 `blocked`). 막힌 자식이 예산을 태우며 맴돌지 않게 하기 위해서다. Claude Code에도 Stop 훅이 8번 연속 턴을 이어 가게 하면 다음 block을 무시하는 상한이 있고, 도구 호출마다 0으로 돌아가며 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`으로 바꾼다 [문서]. 하네스의 3번은 그보다 먼저 온다 |
| 무엇이 세션의 것인가 | 아무것도 바꾸지 않은 세션은 판정하지 않는다(기준 정의를 읽을 수 없을 때도). HEAD가 움직였으면(병합 포함) 판정한다. 시작할 때 이미 바뀌어 있던 파일은 그 뒤 내용이 바뀌거나 세션이 커밋하면 세션의 것이 된다. 커밋 전의 빈 레포에서 시작한 세션은 첫 커밋 전체가 자기 것이다. 세션의 커밋은 HEAD에서 닿되 세션 시작 때의 어떤 ref(기준, 다른 브랜치, 원격 추적 ref)에서도 닿지 않는 병합 아닌 커밋이다. 그래서 세션이 곁가지에서 만들어 병합한 커밋은 세션의 것이고, 원래 있던 브랜치를 병합한 것은 아니다. 무엇이든 바꾼 세션은 트리 전체로 판정받으므로 원래 있던 실패에도 막힌다. 막는 사유가 "건드리지 않았고 원래 있던 실패면 고치지 말고 사용자에게 말하라"고 덧붙인다 |
| worktree도 같은 세션 | 세션의 상태는 하나이고 프로젝트 디렉터리(`CLAUDE_PROJECT_DIR`, worktree에 들어가거나 `cd`해도 그대로 [문서])의 git 디렉터리에 있다. 막힘 횟수도 하나다. 같은 레포의 다른 worktree는 세션이 거기서 일했으면 판정한다: 명령이 들어가거나 경로를 가리킨 곳(`cd X`·`pushd X`, `git -C X`, `sed -i X/f`·`> X/f` 같은 인자와 리다이렉션. 명령이 돌기 전에 읽는다), 명령이 만든 곳(`git worktree add`), 파일을 편집한 곳, 턴이 끝나는 곳(훅 입력의 `cwd`). 판정은 턴이 어디서 끝나든 그 모두에 한다. HEAD는 세션 시작 때 그 worktree의 HEAD와 비교한다. 명령이 만든 worktree는 그 명령이 체크아웃할 커밋(명령이 돌기 전에 읽는다: `git worktree add X <옛 커밋>`도 움직이지 않은 것이다)과, 그 밖에 나중에 생긴 worktree는 시작 때의 ref와 비교한다(원래 있던 브랜치에 만든 worktree는 움직이지 않은 것이다). 파일은 세션이 처음 본 때와 비교하고, 처음 본 것이 명령 뒤(턴 종료의 `cwd`)이거나 명령이 만든 worktree면 바뀐 파일이 모두 세션의 것이다. 하네스 파일 변경은 처음 볼 때의 해시와도 비교한다. 게시는 게시가 도는 트리에서 판정한다. 프로젝트 디렉터리 안의 worktree(`.claude/worktrees/`)는 그 자체로 트리이고 프로젝트 디렉터리의 바뀐 파일로 치지 않는다. 다른 레포(같은 원격의 다른 클론 포함)는 이 하네스의 것이 아니다 |
| 한 번에 다 보고 | 실패한 단계 뒤에도 모든 단계를 돌려 한 번에 보고한다. 단계들은 `budget.gate` 하나를 나눠 쓰고, 시간을 넘긴 단계는 그 단계의 프로세스 그룹째 끝낸다 |

## 3. 작업 레포에 들이기

1. **사람 확인(S4).** `.claude/settings.json`을 바꾸는 일이다. 허브는 `install --repo <경로> --adapter <이름>`을 `--yes` 없이 돌려 계획을 보이고, 사용자 확인을 받은 뒤 `--yes`로 다시 돌린다.
2. 설치가 하는 일: `.aok/harness/`에 코어와 어댑터를 복사, `.aok/harness.json`(`{"adapter": "<이름>"}`)이 없으면 작성, `.claude/agents/aok-reviewer.md`(적대적 리뷰어)가 없으면 작성, `.claude/settings.json`에 훅 다섯 개와 `.aok/`·`.claude/` 편집 시 묻기(`ask`)를 더한다. `allow` 규칙은 더하지 않는다. 기존 설정과 다른 훅은 보존하고, 두 번 돌려도(인터프리터를 바꾼 뒤에도) 하네스 훅이 겹치지 않는다. 다음 경우에는 아무것도 쓰지 않고 2로 끝난다: 설정 파일을 UTF-8 JSON으로 읽을 수 없거나 객체가 아니거나 `hooks`·`permissions.ask`의 모양이 다름(손으로 합친다), 설정이 `disableAllHooks: true`, 이미 있는 `.aok/harness.json`을 읽을 수 없거나 그 어댑터가 `--adapter`와 다름(손으로 바꾼다). 이미 있는 `.aok/harness.json`과 리뷰어는 덮어쓰지 않는다. PreToolUse 훅의 시간 제한은 Stop과 같다(푸시 전에도 같은 게이트가 돈다).
3. 커밋하고 PR로 올린다. 훅은 다음 세션부터 적용된다. 첫 세션의 맥락이 `[aok-harness] adapter …`(Rust를 fsmap에 맡기면 `[harness] fsmap … hooks are active`)로 시작하는지 본다.
4. CI에는 기존 단계를 그대로 두고(정본 게이트), **기준 브랜치의 코어**로 도는 잡을 더한다. PR 체크아웃의 코어·어댑터·정의는 그 PR이 고쳤을 수 있으므로, 코어를 기준 브랜치에서 꺼내 돌리고 `--base`로 어댑터와 정의도 기준 브랜치의 것을 쓰게 한다:
   ```bash
   set -euo pipefail  # 기준 브랜치에 코어가 없으면 여기서 멈춘다
   git fetch origin main
   git show origin/main:.aok/harness/aok_harness.py > "$RUNNER_TEMP/aok_harness.py"
   python3 -I "$RUNNER_TEMP/aok_harness.py" verify --mode full --base origin/main
   ```
   하네스를 처음 들이는 PR은 기준 브랜치에 코어가 없으므로 이 잡이 실패한다. 그 PR은 사람이 검토해 병합한다.
5. 단계 덧붙이기·바꾸기는 `.aok/harness.json`에서 한다. 허용 키는 `adapter`(이름), `gate`(`quick`·`full` 단계 목록을 통째로 바꿈), `edit`(`match`·`steps`), `extra`(양쪽 게이트 끝에 덧붙임), `budget`(`gate` 1–840초, `edit` 1–100초. 훅 시간 제한 900초·120초에서 하네스의 git 호출 몫을 뺀 값)뿐이다. 단계는 `{"name": …, "cmd": [...], "optional": true|false}`이다. 모양이 틀리면 실행 불가(2)다.

## 4. 어댑터

| 어댑터 | 편집 검사 | quick 게이트 | full 게이트 | 약화 보고 |
|---|---|---|---|---|
| `python` | 컴파일(바이트코드를 쓰지 않음), ruff(선택) | ruff 버그 계열(선택), `python3 -I -B -m unittest discover -s tests -t .` | ruff(레포 설정), 시험 | `noqa`(대소문자 무관, 파일 단위 `ruff: noqa`·`flake8: noqa` 포함), `type: ignore`, 건너뛴 시험(`skip`·`skipIf`·`skipUnless`·`expectedFailure`·`SkipTest`·`__unittest_skip__`·`pytest.mark.skip/xfail`·`pytest.skip`), `load_tests` 정의·대입, `pragma: no cover`, 지운 단언(`assert`, `self.assert*`, `with self.assertRaises`, `pytest.raises`, `mock.assert_*`), 지우거나 이름을 바꾼 시험(camelCase·async 포함), `TestCase`에서 뗀 클래스, 수집되지 않는 곳으로 옮기거나 지운 시험 파일(이름이 `test\w*.py`가 아니거나 `__init__.py` 없는 디렉터리), 바뀐 검사 설정 파일(`pyproject.toml`, `setup.cfg`, `tox.ini`, ruff·flake8·pytest·coverage 설정, `conftest.py`, `sitecustomize.py`), 레포 루트에서 표준 모듈을 가리는 파일(`json.py`, `json/__init__.py` 등) |
| `rust` | `cargo check`(`*.rs`, `Cargo.toml`, `build.rs`) | fmt, clippy `-D warnings`, test | + cargo-deny(선택) | `#[allow(`·`#[expect(`(`cfg_attr` 안 포함), `#[ignore]`(`cfg_attr` 안 포함), `cfg(false)`·`cfg(any())`·`cfg(not(all()))`, `unsafe`(블록·`fn`·`impl`·`trait`·`extern`·`#[unsafe(…)]`), 지운 단언(`assert!`·`assert_eq!`·`assert_ne!`·`debug_assert*!`), 지운 `#[test]`, 바뀐 검사 설정 파일(`Cargo.toml`, `.cargo/config.toml`, 툴체인·clippy·rustfmt·deny 설정, `build.rs`) |

- Python 시험은 `-I`로 돌아, 시험 실행기(`unittest`)를 불러올 때 레포 루트가 경로에 없다. 그래서 루트의 `unittest.py`는 실행기를 가리지 못한다. 그러나 `discover -t .`가 루트를 경로 맨 앞에 두므로, 그 뒤 처음 import되는 표준 모듈(`json` 등)은 루트의 같은 이름 파일이 가릴 수 있다. 세션이 루트에 그런 파일을 만들면 약화로 보고한다(`shadow_check`). pytest식 함수 시험은 `unittest`가 모으지 않는다. pytest를 쓰는 레포는 `.aok/harness.json`의 `gate`에서 시험 단계를 `python3 -I -m pytest -q`로 바꾼다.
- 제거 규칙(지운 단언 등)은 파일마다 지운 줄 수에서 더한 줄 수를 뺀 순수 감소로 센다. 단언에 메시지를 더하는 식으로 고쳐 쓴 것은 지운 것이 아니다.

Rust는 fsmap이 `PATH`에 있고 `fsmap hook --help`가 되면 훅 다섯 개와 `verify`를 fsmap에 넘긴다. fsmap은 cargo·rustfmt·clippy·툴체인 설정의 무결성 검사, 환경 변수 정리, git pre-push 검증과 감사까지 하는 1회차 구현이다. 없으면 위 기본 단계로 돌고, 세션 시작 때 그 차이를 알린다.

fsmap에 맡길 때 달라지는 것(fsmap 74b7ffb로 확인):
- 넘기기 전에 코어가 직접 보는 것: 훅을 끄는 git 옵션, 게시 대상(체크아웃한 브랜치만), 같은 명령 안의 HEAD 이동, 이 세션의 하네스·설정·CI 변경. 나머지(게이트, 커밋 안 된 변경의 처리)는 fsmap이 정한다. 게시가 아닌 Bash 명령도 모두 fsmap에 넘긴다. 코어의 pre-push 훅은 깔지 않는다(fsmap이 자기 것을 깐다).
- fsmap의 답은 종료 코드가 0이나 2일 때만 그대로 넘긴다. 그 밖의 코드(Rust 패닉의 101 등), 시작 실패, 시간 초과는 닫힌 실패다: 턴 종료는 막고, 푸시는 거부하고, 다른 훅은 사용자에게 알린다.
- fsmap의 세션 시작 훅은 레포의 git `pre-push` 훅을 설치하고 그 사실을 맥락에 알린다. 클린룸(③) 보고의 SessionStart 출력에 남는다.
- 게이트 정의는 fsmap이 작업 트리에서 읽고, 정의를 약하게 만드는 설정은 fsmap의 무결성 단계가 본다. `verify --base`는 어댑터를 고르는 데만 쓰이고, 그렇다고 stderr에 알린다.
- `verify --json`은 fsmap의 `--json` 보고를 그대로 낸다. 종료 코드는 fsmap도 0 통과 · 1 실패 · 2 실행 불가다.

새 언어는 `harness/adapters/<이름>.json` 하나로 더한다: `detect`(이 파일이 있으면 그 언어), `versions`, `env`, `edit.match`·`edit.steps`, `gate.quick`·`gate.full`, `test_files`(시험 파일 glob), `weakening`(`added` 또는 `removed` 정규식), 선택적으로 `test_name_re`·`test_package_marker`(실행기가 시험 파일을 모으는 조건), `check_config`(바뀌면 보고할 검사 설정 파일 glob), `shadow_check`(지금은 `python-stdlib`만), `delegate`.

## 5. 한계

- **가드레일이지 보안 경계가 아니다.** 에이전트는 코어 파일, 상태 디렉터리(`<git-dir>/aok-harness/`, 통과한 커밋 목록 포함), 로컬 설정을 고칠 수 있다. 손상되거나 지워진 상태는 막지만, 하네스가 쓴 것처럼 꾸민 상태는 알아보지 못한다. 세션이 고친 코어는 그 세션에서 그대로 돈다(보고되고 푸시는 거부된다). `ask` 규칙, 푸시 거부, 통과 보고가 그런 변경을 사람 앞에 드러내고, 기준 브랜치의 코어로 도는 CI가 정본 게이트다.
- **스크립트 한 번 안에서 하는 일은 막지 못한다.** 명령 읽기는 명령 줄에 보이는 것만 본다. 스크립트나 프로그램이 한 번 도는 동안 `CLAUDE_CODE_CHILD_SESSION`을 지우거나, 환경을 비우거나, `GIT_CONFIG_*`로 훅을 끄거나, 훅 파일을 바꾸거나, `git send-pack`을 쓰고 이어서 푸시하면 게이트를 지나지 않은 커밋이 나갈 수 있다. 하네스가 할 수 있는 것은 뒤처리다: 바뀐 훅은 다음 명령과 턴 종료 때 되돌리고 알리며, 세션 중에 생긴 `core.hooksPath`는 턴 종료를 막는다. 그 사이에 나간 커밋은 CI가 잡는다.
- **설정 파일은 하네스에 닿는 키만 본다.** `.claude/settings*.json`은 `hooks`·`disableAllHooks`·`env`의 변경만 하네스 변경으로 친다(없는 파일과 그 키가 없는 파일은 같다). Claude Code는 사용자가 "다시 묻지 않기"를 고르면 `.claude/settings.local.json`에 권한 규칙을 스스로 쓰므로, 권한 규칙의 변경은 막지 않는다. 설치가 더한 `ask` 규칙을 지우는 것도 여기에 들어간다. `.claude/` 편집 자체가 그 규칙으로 사용자에게 묻는다.
- **게시 명령 읽기는 휴리스틱이고, `git push`의 판정은 pre-push 훅이 한다.** 명령 읽기(래퍼·`$(…)`·백틱·`sh -c`·`eval`·줄바꿈·git 별칭을 열어 본다)는 빠른 안내다. 같은 명령 안에서 정의한 별칭과 스크립트 파일 안의 `git push`는 명령 읽기가 놓쳐도 pre-push 훅이 보낸 커밋으로 판정한다. 훅이 보지 못하는 것: `gh api`·`gh pr merge`·MCP 도구처럼 git push를 거치지 않는 게시, core.hooksPath가 이미 있거나 남의 pre-push 훅이 있는 레포(그때는 명령 읽기뿐이고 세션 시작 때 알린다), 사람이 터미널에서 하는 푸시(의도), `CLAUDE_CODE_CHILD_SESSION`이 없는 환경(Claude Code v2.1.172 이전, stdio MCP 서버 안), 위의 스크립트 안의 일. 훅이 보는 변수의 이름이나 명령을 명령 줄에 보이지 않게 만드는 일(조각 없이 이어 붙인 이름 `a=CLAUDE; b=_CODE_…`, `$(…)`의 출력, 파일에서 읽은 이름)도 명령 읽기 밖이다. 거기서는 스크립트와 같이 pre-push 훅과 CI가 남는다. 반대로 `--no-verify`나 `core.hooksPath`라는 글자만 들어가도(커밋 메시지 안이라도) git 명령은 거부된다. `.git/hooks`를 말하는 명령(읽기만 해도)도 거부된다.
- **누가 판정받나.** IDE 확장은 통합 터미널에도 `CLAUDECODE`를 둔다 [문서]. 그래서 pre-push 훅은 Claude Code가 자기가 띄운 프로세스(Bash·Monitor 도구, 훅)에만 두는 `CLAUDE_CODE_CHILD_SESSION`을 본다 [문서]. 이 변수는 v2.1.172부터 있으므로, 그보다 오래된 Claude Code에서는 훅이 판정하지 않고 명령 읽기만 남는다. 거꾸로, Claude Code의 Bash 도구가 처음 띄운 `screen` 세션이나 백그라운드 실행기는 이 변수를 물려받을 수 있다고 문서가 적는다 [문서](tmux도 셸 환경은 같을 것이다 [추론]). 사람이 나중에 그런 셸에 붙어 하는 푸시는 판정받는다(그 셸에서 `unset CLAUDE_CODE_CHILD_SESSION`, 또는 새 터미널).
- **worktree.** 세션이 일한 worktree를 아는 길은 명령 줄(들어간 곳과 가리킨 경로), 편집한 파일, 턴이 끝나는 `cwd`다(§2). 스크립트 안에서만 들어가거나, 명령 줄에 보이지 않게 조립한 경로(`$(…)`의 출력, 셸 변수)로만 쓴 worktree는 모른다. 그곳의 커밋은 pre-push 훅이 막는다(그 worktree의 통과 목록이 비어 있다). Claude Code는 프로젝트·추가 디렉터리 밖으로 간 `cd`를 다음 명령에 넘기지 않는다 [문서]. 그래서 프로젝트 밖의 형제 worktree는 한 명령 안의 `cd X && …`나 `git -C X`로 다루면 판정받고, 그 worktree의 좋은 커밋도 그렇게 해야 푸시할 수 있다. 명령 읽기는 `( )` 안의 `cd`도 이어지는 것으로 읽으므로 다른 트리를 판정할 수 있다. 그때도 pre-push 훅은 실제로 보내는 커밋을 보고, 하네스 파일을 바꾼 커밋은 통과 목록에 없다. 처음 보기 전에 세션이 스크립트로 바꾼 파일, 그리고 처음 보기 전에 바뀐 무시된(ignored) 하네스 파일은 놓칠 수 있다(추적되는 하네스 파일은 커밋이나 바뀐 파일로 보인다). HEAD는 세션 시작 때와 비교하므로 처음 보기 전에 옮긴 HEAD도 판정한다. 남이 바꿔 둔 worktree에서 파일 하나라도 바꾸면 트리 전체로 판정받아 남의 실패에도 막힐 수 있다(그때는 막는 사유대로 사용자에게 말한다). 하네스 코어가 판이 다른 두 세션이 hooks 디렉터리 하나를 쓰면, 앞선 판의 훅은 새 코어도 받아 주지만 앞선 코어는 새 판의 훅을 한 번 자기 판으로 되돌린다. 앞선 판의 훅이 깔려 있는 동안에는 그 판대로 `CLAUDECODE`를 보므로, IDE 통합 터미널에서 사람이 하는 푸시도 판정받는다. 새 세션의 시작이 지금 판으로 바꾼다. Claude Code가 실제로 그 `cwd`를 보내는지는 문서로만 봤다 [문서].
- **커밋과 푸시를 한 명령에 묶으면** 거부된다. 커밋하고 다음 명령에서 푸시한다. 게이트 단계가 무시되지 않는 파일(시험 보고서 등)을 남기면 그 뒤 푸시가 "커밋하지 않은 변경"으로 거부된다. 거부 사유에 파일 이름이 있으니 `.gitignore`에 넣는다.
- **게이트는 작업 트리를 본다.** 무시된(ignored) 파일도 게이트가 보는 트리에 있다. 커밋에 없는 무시된 파일 덕에 통과한 커밋은 깨끗한 체크아웃에서 실패할 수 있다. 깨끗한 체크아웃의 CI가 그것을 잡는다.
- **세션의 것을 가르는 일의 한계.** 세션 중 기준 브랜치로 리베이스하거나, 세션 중에 받아 온(fetch) 남의 커밋을 병합하면 그 커밋이 세션의 것으로 센다(세션 시작 때의 ref에 없으므로). 약화는 세션이 건드린 파일에서 기준 커밋과의 차이로 세므로, 그 파일에 세션 전부터 있던 커밋 안 된 억제 주석이나 같은 파일에 병합으로 들어온 남의 변경이 세션의 약화로 보고될 수 있다. 순수 감소로 세므로 한 파일에서 시험 하나를 지우고 다른 하나를 더하면 보이지 않는다. 시험 첫 줄의 `return`처럼 정규식으로 잡을 수 없는 약화는 보지 못한다. 상태 없이 이어받은 `fork`·`resume`은 이미 바뀐 파일을 모두 세션의 것으로 세지만, 갈라지기 전에 부모가 만든 커밋은 세지 않는다.
- **훅이 도는 곳.** 클라우드 세션에서 레포의 `.claude/settings.json` 훅은 레포 하나로 연 세션(프로젝트 스레드 밖)에서 적용된다 [문서]. 원격 자식 세션에서 실제로 도는지는 시험하지 않았다 [미확인]. `docs/rollout.md`의 T7이 그 시험이다.
- **상태를 쓸 수 없을 때.** 상태 파일을 쓸 수 없으면 막힘 횟수를 임시 디렉터리에 센다. 거기도 쓸 수 없으면 턴 종료를 계속 막고, 그때의 출구는 Claude Code의 8번 연속 상한이다 [문서].
- **훅이 시작하지 못할 때.** `onFailure`는 Claude Code v2.1.295 이상에서 동작하고 Stop 훅에는 효과가 없다 [문서]. 그래서 코어가 시작조차 못 하면(인터프리터 없음, 코어 파일 손상) Stop은 막히지 않는다. PreToolUse는 막히므로 그동안에는 모든 Bash 명령이 거부되고, 사람이 고치거나 훅을 뺀다. 그보다 오래된 판에서 이 필드가 어떻게 다뤄지는지는 확인하지 않았다 [미확인].
- **백그라운드 작업.** Stop 입력의 `background_tasks`는 문서에 정의된 필드이고 셸·서브에이전트·모니터 같은 유형을 가진다 [문서]. 하네스는 그중 에이전트 유형(subagent, workflow, teammate, cloud session)이 있을 때만 미룬다. 미룬 게이트는 다음 Stop에서 돈다. 에이전트가 끝난 뒤 Claude Code가 그 턴에 Stop을 다시 보내는지는 Claude Code를 돌려 확인하지 않았다 [미확인]. 푸시 전 게이트는 미루지 않는다.
- **작업량.** quick 게이트는 턴마다 돈다. 시험이 오래 걸리면 `.aok/harness.json`의 `gate.quick`을 줄이고 full은 CI에 둔다.
- **레포 코드를 실행한다.** 시험과 빌드 스크립트가 돈다. 믿지 않는 레포에는 켜지 않는다.

## 6. 근거와 시험

| 근거 | 내용 | 등급 |
|---|---|---|
| 1회차 | 출발점 세션의 Rust 하네스와 fsmap이 독립 검증 라운드마다 게이트 우회(턴 중 커밋, 첫 커밋, 검사 도구 부재 시 꺼짐)와 공허한 훅 시험을 찾았다 | [기록됨] `reports/2026-10-30-verification-rounds.md`, fsmap `docs/agent-harness.md` |
| 검증 라운드 | 이 코어에 독립 검증(검증자 둘 + 판정자)을 세 라운드 붙였다. 라운드마다 우회·문서 불일치·공허한 장치가 나왔고, 고친 것마다 시험과 변이를 더했다 | [기록됨] `reports/2026-10-30-verification-rounds.md` §7 |
| `CLAUDE_CODE_CHILD_SESSION` | Claude Code가 Bash·PowerShell·Monitor 도구와 훅 명령으로 띄우는 프로세스에 `1`을 둔다. IDE 확장은 두지 않는다(그와 달리 `CLAUDECODE`는 IDE 통합 터미널에도 있다). v2.1.172부터. pre-push 훅은 이 값이 있을 때만 판정한다 | [문서] https://code.claude.com/docs/en/env-vars (2026-11-02 조회). 이 세션의 셸에 둘 다 있는 것은 [확인](Claude Code 2.1.316 클라우드 세션, 2026-11-02) |
| `cd`와 worktree | `cd`는 프로젝트 디렉터리나 추가 디렉터리 안에 머무는 동안만 다음 Bash 명령에 이어지고, 밖으로 가면 프로젝트 디렉터리로 돌아간다. 백그라운드로 보낸 명령의 `cd`·`pushd`는 이어지지 않는다. `EnterWorktree`는 새 worktree를 만들거나 `path`로 있는 worktree에 들어간다(`.claude/worktrees/` 밖이면 묻는다) | [문서] https://code.claude.com/docs/en/tools-reference (2026-11-02 조회) |
| 훅 계약 | 블록·거부·맥락의 JSON 필드(`decision: "block"`·`reason`, `hookSpecificOutput.permissionDecision: "deny"`, `additionalContext`, `systemMessage`), 명령 훅 시간 초과 기본값 600초, `onFailure`(v2.1.295+, Stop에는 효과 없음), Stop 입력의 `background_tasks`, 8번 연속 상한, worktree에 들어가거나 `cd`해도 `CLAUDE_PROJECT_DIR`는 그대로이고 입력의 `cwd`가 따라감 | [문서] https://code.claude.com/docs/en/hooks (2026-11-01 조회) |
| 시험 | `tests/test_harness.py`가 훅 다섯 개, `verify`, `install`, 두 어댑터의 동작을 시험한다. `tests/mutations/harness.json`이 코어와 어댑터의 장치를 하나씩 끄고, 변이마다 지정한 시험이 실패한다. CI의 harness 작업이 목록을 네 조각으로 나눠 조각마다 전체 시험으로 돌린다 | [확인] `.github/workflows/ci.yml`의 harness 작업: `python -I scripts/vacuity_check.py --repo . --jobs 4 --timeout 900 --test "python -m unittest tests.test_harness" --mutations <조각>` |
| 실제 fsmap | 가짜 실행 파일이 아니라 빌드한 fsmap으로 훅 다섯 개와 `verify`가 넘어가고, 막힘·통과와 종료 코드가 그대로 돌아온다. CI의 harness 작업이 고정 커밋을 빌드해 돌린다. `AOK_FSMAP`이 없으면 이 시험은 건너뛰므로 결과에 `skipped`가 없어야 한다 | [확인] `cargo install --git https://github.com/d-j-lee/filesystemMap --rev 74b7ffb --locked && AOK_FSMAP="${CARGO_HOME:-$HOME/.cargo}/bin/fsmap" python3 -m unittest tests.test_harness.RealFsmapTests -v` |
