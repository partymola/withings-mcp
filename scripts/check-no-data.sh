#!/usr/bin/env bash
# Pre-commit hook: reject commits containing database files, config secrets,
# or suspiciously large files that might contain real health data.
#
# Install: cp scripts/check-no-data.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
# Or: make install-hooks

set -euo pipefail

errors=0

# Check for database files and any backups thereof. Uses ERE so the patterns
# behave the same under GNU grep and ugrep (default 'grep' on some systems).
# Patterns matched against full staged-file paths:
#   *.db, *.db-journal, *.db-wal, *.db-shm
#   *.db.<anything>      (e.g. withings.db.bak)
#   *.bak, *.backup      (any backup file, regardless of inner extension)
db_regex='\.db$|\.db-journal$|\.db-wal$|\.db-shm$|\.db\.|\.bak$|\.backup$'
matched=$(git diff --cached --name-only | grep -E "$db_regex" || true)
if [ -n "$matched" ]; then
    echo "ERROR: Staged file(s) look like database/backup data - must not be committed:"
    echo "$matched" | sed 's/^/  /'
    errors=1
fi

# Check for config secrets
if git diff --cached --name-only | grep -E '^config/.*\.(json|env)$' | grep -qvE '\.example\.'; then
    echo "ERROR: Staged file in config/ - credentials and tokens must not be committed"
    errors=1
fi

# Check for large files (>100KB) that might be data dumps
while IFS= read -r file; do
    # A dependency lockfile is legitimately large and only grows.
    case "$file" in
        uv.lock) continue ;;
    esac
    size=$(git cat-file -s ":$file" 2>/dev/null || echo 0)
    if [ "$size" -gt 102400 ]; then
        echo "ERROR: Staged file '$file' is $(( size / 1024 ))KB (>100KB) - possible data leak"
        errors=1
    fi
done < <(git diff --cached --name-only --diff-filter=ACM)

if [ "$errors" -ne 0 ]; then
    echo ""
    echo "Commit rejected. Health data and credentials must never be committed."
    echo "See CLAUDE.md for data safety rules."
    exit 1
fi
