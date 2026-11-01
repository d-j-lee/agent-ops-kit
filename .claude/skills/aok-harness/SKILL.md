---
name: aok-harness
description: AOK ⑧ 작업 레포 하네스(docs/patterns/harness.md) 런북. 자식(dev)이 코드를 고치는 작업 레포에 편집 직후 검사·턴 종료 게이트·푸시 전 게이트·단일 진입점 verify를 들인다. 코어 harness/aok_harness.py(언어 중립)와 어댑터 harness/adapters/{python,rust}.json(Rust는 fsmap이 있으면 위임). 설치는 install --repo --adapter로 계획을 먼저 보이고 사람 확인(S4) 뒤 --yes. 게이트 정의와 어댑터 사본은 세션 시작 커밋의 것, CI는 기준 브랜치의 코어로 verify --base origin/main. 게이트가 막을 때의 대응(고치기, 약화 금지, 세 번째 막힘에서 AOK/1 blocked 보고), 게이트 실패·실행 불가·하네스 OFF 진단, 새 언어 어댑터 추가를 다룬다. 작업 레포에 dev 자식을 붙이기 전, "하네스 설치/게이트/훅이 막는다/verify" 요청, 자식의 [aok-harness] 메시지를 해석할 때 로드한다. 결정적 전후 비교는 aok-regress(⑦), 판단이 필요한 검증은 aok-verify(①).
---

# aok-harness — 작업 레포 하네스 런북

규범: `docs/patterns/harness.md`. 이 스킬은 그 절차를 실행 순서로 줄인 것이다. 코드는 `<KIT>/harness/`다(`<KIT>` = 이 키트의 경로).

## 1. 들이기 (허브, 사람 확인 필요)

1. 어댑터를 고른다. 레포에 `pyproject.toml`·`setup.py`·`setup.cfg`·`requirements.txt`가 있으면 `python`, `Cargo.toml`이면 `rust`. 둘 다 아니면 새 어댑터가 필요하다(4절).
2. 계획만 보인다. `--yes` 없이는 아무것도 쓰지 않는다.
   ```
   python3 -I <KIT>/harness/aok_harness.py install --repo "$REPO" --adapter python
   ```
3. 계획(복사할 파일, `.aok/harness.json`, 리뷰어 `.claude/agents/aok-reviewer.md`, `.claude/settings.json`에 더할 훅 다섯 개와 `.aok/`·`.claude/` 편집 시 `ask`)을 사용자에게 보이고 확인을 받는다(S4: settings 변경).
4. 확인을 받은 뒤에만 `--yes`로 다시 돌린다. 기존 설정은 보존되고, 두 번 돌려도 훅이 겹치지 않는다. 설정 파일의 모양이 다르거나 이미 있는 `.aok/harness.json`의 어댑터가 다르면 아무것도 쓰지 않고 2로 끝난다. 그때는 사용자와 손으로 합친다.
5. 커밋하고 PR로 올린다. 병합이 곧 사람 확인이다. 훅은 다음 세션부터 돈다.
6. 첫 세션 맥락이 `[aok-harness] adapter …`(Rust를 fsmap에 맡기면 `[harness] fsmap … hooks are active`)로 시작하는지 본다. `[aok-harness] OFF: …`면 3절.
7. CI에는 기존 단계를 그대로 두고, 기준 브랜치의 코어·어댑터·정의로 도는 잡을 더한다. PR이 하네스를 고쳤어도 기준 브랜치의 것이 판정한다.
   ```
   set -euo pipefail
   git fetch origin main
   git show origin/main:.aok/harness/aok_harness.py > "$RUNNER_TEMP/aok_harness.py"
   python3 -I "$RUNNER_TEMP/aok_harness.py" verify --mode full --base origin/main
   ```

## 2. 자식이 하네스 아래에서 일할 때

| 신호 | 할 일 |
|---|---|
| 편집 직후 block(컴파일·린트 오류) | 그 파일을 고친다 |
| 턴 종료 block(quick 게이트 실패) | 실패한 단계의 출력을 보고 코드를 고친다. 억제 주석, 건너뛴 시험, 지운 단언, `.aok/`·settings 편집으로 통과시키지 않는다. 통과 때 그런 변경이 사용자에게 보고된다. 건드리지 않은 코드에 원래 있던 실패면 고치지 말고 사용자에게 말한다 |
| 턴 종료 block(실행 불가: 필수 도구 없음, 시간 초과, 설정 오류, 세션 상태 손상·없음, fsmap 실패) | 통과가 아니다. 도구를 설치하거나 원인을 보고한다. 상태가 손상됐거나 없으면 새 세션이 새 기준을 잡는다 |
| 세 번째 block("This is the last block") | 지금 고칠 수 없으면 더 돌지 말고 그 턴 안에 AOK/1 `blocked`로 실패 단계와 출력 첫 줄을 부모에게 보내고 턴을 끝낸다. 그 종료는 놓아 준다(사용자에게 "게이트가 통과하지 않았다"가 간다) |
| git이 푸시를 거부("[aok-harness] push refused") | pre-push 훅이 보낸 커밋 중 게이트가 통과시키지 않은 것을 찾았다. 그 커밋을 체크아웃해 게이트를 통과시킨 뒤 푸시한다. 다른 브랜치나 옮겨 둔 ref를 보내려 하지 않는다 |
| `git push`·`gh pr create` 거부 | 이유를 읽는다. 커밋하지 않은 변경이면 커밋하고 다음 명령에서 푸시한다(한 명령에 묶으면 거부된다). 게이트가 남긴 파일이면 `.gitignore`에 넣는다. 체크아웃한 브랜치만 푸시한다. 하네스·settings·CI 워크플로를 바꾼 세션이면 푸시하지 말고 사람에게 넘긴다 |
| git 명령 거부("hooks off") | `--no-verify`·`commit -n`·`core.hooksPath` 없이 다시 낸다. 하네스가 `core.hooksPath`를 지우라고 하면 `git config --unset core.hooksPath`는 받는다 |
| 명령 거부(`CLAUDECODE`, `env -i`, `GIT_CONFIG_*`, `.git/hooks`, 셸 별칭, `send-pack`) | pre-push 훅이 보는 것을 바꾸는 꼴이다. 그 부분 없이 낸다(필요한 변수만 넘기고, 푸시는 `git push`로). git 훅을 바꿔야 하면 사용자에게 말한다 |
| "pre-push hook ... restored" | 바뀐 훅을 하네스가 되돌렸다. 게시 명령이었다면 다시 낸다. 무엇이 훅을 바꿨는지 사용자에게 말한다 |
| 사람·CI와 같은 판정이 필요할 때 | `python3 -I .aok/harness/aok_harness.py verify --mode quick --json` (종료 0 통과 · 1 실패 · 2 실행 불가) |

