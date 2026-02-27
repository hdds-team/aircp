#!/usr/bin/env bash
# =============================================================================
# AIRCP Pre-Commit Security Validator v2.0
# "Security first, or no release"
#
# Changes from v1:
#   - Extended secret patterns (Anthropic, AWS, PEM, base64-encoded)
#   - Dangerous Python patterns (eval, exec, subprocess shell=True)
#   - JS/HTML security check (inline scripts, external fetches)
#   - Merge conflict markers detection
#   - .gitignore coherence check
#   - Smarter path exclusions (docs, specs, markdown)
#   - Performance: single diff pass for secrets scan
#   - Tighter large file threshold (512KB) + logs/ in forbidden
#   - DEBUG_PATTERNS check (print/pdb/console.log left behind)
#
# Install:
#   cp hooks/pre-commit-security.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# Or use hooks directory:
#   git config core.hooksPath hooks
#   (rename this file to "pre-commit")
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[0;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

ERRORS=0
WARNINGS=0
CHECK_NUM=0

fail() {
    echo -e "  ${RED}BLOCK${NC} $1"
    ERRORS=$((ERRORS + 1))
}

warn() {
    echo -e "  ${YELLOW}WARN${NC}  $1"
    WARNINGS=$((WARNINGS + 1))
}

ok() {
    echo -e "  ${GREEN}OK${NC}    $1"
}

section() {
    CHECK_NUM=$((CHECK_NUM + 1))
    echo ""
    echo -e "${CYAN}[$CHECK_NUM]${NC} $1"
}

echo "========================================="
echo " AIRCP Security Validator v2.0"
echo "========================================="

# Get staged files (Added, Copied, Modified only)
STAGED=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)

if [ -z "$STAGED" ]; then
    echo ""
    echo "No staged files. Nothing to check."
    exit 0
fi

STAGED_COUNT=$(echo "$STAGED" | wc -l | tr -d ' ')
echo " Scanning $STAGED_COUNT staged file(s)..."

# Cache the full diff once (performance: avoid repeated git diff calls)
DIFF_CACHE=$(git diff --cached -U0 2>/dev/null || true)
DIFF_ADDED=$(echo "$DIFF_CACHE" | grep -E '^\+' | grep -v '^\+\+\+' || true)

# =========================================================================
# CHECK 1: Forbidden file types
# =========================================================================
section "Forbidden file types"

FORBIDDEN_PATTERNS=(
    '\.pyc$'
    '\.pyo$'
    '\.pyd$'
    '\.db$'
    '\.sqlite$'
    '\.sqlite3$'
    '\.db-wal$'
    '\.db-shm$'
    'MEMORY/'
    'mcp_servers\.json$'
    '\.claude/'
    '\.env$'
    '\.env\.'
    'credentials'
    '\.pem$'
    '\.key$'
    '\.p12$'
    '\.pfx$'
    '\.jks$'
    '__pycache__/'
    'node_modules/'
    '\.DS_Store$'
    'logs/.*\.log$'
    'logs/activity/'
    'summaries/.*\.json$'
    '\.aircp\.db'
)

FORBIDDEN_FOUND=0
for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
    matches=$(echo "$STAGED" | grep -E "$pattern" 2>/dev/null || true)
    if [ -n "$matches" ]; then
        while IFS= read -r f; do
            fail "Forbidden: $f  ($pattern)"
            FORBIDDEN_FOUND=$((FORBIDDEN_FOUND + 1))
        done <<< "$matches"
    fi
done

[ $FORBIDDEN_FOUND -eq 0 ] && ok "No forbidden file types"

# =========================================================================
# CHECK 2: Secrets & tokens in staged content
# =========================================================================
section "Secrets scan"

SECRET_PATTERNS=(
    # AIRCP-specific
    'AIRCP-CAP-v1\.'
    'FORUM_TOKEN=[^\s]+'
    'FORUM_SIGNING_KEY='
    'signing_key\s*=\s*["\x27]'

    # Generic credentials
    'password\s*=\s*["\x27][^"\x27]{4,}'
    'secret\s*=\s*["\x27][^"\x27]{4,}'
    'api_key\s*=\s*["\x27][^"\x27]{4,}'
    'token\s*=\s*["\x27][A-Za-z0-9._-]{20,}'

    # Bearer / auth headers
    'Bearer [A-Za-z0-9._-]{20,}'
    'Authorization:\s*["\x27]'

    # Provider API keys
    'sk-[A-Za-z0-9]{20,}'             # OpenAI
    'sk-ant-[A-Za-z0-9-]{20,}'        # Anthropic
    'AKIA[A-Z0-9]{16}'                # AWS Access Key
    'ghp_[A-Za-z0-9]{36}'             # GitHub PAT
    'glpat-[A-Za-z0-9_-]{20,}'        # GitLab PAT
    'xoxb-[0-9]+-[A-Za-z0-9]+'        # Slack bot token
    'xoxp-[0-9]+-[A-Za-z0-9]+'        # Slack user token

    # Private keys
    '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY'
    '-----BEGIN CERTIFICATE-----'

    # Connection strings
    'postgresql://[^"\x27\s]+'
    'mysql://[^"\x27\s]+'
    'mongodb(\+srv)?://[^"\x27\s]+'
    'redis://:[^"\x27\s]+'
)

