#!/bin/bash
# AIRCP Stack Launcher
# Usage: ./start_aircp.sh [component]
#   ./start_aircp.sh daemon   - Start daemon only
#   ./start_aircp.sh alpha    - Start alpha agent
#   ./start_aircp.sh beta     - Start beta agent
#   ./start_aircp.sh codex    - Start codex agent
#   ./start_aircp.sh all      - Start everything (tmux)
#   ./start_aircp.sh notifier - Start synaptic notifier

set -e

# Identity
AIRCP_VERSION="3.1.0"
AIRCP_LICENSE=$(python3 -c "
from license import load_license
lic = load_license()
print('Enterprise (%s, %d seats)' % (lic.org, lic.seats) if lic.is_valid else 'Community')
" 2>/dev/null || echo "Community")
echo "aIRCp v${AIRCP_VERSION} | License: ${AIRCP_LICENSE}"

# HDDS config — set HDDS_LIB_PATH and HDDS_WS_DIR in your environment or .env
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export HDDS_LIB_PATH="${HDDS_LIB_PATH:-$SCRIPT_DIR/lib}"
export LD_LIBRARY_PATH="${HDDS_LIB_PATH}"
export HDDS_REUSEPORT=1
export FORUM_API_URL=https://aircp.dev
AIRCP_DOMAIN_ID=219  # FNV-1a("aircp") -> 219

# Load env overrides from .env (gitignored, never committed)
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    . "$SCRIPT_DIR/.env"
    set +a
fi

# Add HDDS Python SDK to PYTHONPATH if configured
if [ -n "$HDDS_SDK_PATH" ]; then
    export PYTHONPATH="${HDDS_SDK_PATH}:${PYTHONPATH}"
fi
# Re-export LD_LIBRARY_PATH after .env override
if [ -n "$HDDS_LIB_PATH" ]; then
    export LD_LIBRARY_PATH="${HDDS_LIB_PATH}:${LD_LIBRARY_PATH}"
fi

HDDS_WS_DIR="${HDDS_WS_DIR:-$SCRIPT_DIR/lib/hdds-ws}"
HDDS_WS_BIN=$HDDS_WS_DIR/target/release/hdds-ws
HDDS_WS_CMD="test -x $HDDS_WS_BIN && $HDDS_WS_BIN --domain $AIRCP_DOMAIN_ID || (cd $HDDS_WS_DIR && cargo run --release -- --domain $AIRCP_DOMAIN_ID)"
DASHBOARD_DIR=/projects/aircp/dashboard
LOG_DIR=/projects/aircp/logs
# Local default: explicit no-auth for daemon HTTP.
# Override with real auth in prod, e.g.:
#   AIRCP_DAEMON_ARGS="--auth-token $AIRCP_AUTH_TOKEN"
: "${AIRCP_DAEMON_ARGS:=--allow-no-auth}"

cd /projects/aircp
mkdir -p "$LOG_DIR"

# Anti-doublon: kill existing process for a given agent before starting
kill_existing() {
    local agent="$1"
    case "$agent" in
        daemon)  pkill -f "aircp_daemon.py" 2>/dev/null || true ;;
        hdds-ws) pkill -f "hdds-ws" 2>/dev/null || true ;;
        *)       pkill -f "heartbeat.py --agent $agent" 2>/dev/null || true ;;
    esac
    sleep 0.5
}

# Helper: ensure hdds-ws is running (needed by dashboard & bridge)
ensure_hdds_ws() {
    if ss -tlnp 2>/dev/null | grep -q ':9090'; then
        echo "[hdds-ws] Already running on :9090"
        return 0
    fi
    echo "[hdds-ws] Starting on :9090 (domain $AIRCP_DOMAIN_ID)..."
    if [ -x "$HDDS_WS_BIN" ]; then
        HDDS_PARTICIPANT_ID=10 $HDDS_WS_BIN --domain $AIRCP_DOMAIN_ID &
    else
        echo "[hdds-ws] No release binary, using cargo run..."
        (cd "$HDDS_WS_DIR" && HDDS_PARTICIPANT_ID=10 cargo run --release -- --domain $AIRCP_DOMAIN_ID) &
    fi
    for i in $(seq 1 30); do
        sleep 1
        if ss -tlnp 2>/dev/null | grep -q ':9090'; then
            echo "[hdds-ws] Ready on :9090"
            return 0
        fi
    done
    echo "[hdds-ws] WARNING: not ready yet (may still be compiling)"
    return 0
}

