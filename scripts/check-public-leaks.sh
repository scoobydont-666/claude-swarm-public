#!/usr/bin/env bash
# Pre-commit hook: defense-in-depth leak scanner for the public fork.
#
# Complements .gitleaks.toml — gitleaks catches its registered patterns,
# this script catches the second layer (internal hostnames, GitHub handles,
# emails) that should never appear in a public repo even when not "secrets"
# in the gitleaks sense.
#
# Triggered automatically via .pre-commit-config.yaml as a `local` hook.
# Fails (rc=1) on any blocked pattern in staged files; allowlist exemptions
# match the same paths/regexes as the gitleaks allowlist for consistency.

set -euo pipefail

# Patterns to block. Each entry is "pattern|category".
# Patterns are extended-regex (grep -E).
BLOCKED_PATTERNS=(
  '192\.168\.201\.85|specific-known-leaked-ip'
  'scoobydont-666|github-handle'
  'r\.josh\.jones@gmail\.com|personal-email'
  '\bminiboss\b|internal-hostname'
)

# Paths/files to ignore even if a blocked pattern matches.
# - .gitleaks.toml: legitimately documents blocklist patterns
# - scripts/check-public-leaks.sh: this script literally contains the patterns
ALLOWLIST_FILES=(
  '\.gitleaks\.toml$'
  'scripts/check-public-leaks\.sh$'
)

# Build paths to scan: prefer staged files, fall back to working tree.
if git rev-parse --git-dir >/dev/null 2>&1; then
  STAGED=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)
else
  STAGED=""
fi

if [[ -z "$STAGED" ]]; then
  # Manual invocation — scan everything tracked.
  FILES=$(git ls-files 2>/dev/null || find . -type f -not -path './.git/*')
else
  FILES="$STAGED"
fi

# Apply file allowlist
SCAN_FILES=""
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  [[ ! -f "$f" ]] && continue
  skip=0
  for allow_re in "${ALLOWLIST_FILES[@]}"; do
    if [[ "$f" =~ $allow_re ]]; then
      skip=1
      break
    fi
  done
  [[ $skip -eq 0 ]] && SCAN_FILES+="$f"$'\n'
done <<< "$FILES"

if [[ -z "$SCAN_FILES" ]]; then
  exit 0
fi

VIOLATIONS=0
declare -a HITS=()

while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  for entry in "${BLOCKED_PATTERNS[@]}"; do
    pattern="${entry%%|*}"
    category="${entry##*|}"
    if matches=$(grep -nE "$pattern" "$f" 2>/dev/null); then
      while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        HITS+=("[$category] $f:$line")
        VIOLATIONS=$((VIOLATIONS + 1))
      done <<< "$matches"
    fi
  done
done <<< "$SCAN_FILES"

if [[ $VIOLATIONS -gt 0 ]]; then
  echo "❌ public-leak-check: $VIOLATIONS violation(s) found." >&2
  for h in "${HITS[@]}"; do
    echo "  $h" >&2
  done
  echo "" >&2
  echo "Refusing to commit. Either:" >&2
  echo "  1. Sanitize the matches and re-stage, or" >&2
  echo "  2. Add the path to ALLOWLIST_FILES in scripts/check-public-leaks.sh" >&2
  echo "     if it's a legitimate documentation/reference (e.g., a blocklist rule)." >&2
  exit 1
fi

exit 0
