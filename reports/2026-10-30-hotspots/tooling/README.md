# Rust 도구 채택 (Rust 코드 0줄)

AI가 쓴 코드를 상시 검사하는 빠른 게이트. 측정은 2 vCPU 클라우드 샌드박스 기준이다.

## work-atlas (Python)

```toml
# pyproject.toml — 버그 계열 규칙만 먼저. 스타일 규칙은 팀 합의 후
[tool.ruff.lint]
select = ["F", "B", "E9", "PLE"]
```

```yaml
# .github/workflows/integrity.yml — 설치는 uv 로, 린트는 테스트 전에
- uses: astral-sh/setup-uv@v6        # 버전 태그는 적용 시 확인
- run: uv pip install --system -e .
- run: uvx ruff@0.17.3 check atlas tests   # 버전 고정: 새 규칙이 CI 를 갑자기 깨지 않게. 올릴 때는 의도적으로
```

측정: ruff 0.17.3, 16,738줄 검사 37~42ms. 버그 계열 18건(+ 쓰이지 않는 `noqa` 42건).
B023(루프 변수를 늦게 묶는 클로저) 2곳은 같은 반복 안에서 바로 호출돼 **지금은 버그가 아님** — 가치는 예방 게이트.
uv 0.13.6: 가상환경 생성 + `-e .` 설치, 캐시 없음 13.5초 → 6.0초, 캐시 있음 6.6초 → 1.2초 (각 1회 측정).

## docbench (TypeScript)

```bash
npx oxlint@1 src server bin scripts      # package.json scripts.check 앞단에 추가
```

측정: oxlint 1.90.0, 약 2만 1천 줄 0.10~0.13초(npx 기동 약 1.1초 제외), 경고 34건 — 스타일·쓰이지 않는 선언 4개, 확인된 버그 없음.
