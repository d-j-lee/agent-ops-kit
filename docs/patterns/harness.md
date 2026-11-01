# ⑧ 작업 레포 하네스

| 항목 | 내용 |
|---|---|
| 실행 스킬 | `.claude/skills/aok-harness` (`/aok-harness`) |
| 코드 | `harness/aok_harness.py`(언어 중립 코어), `harness/adapters/*.json`(언어 어댑터), `harness/agents/aok-reviewer.md`(적대적 리뷰어) |
| 계약 | 게이트 종료 코드 0 통과 · 1 실패 · 2 실행 불가. 훅은 Claude Code 훅 JSON으로 답한다 |
| 상태 | v1 기능(D26, `docs/proposals/regression-harness-hub.md`) |
| 회차 | 1회차는 fsmap(`d-j-lee/filesystemMap`)의 Rust 하네스다. 이 코어가 2회차이고 정본이다. Rust 어댑터는 fsmap이 설치돼 있으면 모든 훅과 게이트를 그것에 맡긴다 |

AOK의 다른 기능은 허브와 세션의 층에서 돈다. ⑧은 자식(dev)이 실제로 코드를 고치는 **작업 레포 안**에 둔다. 편집 직후 검사, 턴 종료 게이트, 푸시 전 게이트, 사람·에이전트·CI가 같이 쓰는 단일 진입점(`verify`)이 한 파일에서 나온다. 깨진 코드로 끝난 턴을 사람이 PR에서 처음 보는 일을 줄이는 것이 목적이다 [추론].

## 1. 무엇이 언제 도나

| 순간 | 훅 | 하는 일 |
|---|---|---|
| 세션 시작 | `SessionStart` → `hook session-start` | `startup`에서만 기준(HEAD, 이미 바뀐 파일의 해시)을 기록한다. 어댑터와 실제 도구 판을 Claude에게 알린다. 어댑터를 고를 수 없거나 설정을 읽을 수 없으면 "OFF"와 이유를 사용자에게 보인다 |
| 편집 직후 | `PostToolUse(Edit\|Write\|MultiEdit)` → `hook post-edit` | 어댑터가 맡는 파일이면 그 파일에 편집 검사(Python: 컴파일, ruff / Rust: cargo check)를 돌린다. 실패는 block 사유로 Claude에게 간다 |
| 턴 종료 | `Stop` → `hook stop` | 이 세션이 트리를 바꿨고 이미 통과한 트리가 아니면 quick 게이트를 돌린다. 모든 단계가 통과해야 통과다. 실패와 실행 불가 모두 막는다. 3번 연속 막히면 놓아 주고 "게이트가 통과하지 않았다"를 사용자에게 알린다. 통과하면 이 세션이 약하게 만든 검사(억제 주석, 건너뛴 시험, 지운 단언)와 하네스 자체의 변경을 보고한다 |
| 사용자 입력 | `UserPromptSubmit` → `hook prompt` | 막힌 횟수를 0으로 |
| 셸 명령 | `PreToolUse(Bash)` → `hook pre-bash` | `git push`·`gh pr create`이면 막는다: 훅을 끄는 옵션(`--no-verify`, `core.hooksPath`), 이 세션에서 하네스·설정이 바뀜, 커밋하지 않은 변경(게이트가 본 트리와 푸시할 커밋이 다름), quick 게이트 실패나 실행 불가 |

| 명령 | 쓰는 쪽 |
|---|---|
| `python3 -I .aok/harness/aok_harness.py verify --mode quick\|full [--json]` | 사람, 에이전트, CI. 훅과 같은 게이트 |
| `python3 -I harness/aok_harness.py install --repo <작업 레포> --adapter <이름> [--yes]` | 허브(사람 확인 뒤). `--yes` 없이는 계획만 보인다 |

## 2. 설계의 축

