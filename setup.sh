#!/usr/bin/env bash
# =============================================================================
# AIRCP Setup
# Installe les hooks git, vérifie les dépendances, prépare l'environnement.
#
# Usage:
#   ./setup.sh          # Setup complet
#   ./setup.sh hooks    # Hooks git seulement
#   ./setup.sh check    # Vérification de l'environnement seulement
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; ERRORS=$((ERRORS + 1)); }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
info() { echo -e "  ${CYAN}→${NC} $1"; }

ERRORS=0

# ─────────────────────────────────────────────
# Git hooks
# ─────────────────────────────────────────────
install_hooks() {
    echo -e "${BOLD}Git hooks${NC}"

    if [ ! -d .git ]; then
        fail "Not a git repository"
        return
    fi

    if [ ! -f hooks/pre-commit-security.sh ]; then
        fail "hooks/pre-commit-security.sh not found"
        return
    fi

    cp hooks/pre-commit-security.sh .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit
    ok "Pre-commit security hook installed"
}

# ─────────────────────────────────────────────
# Environment check
# ─────────────────────────────────────────────
check_env() {
    echo -e "${BOLD}Environment${NC}"

    # Python
    if command -v python3 &>/dev/null; then
        PY_VER=$(python3 --version 2>&1 | awk '{print $2}')
        ok "Python $PY_VER"
    else
        fail "Python 3 not found"
    fi

    # SQLite
    if python3 -c "import sqlite3" 2>/dev/null; then
        ok "SQLite module"
    else
        fail "Python sqlite3 module missing"
    fi

    # HDDS lib
    if [ -n "${HDDS_LIB_PATH:-}" ] && [ -d "$HDDS_LIB_PATH" ]; then
        ok "HDDS lib ($HDDS_LIB_PATH)"
    elif [ -d "$SCRIPT_DIR/lib" ]; then
        ok "HDDS lib (local lib/)"
    else
        warn "HDDS lib not found — set HDDS_LIB_PATH"
    fi

    # Agent configs
    echo ""
    echo -e "${BOLD}Agent configs${NC}"

    for agent in alpha beta sonnet haiku; do
        cfg="agent_config/$agent/mcp_servers.json"
        if [ -f "$cfg" ]; then
            if grep -q "FORUM_TOKEN" "$cfg" 2>/dev/null; then
                ok "$agent — config + forum token"
            else
                warn "$agent — config found but no FORUM_TOKEN"
            fi
        else
            warn "$agent — no mcp_servers.json (copy from agent_config/mcp_servers.json.example)"
        fi
    done

    # Security
    echo ""
    echo -e "${BOLD}Security${NC}"

    if [ -f .git/hooks/pre-commit ]; then
        ok "Pre-commit hook active"
    else
        warn "Pre-commit hook not installed — run ./setup.sh hooks"
    fi

    if grep -q '__pycache__' .gitignore 2>/dev/null; then
        ok ".gitignore covers __pycache__"
    else
        fail ".gitignore missing __pycache__ rule"
    fi

    # Check for tracked sensitive files
    TRACKED_SECRETS=$(git ls-files | grep -E '\.pyc$|\.db$|MEMORY/|mcp_servers\.json$' 2>/dev/null | head -5 || true)
    if [ -n "$TRACKED_SECRETS" ]; then
        fail "Sensitive files still tracked in git:"
        echo "$TRACKED_SECRETS" | while read -r f; do echo "       $f"; done
    else
        ok "No sensitive files tracked"
    fi
}

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}AIRCP Setup${NC}"
echo "─────────────────────────────────"
echo ""

case "${1:-all}" in
    hooks)
        install_hooks
        ;;
    check)
        check_env
        ;;
    all|"")
        install_hooks
        echo ""
        check_env
        ;;
    *)
        echo "Usage: ./setup.sh [hooks|check|all]"
        exit 1
        ;;
esac

echo ""
echo "─────────────────────────────────"
if [ $ERRORS -gt 0 ]; then
    echo -e "${RED}$ERRORS error(s)${NC} — fix before running AIRCP"
    exit 1
else
    echo -e "${GREEN}Ready${NC}"
fi
