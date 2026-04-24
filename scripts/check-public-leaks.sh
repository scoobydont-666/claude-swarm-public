#!/usr/bin/env bash
# Pre-commit leak scanner — blocks RFC1918 IPs, internal hostnames, leaked emails from public commits
# Reads STAGED content (git index) not the working tree to avoid false negatives/positives.
set -uo pipefail

PATTERNS=(
  '192\.168\.[0-9]+\.[0-9]+'
  '10\.[0-9]+\.[0-9]+\.[0-9]+'
  '172\.(1[6-9]|2[0-9]|3[0-1])\.[0-9]+\.[0-9]+'
  '\b(miniboss|giga|mecha|mega|mongo|rainbow)\b'
  'r\.josh\.jones@gmail\.com'
)

# Files to skip (scanner itself, documentation, examples)
SKIP_FILES='(check-public-leaks\.sh|PUBLIC_RELEASE\.md|README|docs/|example)'

FILES=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)
[[ -z "$FILES" ]] && exit 0

HITS=0
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  # Skip allowlisted files
  if [[ "$f" =~ $SKIP_FILES ]]; then
    continue
  fi
  # Read STAGED content from the git index (not working tree)
  content=$(git show ":$f" 2>/dev/null) || continue
  for pattern in "${PATTERNS[@]}"; do
    # Check for leaks, excluding RFC 5737 documentation ranges
    matches=$(echo "$content" | grep -nE "$pattern" 2>/dev/null | grep -vE '192\.0\.2\.|198\.51\.100\.|203\.0\.113\.' || true)
    if [[ -n "$matches" ]]; then
      echo "LEAK (staged): pattern '$pattern' in $f"
      echo "$matches" | head -3
      HITS=$((HITS+1))
    fi
  done
done <<< "$FILES"

if [[ $HITS -gt 0 ]]; then
  echo ""
  echo "==========================================="
  echo "Pre-commit leak scan found $HITS pattern hits."
  echo "Review the above. If a hit is legitimate (docs, example),"
  echo "use RFC 5737 ranges or add to the allowlist in this script."
  echo "To bypass (NOT RECOMMENDED): git commit --no-verify"
  echo "==========================================="
  exit 1
fi
exit 0