| 축 | 어떻게 |
|---|---|
| 규칙의 출처는 하나 | 훅·사람·CI가 같은 `verify`를 부른다. 게이트 단계는 어댑터(데이터)와 작업 레포의 `.aok/harness.json`에서 온다 |
| 언어 중립 | 코어는 언어를 모른다. 지문(HEAD + 바뀐 파일 내용 + 게이트 정의), 기준, 막힘 횟수, 약화 보고, 푸시 판단이 코어의 일이고, 무엇을 돌릴지는 어댑터가 정한다. 어댑터를 둘(Python, Rust) 두는 것은 이 분리가 실제로 언어와 무관한지 시험으로 보이기 위해서다 |
| 판정받는 쪽이 기준을 못 바꾼다 | 게이트 정의는 세션이 시작된 커밋의 `.aok/harness.json`에서 읽는다. 작업 트리에서 바꾸면 쓰이지 않고 보고된다. `.aok/`와 `.claude/settings*.json`을 바꾼 세션의 푸시는 막고 사람에게 넘긴다 |
| 닫힌 실패 | 단계를 실행할 수 없거나(필수 도구 없음, 시간 초과, 설정 오류) 하네스가 죽으면 통과로 치지 않는다. 턴 종료는 막고, 푸시는 거부한다. 선택 도구가 없으면 건너뛰되 세션에 한 번 알린다 |
| 출구가 있다 | 3번 막히면 놓아 준다. 막힌 자식이 예산을 태우며 맴돌지 않고 AOK/1 `blocked`로 보고할 수 있게 하기 위해서다. Claude Code 쪽에도 연속 블록 상한이 있다는 보고가 있으나 원문에서 확인하지 못했다 [미확인]. 하네스는 그것에 기대지 않는다 |
| 남의 실패는 남의 것 | 세션이 아무것도 바꾸지 않았으면 판정하지 않는다. 시작할 때 이미 바뀌어 있던 파일은 그 뒤 내용이 바뀐 경우에만 세션의 것으로 센다 |
| 한 번에 다 보고 | 실패한 단계 뒤에도 모든 단계를 돌려 한 번에 보고한다 |

## 3. 작업 레포에 들이기

1. **사람 확인(S4).** `.claude/settings.json`을 바꾸는 일이다. 허브는 `install --repo <경로> --adapter <이름>`을 `--yes` 없이 돌려 계획을 보이고, 사용자 확인을 받은 뒤 `--yes`로 다시 돌린다.
2. 설치가 하는 일: `.aok/harness/`에 코어와 어댑터를 복사, `.aok/harness.json`(`{"adapter": "<이름>"}`)이 없으면 작성, `.claude/agents/aok-reviewer.md`(적대적 리뷰어)가 없으면 작성, `.claude/settings.json`에 훅 다섯 개와 `.aok/`·`.claude/` 편집 시 묻기(`ask`)를 더한다. 기존 설정은 보존하고, 두 번 돌려도 훅이 겹치지 않는다.
3. 커밋하고 PR로 올린다. 훅은 다음 세션부터 적용된다. 첫 세션의 맥락이 `[aok-harness] adapter …`로 시작하는지 본다.
4. CI에는 기존 단계를 그대로 두고(정본 게이트), 기준 브랜치의 정의로 도는 잡을 더한다. PR이 고친 정의가 아니라 기준 브랜치의 정의로 판정하기 위해서다:
   ```bash
   git fetch origin main
   python3 -I .aok/harness/aok_harness.py verify --mode full --base origin/main
   ```
5. 단계 덧붙이기·바꾸기는 `.aok/harness.json`에서 한다. 허용 키는 `adapter`, `gate`(`quick`·`full` 단계 목록을 통째로 바꿈), `edit`, `extra`(양쪽 게이트 끝에 덧붙임), `budget`(`edit`·`gate` 초)뿐이다. 단계는 `{"name": …, "cmd": [...], "optional": true|false}`이다.

## 4. 어댑터

| 어댑터 | 편집 검사 | quick 게이트 | full 게이트 | 약화 보고 |
|---|---|---|---|---|
| `python` | 컴파일(바이트코드를 쓰지 않음), ruff(선택) | ruff 버그 계열(선택), `unittest discover -s tests` | ruff(레포 설정), 시험 | `noqa`, `type: ignore`, 건너뛴 시험, `pragma: no cover`, 지운 단언 |
| `rust` | `cargo check` | fmt, clippy `-D warnings`, test | + cargo-deny(선택) | `#[allow(`, `#[ignore]`, `cfg(false)`, `unsafe`, 지운 단언 |