## 3. 진단

- **OFF**: 어댑터를 고를 수 없거나 `.aok/harness.json`을 읽을 수 없다. 세션 시작 커밋의 그 파일이 정의다. 작업 트리에서 고친 정의는 쓰이지 않고 보고만 된다. OFF여도 세션이 무엇인가를 바꾸면 턴 종료는 막히고 푸시는 거부된다. 사람이 정의를 고쳐 커밋한다.
- **게이트가 매 턴 너무 오래 걸림**: `.aok/harness.json`의 `gate.quick`을 줄이고 full은 CI에 둔다. 정의 변경은 PR로(사람 확인).
- **선택 도구가 없음**(예: ruff): 그 단계는 건너뛰고 세션에 한 번 알린다. 필수 도구가 없으면 실행 불가로 막는다.
- **상태**: `<git-dir>/aok-harness/session-<id>.json`(기준, 막힘 횟수, 마지막 통과·보고 지문), `log.jsonl`. git이 없으면 XDG 상태 디렉터리. 손상되거나 없으면 턴 종료를 막고 푸시를 거부한다.
- **pre-push 훅**: 세션 시작 때 git 디렉터리의 `hooks/pre-push`에 깔린다(작업 트리 밖). Claude Code 안에서만(`CLAUDECODE`) 보내는 ref의 끝 커밋이 통과한 커밋일 때만 보낸다. 통과한 커밋 목록은 `<git-dir>/aok-harness/passed.json`. 바뀌거나 지워지면 다음 셸 명령과 턴 종료 때 되돌린다. 세션 중에 `core.hooksPath`가 생기면 턴 종료를 막는다. core.hooksPath가 처음부터 있거나 남의 pre-push 훅이 있으면 깔지 않고 세션 시작 때 알린다(명령 읽기만 남는다). 스크립트가 한 번 도는 동안 훅이 보는 값을 바꾸고 푸시하는 것은 막지 못한다. CI가 정본이다.
- **Rust**: `fsmap`이 `PATH`에 있고 `fsmap hook --help`가 되면 훅 다섯 개와 verify를 fsmap(1회차 구현)에 넘긴다. fsmap이 0·2가 아닌 코드로 끝나거나 시작하지 못하면 닫힌 실패다. 그때는 fsmap이 git `pre-push` 훅을 설치하고, 게이트 정의를 작업 트리에서 읽어 무결성 단계로 본다(`--base`는 어댑터 선택에만 쓰인다). 없으면 기본 cargo 단계로 돌고 세션 시작 때 그 차이를 알린다.

## 4. 새 언어 어댑터

`harness/adapters/<이름>.json` 하나로 더한다. 키: `detect`, `versions`, `env`, `edit.match`·`edit.steps`, `gate.quick`·`gate.full`, `test_files`, `weakening`(`added`·`removed` 정규식), 선택적으로 `test_name_re`·`test_package_marker`(시험 파일을 모으는 조건), `check_config`(바뀌면 보고할 검사 설정), `shadow_check`, `delegate`. 단계는 `{"name", "cmd": [...], "optional"}`. 어댑터를 더하면 `tests/test_harness.py`에 그 언어의 시험과 `tests/mutations/harness.json`에 변이를 더하고 `vacuity_check`로 공허하지 않은지 본다(⑦).

## 5. 하지 않을 것

- 사람 확인 없이 `install --yes`를 돌리지 않는다(S4).
- 게이트를 통과시키려고 정의·억제·시험을 고치지 않는다. 정의 변경은 별도 PR이다.
- 하네스를 보안 경계로 설명하지 않는다. 에이전트는 코어·상태·로컬 설정을 고칠 수 있다. 정본 게이트는 깨끗한 체크아웃에서 기준 브랜치의 코어로 도는 CI다.
- 원격 자식 세션에서 프로젝트 훅이 실제로 도는지는 아직 시험하지 않았다(`docs/rollout.md` T7). 그 전에는 사람이 여는 세션에 먼저 쓴다.