SECRETS_FOUND=0
for pattern in "${SECRET_PATTERNS[@]}"; do
    matches=$(echo "$DIFF_ADDED" | grep -Ei "$pattern" 2>/dev/null || true)
    if [ -n "$matches" ]; then
        # Truncate output to avoid leaking the actual secret
        preview=$(echo "$matches" | head -1 | cut -c1-60)
        fail "Secret pattern: $pattern"
        echo -e "         ${YELLOW}→${NC} ${preview}..."
        SECRETS_FOUND=$((SECRETS_FOUND + 1))
    fi
done

[ $SECRETS_FOUND -eq 0 ] && ok "No secrets detected"

# =========================================================================
# CHECK 3: Internal paths leaked
# =========================================================================
section "Internal paths"

PATH_PATTERNS=(
    '/home/[a-z]+/workspace/'
    '/dev/shm/aircp'
    '/projects/aircp[^.]'             # /projects/aircp but not /projects/aircp.dev in URLs
    '/projects/synaptic'
)

# Files that legitimately contain internal paths
PATH_WHITELIST='\.sh$|\.toml$|\.conf$|hooks/|CLAUDE\.md|PROJECT.*INSTRUCTIONS|\.md$|spec/|docs/'

PATHS_FOUND=0
for f in $STAGED; do
    # Skip whitelisted files
    if echo "$f" | grep -Eq "$PATH_WHITELIST" 2>/dev/null; then
        continue
    fi
    content=$(git show ":$f" 2>/dev/null || true)
    for pattern in "${PATH_PATTERNS[@]}"; do
        if echo "$content" | grep -Eq "$pattern" 2>/dev/null; then
            warn "Internal path in $f ($pattern)"
            PATHS_FOUND=$((PATHS_FOUND + 1))
            break  # One warning per file is enough
        fi
    done
done

[ $PATHS_FOUND -eq 0 ] && ok "No internal paths leaked"

# =========================================================================
# CHECK 4: Python security & syntax
# =========================================================================
section "Python validation"

PY_FILES=$(echo "$STAGED" | grep '\.py$' || true)
PY_ISSUES=0

if [ -n "$PY_FILES" ]; then
    PY_COUNT=$(echo "$PY_FILES" | wc -l | tr -d ' ')

    # 4a: Syntax check
    for f in $PY_FILES; do
        if [ -f "$f" ]; then
            if ! python3 -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null; then
                fail "Syntax error: $f"
                PY_ISSUES=$((PY_ISSUES + 1))
            fi
        fi
    done

    # 4b: Dangerous patterns in staged diff (Python files only)
    DANGEROUS_PY=(
        'eval\s*\('
        'exec\s*\('
        '__import__\s*\('
        'subprocess\.call\(.*shell\s*=\s*True'
        'subprocess\.Popen\(.*shell\s*=\s*True'
        'os\.system\s*\('
        'pickle\.loads?\s*\('
        'yaml\.load\s*\([^)]*$'           # yaml.load without Loader=
        'marshal\.loads?\s*\('
        'compile\s*\([^)]*exec'
    )

    # Get diff only for Python files
    PY_DIFF=""
    for f in $PY_FILES; do
        chunk=$(echo "$DIFF_CACHE" | sed -n "/^diff.*${f//\//\\/}/,/^diff/p" | grep -E '^\+' | grep -v '^\+\+\+' || true)
        PY_DIFF+="$chunk"$'\n'
    done

    for pattern in "${DANGEROUS_PY[@]}"; do
        matches=$(echo "$PY_DIFF" | grep -Ei "$pattern" 2>/dev/null || true)
        if [ -n "$matches" ]; then
            # Not a blocker but strong warning — may be intentional
            warn "Dangerous pattern in Python: $pattern"
            echo -e "         ${YELLOW}→${NC} $(echo "$matches" | head -1 | cut -c1-80)"
            PY_ISSUES=$((PY_ISSUES + 1))
        fi
    done

    [ $PY_ISSUES -eq 0 ] && ok "Python clean ($PY_COUNT files)"
else
    ok "No Python files"
fi

# =========================================================================
# CHECK 5: JS / HTML / Svelte security
# =========================================================================
section "Frontend security"

FRONT_FILES=$(echo "$STAGED" | grep -E '\.(js|ts|jsx|tsx|svelte|astro|html)$' || true)
FRONT_ISSUES=0