Rust는 fsmap이 `PATH`에 있고 `fsmap hook`을 알면 모든 훅과 `verify`를 fsmap에 넘긴다. fsmap은 cargo·rustfmt·clippy·툴체인 설정의 무결성 검사, 환경 변수 정리, git pre-push 검증과 감사까지 하는 1회차 구현이다. 없으면 위 기본 단계로 돌고, 세션 시작 때 그 차이를 알린다.

새 언어는 `harness/adapters/<이름>.json` 하나로 더한다: `detect`(이 파일이 있으면 그 언어), `versions`, `env`, `edit.match`·`edit.steps`, `gate.quick`·`gate.full`, `weakening`(`added` 또는 `removed` 정규식), 선택적으로 `delegate`.

## 5. 한계

- **가드레일이지 보안 경계가 아니다.** 에이전트는 코어 파일, 상태 디렉터리(`<git-dir>/aok-harness/`), 로컬 설정을 고칠 수 있다. `ask` 규칙과 푸시 거부가 그런 변경을 사람 앞에 드러내고, 깨끗한 체크아웃에서 기준 브랜치 정의로 도는 CI가 정본 게이트다.
- **푸시 명령 읽기는 휴리스틱이다.** `git … push`와 `gh pr create`를 정규식으로 찾는다. 별칭, 스크립트 파일, `gh api`·MCP 도구로 하는 게시는 보지 못한다. 커밋 메시지 안의 "git push" 같은 글은 게시로 오인될 수 있다(게이트를 한 번 더 돌릴 뿐이다). git 자체의 pre-push 훅으로 보는 방식은 fsmap(1회차)에 있다.
- **커밋과 푸시를 한 명령에 묶으면** 실행 전에는 커밋되지 않은 변경이 있어 거부된다. 커밋하고 다음 명령에서 푸시한다.
- **훅이 도는 곳.** 클라우드 세션에서 레포의 `.claude/settings.json` 훅은 레포 하나로 연 세션(프로젝트 스레드 밖)에서 적용된다 [문서]. 원격 자식 세션에서 실제로 도는지는 시험하지 않았다 [미확인]. `docs/rollout.md`의 T7이 그 시험이다.
- **백그라운드 작업.** Stop 입력에 `background_tasks`가 있으면 게이트를 미룬다. 이 필드는 공식 문서에 없고, fsmap이 실제 세션 기록에서 본 것이다 [기록됨]. 필드가 없으면 미루지 않는다.
- **작업량.** quick 게이트는 턴마다 돈다. 시험이 오래 걸리면 `.aok/harness.json`의 `gate.quick`을 줄이고 full은 CI에 둔다.
- **레포 코드를 실행한다.** 시험과 빌드 스크립트가 돈다. 믿지 않는 레포에는 켜지 않는다.

## 6. 근거와 시험

| 근거 | 내용 | 등급 |
|---|---|---|
| 1회차 | 출발점 세션의 Rust 하네스와 fsmap이 독립 검증 라운드마다 게이트 우회(턴 중 커밋, 첫 커밋, 검사 도구 부재 시 꺼짐)와 공허한 훅 시험을 찾았다 | [기록됨] `reports/2026-10-30-verification-rounds.md`, fsmap `docs/agent-harness.md` |
| 훅 계약 | 블록·거부·맥락의 JSON 필드(`decision: "block"`·`reason`, `hookSpecificOutput.permissionDecision: "deny"`, `additionalContext`, `systemMessage`), 명령 훅 시간 초과 기본값 600초 | [문서] https://code.claude.com/docs/en/hooks (2026-11-01 조회) |
| 시험 | `tests/test_harness.py`: 문서화한 동작마다 시험이 있고, 장치를 하나씩 끈 변이(`tests/mutations/harness.json`)마다 지정한 시험이 실패한다. CI가 돌린다 | [확인] `python3 -I scripts/vacuity_check.py --repo . --test "python3 -m unittest tests.test_harness" --mutations tests/mutations/harness.json --jobs 4` |
