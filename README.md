# DEX Workers — Claude Code plugin

Claude Code를 항상 메인 에이전트로 유지하면서, Claude가 명확한 하위 작업을 위임할 때만 Claude native subagent, Codex CLI, Antigravity `agy` 중 하나를 자동 선택하는 독립 플러그인입니다. 프롬프트를 가로채지 않으며 사용자가 별도 실행 명령을 입력할 필요가 없습니다. Python 3 표준 라이브러리만 사용하며 `dex-usage`를 import하지 않습니다. 설치·인증 준비 상태와 사용량 캐시의 남은 비율을 라우팅 신호로 사용합니다.

## 한국어 사용법

```bash
claude --plugin-dir "$PWD/plugins/dex-workers"
```

- `delegate` skill: 구현은 Claude/Codex 중심, 일반 리뷰는 Antigravity 우선으로 라우팅합니다. 심층·고위험 검증은 Claude/Codex가 판정을 주도합니다.
- 로컬에 설치·로그인된 Antigravity는 별도 실행 승인 없이 일반 자동 후보로 `agy`를 실행합니다. 인증정보를 추출하거나 복사하지 않습니다.
- `/dex-workers:run <작업>`: 선택·실행을 수동 진단할 때만 선택적으로 사용합니다.
- `/dex-workers:review <검토 관점>`: 일반 단일 리뷰 또는 3-provider 다관점 리뷰를 요청합니다. 다관점 모드는 Claude native, Codex, Antigravity를 모두 사용하되 신뢰 가능한 잔량이 5% 미만인 provider만 제외합니다. 잔량 미상인 준비된 Antigravity는 포함됩니다.
- `/dex-workers:doctor`, `/dex-workers:status`: 설치·로그인·라우팅 상태를 진단합니다.
- `/dex-workers:cancel <run-id>`: 이 플러그인이 관리 중인 프로세스 그룹만 종료합니다. 다른 터미널이나 병렬 Claude 세션에서 실행 중인 작업을 취소할 때 사용합니다.

모든 실행은 기본 읽기 전용입니다. 파일 변경은 사용자가 명시적으로 허용한 경우에만 `run ... --write`를 지정해야 합니다. `review`는 항상 읽기 전용입니다. 실행 파일이 없거나, 로그인되지 않았거나, 현재 CLI가 지원되지 않거나, 실행 실패/시간 초과가 발생하면 JSON `status: CLAUDE_FALLBACK`을 반환합니다. 이 결과는 Claude가 작업을 직접 계속하라는 의미입니다.

자동 위임에는 아래 명령이 필요하지 않습니다. 직접 실행은 진단용입니다:

```bash
plugins/dex-workers/bin/dex-workers status
plugins/dex-workers/bin/dex-workers select
plugins/dex-workers/bin/dex-workers run "문제를 분석해 줘" --cwd "$PWD"
plugins/dex-workers/bin/dex-workers run "변경 방법을 분석" --cwd "$PWD" --provider codex
plugins/dex-workers/bin/dex-workers run "명시적으로 승인된 변경을 적용" --cwd "$PWD" --provider codex --write
plugins/dex-workers/bin/dex-workers review "보안과 회귀 위험 중심" --cwd "$PWD"
```

`dex-usage` 캐시는 `${DEX_USAGE_CACHE_DIR}/usage.json`, `${XDG_CACHE_HOME}/dex-usage/usage.json`, 또는 `~/.cache/dex-usage/usage.json` 순서의 해당 위치에서 읽습니다. 기존 v1 요약 캐시와 5시간/주간 창을 보존하는 v2 캐시를 모두 지원하며, 라우팅에는 provider별 보수적인 `remaining_percent` 요약값을 사용합니다. 캐시가 없거나 잘못되어도 정상 동작합니다. credential 파일과 환경 변수는 출력하거나 상태 파일에 기록하지 않습니다.

Antigravity에는 신뢰할 수 있는 headless quota 계약이 없으므로 숫자를 만들어내지 않습니다. `agy`가 준비됐지만 quota가 unknown이면 일반 리뷰 후보 및 다관점 리뷰에 포함합니다. 5% 경계는 정확히 `< 5%`이며 5%는 포함됩니다. 다관점 결과는 메인 Claude가 중복과 충돌을 종합하고, 중요 판정은 Claude/Codex 확인을 기준으로 합니다. Antigravity 단독 지적은 보조·미확인으로 표시합니다.

## English guide

Load with `claude --plugin-dir "$PWD/plugins/dex-workers"`. Claude remains the main agent. When it decides to delegate a bounded subtask, the auto-invocable `delegate` skill calls the selector and routes that subtask to a native Claude subagent, Codex, or Antigravity. It does not intercept prompts and users do not need to type a run command. `/dex-workers:run` and `/dex-workers:review` remain optional diagnostics, alongside `/dex-workers:doctor`, `/dex-workers:status`, and `/dex-workers:cancel`.

Runs are read-only by default. A workspace-writing run requires an explicit, user-authorized `run ... --write`; `review` is always read-only. Automatic routing excludes missing, unauthenticated, or unsupported CLIs; a valid `dex-usage` cache is advisory. Any unavailable route, launch failure, timeout, or worker failure returns a clear `CLAUDE_FALLBACK` result so Claude can continue locally. Cancellation targets only a run ID created by this wrapper. Provider output is captured as structured JSON; credentials and environment contents are never logged.

## Marketplace

```bash
claude plugin marketplace add /absolute/path/to/plugins/dex-workers
claude plugin install dex-workers@dex-team --scope user
```

For an auditable team handoff, use `scripts/install-folder.py <destination>` or create a deterministic archive with `scripts/package.py --out <outside-plugin-dir>/dex-workers.tar.gz`. Both tools ship only the fixed release inventory and refuse symbolic links or special source files.