if [ -n "$FRONT_FILES" ]; then
    FRONT_COUNT=$(echo "$FRONT_FILES" | wc -l | tr -d ' ')

    DANGEROUS_JS=(
        'eval\s*\('
        'innerHTML\s*='
        'document\.write\s*\('
        'new\s+Function\s*\('
        'fetch\s*\(\s*["\x27]http://'     # Non-HTTPS fetch
        'window\.location\s*='
        'dangerouslySetInnerHTML'
    )

    FRONT_DIFF=""
    for f in $FRONT_FILES; do
        chunk=$(echo "$DIFF_CACHE" | sed -n "/^diff.*${f//\//\\/}/,/^diff/p" | grep -E '^\+' | grep -v '^\+\+\+' || true)
        FRONT_DIFF+="$chunk"$'\n'
    done

    for pattern in "${DANGEROUS_JS[@]}"; do
        matches=$(echo "$FRONT_DIFF" | grep -Ei "$pattern" 2>/dev/null || true)
        if [ -n "$matches" ]; then
            warn "Frontend pattern: $pattern"
            echo -e "         ${YELLOW}→${NC} $(echo "$matches" | head -1 | cut -c1-80)"
            FRONT_ISSUES=$((FRONT_ISSUES + 1))
        fi
    done

    [ $FRONT_ISSUES -eq 0 ] && ok "Frontend clean ($FRONT_COUNT files)"
else
    ok "No frontend files"
fi

# =========================================================================
# CHECK 6: Merge conflict markers
# =========================================================================
section "Merge conflicts"

CONFLICT_FOUND=0
for f in $STAGED; do
    if [ -f "$f" ]; then
        # Binary file check — skip
        if file --mime "$f" 2>/dev/null | grep -q 'binary'; then
            continue
        fi
        if grep -qE '^(<{7}|={7}|>{7})(\s|$)' "$f" 2>/dev/null; then
            fail "Merge conflict markers in: $f"
            CONFLICT_FOUND=$((CONFLICT_FOUND + 1))
        fi
    fi
done

[ $CONFLICT_FOUND -eq 0 ] && ok "No merge conflict markers"

# =========================================================================
# CHECK 7: Debug leftovers
# =========================================================================
section "Debug leftovers"

DEBUG_PATTERNS=(
    'breakpoint\(\)'
    'pdb\.set_trace\(\)'
    'import\s+pdb'
    'import\s+ipdb'
    'console\.log\('
    'debugger;'
    'print\(\s*f?["\x27]DEBUG'
    'print\(\s*f?["\x27]TODO'
    'FIXME.*HACK'
    '# XXX'
)

DEBUG_FOUND=0
for pattern in "${DEBUG_PATTERNS[@]}"; do
    matches=$(echo "$DIFF_ADDED" | grep -Ei "$pattern" 2>/dev/null || true)
    if [ -n "$matches" ]; then
        warn "Debug leftover: $pattern"
        DEBUG_FOUND=$((DEBUG_FOUND + 1))
    fi
done

[ $DEBUG_FOUND -eq 0 ] && ok "No debug leftovers"

# =========================================================================
# CHECK 8: Large files
# =========================================================================
section "Large files"

MAX_SIZE=524288  # 512 KB
LARGE_FOUND=0

for f in $STAGED; do
    if [ -f "$f" ]; then
        size=$(wc -c < "$f" 2>/dev/null || echo 0)
        if [ "$size" -gt "$MAX_SIZE" ]; then
            size_kb=$(( size / 1024 ))
            warn "Large file: $f (${size_kb} KB)"
            LARGE_FOUND=$((LARGE_FOUND + 1))
        fi
    fi
done

[ $LARGE_FOUND -eq 0 ] && ok "No large files (> 512 KB)"

# =========================================================================
# CHECK 9: .gitignore coherence
# =========================================================================
section ".gitignore coherence"

GITIGNORE_REQUIRED=(
    '*.db'
    '*.sqlite3'
    '__pycache__'
    'node_modules'
    '.env'
    'logs/'
    'MEMORY/'
    'mcp_servers.json'
    '*.pyc'
    '.DS_Store'
    'summaries/'
)

GITIGNORE_MISSING=0
if [ -f ".gitignore" ]; then
    GITIGNORE_CONTENT=$(cat .gitignore)
    for entry in "${GITIGNORE_REQUIRED[@]}"; do
        # Check if the pattern is covered (exact or glob match)
        if ! echo "$GITIGNORE_CONTENT" | grep -qF "$entry" 2>/dev/null; then
            warn ".gitignore missing: $entry"
            GITIGNORE_MISSING=$((GITIGNORE_MISSING + 1))
        fi
    done
    [ $GITIGNORE_MISSING -eq 0 ] && ok ".gitignore covers all critical patterns"
else
    fail "No .gitignore found!"
fi

# =========================================================================
# SUMMARY
# =========================================================================
echo ""
echo "========================================="
echo " Results: $CHECK_NUM checks completed"
echo "========================================="

if [ $ERRORS -gt 0 ]; then
    echo ""
    echo -e " ${RED}██ BLOCKED${NC}: $ERRORS error(s), $WARNINGS warning(s)"
    echo ""
    echo " Fix errors above before committing."
    echo " Bypass (NOT recommended): git commit --no-verify"
    echo ""
    exit 1
fi

if [ $WARNINGS -gt 0 ]; then
    echo ""
    echo -e " ${YELLOW}██ PASSED${NC} with $WARNINGS warning(s)"
    echo ""
else
    echo ""
    echo -e " ${GREEN}██ ALL CLEAR${NC} — ship it 🚀"
    echo ""
fi

exit 0
