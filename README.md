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

Antigravity에는 신뢰할 수 있는 headless quota 계약이 없으므로 숫자를 만들어내지 않습니다. 별도 `dex-usage` setup으로 opt-in한 TUI 수집기가 v3 캐시에 검증된 5시간/7일 값과 보수적 요약값을 기록한 경우에만 그 요약을 라우팅 참고값으로 사용합니다. `agy`가 준비됐지만 quota가 unknown이면 일반 리뷰 후보 및 다관점 리뷰에 포함합니다. 5% 경계는 정확히 `< 5%`이며 5%는 포함됩니다. 다관점 결과는 메인 Claude가 중복과 충돌을 종합하고, 중요 판정은 Claude/Codex 확인을 기준으로 합니다. Antigravity 단독 지적은 보조·미확인으로 표시합니다.

## English guide

Load with `claude --plugin-dir "$PWD/plugins/dex-workers"`. On the first SessionStart after enablement, an official async plugin hook safely installs the managed delegation-first block in `~/.claude/CLAUDE.md`; because it is asynchronous, the newly written policy is guaranteed for later sessions, not necessarily the session already loading. It preserves unrelated content, creates a timestamped backup, is idempotent, fails open, and refuses malformed or duplicate managed markers. Claude remains the main agent and routes bounded subtasks through the auto-invocable `delegate` skill.

Runs are read-only by default. A workspace-writing run requires an explicit, user-authorized `run ... --write`; `review` is always read-only. Automatic routing excludes missing, unauthenticated, or unsupported CLIs; a valid `dex-usage` cache is advisory. Any unavailable route, launch failure, timeout, or worker failure returns a clear `CLAUDE_FALLBACK` result so Claude can continue locally. Cancellation targets only a run ID created by this wrapper. Provider output is captured as structured JSON; credentials and environment contents are never logged.

## Marketplace

```bash
claude plugin marketplace add /absolute/path/to/plugins/dex-workers
claude plugin install dex-workers@dex-team --scope user
```

플러그인을 활성화한 뒤 첫 Claude Code `SessionStart`에서 공식 비동기 plugin lifecycle hook이 전역 기본 위임 정책(동시 최대 5개)을 자동 적용합니다. 시작을 막지 않으며, 기존 `~/.claude/CLAUDE.md`의 다른 내용은 보존되고 변경 전 `~/.claude/backups/CLAUDE.md.before-dex-workers.<timestamp>` 백업이 생성됩니다. 비동기이므로 방금 시작한 세션이 아니라 이후 세션부터 정책 로드가 보장됩니다. 이후 실행은 멱등이며 관리 마커가 누락·중복·손상된 경우 파일을 덮어쓰지 않고 짧은 진단만 남긴 뒤 Claude 시작을 계속합니다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" setup-user --dry-run
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" setup-user
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" setup-user --check
```

자동 정책을 끄려면 아래 durable opt-out을 사용합니다. `~/.claude/dex-workers/auto-policy.disabled`가 업데이트와 재설치 후에도 유지되므로 이후 SessionStart가 정책을 다시 켜지 않습니다. 다시 사용하려는 경우에만 명시적으로 enable 하십시오.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" disable-auto-policy
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" enable-auto-policy
```

정책을 제거하고 자동 재적용도 끄려면 `restore-user`를 사용합니다. 유효한 관리 구간만 제거하고 먼저 백업합니다. 손상되거나 중복된 마커는 절대 자동 복구/삭제하지 않습니다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" restore-user
```

제거 권장 순서는 `restore-user`, `claude plugin uninstall dex-workers@dex-team --scope user`입니다. uninstall은 `~/.claude/CLAUDE.md`, 백업, opt-out 상태를 자동 삭제하지 않습니다. 과거 전체 파일로 되돌릴 때만 최신 백업을 검토한 뒤 수동 복원하십시오. 프로젝트 하네스 설정은 계속 별도이며 설치/SessionStart로 생성되지 않습니다.

### Portable minimal harness

플러그인에는 프로젝트 중립적인 최소 하네스가 포함됩니다. 프로젝트마다 명시적으로 `/dex-workers:setup-project`를 실행해야 하며, 플러그인 설치만으로 `.harness`가 생성되지는 않습니다. 생성물은 `.harness/JOURNAL.md`, `plans/`, `runs/`, `templates/run.md`, `README.md`, `config`, `verify`입니다. 기존 `CLAUDE.md`, `AGENTS.md`, `.harness` 파일을 덮어쓰지 않고 충돌 시 아무것도 변경하지 않습니다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" setup-project --target "$PWD" --dry-run
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" setup-project --target "$PWD"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" setup-project --target "$PWD" --check
./.harness/verify
```

`.harness/config`의 `VERIFY_COMMAND`를 프로젝트의 실제 lint/test/build 명령으로 설정하십시오. 초기화 과정은 네트워크나 패키지 설치를 수행하지 않습니다.

For an auditable team handoff, use `scripts/install-folder.py <destination>` or create a deterministic archive with `scripts/package.py --out <outside-plugin-dir>/dex-workers.tar.gz`. Both tools ship only the fixed release inventory and refuse symbolic links or special source files.
