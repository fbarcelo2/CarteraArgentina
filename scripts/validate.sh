#!/usr/bin/env bash
# The project's validation stack, in one command, for a human or for CI.
#
# Every tool is reported, including the ones that are missing: a check that is
# silently skipped reads as a pass, which is worse than a failure. External
# binaries are skipped with a notice when they are not installed, so a
# contributor without the full stack still gets a useful run.
#
# Usage: scripts/validate.sh [--quick]
#   --quick   static checks only, no pytest/coverage (for a fast pre-commit loop)
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

PASS=0
FAIL=0
SKIP=0
declare -a FAILED_TOOLS=()

run() {
  local label="$1"
  shift
  printf '%-24s' "$label"
  local output
  if output=$("$@" 2>&1); then
    printf 'PASS\n'
    PASS=$((PASS + 1))
  else
    printf 'FAIL (exit %d)\n' "$?"
    printf '%s\n' "$output" | head -20 | sed 's/^/    /'
    FAIL=$((FAIL + 1))
    FAILED_TOOLS+=("$label")
  fi
}

need() {
  local label="$1" binary="$2"
  if command -v "$binary" >/dev/null 2>&1; then
    return 0
  fi
  printf '%-24sSKIP (%s not installed)\n' "$label" "$binary"
  SKIP=$((SKIP + 1))
  return 1
}

echo "== lint and format =="
run "ruff check" uv run ruff check .
run "ruff format" uv run ruff format --check .

echo
echo "== static analysis =="
run "pyright (types)" uv run pyright
run "bandit (py security)" uv run bandit -q -r src
need "vulture (dead code)" vulture && run "vulture (dead code)" vulture src vulture_whitelist.py --min-confidence 80
run "deptry (deps)" uv run deptry .
need "semgrep (patterns)" semgrep && run "semgrep (patterns)" semgrep --config p/python --config p/security-audit --quiet --error .

echo
echo "== artifacts =="
need "actionlint (workflows)" actionlint && run "actionlint (workflows)" actionlint
need "yamllint (yaml)" yamllint && run "yamllint (yaml)" yamllint -f parsable .
need "taplo (toml)" taplo && run "taplo (toml)" taplo lint pyproject.toml
need "shellcheck (shell)" shellcheck && run "shellcheck (shell)" shellcheck --severity=warning scripts/*.sh
if ! compgen -G "Dockerfile*" > /dev/null; then
  printf '%-24sSKIP (no Dockerfile yet)\n' "hadolint (docker)"
  SKIP=$((SKIP + 1))
elif need "hadolint (docker)" hadolint; then
  for dockerfile in Dockerfile*; do
    run "hadolint (docker)" hadolint "$dockerfile"
  done
fi
need "typos (spelling)" typos && run "typos (spelling)" typos --format brief .

echo
echo "== secrets and history =="
need "gitleaks (history)" gitleaks && run "gitleaks (history)" gitleaks detect --source . --redact --no-banner

echo
echo "== dynamic =="
if [[ "$QUICK" -eq 1 ]]; then
  printf '%-24sSKIP (--quick)\n' "pytest"
  SKIP=$((SKIP + 1))
else
  run "pytest" uv run pytest -q
  run "coverage (>=70%)" uv run pytest -q --cov=cartera --cov-fail-under=70 --cov-report=term-missing:skip-covered
fi

echo
printf 'pass: %d   fail: %d   skip: %d\n' "$PASS" "$FAIL" "$SKIP"
if [[ "$FAIL" -gt 0 ]]; then
  printf 'failed: %s\n' "${FAILED_TOOLS[*]}"
  exit 1
fi
echo "all available checks passed"
