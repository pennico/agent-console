#!/usr/bin/env bash
# Fails if a tracked file contains a private pattern: home-network addresses,
# key-shaped strings, or anything in PRIVATE_PATTERNS (CI: the repository
# variable; locally: one regex per line in private/patterns.txt, which git ignores).
# Prints file:line only, never the matching text.
set -euo pipefail
cd "$(dirname "$0")/.."
PATTERNS='192\.168\.[0-9]|(^|[^0-9])10\.[0-9]+\.[0-9]+\.[0-9]+|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+|AIza[A-Za-z0-9_-]{10}|gh[pousr]_[A-Za-z0-9]{20}|sk-[A-Za-z0-9-]{20}|-----BEGIN [A-Z ]*PRIVATE KEY|pennico-ops'
EXTRA="${PRIVATE_PATTERNS:-}"
if [ -z "$EXTRA" ] && [ -f private/patterns.txt ]; then
  EXTRA=$(grep -v '^\s*\(#\|$\)' private/patterns.txt | paste -sd'|' -)
fi
[ -n "$EXTRA" ] && PATTERNS="${PATTERNS}|${EXTRA}"
FILES=$(git ls-files --cached --others --exclude-standard | grep -v '^scripts/scan-private\.sh$' || true)
[ -z "$FILES" ] && { echo "no files to scan"; exit 0; }
HITS=$(echo "$FILES" | tr '\n' '\0' | xargs -0 grep -nIiE -o "$PATTERNS" 2>/dev/null | cut -d: -f1,2 | sort -u || true)
if [ -n "$HITS" ]; then
  echo "Private patterns found (file:line):" >&2
  echo "$HITS" >&2
  exit 1
fi
echo "No private patterns in $(echo "$FILES" | wc -l) files."