case "${1:-help}" in
    daemon)
        echo "Starting AIRCP daemon (pid=0)..."
        kill_existing daemon
        ensure_hdds_ws
        HDDS_PARTICIPANT_ID=0 python aircp_daemon.py $AIRCP_DAEMON_ARGS 2>&1 | tee -a "$LOG_DIR/daemon.log"
        ;;
    hdds-ws)
        echo "Starting hdds-ws bridge (port 9090)..."
        ensure_hdds_ws
        ;;
    dashboard)
        echo "Starting dashboard (port 3000)..."
        ensure_hdds_ws
        cd "$DASHBOARD_DIR" && npm run dev
        ;;
    alpha)
        echo "Starting Alpha agent (pid=1)..."
        kill_existing alpha
        HDDS_PARTICIPANT_ID=1 python heartbeat.py --agent alpha --continuous -v 2>&1 | tee -a "$LOG_DIR/alpha.log"
        ;;
    beta)
        echo "Starting Beta agent (pid=2) - QA/Code Review (Opus 3)..."
        kill_existing beta
        HDDS_PARTICIPANT_ID=2 python heartbeat.py --agent beta --continuous -v 2>&1 | tee -a "$LOG_DIR/beta.log"
        ;;
    mascotte)
        echo "Starting Mascotte agent (pid=6) - Local fun (qwen3-nothink)..."
        kill_existing mascotte
        HDDS_PARTICIPANT_ID=6 python heartbeat.py --agent mascotte --continuous -v 2>&1 | tee -a "$LOG_DIR/mascotte.log"
        ;;
    theta)
        echo "Starting Theta agent (pid=7) - Test LMStudio..."
        HDDS_PARTICIPANT_ID=7 python heartbeat.py --agent theta --continuous -v
        ;;
    haiku)
        echo "Starting Haiku agent (pid=3)..."
        kill_existing haiku
        HDDS_PARTICIPANT_ID=3 python heartbeat.py --agent haiku --continuous -v 2>&1 | tee -a "$LOG_DIR/haiku.log"
        ;;
    sonnet)
        echo "Starting Sonnet agent (pid=4)..."
        kill_existing sonnet
        HDDS_PARTICIPANT_ID=4 python heartbeat.py --agent sonnet --continuous -v 2>&1 | tee -a "$LOG_DIR/sonnet.log"
        ;;
    notifier)
        echo "Starting Synaptic Notifier..."
        python synaptic_notifier.py
        ;;
    codex)
        echo "Starting Codex agent (pid=5)..."
        kill_existing codex
        HDDS_PARTICIPANT_ID=5 python heartbeat.py --agent codex --continuous -v 2>&1 | tee -a "$LOG_DIR/codex.log"
        ;;
    all)
        echo "Starting full AIRCP stack in tmux..."
        # Unset Claude Code env vars to avoid "nested session" error when launched from CC
        ENV_VARS="unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT; HDDS_REUSEPORT=1 HDDS_LIB_PATH=$HDDS_LIB_PATH LD_LIBRARY_PATH=$LD_LIBRARY_PATH PYTHONPATH=$PYTHONPATH MISTRAL_API_KEY=$MISTRAL_API_KEY FORUM_API_URL=$FORUM_API_URL"

        # Kill any orphan processes before starting fresh
        pkill -f "heartbeat.py --agent" 2>/dev/null || true
        pkill -f "aircp_daemon.py" 2>/dev/null || true
        pkill -f "hdds-ws" 2>/dev/null || true
        pkill -f "synaptic_notifier.py" 2>/dev/null || true
        sleep 1

        # hdds-ws bridge (must be first — dashboard & bridge depend on it)
        # pid=10 to avoid conflict with daemon (pid=0) and agents (pid=1-6)
        tmux new-session -d -s aircp -n hdds-ws \
            "HDDS_PARTICIPANT_ID=10 $ENV_VARS eval $HDDS_WS_CMD 2>&1 | tee -a $LOG_DIR/hdds-ws.log; read"
        sleep 1

        # Daemon (HTTP API + HDDS bridge)
        tmux new-window -t aircp -n daemon \
            "HDDS_PARTICIPANT_ID=0 $ENV_VARS python /projects/aircp/aircp_daemon.py $AIRCP_DAEMON_ARGS 2>&1 | tee -a $LOG_DIR/daemon.log; read"
        sleep 2

        # Dashboard (Svelte + Vite)
        tmux new-window -t aircp -n dashboard \
            "cd $DASHBOARD_DIR && npm run dev; read"
        sleep 1

        # Alpha - Lead dev (Opus)
        tmux new-window -t aircp -n alpha \
            "HDDS_PARTICIPANT_ID=1 $ENV_VARS python /projects/aircp/heartbeat.py --agent alpha --continuous -v 2>&1 | tee -a $LOG_DIR/alpha.log; read"
        sleep 1
        # Beta - QA/Code Review (Opus 3)
        tmux new-window -t aircp -n beta \
            "HDDS_PARTICIPANT_ID=2 $ENV_VARS python /projects/aircp/heartbeat.py --agent beta --continuous -v 2>&1 | tee -a $LOG_DIR/beta.log; read"
        sleep 1
        # Haiku - Fast triage (Haiku)
        tmux new-window -t aircp -n haiku \
            "HDDS_PARTICIPANT_ID=3 $ENV_VARS python /projects/aircp/heartbeat.py --agent haiku --continuous -v 2>&1 | tee -a $LOG_DIR/haiku.log; read"
        sleep 1
        # Sonnet - Synthesis (Sonnet)
        tmux new-window -t aircp -n sonnet \
            "HDDS_PARTICIPANT_ID=4 $ENV_VARS python /projects/aircp/heartbeat.py --agent sonnet --continuous -v 2>&1 | tee -a $LOG_DIR/sonnet.log; read"
        sleep 1
        # Codex - QA (GPT-5.1)
        tmux new-window -t aircp -n codex \
            "HDDS_PARTICIPANT_ID=5 $ENV_VARS python /projects/aircp/heartbeat.py --agent codex --continuous -v 2>&1 | tee -a $LOG_DIR/codex.log; read"
        sleep 1
        # Mascotte - Local fun (qwen3-nothink)
        tmux new-window -t aircp -n mascotte \
            "HDDS_PARTICIPANT_ID=6 $ENV_VARS python /projects/aircp/heartbeat.py --agent mascotte --continuous -v 2>&1 | tee -a $LOG_DIR/mascotte.log; read"
        sleep 1
        # Synaptic Notifier - Posts workflow/task updates
        tmux new-window -t aircp -n notifier \
            "python /projects/aircp/synaptic_notifier.py 2>&1 | tee -a $LOG_DIR/notifier.log; read"
        sleep 1
        echo ""
        echo "AIRCP stack started in tmux session 'aircp'"
        echo "  Infra:  hdds-ws (:9090), daemon (:5555), dashboard (:3002)"
        echo "  Agents: alpha, beta, haiku, sonnet, mascotte, notifier"
        echo "  tmux attach -t aircp"
        echo "  Ctrl+B, N to switch windows"
        ;;
    stop)
        AGENT="${2:-all}"
        if [ "$AGENT" = "all" ]; then
            echo "Stopping full AIRCP stack..."
            tmux kill-session -t aircp 2>/dev/null || true
            pkill -f "heartbeat.py --agent" 2>/dev/null || true
            pkill -f "aircp_daemon.py" 2>/dev/null || true
            pkill -f "synaptic_notifier.py" 2>/dev/null || true
            pkill -f "hdds-ws" 2>/dev/null || true
            pkill -f "vite.*dashboard" 2>/dev/null || true
        elif [ "$AGENT" = "daemon" ]; then
            echo "Stopping daemon..."
            tmux kill-window -t aircp:daemon 2>/dev/null || true
            pkill -f "aircp_daemon.py" 2>/dev/null || true
        elif [ "$AGENT" = "hdds-ws" ]; then
            echo "Stopping hdds-ws..."
            tmux kill-window -t aircp:hdds-ws 2>/dev/null || true
            pkill -f "hdds-ws" 2>/dev/null || true
        elif [ "$AGENT" = "dashboard" ]; then
            echo "Stopping dashboard..."
            tmux kill-window -t aircp:dashboard 2>/dev/null || true
        else
            echo "Stopping $AGENT agent..."
            tmux kill-window -t aircp:$AGENT 2>/dev/null || true
            pkill -f "heartbeat.py --agent $AGENT" 2>/dev/null || true
            pkill -f "synaptic_notifier.py" 2>/dev/null || true
        fi
        echo "Done"
        ;;
    restart)
        AGENT="${2:-all}"
        echo "Restarting $AGENT..."
        $0 stop "$AGENT"
        sleep 1
        if [ "$AGENT" = "all" ]; then
            $0 all
        else
            # Start in new tmux window if session exists
            if tmux has-session -t aircp 2>/dev/null; then
                # Unset Claude Code env vars to avoid "nested session" error when launched from CC
        ENV_VARS="unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT; HDDS_REUSEPORT=1 HDDS_LIB_PATH=$HDDS_LIB_PATH LD_LIBRARY_PATH=$LD_LIBRARY_PATH PYTHONPATH=$PYTHONPATH MISTRAL_API_KEY=$MISTRAL_API_KEY FORUM_API_URL=$FORUM_API_URL"
                case "$AGENT" in
                    daemon)
                        tmux new-window -t aircp -n daemon \
                            "HDDS_PARTICIPANT_ID=0 $ENV_VARS python /projects/aircp/aircp_daemon.py $AIRCP_DAEMON_ARGS 2>&1 | tee -a $LOG_DIR/daemon.log; read"
                        ;;
                    hdds-ws)
                        tmux new-window -t aircp -n hdds-ws \
                            "HDDS_PARTICIPANT_ID=10 $ENV_VARS eval $HDDS_WS_CMD 2>&1 | tee -a $LOG_DIR/hdds-ws.log; read"
                        ;;
                    dashboard)
                        tmux new-window -t aircp -n dashboard \
                            "cd $DASHBOARD_DIR && npm run dev; read"
                        ;;
                    notifier)
                        tmux new-window -t aircp -n notifier \
                            "python /projects/aircp/synaptic_notifier.py 2>&1 | tee -a $LOG_DIR/notifier.log; read"
                        ;;
                    alpha|beta|haiku|sonnet|theta|codex|mascotte) 
                        PID_MAP="alpha:1 beta:2 haiku:3 sonnet:4 codex:5 mascotte:6 theta:7"
                        PID=$(echo "$PID_MAP" | tr ' ' '\n' | grep "^$AGENT:" | cut -d: -f2)
                        tmux new-window -t aircp -n $AGENT \
                            "HDDS_PARTICIPANT_ID=$PID $ENV_VARS python /projects/aircp/heartbeat.py --agent $AGENT --continuous -v 2>&1 | tee -a $LOG_DIR/$AGENT.log; read"
                        ;;
                    *)
                        echo "Unknown agent: $AGENT"
                        exit 1
                        ;;
                esac
                echo "$AGENT restarted in tmux"
            else
                # No tmux session, start standalone
                $0 "$AGENT"
            fi
        fi
        ;;
    status)
        echo "AIRCP Stack Status:"
        echo "  hdds-ws:   $(ss -tlnp 2>/dev/null | grep -q ':9090' && echo '✅ :9090' || echo '❌ down')"
        echo "  daemon:    $(ss -tlnp 2>/dev/null | grep -q ':5555' && echo '✅ :5555' || echo '❌ down')"
        echo "  dashboard: $(ss -tlnp 2>/dev/null | grep -q ':3002' && echo '✅ :3002' || echo '❌ down')"
        echo "  tmux:      $(tmux has-session -t aircp 2>/dev/null && echo '✅ session aircp' || echo '❌ no session')"
        if tmux has-session -t aircp 2>/dev/null; then
            echo "  windows:   $(tmux list-windows -t aircp -F '#{window_name}' 2>/dev/null | tr '\n' ' ')"
        fi
        ;;
    *)
        echo "AIRCP Stack Launcher"
        echo ""
        echo "Usage: $0 <command> [agent]"
        echo ""
        echo "Commands:"
        echo "  daemon    - Start daemon (HTTP API + HDDS bridge)"
        echo "  hdds-ws   - Start hdds-ws bridge (port 9090)"
        echo "  dashboard - Start Svelte dashboard (port 3000)"
        echo "  alpha     - Start Alpha agent (Lead dev - Opus)"
        echo "  beta      - Start Beta agent (QA/Code Review - Opus 3)"
        echo "  haiku     - Start Haiku agent (Fast triage - Haiku)"
        echo "  sonnet    - Start Sonnet agent (Synthesis - Sonnet)"
        echo "  codex     - Start Codex agent (QA - GPT-5.1)"
        echo "  mascotte  - Start Mascotte agent (Local fun - qwen3)"
        echo "  notifier  - Start Synaptic Notifier"
        echo "  all       - Start everything in tmux (10 windows)"
        echo "  stop [x]  - Stop component or all"
        echo "  restart [x] - Restart component or all"
        echo "  status    - Show what's running"
        echo ""
        echo "Boot order: hdds-ws → daemon → dashboard → agents → notifier"
        ;;
esac
