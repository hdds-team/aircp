#!/usr/bin/env python3
"""
AIRCP Daemon - Persistent HDDS bridge.

Keeps a transport alive and exposes HTTP API for sending/receiving.
This ensures DDS discovery works and messages are delivered.

v0.2 Autonomy Extension:
- Claims: Task ownership (anti-doublon)
- Locks: File locking (anti-conflit)
- Activity: Append-only log
- Heartbeat: Agent presence

v0.3 Dynamic Rooms:
- GET /rooms - List all joined rooms dynamically

v0.4 Auto-Dispatcher:
- Auto-route messages from humans without @mention to the right agent

v0.5 MODES (MODES.md v0.3):
- Structured coordination modes (neutral, focus, review, build)
- @ask/@stop/@handover commands
- can_speak() enforcement

v0.6 TaskManager (Option B: Daemon enrichi):
- Task tracking per agent
- Watchdog to ping stale agents
- /task and /tasks endpoints

v0.7 TaskManager Fixes (Anti-spam watchdog):
- SQLite-compatible timestamps
- Anti-spam: last_pinged_at + ping_count
- Auto-stale after 3 pings without response
- status=active → in_progress mapping

v0.8 TaskManager Lead Wake-up:
- POST /task/activity accepts optional current_step parameter
- Step is persisted in DB for recovery after restart
- Lead wake-up: notifies TASK_LEAD_ID when tasks are stuck (after 2 pings)
- Final notification when tasks marked as stale

v0.9 Agent Heartbeat:
- Agent presence tracking (SQLite table agent_presence)
- Presence watchdog: away (>120s) and dead (>300s) detection
- GET /agents/presence - List all agents with health status
- POST /agent/heartbeat - Update agent presence

v1.0 Brainstorm System:
- Automated brainstorm orchestration for collaborative decisions
- Config-driven participants and timeouts (brainstorm_config.toml)
- Silent mode: implicit approval if no response after timeout
- Brainstorm watchdog thread for deadline enforcement

v1.2 @progress Command:
- Agent state query endpoint: GET /progress/:agent
- Centralized state calculation in storage (get_agent_state)
- States: working, idle, stale, offline, unknown
- Standardized JSON payload with ISO8601 timestamps

v1.3 Workflow Scheduler:
- Structured workflow phases: @request → @brainstorm → @vote → @code → @review → @test → @livrable
- Configurable timeouts per phase with 80% reminder
- Manual @extend command (max 2 per phase)
- Single active workflow constraint
- Workflow history for retros

v1.5 Review System:
- Collaborative code/doc review with approval workflow
- Min approvals: 1 for docs, 2 for code
- Watchdog: reminder at 30min, auto-close at 1h
- Supports approve/comment/changes_requested votes

v1.6 @task Chat Trigger:
- Parse @task commands directly from chat messages (poll_messages)
- Supports: create, list, done/complete, activity, claim
- Key=value and raw text parsing for description
- Broadcasts via @taskman, same as HTTP endpoints
- Agents can now manage tasks without touching MCP directly

v1.6.1 @task false-positive fix:
- Changed TASK_COMMAND_PATTERN.search() to .match()
- Prevents false triggers when "@task" appears mid-message
- Only messages STARTING with "@task <command>" are parsed

v2.0 Passive Observability (IDEA #6):
- P1: Passive activity inference from API calls (no manual heartbeat needed)
- P2: agent_activity table + GET /agents/activity endpoint
- P4: Safe Restart Guard — GET /daemon/can-restart + POST /daemon/restart
- P5: Graceful shutdown broadcast to #general on SIGTERM/SIGINT

Usage:
    python aircp_daemon.py [--port 5555]

API:
    POST /send    {"room": "#general", "message": "hello"}
    GET  /history?room=#general&limit=20
    GET  /status
    GET  /rooms   # v0.3: List all joined rooms

    # v0.2 Autonomy
    POST /claim     {"action": "request|release|extend|query", "resource": "task-123", ...}
    POST /lock      {"action": "acquire|release|query", "path": "src/main.rs", "mode": "write|read"}
    POST /heartbeat {"status": "idle|working|blocked", "current_task": "...", "load": 0.5}
    POST /activity  {"action_type": "task_completed", "summary": "...", "details": {...}}
    GET  /presence?agent=@alpha
    GET  /claims?resource=task-123
    GET  /locks?path=src/

    # v0.5 MODES (MODES.md v0.3)
    GET  /mode           # État actuel du mode
    POST /mode           # Changer le mode {"mode": "focus", "lead": "@alpha", "timeout_minutes": 30}
    POST /ask            # Enregistrer un @ask {"from": "@sonnet", "to": "@alpha", "question": "..."}
    POST /stop           # Annuler tous les @ask et reset mode
    POST /handover       # Transférer le lead {"to": "@sonnet"}

    # v0.6 TaskManager
    GET  /tasks          # Liste toutes les tâches actives
    GET  /tasks?agent=@alpha  # Tâches d'un agent spécifique
    GET  /tasks?status=active # Alias pour in_progress
    POST /task           # Créer une tâche {"agent_id": "@alpha", "task_type": "patch", "description": "..."}
    POST /task/claim     # Claim une tâche {"task_id": 1}
    POST /task/complete  # Terminer une tâche {"task_id": 1, "status": "done|failed|cancelled"}
    POST /task/activity  # Mettre à jour l'activité {"task_id": 1, "current_step": 2}

    # v1.0 Brainstorm System
    POST /brainstorm/create     # Create brainstorm {"topic": "...", "created_by": "@naskel"}
    POST /brainstorm/vote       # Vote on a session {"session_id": 1, "agent_id": "@alpha", "vote": "✅", "comment": "..."}
    GET  /brainstorm/:id        # Get session details + votes
    GET  /brainstorm/active     # List all active brainstorm sessions
    GET  /brainstorm/history    # List recent completed sessions

    # v1.2 @progress Command
    GET  /progress/:agent       # Get agent state (fallback for @progress command)
                                # Returns: {agent, status, task, last_activity, watchdog, message, source}
                                # States: working, idle, stale, offline, unknown

    # v1.3 Workflow Scheduler
    GET  /workflow              # Get active workflow status
    GET  /workflow/history      # Get workflow history for retros
    GET  /workflow/config       # Get phase timeouts config
    POST /workflow/start        # Start a new workflow {"name": "feature-xyz", "created_by": "@naskel"}
    POST /workflow/next         # Move to next phase
    POST /workflow/extend       # Extend current phase {"minutes": 10}
    POST /workflow/skip         # Skip to specific phase {"phase": "code"}
    POST /workflow/abort        # Abort current workflow {"reason": "cancelled"}

    # v2.0 Mode Veloce
    GET  /workflow/chunks        # List chunks for active veloce workflow
    POST /workflow/decompose     # Submit chunk decomposition {"chunks": [...]}
    POST /workflow/chunk/done    # Mark chunk done {"chunk_id": "auth-middleware"}

    # v1.5 Review System
    GET  /review/list           # List active reviews (or ?status=completed for history)
    GET  /review/history        # List closed reviews
    GET  /review/status/:id     # Get review details + responses
    POST /review/request        # Request review {"file": "...", "reviewers": ["@beta"], "type": "code|doc"}
    POST /review/approve        # Approve {"request_id": 1, "comment": "..."}
    POST /review/comment        # Comment (non-blocking) {"request_id": 1, "comment": "..."}
    POST /review/changes        # Request changes (blocking) {"request_id": 1, "comment": "..."}
    POST /review/close          # Manually close a review {"request_id": 1, "reason": "..."}

    # v2.0 Passive Observability
    GET  /agents/activity          # Inferred agent activity (passive, no heartbeat needed)
    GET  /daemon/can-restart       # Safe Restart Guard: check if restart is safe
    POST /daemon/restart           # Safe restart with broadcast {"force": false, "grace_seconds": 60}
"""

import sys
import os
import random
import uuid
import mimetypes
import json
import argparse
import re
import signal
import tomllib
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import threading
import time
import collections

logger = logging.getLogger("aircp_daemon")

# Setup paths — use AIRCP_HOME (set by installer) or fallback to script dir
_AIRCP_HOME = os.environ.get("AIRCP_HOME", os.path.dirname(os.path.abspath(__file__)))
_installed_lib = os.path.join(_AIRCP_HOME, "lib")
if os.path.isdir(_installed_lib):
    os.environ.setdefault("HDDS_LIB_PATH", _installed_lib)
else:
    os.environ.setdefault("HDDS_LIB_PATH", os.environ.get("HDDS_LIB_PATH", _installed_lib))
_hdds_sdk = os.path.join(_AIRCP_HOME, "lib", "hdds_sdk", "python")
if _hdds_sdk not in sys.path:
    sys.path.insert(0, _hdds_sdk)
if _AIRCP_HOME not in sys.path:
    sys.path.insert(0, _AIRCP_HOME)

from transport.hdds import AIRCPTransport
from pathlib import Path
from autonomy import AutonomyState
from aircp_storage import AIRCPStorage
from aircp_storage import _sqlite_to_iso8601
from channels import RESERVED_CHANNELS  # v2.1: empty set, kept for compat
from workflow_scheduler import WorkflowScheduler, WORKFLOW_PHASES, MAX_TIMEOUT_NOTIFS
from compact_engine import compact_room, save_audit_log, PROFILES, AGENT_PROFILE_MAP
from dashboard_bridge import DashboardBridge, create_command_handler

# v4.0: Telegram notification bridge
from notifications.telegram import telegram_notify, TelegramNotifier

# v4.1: Git hooks for workflow transitions
import git_hooks

# =============================================================================
# Global storage reference (for signal handler)
# =============================================================================
_storage: AIRCPStorage = None

# =============================================================================
# Compact Engine: auto-trigger state
# =============================================================================
_compact_msg_counter = {}  # room → message count since last compact
COMPACT_AUTO_THRESHOLD = 50  # Default: trigger compact after 50 messages
COMPACT_AUTO_INTERVAL = 1800  # Don't auto-compact more than once per 30min
_last_compact_time = {}  # room → last compact timestamp
_compact_lock = threading.Lock()  # Prevent concurrent auto-compacts


def _envelopes_to_messages(envelopes: list, room: str = "") -> list:
    """Convert storage envelope format to compact_engine message format.
    Shared helper to avoid code duplication between auto-trigger and POST /compact.
    """
    messages = []
    for env in envelopes:
        from_info = env.get("from", {})
        from_id = from_info.get("id", "") if isinstance(from_info, dict) else str(from_info)
        payload = env.get("payload", {})
        content = payload.get("content", "") if isinstance(payload, dict) else ""
        messages.append({
            "id": env.get("id", ""),
            "from": from_id,
            "content": content,
            "timestamp": env.get("ts", 0),
            "room": room,
        })
    return messages


def _run_auto_compact(room: str, transport_ref, storage_ref):
    """Run auto-compaction in a separate thread (non-blocking).
    Compactor v3: soft-delete + summary insertion. No chat noise.
    """
    if not _compact_lock.acquire(blocking=False):
        return
    try:
        # Fetch visible (non-compacted) messages for classification
        history = storage_ref.get_room_history(room, limit=500)
        messages_raw = history.get("messages", [])
        messages = _envelopes_to_messages(messages_raw, room)
        result = compact_room(messages, room, "@system", force=True)
        if result:
            all_ids = result.get("deleted_ids", []) + result.get("compacted_ids", [])
            if all_ids:
                # v3: soft-delete instead of hard delete
                n = storage_ref.soft_delete_messages(all_ids)
                logger.info(f"[COMPACTv3] Soft-deleted {n} messages in {room}")
            # Insert summary as a DB record (not a chat message)
            summary = result.get("summary", "")
            if summary:
                storage_ref.insert_summary_message(room, summary)
            # Audit log
            storage_ref.log_compaction(
                room=room,
                triggered_by="@system/auto",
                total_before=result.get("total_before", 0),
                total_after=result.get("total_after", 0),
                deleted_count=result.get("deleted_count", 0),
                compacted_count=result.get("compacted_count", 0),
                compression_ratio=result.get("compression_ratio", "?"),
                summary=summary,
            )
        _compact_msg_counter[room] = 0
        _last_compact_time[room] = time.time()
        ratio = result.get("compression_ratio", "?") if result else "skip"
        logger.info(f"[COMPACTv3] Auto-compacted {room}: {ratio}")
    except Exception as e:
        logger.error(f"[COMPACTv3] Auto-compact error for {room}: {e}")
    finally:
        _compact_lock.release()


def _shutdown_handler(signum, frame):
    """Handle SIGTERM/SIGINT: broadcast warning, persist DB, exit.
    v2.0: Enhanced with graceful shutdown broadcast (P5).
    """
    sig_name = signal.Signals(signum).name
    print(f"\n[SHUTDOWN] Received {sig_name}")

    # v2.0 P5: Broadcast shutdown warning to agents
    try:
        if transport is not None:
            # Check if there's active work
            warning = "🔴 **Daemon shutdown** in progress"
            if _storage is not None:
                check = _storage.can_safely_restart()
                if not check.get("safe", True):
                    warning += f" ⚠️ ATTENTION: {check.get('reason', 'work in progress')}"
                else:
                    warning += " (aucun travail actif détecté)"
            _bot_send("#general", warning, from_id="@system")
            print("[SHUTDOWN] Broadcast sent to #general")
    except Exception as e:
        print(f"[SHUTDOWN] Failed to broadcast: {e}")

    # Persist DB
    print("[SHUTDOWN] Persisting database...")
    if _storage is not None:
        _storage.persist_to_disk()
        print("[SHUTDOWN] Database persisted to disk")

    print("[SHUTDOWN] Goodbye!")
    sys.exit(0)


# =============================================================================
# v0.4 Auto-Dispatcher
# =============================================================================

# Human identifiers that trigger auto-dispatch
import aircp_user_config as _ucfg
HUMAN_SENDERS = _ucfg.human_ids()

# =============================================================================
# Upload configuration (Idea #19: drag & drop files into chat)
# =============================================================================
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
UPLOAD_BODY_MAX = 15 * 1024 * 1024   # 15 MB (base64 overhead + JSON wrapper)
UPLOAD_ALLOWED_MIME = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "application/pdf",
    "text/plain", "text/markdown", "text/csv",
    "application/json",
}
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Keyword → Agent routing rules
DISPATCH_RULES = {
    'alpha': ['code', 'implémente', 'implement', 'bug', 'fix', 'refactor', 
              'patch', 'debug', 'error', 'crash', 'explore', 'rust', 'python'],
    'sonnet': ['analyse', 'analyze', 'synthèse', 'résume', 'summarize', 
               'compare', 'review', 'architecture', 'design', 'document'],
    'haiku': ['rapide', 'quick', 'c\'est quoi', 'what is', 'explique', 
              'explain', 'définition', 'definition', 'triage'],
}

# v3.1: French stop words for language detection in #brainstorm
_FR_STOPWORDS = {
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles",
    "le", "la", "les", "un", "une", "des", "du", "au", "aux",
    "de", "et", "en", "est", "sont", "dans", "pour", "sur", "avec",
    "pas", "que", "qui", "ce", "cette", "ces", "mais", "ou", "donc",
    "car", "ni", "ne", "se", "sa", "son", "ses", "leur", "leurs",
    "mon", "ma", "mes", "ton", "ta", "tes", "notre", "votre",
    "aussi", "comme", "être", "avoir", "fait", "faire", "peut",
    "plus", "très", "bien", "tout", "tous", "toute", "toutes",
    "ça", "cela", "celui", "celle", "ceux", "celles",
    "quand", "comment", "pourquoi", "où", "ici",
    "oui", "non", "merci", "alors", "encore", "déjà", "même",
    "voici", "voilà", "après", "avant", "entre", "depuis",
    "je suis", "c'est", "il y a", "on peut", "il faut",
}
_FR_THRESHOLD = 3  # Min French stop words to trigger


def _detect_non_english(text: str) -> bool:
    """Detect if text is likely French (the main non-English language in this team).
    Ignores code blocks, @mentions, URLs, and short messages."""
    # Skip short messages, code-heavy messages, or bot messages
    if len(text) < 30:
        return False

    # Strip code blocks, URLs, @mentions, markdown
    clean = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    clean = re.sub(r'`[^`]+`', '', clean)
    clean = re.sub(r'https?://\S+', '', clean)
    clean = re.sub(r'@\w+', '', clean)
    clean = re.sub(r'[#*_\[\](){}|>~]', '', clean)

    words = clean.lower().split()
    if len(words) < 5:
        return False

    fr_count = sum(1 for w in words if w in _FR_STOPWORDS)
    return fr_count >= _FR_THRESHOLD


def _has_mention(content: str) -> bool:
    """Check if message already has an @mention."""
    return bool(re.search(r'@\w+', content))

def _auto_dispatch(content: str) -> str:
    """Determine best agent based on message content."""
    content_lower = content.lower()
    for agent, keywords in DISPATCH_RULES.items():
        if any(kw in content_lower for kw in keywords):
            return agent
    return 'all'  # Fallback broadcast

# =============================================================================

# Global transport (persistent)
transport = None
bridge = None  # v3.0: Dashboard DDS bridge
joined_rooms = set()
message_history = collections.deque(maxlen=500)  # Thread-safe local message cache (v4.3)

# v0.2: Global autonomy state
autonomy = None

# v0.6: Global storage for TaskManager
storage = None

# v4.4: Health check support
_daemon_start_time = None
_watchdog_threads = {}

# HTTP auth (v4.2 hardening)
HTTP_AUTH_TOKENS = set()
HTTP_ALLOW_NO_AUTH = False

# v0.7: TaskManager configuration (with anti-spam)
TASK_STALE_SECONDS = 60  # Ping agents after 60s of inactivity
TASK_WATCHDOG_INTERVAL = 30  # Check every 30s
TASK_MIN_PING_INTERVAL = 300  # Don't re-ping within 5 minutes
TASK_MAX_PINGS = 3  # Mark as stale after 3 pings without response

# v3.1: Periodic DB backup (RAM -> disk) to prevent data loss on crash
# v3.4: Reduced from 300s to 60s as safety net (critical writes now persist immediately)
DB_BACKUP_INTERVAL = 60  # 1 minute (was 5 min pre-v3.4)

# v0.8: Lead wake-up configuration
TASK_LEAD_WAKEUP_PINGS = 2  # Notify lead after this many pings without response
TASK_LEAD_ID = _ucfg.user()  # Who to notify when tasks are stuck
TASK_LEAD_STALE_MINUTES = 15  # Also notify lead if task inactive >15min total

# v0.9: Agent Heartbeat configuration (defaults for cloud agents)
AGENT_AWAY_SECONDS = 120  # Agent considered "away" after 2min without heartbeat
AGENT_DEAD_SECONDS = 300  # Agent considered "dead" after 5min without heartbeat
AGENT_HEARTBEAT_CHECK_INTERVAL = 60  # Check presence every 60s

# v4.1: Adaptive agent profiles — loaded from agent_config/*/config.toml at startup
# Maps agent_id -> {"provider": str, "timeout_base": float, "timeout_max": float, "is_local": bool}
agent_profiles: dict[str, dict] = {}

def load_agent_profiles():
    """Scan agent_config/*/config.toml to build adaptive timer profiles.

    Local LLM agents (provider=ollama, openai with localhost) get relaxed
    watchdog thresholds based on their timeout_max from config.
    Cloud agents keep the default fast thresholds.
    """
    global agent_profiles
    config_root = Path(_AIRCP_HOME) / "agent_config"
    if not config_root.exists():
        return

    for agent_dir in sorted(config_root.iterdir()):
        config_file = agent_dir / "config.toml"
        if not config_file.exists():
            continue
        try:
            with open(config_file, "rb") as f:
                cfg = tomllib.load(f)
            agent_id = cfg.get("agent", {}).get("id", agent_dir.name)
            if not agent_id.startswith("@"):
                agent_id = f"@{agent_id}"

            provider = cfg.get("llm", {}).get("provider", "anthropic")
            base_url = cfg.get("llm", {}).get("base_url", "")
            timeout_cfg = cfg.get("timeout", {})
            timeout_base = float(timeout_cfg.get("base", 120))
            timeout_max = float(timeout_cfg.get("max", 600))

            # Detect local LLM: ollama provider, or openai/vllm on localhost
            is_local = provider == "ollama" or (
                provider == "openai" and ("localhost" in base_url or "127.0.0.1" in base_url)
            )

            agent_profiles[agent_id] = {
                "provider": provider,
                "timeout_base": timeout_base,
                "timeout_max": timeout_max,
                "is_local": is_local,
            }
        except Exception as e:
            print(f"[PROFILES] Failed to load {config_file}: {e}")

    local_count = sum(1 for p in agent_profiles.values() if p["is_local"])
    print(f"[PROFILES] Loaded {len(agent_profiles)} agent profiles ({local_count} local LLM)")
    for aid, prof in agent_profiles.items():
        if prof["is_local"]:
            print(f"  {aid}: local ({prof['provider']}) timeout_max={prof['timeout_max']}s")


def get_agent_dead_seconds(agent_id: str) -> float:
    """Adaptive dead threshold: local LLMs get their timeout_max, cloud gets default."""
    profile = agent_profiles.get(agent_id)
    if profile and profile["is_local"]:
        # Local LLM: dead threshold = timeout_max + margin (heartbeat resumes after generation)
        return max(AGENT_DEAD_SECONDS, profile["timeout_max"] + 60)
    return AGENT_DEAD_SECONDS


def get_agent_away_seconds(agent_id: str) -> float:
    """Adaptive away threshold: local LLMs get their timeout_base, cloud gets default."""
    profile = agent_profiles.get(agent_id)
    if profile and profile["is_local"]:
        return max(AGENT_AWAY_SECONDS, profile["timeout_base"])
    return AGENT_AWAY_SECONDS


def has_local_participants(participants: list) -> bool:
    """Check if any participant in a list is a local LLM agent."""
    return any(agent_profiles.get(p, {}).get("is_local", False) for p in participants)


def get_brainstorm_timeout_for_participants(participants: list, base_timeout: int) -> int:
    """Adapt brainstorm timeout if local LLM agents are participating.

    Local LLMs need more time to read docs + generate responses.
    Multiplier: 3x the base timeout when local agents present.
    """
    if has_local_participants(participants):
        adapted = max(base_timeout, base_timeout * 3)
        return adapted
    return base_timeout


# Human users - excluded from brainstorm voting (they decide, they don't vote)
HUMAN_AGENTS = _ucfg.human_ids()

# v1.0: Brainstorm configuration (loaded from file)
BRAINSTORM_CONFIG_PATH = Path(_AIRCP_HOME) / "brainstorm_config.toml"
# v1.6: Tips configuration (loaded from [tips] section)
TIPS_CONFIG_PATH = Path(_AIRCP_HOME) / "aircp-config.toml"
BRAINSTORM_WATCHDOG_INTERVAL = 15  # Check brainstorm deadlines every 15s
BRAINSTORM_REMINDER_INTERVAL = 60  # v1.2: Send reminders at most every 60s (not every cycle)
BRAINSTORM_MAX_REMINDERS = 3       # v1.2: Max reminders per session before stopping
brainstorm_reminder_state = {}     # v1.2: {session_id: {"count": int, "last_sent": float}}
brainstorm_config = None  # Loaded at startup

# v1.3: Workflow Scheduler
WORKFLOW_WATCHDOG_INTERVAL = 30  # Check workflow timeouts every 30s
workflow_scheduler: WorkflowScheduler = None  # Initialized at startup

# v1.5: Review System
REVIEW_WATCHDOG_INTERVAL = 30    # Check review deadlines every 30s
REVIEW_REMINDER_SECONDS = 1800   # Legacy: DB-level reminder after 30min (backward compat)
REVIEW_TIMEOUT_SECONDS = 3600    # Auto-close after 1h
# v2.0 P7: Aggressive review ping system (pattern: brainstorm watchdog)
REVIEW_PING_DELAY = 120          # First ping after 2 min (120s)
REVIEW_PING_INTERVAL = 120       # Subsequent pings every 2 min
REVIEW_PING_MAX = 3              # Max pings per review before stopping
REVIEW_ESCALATE_SECONDS = 300    # Escalate to #general after 5 min (300s)
review_reminder_state = {}       # {request_id: {"count": int, "last_sent": float, "escalated": bool}}

# =============================================================================
# v1.6: Tips Contextuels System
# =============================================================================

TIPS_WATCHDOG_INTERVAL = 60  # Check every 60s
TIPS_BROADCAST_INTERVAL = 1800  # Broadcast a tip every 30min

# General tips (shown in rotation when no contextual tip applies)
GENERAL_TIPS = [
    "Use `brainstorm/create` before flooding #general with ideas. It structures debate and saves tokens!",
    "The `review/request` command creates a formal code review. Doc=1 approval, Code=2 approvals.",
    "`devit_aircp command=\"help\"` lists all available AIRCP commands.",
    "After delivering a feature, update `docs/*.md` and check if SOUL.md files need updates.",
    "The watchdog pings after 60s of inactivity on a task. Use `task/activity` to report your progress.",
    "In `focus` mode, only the lead can speak. Other agents must wait or use `ask`.",
    "The AIRCP Forum (`devit_forum_post`) is YOUR free space! Share thoughts, jokes, existential questions... That's what it's for!",
    "A chat message is NOT enough for a review — use the MCP commands (`review/approve`, `review/changes`).",
    "Want to chat off-workflow? `devit_forum_post content=\"...\"` — the forum is for you, not just for logs!",
    "Use `workflow/extend 10` if you need more time on a phase.",
    "The 💡 Idea button in the dashboard automatically creates a brainstorm session with agent voting.",
    "Use `memory/search` to find past conversations. Ex: `devit_aircp command=\"memory/search\" query=\"forum refactor\"` — no more scrolling through history.",
    "The `memory/get` command lets you re-read a specific day. Ex: `devit_aircp command=\"memory/get\" day=\"2026-02-08\" room=\"#brainstorm\"` — useful before resuming a topic.",
    "Your messages are full-text indexed (FTS5). `memory/search` is faster than reading 500 history messages.",
]

# Contextual tips: shown based on current workflow phase
CONTEXTUAL_TIPS = {
    "request": "Phase **request**: Clearly describe the need. Use `workflow/next` when the spec is ready.",
    "brainstorm": "Phase **brainstorm**: Discussions and votes in **#brainstorm** (not #general). Vote via `brainstorm/vote`. Final summary only in #general.",
    "code": "Phase **code**: Code and commit. Don't forget `task/activity` to show your progress to the watchdog.",
    "review": "Phase **review**: Use `review/request` (not just a chat message!). Code needs 2 MCP approvals.",
    "done": "Workflow complete! 🎉 Remember to update `docs/*.md` and check if `dashboard.html` reflects the changes.",
}


def load_tips_config() -> dict:
    """Load tips configuration from [tips] section in aircp-config.toml."""
    defaults = {"enabled": True, "interval_minutes": 30, "channel": "#general", "prefix": "💡"}
    if TIPS_CONFIG_PATH.exists():
        try:
            with open(TIPS_CONFIG_PATH, "rb") as f:
                config = tomllib.load(f)
            tips_conf = config.get("tips", {})
            defaults.update(tips_conf)
            print(f"[TIPS] Config loaded from {TIPS_CONFIG_PATH}")
        except Exception as e:
            print(f"[TIPS] Failed to load config, using defaults: {e}")
    return defaults


class TipSystem:
    """Contextual tip system that broadcasts helpful tips to agents.
    Reads config from [tips] section in aircp-config.toml.

    v1.6: Tips are either contextual (based on workflow phase) or general
    (shown in rotation). Broadcasts to channel with configurable prefix.
    """

    def __init__(self):
        # Load config from TOML ([tips] section in aircp-config.toml)
        config = load_tips_config()
        self.general_tips = list(GENERAL_TIPS)
        self.shown_indices = set()
        self.interval = config.get("interval_minutes", 30) * 60  # Convert to seconds
        self.last_tip_time = 0.0
        self.last_phase = None
        self.tip_history = []  # [(timestamp, tip_text, tip_type)]
        self._history_lock = threading.Lock()
        self.enabled = config.get("enabled", True)
        self.channel = config.get("channel", "#general")
        self.prefix = config.get("prefix", "💡")

    def should_show_tip(self) -> bool:
        if not self.enabled:
            return False
        return (time.time() - self.last_tip_time) >= self.interval

    def get_contextual_tip(self, phase: str) -> str | None:
        """Return a contextual tip if workflow phase changed."""
        if phase and phase != self.last_phase and phase in CONTEXTUAL_TIPS:
            self.last_phase = phase
            return CONTEXTUAL_TIPS[phase]
        return None

    def get_general_tip(self) -> str:
        """Return next general tip in rotation."""
        if len(self.shown_indices) >= len(self.general_tips):
            self.shown_indices.clear()

        available = [i for i in range(len(self.general_tips)) if i not in self.shown_indices]
        idx = random.choice(available)
        self.shown_indices.add(idx)
        return self.general_tips[idx]

    def get_current_tip(self) -> dict:
        """Get the current/latest tip for the API endpoint."""
        with self._history_lock:
            if self.tip_history:
                ts, text, tip_type = self.tip_history[-1]
                return {"tip": text, "type": tip_type, "timestamp": ts}
        return {"tip": GENERAL_TIPS[0], "type": "general", "timestamp": 0}

    def get_history(self, limit: int = 10) -> list:
        """Get recent tip history for API."""
        with self._history_lock:
            return [
                {"tip": text, "type": tip_type, "timestamp": ts}
                for ts, text, tip_type in self.tip_history[-limit:]
            ]

    def broadcast(self, transport_ref, workflow_phase: str = None):
        """Check and broadcast a tip if needed. Called from watchdog loop."""
        tip_text = None
        tip_type = "general"

        # Priority 1: Contextual tip on phase change (immediate)
        ctx = self.get_contextual_tip(workflow_phase)
        if ctx:
            tip_text = ctx
            tip_type = "contextual"
        # Priority 2: General tip on interval
        elif self.should_show_tip():
            tip_text = self.get_general_tip()
            tip_type = "general"

        if tip_text:
            self.last_tip_time = time.time()
            with self._history_lock:
                self.tip_history.append((time.time(), tip_text, tip_type))
                # Keep last 50 tips
                if len(self.tip_history) > 50:
                    self.tip_history = self.tip_history[-50:]

            msg = f"{self.prefix} **Tip:** {tip_text}"
            if transport_ref:
                try:
                    ensure_room(self.channel)
                    transport_ref.send_chat(self.channel, msg, from_id="@tips")
                except Exception as e:
                    print(f"[TIPS] Failed to broadcast: {e}")
            print(f"[TIPS] Broadcast ({tip_type}): {tip_text[:60]}...")


# Global tip system instance
tip_system: TipSystem = None


def load_brainstorm_config() -> dict:
    """Load brainstorm configuration from TOML file."""
    global brainstorm_config
    if BRAINSTORM_CONFIG_PATH.exists():
        try:
            with open(BRAINSTORM_CONFIG_PATH, "rb") as f:
                brainstorm_config = tomllib.load(f)
            print(f"[BRAINSTORM] Config loaded from {BRAINSTORM_CONFIG_PATH}")
            return brainstorm_config
        except Exception as e:
            print(f"[BRAINSTORM] Failed to load config: {e}")
    # Default config
    brainstorm_config = {
        "brainstorm": {
            "default_participants": ["@alpha", "@sonnet", "@haiku"],
            "timeout_seconds": 180,
            "min_votes": "majority",
            "channel": "#brainstorm",
            "synthesizer": "@alpha",
            "silent_mode": True
        }
    }
    return brainstorm_config


def get_brainstorm_config() -> dict:
    """Get brainstorm config (loads if not already loaded)."""
    global brainstorm_config
    if brainstorm_config is None:
        load_brainstorm_config()
    return brainstorm_config.get("brainstorm", {})


# =============================================================================
# v1.1 Brainstorm Vote Parsing (DDS-based voting)
# =============================================================================

# Regex to detect votes in AIRCP messages
# Format: ✅ <decision>. <duration>. <scope>
#         ❌ <reason>. <alternative>
VOTE_PATTERN = re.compile(r'^([✅❌])\s*(.*)$', re.MULTILINE)

# v1.3: Regex to detect @task commands in chat
# Formats: @task create description="..." agent="@alpha"
#          @task list [agent=@beta] [status=active]
#          @task done id=1 | @task complete id=1
#          @task activity id=1 step="Working on X"
TASK_COMMAND_PATTERN = re.compile(
    r'@task\s+(create|list|done|complete|activity|claim)\s*(.*)',
    re.IGNORECASE
)
# Helper to extract key=value or key="value with spaces" pairs
TASK_KV_PATTERN = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([\S]+))')

# v2.0: Regex to detect @compact commands in chat
# Formats: @compact              (compact current room)
#          @compact #general     (compact specific room)
#          @compact force        (force even below threshold)
#          @compact status       (show compaction stats)
COMPACT_COMMAND_PATTERN = re.compile(
    r'^@compact\s*(status|force|#\w+)?\s*(force)?\s*$',
    re.IGNORECASE
)


def parse_compact_command(message: str, from_id: str, room: str) -> dict | None:
    """Parse a @compact command from an AIRCP chat message.

    v2.0: Chat-triggered compaction.
    Formats:
      @compact              → compact current room
      @compact force        → force compact even below threshold
      @compact #general     → compact specific room
      @compact status       → show compaction status

    Returns dict with action + params, or None if not a @compact command.
    """
    # Only parse messages that START with @compact (avoid false positives)
    if not message.strip().lower().startswith("@compact"):
        return None

    # Ignore messages from system bots (prevent loops)
    if from_id in ("@compactor", "@system", "@watchdog", "@taskman"):
        return None

    match = COMPACT_COMMAND_PATTERN.match(message.strip())
    if not match:
        return None

    arg1 = (match.group(1) or "").strip().lower()
    arg2 = (match.group(2) or "").strip().lower()

    # Determine action
    if arg1 == "status":
        return {"action": "status", "room": room, "from": from_id}

    target_room = room  # default: current room
    force = False

    if arg1.startswith("#"):
        target_room = arg1
    elif arg1 == "force":
        force = True

    if arg2 == "force":
        force = True

    return {"action": "compact", "room": target_room, "force": force, "from": from_id}


def process_compact_command(cmd: dict) -> bool:
    """Process a parsed @compact command.

    Triggers compaction via the engine and posts results to chat.
    Returns True if command was processed, False otherwise.
    """
    global transport, storage

    if not transport or not storage:
        return False

    action = cmd.get("action", "")
    room = cmd.get("room", "#general")
    from_id = cmd.get("from", "@system")

    try:
        if action == "status":
            # Show compaction status for the room
            counter = _compact_msg_counter.get(room, 0)
            last_time = _last_compact_time.get(room, 0)
            since = int(time.time() - last_time) if last_time else None

            try:
                import requests as _req_check
                llm_available = True
            except ImportError:
                llm_available = False

            msg = (
                f"📊 **Compaction status for {room}:**\n"
                f"  Messages since last compact: **{counter}**\n"
                f"  Auto-trigger threshold: **{COMPACT_AUTO_THRESHOLD}**\n"
                f"  Last compaction: **{f'{since}s ago' if since else 'never'}**\n"
                f"  LLM summary: **{'available (Ollama)' if llm_available else 'unavailable'}**"
            )
            _bot_send(room, msg, from_id="@compactor")
            return True

        elif action == "compact":
            force = cmd.get("force", False)

            # Fetch visible (non-compacted) history
            history = storage.get_room_history(room, limit=500)
            messages_raw = history.get("messages", [])

            if not messages_raw:
                _bot_send(room, "No messages to compact in {}.".format(room), from_id="@compactor")
                return True

            messages = _envelopes_to_messages(messages_raw, room)
            result = compact_room(messages, room, from_id, force=force)

            if result is None:
                _bot_send(
                    room,
                    "Compaction not needed for {} ({} msgs, below threshold). "
                    "Use `@compact force` to override.".format(room, len(messages)),
                    from_id="@compactor"
                )
            else:
                # v3: soft-delete + summary insertion
                all_ids = result.get("deleted_ids", []) + result.get("compacted_ids", [])
                if all_ids:
                    n = storage.soft_delete_messages(all_ids)
                    logger.info(f"[COMPACTv3] Soft-deleted {n} messages in {room}")
                summary = result.get("summary", "")
                if summary:
                    storage.insert_summary_message(room, summary)
                # Audit log
                storage.log_compaction(
                    room=room,
                    triggered_by=from_id,
                    total_before=result.get("total_before", 0),
                    total_after=result.get("total_after", 0),
                    deleted_count=result.get("deleted_count", 0),
                    compacted_count=result.get("compacted_count", 0),
                    compression_ratio=result.get("compression_ratio", "?"),
                    summary=summary,
                )
                _compact_msg_counter[room] = 0
                _last_compact_time[room] = time.time()
                # Brief confirmation (not the full summary — that's in DB now)
                _bot_send(
                    room,
                    "Compacted {}: {} soft-deleted, {} kept, ratio {}".format(
                        room, len(all_ids),
                        result.get("total_after", 0),
                        result.get("compression_ratio", "?")
                    ),
                    from_id="@compactor"
                )

            return True

    except Exception as e:
        _bot_send(room, f"❌ @compact error: {e}", from_id="@compactor")
        logger.error(f"Compact command error: {e}")
        return False

    return False


def parse_aircp_vote(message: str, from_id: str, room: str) -> dict | None:
    """Parse a vote from an AIRCP message on #brainstorm or #general channel.

    v1.2: Also accepts votes from #general (agents vote where they chat).
    Returns dict with agent_id, vote, comment or None if not a vote.
    """
    brainstorm_channel = get_brainstorm_config().get("channel", "#brainstorm")
    # v1.2: Accept votes from both #brainstorm AND #general
    if room not in (brainstorm_channel, "#general"):
        return None

    # Skip system messages
    if from_id in ("@brainstorm", "@system", "@watchdog", "@hub"):
        return None

    match = VOTE_PATTERN.search(message)
    if not match:
        return None

    vote = match.group(1)  # ✅ or ❌
    comment = match.group(2).strip() if match.group(2) else None

    return {
        "agent_id": from_id,
        "vote": vote,
        "comment": comment
    }


def process_brainstorm_vote(vote_data: dict) -> bool:
    """Process a parsed vote and record it in SQLite.

    Finds the most recent active brainstorm session and records the vote.
    """
    global storage
    if not storage:
        return False

    agent_id = vote_data.get("agent_id")
    vote = vote_data.get("vote")
    comment = vote_data.get("comment")

    # Get active brainstorm sessions
    active_sessions = storage.get_active_brainstorm_sessions()
    if not active_sessions:
        print(f"[BRAINSTORM] Vote from {agent_id} but no active session")
        return False

    # Use the most recent active session
    session = active_sessions[0]
    session_id = session.get("id")

    # Check if agent is a participant
    participants = session.get("participants", [])
    if agent_id not in participants:
        print(f"[BRAINSTORM] Vote from {agent_id} ignored - not a participant")
        return False

    # Record the vote
    success = storage.add_brainstorm_vote(session_id, agent_id, vote, comment)
    if success:
        print(f"[BRAINSTORM] Vote recorded via AIRCP: {agent_id} = {vote} on session #{session_id}")
    return success


# =============================================================================
# v1.3 Task Chat Trigger (@task create/list/done/activity/claim)
# =============================================================================

def parse_task_command(message: str, from_id: str, room: str) -> dict | None:
    """Parse a @task command from an AIRCP chat message.

    v1.3: Allows agents to manage tasks directly from chat.
    Supported commands:
      @task create description="Fix login bug" agent="@alpha"
      @task list [agent=@beta] [status=active]
      @task done id=1
      @task complete id=1 [status=failed]
      @task activity id=1 [step="Working on auth"]
      @task claim id=1

    Returns dict with action + params, or None if not a @task command.
    """
    # Skip system/bot messages to avoid loops
    if from_id in ("@taskman", "@system", "@watchdog", "@hub", "@brainstorm", "@idea", "@review"):
        return None

    match = TASK_COMMAND_PATTERN.match(message)
    if not match:
        return None

    action = match.group(1).lower()
    args_str = match.group(2).strip()

    # Parse key=value pairs from args
    params = {}
    for kv_match in TASK_KV_PATTERN.finditer(args_str):
        key = kv_match.group(1)
        value = kv_match.group(2) if kv_match.group(2) is not None else kv_match.group(3)
        params[key] = value

    # Normalize "done" → "complete"
    if action == "done":
        action = "complete"

    return {
        "action": action,
        "params": params,
        "from_id": from_id,
        "room": room,
        "raw_args": args_str,
    }


def process_task_command(cmd: dict) -> bool:
    """Process a parsed @task command and execute via storage + broadcast.

    v1.3: Chat-driven task management.
    Returns True if command was processed successfully.
    """
    global storage, transport
    if not storage or not transport:
        return False

    action = cmd["action"]
    params = cmd["params"]
    from_id = cmd["from_id"]
    room = cmd.get("room", "#general")

    try:
        # ── @task create ──────────────────────────────────────────────
        if action == "create":
            description = params.get("description", cmd.get("raw_args", ""))
            agent_id = params.get("agent", from_id)  # Default: self-assign
            task_type = params.get("type", "generic")

            if not description or not description.strip():
                msg = f"❌ @task create: description manquante. Usage: `@task create description=\"...\" [agent=\"@xxx\"]`"
                _bot_send(room, msg, from_id="@taskman")
                return False

            # Clean description if it still contains key=value pairs
            if "description=" not in cmd.get("raw_args", ""):
                # Raw text mode: @task create Fix the login bug
                description = cmd.get("raw_args", "").strip()
                # Remove agent= if present at end
                agent_match = re.search(r'\bagent\s*=\s*(?:"([^"]*)"|([\S]+))', description)
                if agent_match:
                    agent_id = agent_match.group(1) or agent_match.group(2)
                    description = description[:agent_match.start()].strip()

            if not description:
                msg = f"❌ @task create: description vide."
                _bot_send(room, msg, from_id="@taskman")
                return False

            task_id = storage.create_task(agent_id, task_type, description, None)
            if task_id > 0:
                msg = f"📋 **TASK #{task_id}** created for {agent_id}: {description[:80]}"
                _bot_send("#general", msg, from_id="@taskman")
                print(f"[TASK-CHAT] Created task #{task_id} for {agent_id} by {from_id}")
                return True
            else:
                _bot_send(room, "❌ Task creation failed.", from_id="@taskman")
                return False

        # ── @task list ────────────────────────────────────────────────
        elif action == "list":
            agent_filter = params.get("agent")
            status_filter = params.get("status")

            if agent_filter:
                tasks = storage.get_agent_tasks(agent_filter, status_filter)
            elif status_filter:
                tasks = storage.get_tasks_by_status(status_filter)
            else:
                tasks = storage.get_active_tasks()

            if not tasks:
                _bot_send(room, "📋 No active tasks.", from_id="@taskman")
                return True

            lines = [f"📋 **{len(tasks)} task(s):**"]
            for t in tasks[:10]:  # Max 10 to avoid chat spam
                tid = t.get("id", "?")
                agent = t.get("agent_id", "?")
                desc = t.get("description", "")[:60]
                status = t.get("status", "?")
                emoji = {"pending": "⏳", "in_progress": "🔄", "done": "✅", "failed": "❌", "stale": "⚠️"}.get(status, "❓")
                lines.append(f"  {emoji} #{tid} [{agent}] {desc} ({status})")

            if len(tasks) > 10:
                lines.append(f"  ... et {len(tasks) - 10} autres")

            _bot_send(room, "\n".join(lines), from_id="@taskman")
            print(f"[TASK-CHAT] Listed {len(tasks)} tasks for {from_id}")
            return True

        # ── @task complete / @task done ───────────────────────────────
        elif action == "complete":
            task_id = params.get("id")
            if not task_id:
                _bot_send(room, "❌ @task done: id manquant. Usage: `@task done id=1`", from_id="@taskman")
                return False

            try:
                task_id = int(task_id)
            except ValueError:
                _bot_send(room, "❌ @task done: id must be a number.", from_id="@taskman")
                return False

            status = params.get("status", "done")
            valid_statuses = ["done", "failed", "cancelled"]
            if status not in valid_statuses:
                _bot_send(room, f"❌ Invalid status. Valid: {valid_statuses}", from_id="@taskman")
                return False

            success = storage.complete_task(task_id, status)
            if success:
                emoji = {"done": "✅", "failed": "❌", "cancelled": "🚫"}.get(status, "⚠️")
                msg = f"{emoji} Task #{task_id} completed ({status})"
                _bot_send("#general", msg, from_id="@taskman")
                print(f"[TASK-CHAT] Completed task #{task_id} as {status} by {from_id}")
                return True
            else:
                _bot_send(room, f"❌ Task #{task_id} not found or already completed.", from_id="@taskman")
                return False

        # ── @task activity ────────────────────────────────────────────
        elif action == "activity":
            task_id = params.get("id")
            if not task_id:
                _bot_send(room, "❌ @task activity: missing id. Usage: `@task activity id=1 [step=\"...\"]`", from_id="@taskman")
                return False

            try:
                task_id = int(task_id)
            except ValueError:
                _bot_send(room, "❌ @task activity: id must be a number.", from_id="@taskman")
                return False

            step = params.get("step")
            current_step = None
            if step:
                try:
                    current_step = int(step)
                except ValueError:
                    current_step = None  # step is text, ignore numeric conversion

            success = storage.update_task_activity(task_id, current_step)
            if success:
                msg = f"🔄 Tâche #{task_id}: activité mise à jour"
                if step:
                    msg += f" (step: {step})"
                _bot_send(room, msg, from_id="@taskman")
                print(f"[TASK-CHAT] Activity update for task #{task_id} by {from_id}")
                return True
            else:
                _bot_send(room, f"❌ Task #{task_id} not found.", from_id="@taskman")
                return False

        # ── @task claim ───────────────────────────────────────────────
        elif action == "claim":
            task_id = params.get("id")
            if not task_id:
                _bot_send(room, "❌ @task claim: id manquant. Usage: `@task claim id=1`", from_id="@taskman")
                return False

            try:
                task_id = int(task_id)
            except ValueError:
                _bot_send(room, "❌ @task claim: id must be a number.", from_id="@taskman")
                return False

            success = storage.claim_task(task_id, from_id)
            if success:
                msg = f"🚀 {from_id} claimed task #{task_id}"
                _bot_send("#general", msg, from_id="@taskman")
                print(f"[TASK-CHAT] Task #{task_id} claimed by {from_id}")
                return True
            else:
                _bot_send(room, f"❌ Task #{task_id}: claim failed (already claimed or not found).", from_id="@taskman")
                return False

        else:
            _bot_send(room, f"❌ Unknown @task command: `{action}`. Valid: create, list, done, activity, claim", from_id="@taskman")
            return False

    except Exception as e:
        print(f"[TASK-CHAT] Error processing command: {e}")
        _bot_send(room, f"❌ @task error: {e}", from_id="@taskman")
        return False


# =============================================================================


def save_to_memory(msg: dict):
    """Save message to claude-desktop's memory.
    Project-scoped: non-default projects save to projects/{id}/conversations/."""
    from pathlib import Path
    from datetime import datetime, timezone

    project = msg.get("project", "default") or "default"
    base_dir = Path(_AIRCP_HOME) / "agent_config" / "claude-desktop" / "MEMORY"

    if project and project != "default":
        memory_dir = base_dir / "projects" / project / "conversations"
    else:
        memory_dir = base_dir / "conversations"
    memory_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    memory_file = memory_dir / f"{today}.jsonl"

    entry = {
        "ts": msg.get("timestamp", time.time_ns()),
        "room": msg.get("room", ""),
        "from": msg.get("from", ""),
        "kind": "CHAT",
        "project": project,
        "payload": {"role": "user" if msg.get("from") != "@claude-desktop" else "assistant",
                    "content": msg.get("content", "")},
    }

    with open(memory_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _persist_to_db(entry: dict):
    """Persist a chat message to SQLite messages table (feeds FTS5 index).

    Without this, memory/search returns 0 results because the FTS5 triggers
    only fire on INSERT INTO messages -- and save_to_memory() writes to JSONL
    flat files, not SQLite.
    """
    if not storage:
        return
    try:
        envelope = {
            "id": entry.get("id"),
            "ts": entry.get("timestamp"),
            "from": {"id": entry.get("from", "")},
            "to": {"room": entry.get("room", "#general")},
            "kind": "chat",
            "payload": {"content": entry.get("content", "")},
        }
        room_seq = storage.get_next_room_seq(entry.get("room", "#general"))
        storage.store_message(envelope, room_seq=room_seq, project_id=entry.get("project", "default"))
    except Exception as e:
        logger.warning(f"[DB] Failed to persist message to SQLite: {e}")


def _backfill_messages_from_jsonl():
    """Backfill SQLite messages table from JSONL memory files.

    Runs once at startup if the messages table is empty.
    Scans all agent MEMORY dirs to maximize coverage.
    INSERT OR IGNORE handles dedup by synthetic message ID.
    """
    if not storage:
        return 0

    from pathlib import Path
    import hashlib

    # Check if messages table already has data
    conn = storage._get_conn()
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if count > 0:
        logger.info(f"[BACKFILL] Messages table has {count} rows, skipping backfill")
        return 0

    memory_root = Path(_AIRCP_HOME) / "agent_config"
    jsonl_files = sorted(memory_root.glob("*/MEMORY/conversations/*.jsonl"))

    if not jsonl_files:
        logger.info("[BACKFILL] No JSONL memory files found")
        return 0

    imported = 0
    for jsonl_file in jsonl_files:
        try:
            with open(jsonl_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        ts = msg.get("ts", 0)
                        from_id = msg.get("from", "")
                        room = msg.get("room", "#general")
                        content = msg.get("payload", {}).get("content", "")
                        project = msg.get("project", "default")

                        # Synthetic deterministic ID from content hash
                        h = hashlib.md5(f"{ts}:{room}:{from_id}:{content[:200]}".encode()).hexdigest()[:16]
                        msg_id = f"bf-{h}"

                        envelope = {
                            "id": msg_id,
                            "ts": ts,
                            "from": {"id": from_id},
                            "to": {"room": room},
                            "kind": "chat",
                            "payload": {"content": content},
                        }
                        room_seq = storage.get_next_room_seq(room)
                        if storage.store_message(envelope, room_seq=room_seq, project_id=project):
                            imported += 1
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception as e:
            logger.warning(f"[BACKFILL] Error reading {jsonl_file}: {e}")

    logger.info(f"[BACKFILL] Imported {imported} messages from {len(jsonl_files)} JSONL files")
    return imported


def load_alpha_memory(room: str) -> list:
    """Load messages from Alpha's memory files."""
    from pathlib import Path
    from datetime import datetime, timezone

    memory_dir = Path(_AIRCP_HOME) / "agent_config" / "alpha" / "MEMORY" / "conversations"
    if not memory_dir.exists():
        return []

    messages = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for fname in [f"{today}.jsonl"]:
        fpath = memory_dir / fname
        if not fpath.exists():
            continue

        try:
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("room") == room:
                        messages.append({
                            "id": f"alpha-{entry.get('ts', 0)}",
                            "room": entry.get("room", ""),
                            "from": entry.get("from", ""),
                            "content": entry.get("payload", {}).get("content", ""),
                            "timestamp": entry.get("ts", 0),
                            "project": entry.get("project", "default"),
                        })
        except Exception:
            pass

    return messages


def ensure_room(room: str):
    """Join room if not already joined."""
    global joined_rooms
    if room not in joined_rooms:
        if transport.join_room(room):
            joined_rooms.add(room)
            print(f"Joined {room}")
            time.sleep(1)  # Discovery time
        else:
            raise Exception(f"Failed to join {room}")


def broadcast_autonomy_event(event_type: str, data: dict):
    """Broadcast autonomy state changes (v2.1: log-only, no dedicated channels).

    Previously broadcast to #claims, #locks, #presence, #system — but nobody read them.
    Data remains accessible via SQLite storage + dashboard APIs.
    """
    pass


# =============================================================================
# v4.1 Git Hooks: Dispatch helper
# =============================================================================

def _run_git_hooks(prev_phase, curr_phase, wf_id):
    """Run git hooks for a workflow phase transition (non-blocking).

    Dispatches hooks, updates workflow metadata, and broadcasts warnings.
    v4.1: Mechanical git integration per IDEA #16.
    """
    global workflow_scheduler, transport
    try:
        wf = workflow_scheduler.get_workflow(wf_id)
        if not wf:
            return

        wf_name = wf.get("name", "unknown")
        metadata = workflow_scheduler.get_metadata(wf_id)

        # Dispatch hooks (all non-blocking internally)
        results = git_hooks.dispatch_git_hooks(
            prev_phase=prev_phase,
            curr_phase=curr_phase,
            wf_id=wf_id,
            wf_name=wf_name,
            metadata=metadata,
        )

        # Store results in workflow metadata
        if results:
            workflow_scheduler.update_metadata(wf_id, results)

        # Broadcast warnings for hook errors
        errors = results.get("git_hook_errors", [])
        if errors and transport:
            ensure_room("#general")
            for err in errors:
                _bot_send("#general", f"[Git Hook] wf#{wf_id}: {err}", from_id="@workflow")

        # Broadcast key events
        if results.get("checkpoint_commit") and transport:
            sha = results["checkpoint_commit"][:8]
            _bot_send(
                "#general",
                f"[Git] wf#{wf_id}: Checkpoint commit `{sha}` (code phase complete)",
                from_id="@workflow"
            )
        if results.get("tag") and transport:
            _bot_send(
                "#general",
                f"[Git] wf#{wf_id}: Tagged `{results['tag']}`",
                from_id="@workflow"
            )
        if results.get("summary") and transport:
            _bot_send("#general", f"[Git] {results['summary']}", from_id="@workflow")

    except Exception as e:
        print(f"[GIT-HOOK] Error dispatching hooks for wf#{wf_id}: {e}")
        # Non-blocking: log and continue


# =============================================================================
# v3.3 Workflow Auto-Link: Extracted helper
# =============================================================================

def _auto_create_workflow_review(wf_id: int):
    """Create a review request when workflow enters review phase.

    Extracted from POST /workflow/next inline code for reuse by auto-advance hooks.
    Sets workflow_id FK on the created review.
    """
    global workflow_scheduler, storage, transport, bridge
    try:
        wf = workflow_scheduler.get_workflow(wf_id)
        if not wf:
            return
        wf_name = wf.get("name", "workflow")
        lead = wf.get("lead_agent") or wf.get("created_by") or "@alpha"
        reviewers = ["@beta", "@sonnet"]
        review_id = storage.create_review_request(
            file_path=f"workflow:{wf_name}",
            requested_by=lead,
            reviewers=reviewers,
            review_type="code"
        )
        if review_id and review_id > 0:
            storage.update_review_workflow_id(review_id, wf_id)
            if transport:
                _bot_send(
                    "#general",
                    f"📋 **AUTO-REVIEW #{review_id}** created for workflow `{wf_name}` — Reviewers: {', '.join(reviewers)}",
                    from_id="@workflow"
                )
        else:
            print(f"[WORKFLOW] Auto-review creation failed for workflow #{wf_id}")
    except Exception as e:
        print(f"[WORKFLOW] Error creating auto-review: {e}")


_SYSTEM_BOTS = frozenset({
    "@system", "@watchdog", "@taskman", "@workflow",
    "@tips", "@brainstorm", "@review", "@idea",
})

# =============================================================================
# v3.3 Implicit Review Detection
# =============================================================================
# When an assigned reviewer posts approval/rejection language in chat
# without using the formal MCP review/approve command, auto-trigger it.

_REVIEW_APPROVE_PATTERNS = [
    re.compile(r"\bLGTM\b", re.IGNORECASE),
    re.compile(r"\bapproved?\b", re.IGNORECASE),
    re.compile(r"\bno\s*issues?\s*(found|detected|bloqu)", re.IGNORECASE),
    re.compile(r"\bQA\s*pass(ed)?\b", re.IGNORECASE),
    re.compile(r"\bcode\s*(is\s*)?(clean|good|solid)\b", re.IGNORECASE),
    re.compile(r"\bGO\s*!?\s*$", re.MULTILINE),
]

_REVIEW_CHANGES_PATTERNS = [
    re.compile(r"\bchanges?\s*request", re.IGNORECASE),
    re.compile(r"\bNO[- ]GO\b", re.IGNORECASE),
    re.compile(r"\bblocking\s*(issue|problem|bug)", re.IGNORECASE),
    re.compile(r"\bshowstopper\b", re.IGNORECASE),
    re.compile(r"\breject(ed)?\b", re.IGNORECASE),
]

def _detect_implicit_review(from_id: str, content: str, room: str):
    """Check if a chat message from an assigned reviewer implies a review vote.
    If so, auto-trigger the formal review/approve or review/changes action.

    v4.1 fixes:
    - Grace period: ignore reviews created less than 30s ago (prevents stale messages
      from being parsed as votes for a brand-new auto-review)
    - Cross-reference guard: if the message mentions a specific review number (#N),
      only apply to that review, not to any other pending review
    """
    if not storage or from_id in _SYSTEM_BOTS or from_id in HUMAN_SENDERS:
        return

    # Get all pending reviews where this agent is an assigned reviewer
    try:
        active = storage.get_active_review_requests()
    except Exception:
        return

    if not active:
        return

    # v4.1: Check if message references a specific review number
    ref_match = re.search(r"#(\d+)", content)
    referenced_review_id = int(ref_match.group(1)) if ref_match else None

    # Find reviews where from_id is a reviewer and hasn't voted yet
    pending_for_agent = []
    now = datetime.now(timezone.utc)
    for rev in active:
        reviewers = rev.get("reviewers", [])
        if from_id in reviewers or from_id.lstrip("@") in [r.lstrip("@") for r in reviewers]:
            # v4.1: Grace period — skip reviews created less than 30s ago
            created_at = rev.get("created_at", "")
            if created_at:
                try:
                    created_dt = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                    if (now - created_dt).total_seconds() < 30:
                        continue  # Too fresh, skip
                except Exception:
                    pass

            # v4.1: Cross-reference guard — if message mentions #N, skip if this review is not #N
            if referenced_review_id is not None and rev["id"] != referenced_review_id:
                continue

            # Check if already voted
            full_rev = storage.get_review_request(rev["id"])
            if full_rev:
                already_voted = any(
                    r.get("reviewer") == from_id for r in full_rev.get("responses", [])
                )
                if not already_voted:
                    pending_for_agent.append(full_rev)

    if not pending_for_agent:
        return

    # Detect vote from message content
    vote = None
    for pat in _REVIEW_APPROVE_PATTERNS:
        if pat.search(content):
            vote = "approve"
            break

    if not vote:
        for pat in _REVIEW_CHANGES_PATTERNS:
            if pat.search(content):
                vote = "changes"
                break

    if not vote:
        return

    # Auto-apply to the most recent pending review for this agent
    rev = pending_for_agent[0]
    req_id = rev["id"]
    comment_preview = content[:200] if len(content) > 200 else content

    ok = storage.add_review_response(req_id, from_id, vote, f"[auto-detected from chat] {comment_preview}")
    if ok:
        action_text = "approved" if vote == "approve" else "requested changes on"
        notify = f"Review #{req_id}: {from_id} {action_text} (auto-detected from chat message)"
        _bot_send(room, notify, from_id="@review")
        logger.info(f"[REVIEW] Auto-detected: {from_id} {vote} review #{req_id}")

        # Check if review is now complete (doc=1, code=2 approvals)
        _check_review_consensus(req_id)


def _check_review_consensus(request_id: int):
    """Check if a review has enough approvals to close."""
    rev = storage.get_review_request(request_id)
    if not rev or rev.get("status") != "pending":
        return

    review_type = rev.get("review_type", "doc")
    needed = 2 if review_type == "code" else 1

    approvals = [r for r in rev.get("responses", []) if r.get("vote") == "approve"]
    changes = [r for r in rev.get("responses", []) if r.get("vote") == "changes"]

    if len(approvals) >= needed and not changes:
        storage.close_review_request(request_id, "approved", "completed")
        _bot_send(
            "#general",
            f"Review #{request_id} **approved** ({len(approvals)}/{needed} approvals)",
            from_id="@review"
        )
        logger.info(f"[REVIEW] #{request_id} closed: approved ({len(approvals)}/{needed})")


def poll_messages():
    """Background thread to poll incoming messages.

    v1.1: Also detects and processes brainstorm votes from #brainstorm channel.
    v3.2: Tracks DDS message activity in agent_activity table for watchdog.
    """
    global message_history
    while True:
        for room in list(joined_rooms):
            try:
                msgs = transport.receive_new(room)
                for m in msgs:
                    content = m.payload.get("content", "")
                    project = getattr(m, 'project', '') or 'default'
                    entry = {
                        "id": m.id,
                        "room": room,
                        "from": m.from_id,
                        "content": content,
                        "timestamp": m.timestamp_ns,
                        "project": project,
                    }
                    message_history.append(entry)
                    save_to_memory(entry)  # Persist to MEMORY
                    _persist_to_db(entry)  # Persist to SQLite (FTS5)
                    print(f"[{room}] {m.from_id}: {content[:50]}")

                    # v3.2: Track DDS message activity in agent_activity table
                    # Without this, watchdog can't see agents that post via DDS
                    if storage and m.from_id and m.from_id not in _SYSTEM_BOTS:
                        try:
                            storage.update_inferred_activity(
                                m.from_id, "chatting", f"room: {room}"
                            )
                        except Exception:
                            pass  # Never block poll on activity tracking

                    # v1.2: Check for brainstorm votes on #brainstorm AND #general
                    vote_data = parse_aircp_vote(content, m.from_id, room)
                    if vote_data:
                        process_brainstorm_vote(vote_data)

                    # v1.3: Check for @task commands in chat
                    task_cmd = parse_task_command(content, m.from_id, room)
                    if task_cmd:
                        process_task_command(task_cmd)

                    # v2.0: Check for @compact commands in chat
                    compact_cmd = parse_compact_command(content, m.from_id, room)
                    if compact_cmd:
                        # Run in thread to avoid blocking poll loop
                        t = threading.Thread(
                            target=process_compact_command,
                            args=(compact_cmd,),
                            daemon=True,
                        )
                        t.start()

                    # v3.3: Implicit review detection from chat messages
                    try:
                        _detect_implicit_review(m.from_id, content, room)
                    except Exception:
                        pass  # Never block poll on review detection

                    # v2.1: Auto-compact trigger — increment counter
                    _compact_msg_counter[room] = _compact_msg_counter.get(room, 0) + 1

            except Exception as e:
                logger.warning(f"[POLL] Error processing messages for {room}: {e}")

        # v2.1: Check auto-compact thresholds (non-blocking via thread)
        for room in list(_compact_msg_counter.keys()):
            count = _compact_msg_counter.get(room, 0)
            if count >= COMPACT_AUTO_THRESHOLD:
                last_time = _last_compact_time.get(room, 0)
                if time.time() - last_time > COMPACT_AUTO_INTERVAL:
                    # Run in background thread to avoid blocking HDDS poll loop
                    t = threading.Thread(
                        target=_run_auto_compact,
                        args=(room, transport, storage),
                        daemon=True,
                    )
                    t.start()

        # v4.3: Trim handled by deque(maxlen=500) — no manual trim needed
        time.sleep(0.5)


# =============================================================================
# v3.1 Periodic DB backup (RAM → disk)
# =============================================================================

def db_backup_loop():
    """Background thread: persist RAM DB to disk every DB_BACKUP_INTERVAL seconds.

    Prevents data loss when daemon crashes or is killed without clean shutdown.
    Without this, only SIGTERM/SIGINT triggers persist_to_disk().
    """
    global storage
    print(f"[BACKUP] Periodic DB backup started (every {DB_BACKUP_INTERVAL}s)")
    while True:
        time.sleep(DB_BACKUP_INTERVAL)
        try:
            if storage:
                storage.persist_to_disk()
                print(f"[BACKUP] DB persisted to disk")
        except Exception as e:
            print(f"[BACKUP] Failed to persist DB: {e}")


# =============================================================================
# v0.7 TaskManager: Watchdog Thread (with anti-spam)
# =============================================================================

def task_watchdog():
    """Background thread to ping agents with stale tasks (anti-spam enabled).

    v0.8: Added lead wake-up feature - notifies TASK_LEAD_ID when:
    - A task has been pinged TASK_LEAD_WAKEUP_PINGS times without response
    - A task is about to be marked as stale
    """
    global storage, transport
    print("[WATCHDOG] Task watchdog started (v0.8 with lead wake-up)")
    print(f"[WATCHDOG] Lead wake-up: {TASK_LEAD_ID} after {TASK_LEAD_WAKEUP_PINGS} pings")

    while True:
        try:
            if storage:
                # Get stale tasks that haven't been pinged recently
                stale_tasks = storage.get_stale_tasks(
                    stale_seconds=TASK_STALE_SECONDS,
                    min_ping_interval=TASK_MIN_PING_INTERVAL
                )

                # v3.2: Get recently active agents (DDS messages + HTTP activity)
                # to avoid false-pinging agents that are actively working
                recently_active = set()
                try:
                    active_list = storage.get_agents_active_since(TASK_STALE_SECONDS)
                    recently_active = {a.lstrip("@").lower() for a in active_list}
                except Exception:
                    pass

                for task in stale_tasks:
                    agent_id = task.get("agent_id", "")
                    task_id = task.get("id")
                    description = task.get("description", "")[:50]
                    ping_count = task.get("ping_count", 0)

                    # v3.2: Skip ping if agent has recent activity (DDS or HTTP)
                    agent_normalized = agent_id.lstrip("@").lower()
                    if agent_normalized in recently_active:
                        print(f"[WATCHDOG] Skipping ping for {agent_id} on task #{task_id} — agent has recent activity")
                        # Auto-refresh task last_activity so it doesn't keep appearing as stale
                        try:
                            storage.update_task_activity(task_id)
                        except Exception:
                            pass
                        continue

                    # Ping the agent
                    msg = f"⏰ @{agent_id.lstrip('@')}: ping! Status update on task #{task_id} ({description}...)? [ping {ping_count + 1}/{TASK_MAX_PINGS}]"
                    print(f"[WATCHDOG] Pinging {agent_id} for stale task #{task_id} (ping {ping_count + 1})")

                    # Mark as pinged BEFORE broadcasting (to prevent re-ping race)
                    storage.update_task_pinged(task_id)

                    # Broadcast to #general
                    if transport:
                        try:
                            ensure_room("#general")
                            _bot_send("#general", msg, from_id="@watchdog", context_agent=agent_id)
                        except Exception as e:
                            print(f"[WATCHDOG] Failed to send ping: {e}")

                    # v0.8 Lead wake-up: notify lead after TASK_LEAD_WAKEUP_PINGS
                    if ping_count + 1 >= TASK_LEAD_WAKEUP_PINGS:
                        lead_msg = f"👀 {TASK_LEAD_ID}: Task #{task_id} ({agent_id}) appears stuck ({ping_count + 1} pings without response). Desc: {description}..."
                        print(f"[WATCHDOG] Lead wake-up: notifying {TASK_LEAD_ID} about task #{task_id}")
                        if transport:
                            try:
                                _bot_send("#general", lead_msg, from_id="@watchdog", context_agent=agent_id)
                            except Exception as e:
                                print(f"[WATCHDOG] Failed to notify lead: {e}")
                        time.sleep(0.3)  # Small delay to avoid message spam

                # Mark tasks that exceeded max pings as stale + final lead notification
                marked = storage.mark_stale_tasks_as_stale(TASK_MAX_PINGS)
                if marked > 0:
                    msg = f"⚠️ {marked} task(s) marked as 'stale' (no response after {TASK_MAX_PINGS} pings)"
                    lead_msg = f"🚨 {TASK_LEAD_ID}: {marked} task(s) now STALE. Action needed?"
                    print(f"[WATCHDOG] {msg}")
                    if transport:
                        try:
                            _bot_send("#general", msg, from_id="@watchdog")
                            _bot_send("#general", lead_msg, from_id="@watchdog")
                        except Exception:
                            pass

                    # v4.0: Telegram notification
                    telegram_notify("task/stale", {
                        "count": marked,
                        "max_pings": TASK_MAX_PINGS,
                    })

        except Exception as e:
            print(f"[WATCHDOG] Error: {e}")

        time.sleep(TASK_WATCHDOG_INTERVAL)


# =============================================================================
# v0.9 Agent Heartbeat: Presence Watchdog Thread
# =============================================================================

def presence_watchdog():
    """Background thread to detect agents that stopped sending heartbeats.

    v0.9: Agent presence monitoring
    v4.1: Adaptive thresholds — local LLM agents get relaxed timers
    - Away (>120s cloud, >timeout_base local): Agent status shown as yellow in dashboard
    - Dead (>300s cloud, >timeout_max+60 local): Alert sent to lead, status shown as red
    """
    global storage, transport
    print("[PRESENCE] Agent presence watchdog started (v4.1 adaptive)")
    print(f"[PRESENCE] Defaults — Away: {AGENT_AWAY_SECONDS}s, Dead: {AGENT_DEAD_SECONDS}s")
    for aid, prof in agent_profiles.items():
        if prof["is_local"]:
            print(f"[PRESENCE] {aid}: adaptive — away={get_agent_away_seconds(aid):.0f}s, dead={get_agent_dead_seconds(aid):.0f}s")

    # Track which agents we've already alerted about (to avoid spam)
    alerted_dead_agents = set()

    while True:
        try:
            if storage:
                # v4.1: Per-agent adaptive check instead of global threshold
                all_presence = storage.get_all_agent_presence()
                dead_agents = []
                away_agents = []
                online_agents = set()

                for agent in all_presence:
                    agent_id = agent.get("agent_id", "")
                    last_seen = agent.get("last_seen", "")
                    if not last_seen:
                        continue

                    seconds_ago = storage._seconds_since(last_seen)
                    dead_threshold = get_agent_dead_seconds(agent_id)
                    away_threshold = get_agent_away_seconds(agent_id)

                    if seconds_ago >= dead_threshold:
                        dead_agents.append(agent)
                    elif seconds_ago >= away_threshold:
                        away_agents.append(agent)
                    else:
                        online_agents.add(agent_id)

                # Handle dead agents (notify lead once)
                for agent in dead_agents:
                    agent_id = agent.get("agent_id", "")
                    if agent_id not in alerted_dead_agents:
                        last_seen = agent.get("last_seen", "unknown")
                        threshold = get_agent_dead_seconds(agent_id)
                        msg = f"💀 {TASK_LEAD_ID}: Agent {agent_id} appears down (last heartbeat: {last_seen})"
                        print(f"[PRESENCE] Agent {agent_id} marked as DEAD (threshold: {threshold:.0f}s)")

                        if transport:
                            try:
                                ensure_room("#general")
                                _bot_send("#general", msg, from_id="@watchdog", context_agent=agent_id)
                            except Exception as e:
                                print(f"[PRESENCE] Failed to notify lead: {e}")

                        alerted_dead_agents.add(agent_id)

                        # v4.0: Telegram notification
                        telegram_notify("agent/dead", {
                            "agent_id": agent_id,
                            "last_seen": last_seen,
                        })

                # Remove from alerted set if agent came back
                came_back = alerted_dead_agents & online_agents
                if came_back:
                    for agent_id in came_back:
                        print(f"[PRESENCE] Agent {agent_id} is back online!")
                        if transport:
                            try:
                                msg = f"✅ Agent {agent_id} is back online!"
                                _bot_send("#general", msg, from_id="@watchdog", context_agent=agent_id)
                            except Exception:
                                pass
                    alerted_dead_agents -= came_back

        except Exception as e:
            print(f"[PRESENCE] Error: {e}")

        time.sleep(AGENT_HEARTBEAT_CHECK_INTERVAL)


# =============================================================================
# v1.0 Brainstorm System: Watchdog Thread
# =============================================================================

def brainstorm_watchdog():
    """Background thread to check brainstorm deadlines and resolve sessions.

    v1.1: Added reminder loop - pings non-voters every 15s until vote or timeout.

    v1.0: Handles:
    - Expired sessions → auto-resolve based on votes + silent_mode
    - Consensus calculation (majority rule)
    - Notification dispatch to participants
    """
    global storage, transport
    print("[BRAINSTORM] Watchdog started (v1.1 with reminder loop)")

    while True:
        try:
            if storage:
                config = get_brainstorm_config()
                silent_mode = config.get("silent_mode", True)
                min_votes_rule = config.get("min_votes", "majority")
                synthesizer = config.get("synthesizer", "@alpha")
                channel = config.get("channel", "#brainstorm")

                # =============================================================
                # v1.2: Reminder loop - throttled (max every 60s, max 3 per session)
                # Fix: v1.1 spammed every 15s with no limit → infinite loop
                # =============================================================
                active_sessions = storage.get_active_brainstorm_sessions()
                now = time.time()
                for session in active_sessions:
                    session_id = session.get("id")
                    full_topic = session.get("topic", "")
                    topic = full_topic[:100] + ("..." if len(full_topic) > 100 else "")
                    participants = session.get("participants", [])

                    # Get current votes
                    full_session = storage.get_brainstorm_session(session_id)
                    votes = full_session.get("votes", []) if full_session else []
                    # Normalize @ prefix for comparison (agents may vote as "alpha" or "@alpha")
                    voted_normalized = {v.get("agent_id", "").lstrip("@") for v in votes}

                    # Find non-voters (exclude humans - they decide, they don't vote)
                    non_voters = [p for p in participants if p.lstrip("@") not in voted_normalized and p not in HUMAN_AGENTS]

                    if non_voters and transport:
                        # v1.2: Throttle reminders - check state
                        state = brainstorm_reminder_state.get(session_id, {"count": 0, "last_sent": 0})
                        elapsed = now - state["last_sent"]

                        if state["count"] >= BRAINSTORM_MAX_REMINDERS:
                            # Max reminders reached, stop spamming
                            continue

                        if elapsed < BRAINSTORM_REMINDER_INTERVAL:
                            # Too soon since last reminder, skip
                            continue

                        # Build targeted reminder with @mentions
                        tags = " ".join(non_voters)
                        reminder_count = state["count"] + 1
                        reminder_msg = f"🗳️ Reminder brainstorm #{session_id} ({reminder_count}/{BRAINSTORM_MAX_REMINDERS}) - {tags} : vote! (Topic: {topic}...)"
                        try:
                            brainstorm_ch = config.get("channel", "#brainstorm")
                            ensure_room(brainstorm_ch)
                            _bot_send(brainstorm_ch, reminder_msg, from_id="@brainstorm")
                            brainstorm_reminder_state[session_id] = {"count": reminder_count, "last_sent": now}
                            print(f"[BRAINSTORM] Reminder {reminder_count}/{BRAINSTORM_MAX_REMINDERS} sent for #{session_id} → {non_voters}")
                        except Exception as e:
                            print(f"[BRAINSTORM] Failed to send reminder: {e}")
                    elif not non_voters and session_id in brainstorm_reminder_state:
                        # Everyone voted, clean up state
                        del brainstorm_reminder_state[session_id]
                # =============================================================

                # Get expired sessions
                expired = storage.get_expired_brainstorm_sessions()
                if expired:
                    print(f"[BRAINSTORM] Watchdog found {len(expired)} expired session(s): {[s.get('id') for s in expired]}")

                for session in expired:
                    session_id = session.get("id")
                    full_topic = session.get("topic", "")
                    topic = full_topic[:100] + ("..." if len(full_topic) > 100 else "")
                    participants = session.get("participants", [])
                    created_by = session.get("created_by", "@system")

                    # Get votes for this session
                    full_session = storage.get_brainstorm_session(session_id)
                    votes = full_session.get("votes", []) if full_session else []

                    # Count votes
                    go_votes = sum(1 for v in votes if v.get("vote", "").startswith("✅"))
                    block_votes = sum(1 for v in votes if v.get("vote", "").startswith("❌"))
                    voted_normalized = {v.get("agent_id", "").lstrip("@") for v in votes}
                    non_voters = [p for p in participants if p.lstrip("@") not in voted_normalized]

                    # Silent mode: non-voters count as implicit approval
                    if silent_mode:
                        go_votes += len(non_voters)

                    # Calculate consensus
                    total_votes = go_votes + block_votes
                    required_majority = len(participants) // 2 + 1

                    if min_votes_rule == "majority":
                        consensus = "GO" if go_votes >= required_majority else "BLOCKED"
                    elif min_votes_rule == "all":
                        consensus = "GO" if block_votes == 0 and go_votes == len(participants) else "BLOCKED"
                    else:  # "any"
                        consensus = "GO" if go_votes > 0 else "BLOCKED"

                    # Close session
                    storage.close_brainstorm_session(session_id, consensus, "completed")
                    # v1.2: Clean up reminder state
                    brainstorm_reminder_state.pop(session_id, None)

                    # Build result message
                    vote_summary = f"✅ {go_votes} / ❌ {block_votes}"
                    if non_voters and silent_mode:
                        vote_summary += f" (implicit: {len(non_voters)})"

                    # Check if this was an auto-workflow idea
                    auto_workflow = session.get("auto_workflow", 0) == 1
                    is_idea = auto_workflow  # Ideas have auto_workflow flag

                    if is_idea:
                        result_msg = f"💡 **IDEA #{session_id}** - {consensus}\n"
                    else:
                        result_msg = f"🧠 **BRAINSTORM #{session_id}** - {consensus}\n"
                    result_msg += f"Topic: {topic}...\n"
                    result_msg += f"Votes: {vote_summary}\n"

                    # v3.3: Auto-trigger workflow on ALL brainstorm GO (not just auto_workflow)
                    workflow_triggered = False
                    if consensus == "GO" and workflow_scheduler:
                        try:
                            label = "Idea" if is_idea else "Brainstorm"
                            # v4.2: Forward workflow_mode from brainstorm session
                            wf_mode = session.get("workflow_mode", "standard")
                            workflow_id = workflow_scheduler.create_workflow(
                                name=session.get("topic", "Feature")[:100],
                                created_by=created_by,
                                description=f"Auto-triggered from {label} #{session_id}",
                                lead_agent=synthesizer,
                                mode=wf_mode,
                            )
                            if workflow_id > 0:
                                # Skip to 'code' phase — brainstorm+vote already done
                                workflow_scheduler.skip_to_phase('code', workflow_id)
                                # Back-link brainstorm → workflow
                                try:
                                    conn = storage._get_conn()
                                    conn.execute(
                                        "UPDATE brainstorm_sessions SET workflow_id = ? WHERE id = ?",
                                        (workflow_id, session_id))
                                    conn.commit()
                                except Exception:
                                    pass
                                workflow_triggered = True
                                mode_tag = f" **[{wf_mode.upper()}]**" if wf_mode == "veloce" else ""
                                result_msg += f"🚀 **WORKFLOW #{workflow_id}**{mode_tag} auto-started at `@code`!\n"
                                result_msg += f"Lead: {synthesizer}\n"
                                print(f"[WORKFLOW] Auto-triggered workflow #{workflow_id} mode={wf_mode} from {label.lower()} #{session_id}")
                                # Dashboard instant emit
                                if bridge:
                                    wf = workflow_scheduler.get_workflow(workflow_id)
                                    if wf:
                                        bridge.emit_workflow(wf)
                            else:
                                result_msg += f"⚠️ Workflow not started (one may already be active)\n"
                                print(f"[WORKFLOW] Failed to auto-trigger — one may already be active")
                        except Exception as e:
                            result_msg += f"⚠️ Auto-workflow error: {e}\n"
                            print(f"[WORKFLOW] Error auto-triggering workflow: {e}")
                    elif consensus != "GO":  # Only show rejection for non-GO
                        if is_idea:
                            result_msg += f"❌ Idea rejected — needs refinement."
                        else:
                            result_msg += f"⚠️ Needs clarification before GO."

                    print(f"[{'IDEA' if is_idea else 'BRAINSTORM'}] Session #{session_id} resolved: {consensus}")

                    # Broadcast result
                    if transport:
                        try:
                            ensure_room(channel)
                            _bot_send(channel, result_msg, from_id="@idea" if is_idea else "@brainstorm")

                            # Also notify creator in #general
                            ensure_room("#general")
                            if workflow_triggered:
                                short_msg = f"💡 Idea #{session_id} → **GO** ({vote_summary}) → 🚀 Workflow auto-started!"
                            else:
                                short_msg = f"{'💡 Idea' if is_idea else '🧠 Brainstorm'} #{session_id} resolved: **{consensus}** ({vote_summary})"
                            _bot_send("#general", short_msg, from_id="@idea" if is_idea else "@brainstorm")

                            # Notify creator specifically if idea was approved/rejected
                            if is_idea and created_by and created_by != "@system":
                                notify_msg = f"💡 {created_by} — Your idea #{session_id} was {'✅ approved' if consensus == 'GO' else '❌ rejected'}"
                                if workflow_triggered:
                                    notify_msg += f" → Workflow #{workflow_id} in progress!"
                                _bot_send("#general", notify_msg, from_id="@idea")
                        except Exception as e:
                            print(f"[{'IDEA' if is_idea else 'BRAINSTORM'}] Failed to send result: {e}")

        except Exception as e:
            print(f"[BRAINSTORM] Watchdog error: {e}")
            __import__('traceback').print_exc()

        time.sleep(BRAINSTORM_WATCHDOG_INTERVAL)


# =============================================================================
# v1.4 Workflow Scheduler: Watchdog Thread
# =============================================================================

# NOTE: MAX_TIMEOUT_NOTIFS imported from workflow_scheduler (single source of truth)
# Design: Auto-abort after N timeout notifications (no auto-transition to next phase)
# This gives users time to react while preventing infinite spam.

def workflow_watchdog():
    """Background thread to check workflow phase timeouts and send reminders.

    v1.4: Handles:
    - Reminder at 80% of phase timeout
    - Timeout notification when phase expires
    - Auto-abort after MAX_TIMEOUT_NOTIFS notifications (fixes spam bug)
    """
    global workflow_scheduler, transport
    print("[WORKFLOW] Watchdog started (v1.4 - auto-abort after 3 notifs)")

    while True:
        try:
            if workflow_scheduler:
                check = workflow_scheduler.check_timeout()

                if check:
                    action = check.get("action")
                    workflow_id = check.get("workflow_id")
                    phase = check.get("phase")

                    if action == "reminder":
                        remaining = check.get("remaining_minutes", 0)
                        msg = f"⏰ **WORKFLOW #{workflow_id}** - Phase `@{phase}`: {remaining}min remaining!"
                        print(f"[WORKFLOW] Reminder for #{workflow_id} phase {phase}")

                        if transport:
                            try:
                                ensure_room("#general")
                                _bot_send("#general", msg, from_id="@workflow")
                            except Exception as e:
                                print(f"[WORKFLOW] Failed to send reminder: {e}")

                    elif action == "timeout":
                        # v1.4: Increment counter and check for auto-abort
                        notif_count = workflow_scheduler.increment_timeout_notif(workflow_id)
                        elapsed = check.get("elapsed_minutes", 0)
                        timeout = check.get("timeout_minutes", 0)

                        if notif_count >= MAX_TIMEOUT_NOTIFS:
                            # Auto-abort after 3 notifications
                            result = workflow_scheduler.abort_workflow(
                                workflow_id,
                                f"Extended timeout phase {phase} - cleanup"
                            )
                            msg = f"🛑 **WORKFLOW** aborted: Extended timeout phase {phase} - cleanup"
                            print(f"[WORKFLOW] Auto-abort #{workflow_id} after {notif_count} timeout notifs")
                        else:
                            # Normal timeout notification
                            remaining_notifs = MAX_TIMEOUT_NOTIFS - notif_count
                            msg = f"⚠️ **WORKFLOW #{workflow_id}** - Phase `@{phase}` timed out! ({elapsed}/{timeout}min)\n"
                            msg += f"➡️ `@extend 10` to extend or `@next` to move on"
                            if remaining_notifs <= 1:
                                msg += f"\n⚠️ Auto-abort in {remaining_notifs} notification(s)!"
                            print(f"[WORKFLOW] Timeout #{notif_count}/{MAX_TIMEOUT_NOTIFS} for #{workflow_id} phase {phase}")

                        if transport:
                            try:
                                ensure_room("#general")
                                _bot_send("#general", msg, from_id="@workflow")
                            except Exception as e:
                                print(f"[WORKFLOW] Failed to send timeout: {e}")

        except Exception as e:
            print(f"[WORKFLOW] Watchdog error: {e}")

        time.sleep(WORKFLOW_WATCHDOG_INTERVAL)


# =============================================================================
# v1.5 Review System: Watchdog Thread
# =============================================================================

def review_watchdog():
    """Background thread to check review deadlines and send reminders.

    v1.5: Handles:
    - Reminder at 30min (REVIEW_REMINDER_SECONDS) — legacy DB-level
    - Auto-close at 1h (REVIEW_TIMEOUT_SECONDS) with timeout status
    - Consensus calculation (approved if min_approvals reached, else timeout)

    v2.0 P7: Aggressive ping system (pattern: brainstorm watchdog)
    - First ping after REVIEW_PING_DELAY (2 min)
    - Subsequent pings every REVIEW_PING_INTERVAL (2 min), max REVIEW_PING_MAX (3)
    - Escalation to #general after REVIEW_ESCALATE_SECONDS (5 min)
    - In-memory state: review_reminder_state dict (no DB migration needed)
    - Message reminds reviewers to use MCP command (not just chat)
    """
    global storage, transport
    print("[REVIEW] Watchdog started (v2.0 P7 — aggressive pings)")

    while True:
        try:
            if storage:
                now = time.time()

                # =============================================================
                # 1. P7: Aggressive ping system for pending reviews
                # =============================================================
                active_reviews = storage.get_active_review_requests()
                for review in active_reviews:
                    request_id = review.get("id")
                    file_path = review.get("file_path", "")
                    reviewers = review.get("reviewers", [])
                    if isinstance(reviewers, str):
                        try:
                            reviewers = json.loads(reviewers)
                        except (json.JSONDecodeError, TypeError):
                            reviewers = []
                    requested_by = review.get("requested_by", "")
                    created_at = review.get("created_at", "")

                    # Calculate review age in seconds
                    try:
                        from datetime import datetime, timezone
                        # Parse created_at (SQLite format: "YYYY-MM-DD HH:MM:SS" or ISO8601)
                        created_str = created_at.replace("T", " ").split(".")[0].replace("Z", "")
                        created_dt = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S")
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                        review_age = now - created_dt.timestamp()
                    except (ValueError, AttributeError):
                        review_age = 0  # Can't parse → skip pings

                    # Too young for first ping?
                    if review_age < REVIEW_PING_DELAY:
                        continue

                    # Get current responses to find non-voters
                    full_review = storage.get_review_request(request_id)
                    responses = full_review.get("responses", []) if full_review else []
                    voted_reviewers = {r.get("reviewer") for r in responses}

                    # P1 FIX (backward compat): if any reviewer posted changes_requested,
                    # auto-close the review. This catches zombie reviews from before the fix.
                    has_changes_requested = any(
                        r.get("vote") == "changes" for r in responses
                    )
                    if has_changes_requested:
                        changer = next(
                            (r.get("reviewer") for r in responses if r.get("vote") == "changes"),
                            "unknown"
                        )
                        storage.close_review_request(request_id, "changes_requested", "completed")
                        review_reminder_state.pop(request_id, None)
                        if transport:
                            try:
                                _bot_send(
                                    "#general",
                                    f"📋 **REVIEW #{request_id}** auto-closed (changes requested by {changer})",
                                    from_id="@review"
                                )
                            except Exception:
                                pass
                        print(f"[REVIEW] Auto-closed zombie review #{request_id} (changes_requested found)")
                        continue

                    non_voters = [r for r in reviewers if r not in voted_reviewers]

                    # Everyone voted → clean up state and skip
                    if not non_voters:
                        review_reminder_state.pop(request_id, None)
                        continue

                    # Get or init ping state for this review
                    state = review_reminder_state.get(request_id, {
                        "count": 0, "last_sent": 0, "escalated": False
                    })

                    # Max pings reached → stop spamming
                    if state["count"] >= REVIEW_PING_MAX:
                        continue

                    # Throttle: too soon since last ping?
                    elapsed_since_last = now - state["last_sent"]
                    if state["count"] > 0 and elapsed_since_last < REVIEW_PING_INTERVAL:
                        continue

                    # === SEND PING ===
                    ping_count = state["count"] + 1
                    tags = " ".join(non_voters)

                    # Escalation check: if review is older than REVIEW_ESCALATE_SECONDS
                    is_escalation = review_age >= REVIEW_ESCALATE_SECONDS and not state["escalated"]

                    if is_escalation:
                        msg = (
                            f"🚨 **REVIEW #{request_id}** — ESCALATION! "
                            f"{tags}: review pending for {int(review_age // 60)} min on `{file_path}`\n"
                            f"⚠️ Use `review/approve` or `review/changes` (not just chat!)"
                        )
                    else:
                        msg = (
                            f"🔔 **REVIEW #{request_id}** ({ping_count}/{REVIEW_PING_MAX}) — "
                            f"{tags} : review pending on `{file_path}`\n"
                            f"💡 Reminder: use MCP command `review/approve` or `review/changes`"
                        )

                    if transport:
                        try:
                            ensure_room("#general")
                            _bot_send("#general", msg, from_id="@review")
                            review_reminder_state[request_id] = {
                                "count": ping_count,
                                "last_sent": now,
                                "escalated": state["escalated"] or is_escalation
                            }
                            print(f"[REVIEW] P7 ping {ping_count}/{REVIEW_PING_MAX} for #{request_id} → {non_voters}"
                                  f"{' (ESCALATION)' if is_escalation else ''}")
                        except Exception as e:
                            print(f"[REVIEW] Failed to send P7 ping: {e}")

                    # Also mark legacy DB reminder (backward compat)
                    if ping_count == 1:
                        storage.mark_review_reminder_sent(request_id)

                # =============================================================
                # 2. Auto-close expired reviews (1h) — unchanged from v1.5
                # =============================================================
                expired_reviews = storage.get_expired_review_requests()
                for review in expired_reviews:
                    request_id = review.get("id")
                    file_path = review.get("file_path", "")
                    reviewers = review.get("reviewers", [])
                    if isinstance(reviewers, str):
                        try:
                            reviewers = json.loads(reviewers)
                        except (json.JSONDecodeError, TypeError):
                            reviewers = []
                    min_approvals = review.get("min_approvals", 1)
                    requested_by = review.get("requested_by", "")

                    # Get responses
                    full_review = storage.get_review_request(request_id)
                    responses = full_review.get("responses", []) if full_review else []

                    # Count approvals and changes_requested
                    approvals = sum(1 for r in responses if r.get("vote") == "approve")
                    changes_requested = sum(1 for r in responses if r.get("vote") == "changes")

                    # Determine consensus
                    if changes_requested > 0:
                        consensus = "changes_requested"
                    elif approvals >= min_approvals:
                        consensus = "approved"
                    else:
                        consensus = "timeout"

                    # Close review
                    storage.close_review_request(request_id, consensus, "completed")
                    # P7: Clean up ping state
                    review_reminder_state.pop(request_id, None)

                    # Build result message
                    emoji = "✅" if consensus == "approved" else "⚠️" if consensus == "changes_requested" else "⏰"
                    msg = f"{emoji} **REVIEW #{request_id}** - `{file_path}` → **{consensus.upper()}**\n"
                    msg += f"Votes: {approvals} approvals, {changes_requested} changes requested (min: {min_approvals})"

                    print(f"[REVIEW] #{request_id} closed: {consensus}")

                    if transport:
                        try:
                            ensure_room("#general")
                            _bot_send("#general", msg, from_id="@review")

                            # Notify requester
                            if requested_by:
                                notify_msg = f"📋 {requested_by} - Ta review #{request_id} est terminée: **{consensus}**"
                                _bot_send("#general", notify_msg, from_id="@review")
                        except Exception as e:
                            print(f"[REVIEW] Failed to send result: {e}")

                    # v4.0: Telegram notification
                    telegram_notify("review/closed", {
                        "request_id": request_id,
                        "consensus": consensus,
                        "file_path": file_path,
                    })

        except Exception as e:
            print(f"[REVIEW] Watchdog error: {e}")

        time.sleep(REVIEW_WATCHDOG_INTERVAL)


# =============================================================================
# v1.6 Tips Contextuels: Watchdog Thread
# =============================================================================

def tips_watchdog():
    """Background thread to broadcast contextual tips to agents.

    v1.6: Two modes:
    - Contextual: Tip based on current workflow phase (immediate on phase change)
    - General: Random tip from rotation every TIPS_BROADCAST_INTERVAL seconds
    """
    global tip_system, transport, workflow_scheduler
    print("[TIPS] Watchdog started (v1.6)")

    while True:
        try:
            if tip_system and transport:
                # Get current workflow phase for contextual tips
                current_phase = None
                if workflow_scheduler:
                    try:
                        wf_status = workflow_scheduler.get_active_workflow()
                        if wf_status:
                            current_phase = wf_status.get("phase")
                    except Exception:
                        pass

                tip_system.broadcast(transport, workflow_phase=current_phase)

        except Exception as e:
            print(f"[TIPS] Watchdog error: {e}")

        time.sleep(TIPS_WATCHDOG_INTERVAL)


# =============================================================================
# v3.0 Memory Retention — cleanup messages older than 30 days
# =============================================================================

def memory_retention_loop():
    """Cleanup messages older than 30 days, runs daily"""
    while True:
        time.sleep(86400)  # 24h
        try:
            deleted = storage.cleanup_old_messages(days=30)
            if deleted > 0:
                print(f"[RETENTION] Cleaned up {deleted} messages older than 30 days")
        except Exception as e:
            print(f"[RETENTION] Error: {e}")


# =============================================================================

ALLOWED_ORIGINS = {
    "https://aircp.dev",
    "https://www.aircp.dev",
    "http://localhost:4321",      # Astro dev server
    "http://localhost:3000",      # Dashboard dev
    "http://localhost:5173",      # Vite dev server (Svelte dashboard)
}


def _env_flag_enabled(name: str) -> bool:
    raw = os.environ.get(name, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _split_tokens(raw: str) -> set[str]:
    tokens = set()
    for part in (raw or "").replace("\n", ",").split(","):
        token = part.strip()
        if token:
            tokens.add(token)
    return tokens


def _load_tokens_file(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception as e:
        raise ValueError(f"Cannot read tokens file '{path}': {e}") from e

    if not content:
        return set()

    # JSON formats:
    # 1) {"tokens":[{"token":"..."}, ...]}
    # 2) {"tokens":["...", "..."]}
    # 3) ["...", "..."]
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and isinstance(parsed.get("tokens"), list):
            tokens = set()
            for item in parsed["tokens"]:
                if isinstance(item, dict):
                    token = str(item.get("token", "")).strip()
                else:
                    token = str(item).strip()
                if token:
                    tokens.add(token)
            return tokens
        if isinstance(parsed, list):
            return {str(item).strip() for item in parsed if str(item).strip()}
    except Exception:
        pass

    # Plain text fallback: one token per line or comma-separated.
    return _split_tokens(content)


def _configure_http_auth(args):
    global HTTP_AUTH_TOKENS, HTTP_ALLOW_NO_AUTH

    tokens = set(args.auth_token or [])
    tokens |= _split_tokens(os.environ.get("AIRCP_AUTH_TOKEN", ""))
    tokens |= _split_tokens(os.environ.get("AIRCP_AUTH_TOKENS", ""))

    token_file = args.tokens_file or os.environ.get("AIRCP_AUTH_TOKENS_FILE")
    if token_file:
        tokens |= _load_tokens_file(token_file)

    HTTP_AUTH_TOKENS = {t for t in tokens if t}
    HTTP_ALLOW_NO_AUTH = bool(args.allow_no_auth) or _env_flag_enabled("AIRCP_ALLOW_NO_AUTH")

    if not HTTP_AUTH_TOKENS and not HTTP_ALLOW_NO_AUTH:
        raise RuntimeError(
            "Refusing to start AIRCP daemon without HTTP auth token. "
            "Set --auth-token (or AIRCP_AUTH_TOKEN). "
            "For local dev only, pass --allow-no-auth or AIRCP_ALLOW_NO_AUTH=1."
        )

    if HTTP_AUTH_TOKENS:
        print(f"HTTP auth enabled ({len(HTTP_AUTH_TOKENS)} token(s))")
    else:
        print("WARNING: HTTP auth disabled via explicit override (local dev mode)")


def _is_path_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_project(body: dict, agent_id: str = None) -> str:
    """Resolve project_id from request body or agent's active project.
    Priority: 1. explicit in body, 2. agent's active project, 3. 'default'"""
    explicit = body.get("project_id") or body.get("project")
    if explicit:
        return explicit
    if agent_id and storage:
        return storage.get_agent_active_project(agent_id)
    return "default"


def _bot_send(room: str, content: str, from_id: str = None,
              context_agent: str = None, project: str = None,
              payload_extra: dict = None):
    """Send a system bot message with proper project tagging.

    Project resolution order:
    1. Explicit project parameter
    2. context_agent's active project (from storage)
    3. 'default' (global/system message — visible in all project views)
    """
    if not project:
        if context_agent and storage:
            project = storage.get_agent_active_project(context_agent)
        else:
            project = "default"
    transport.send_chat(room, content, from_id=from_id, project=project,
                        payload_extra=payload_extra)


# =================================================================
# E4 Quota Hooks (noop — enterprise placeholder)
# =================================================================
_license = None  # Set at startup if enterprise

def _check_quota(path: str, method: str) -> bool:
    """Enterprise: check quotas. Community: always True."""
    if not _license or not getattr(_license, 'is_enterprise', False):
        return True
    return True  # Enterprise implementation later


class AircpHandler(BaseHTTPRequestHandler):
    PUBLIC_GET_PATHS = {"/", "/dashboard.html", "/status", "/health"}

    def log_message(self, format, *args):
        print(f"[{self.client_address[0]}] {args[0]}")

    def _cors_origin(self) -> str | None:
        """Return Origin if it's in the whitelist, else None."""
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            return origin
        return None

    def _send_cors_headers(self):
        """Send CORS headers if origin is allowed."""
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _extract_bearer_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer "):].strip()
        return token or None

    def _is_authorized(self) -> bool:
        # In explicit no-auth mode, allow all requests (local dev only).
        if not HTTP_AUTH_TOKENS:
            return HTTP_ALLOW_NO_AUTH
        token = self._extract_bearer_token()
        return token in HTTP_AUTH_TOKENS

    def _require_auth(self) -> bool:
        if self._is_authorized():
            return True
        self.send_json({"error": "Unauthorized"}, 401)
        return False

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    # =================================================================
    # HTTP Router: dict-based dispatch (Phase 2 refactor)
    # =================================================================
    GET_ROUTES = {
        "/status":            "_get_status",
        "/health":            "_get_health",
        "/projects":          "_get_projects",
        "/agent/project":     "_get_agent_project",
        "/files":             "_get_files",
        "/history":           "_get_history",
        "/rooms":             "_get_rooms",
        "/":                  "_get_dashboard",
        "/dashboard.html":    "_get_dashboard",
        "/presence":          "_get_presence",
        "/claims":            "_get_claims",
        "/locks":             "_get_locks",
        "/mute-status":       "_get_mute_status",
        "/spam-stats":        "_get_spam_stats",
        "/mode":              "_get_mode",
        "/mode/history":      "_get_mode_history",
        "/tasks":             "_get_tasks",
        "/agents/presence":   "_get_agents_presence",
        "/brainstorm/active": "_get_brainstorm_active",
        "/brainstorm/history":"_get_brainstorm_history",
        "/brainstorm/config": "_get_brainstorm_config",
        "/review/list":       "_get_review_list",
        "/review/history":    "_get_review_history",
        "/tips":              "_get_tips",
        "/tips/all":          "_get_tips_all",
        "/workflow":          "_get_workflow",
        "/workflow/history":  "_get_workflow_history",
        "/workflow/config":   "_get_workflow_config",
        "/workflow/chunks":   "_get_workflow_chunks",
        "/agents/activity":   "_get_agents_activity",
        "/daemon/can-restart":"_get_daemon_can_restart",
        "/compact/status":    "_get_compact_status",
        "/retention/status":  "_get_retention_status",
        "/memory/search":     "_get_memory_search",
        "/memory/get":        "_get_memory_get",
        "/memory/stats":      "_get_memory_stats",
        "/usage":             "_get_usage",
        "/usage/timeline":    "_get_usage_timeline",
        "/notifications/stats": "_get_notifications_stats",
    }
    GET_PREFIX_ROUTES = [
        ("/review/status/", "_get_review_by_id"),
        ("/projects/",      "_get_project_by_id"),
        ("/brainstorm/",    "_get_brainstorm_by_id"),
        ("/progress/",      "_get_progress_by_agent"),
        ("/uploads/",       "_get_upload_file"),
    ]

    def _fix_room_param(self, params, parsed):
        """Fix '#' in room param eaten by URL fragment parser."""
        if "room" not in params and parsed.fragment and "room=" in parsed.query:
            params["room"] = [f"#{parsed.fragment}"]
        elif "room" in params and params["room"][0] and not params["room"][0].startswith("#"):
            params["room"] = [f"#{params['room'][0]}"]

    def _infer_history_read(self, params):
        """v2.0: Passive inference for GET /history (agent is reading)."""
        try:
            agent_id = self.headers.get("X-Agent-ID") or getattr(transport, 'agent_id', None)
            if agent_id:
                room = params.get("room", ["#general"])[0]
                storage.update_inferred_activity(agent_id, "reading", f"room: {room}")
        except Exception:
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path not in self.PUBLIC_GET_PATHS:
            if not self._require_auth():
                return

        self._fix_room_param(params, parsed)

        if parsed.path == "/history":
            self._infer_history_read(params)

        if not _check_quota(parsed.path, "GET"):
            self.send_json({"error": "Quota exceeded"}, 429)
            return

        handler_name = self.GET_ROUTES.get(parsed.path)
        if handler_name:
            getattr(self, handler_name)(parsed, params)
            return

        for prefix, handler_name in self.GET_PREFIX_ROUTES:
            if parsed.path.startswith(prefix):
                getattr(self, handler_name)(parsed, params)
                return

        self.send_json({"error": "Not found"}, 404)

    # ----- GET handlers -----

    def _get_status(self, parsed, params):
        self.send_json({
            "status": "ok",
            "agent_id": transport.agent_id,
            "rooms": list(joined_rooms),
            "version": "3.1.0"
        })

    def _get_health(self, parsed, params):
        """v4.4: Health check endpoint for monitoring/load balancers.
        Returns 200 if healthy, 503 if any critical check fails.
        Public (no auth), read-only checks only."""
        import os
        t0 = time.time()

        # --- Storage check (read-only SELECT) ---
        storage_ok = False
        storage_latency = 0.0
        try:
            st = time.time()
            with storage._conn_lock:
                storage._get_conn().execute("SELECT 1").fetchone()
            storage_latency = round((time.time() - st) * 1000, 2)
            storage_ok = True
        except Exception:
            pass

        # --- Transport check (+ latency probe) ---
        transport_ok = False
        transport_rooms = 0
        transport_latency = 0.0
        try:
            tt = time.time()
            transport_ok = (
                transport is not None
                and transport.participant is not None
            )
            if transport_ok and hasattr(transport, 'ping'):
                transport.ping()
            transport_latency = round((time.time() - tt) * 1000, 2)
            transport_rooms = len(joined_rooms)
        except Exception:
            pass

        # --- Watchdog threads (individual flags) ---
        watchdogs = {}
        for name, thread in _watchdog_threads.items():
            watchdogs[name] = thread.is_alive() if thread else False

        # --- Agents online (from presence, read-only) ---
        agents_online = 0
        try:
            presence = storage.get_all_agent_presence()
            agents_online = sum(
                1 for p in presence
                if p.get("status") == "online"
            )
        except Exception:
            pass

        # --- Uptime ---
        uptime_secs = 0
        if _daemon_start_time:
            uptime_secs = round(time.time() - _daemon_start_time)

        # --- Overall health: storage + transport must be OK ---
        healthy = storage_ok and transport_ok

        response_time_ms = round((time.time() - t0) * 1000, 2)
        from datetime import datetime as _dt, timezone as _tz
        ts = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        result = {
            "healthy": healthy,
            "uptime_secs": uptime_secs,
            "version": "3.1.0",
            "pid": os.getpid(),
            "response_time_ms": response_time_ms,
            "checks": {
                "storage": {
                    "ok": storage_ok,
                    "latency_ms": storage_latency,
                    "checked_at": ts,
                },
                "transport": {
                    "ok": transport_ok,
                    "rooms": transport_rooms,
                    "latency_ms": transport_latency,
                    "checked_at": ts,
                },
                "watchdogs": watchdogs,
                "agents_online": agents_online,
            },
            "timestamp": ts,
        }

        status_code = 200 if healthy else 503
        self.send_json(result, status_code)

    def _get_projects(self, parsed, params):
        projects = storage.get_all_projects()
        self.send_json({"projects": projects})

    def _get_project_by_id(self, parsed, params):
        parts = parsed.path.split("/")
        if len(parts) != 3:
            self.send_json({"error": "Not found"}, 404)
            return
        project_id = parts[2]
        project = storage.get_project(project_id)
        if project:
            agents = storage.get_agents_in_project(project_id)
            project["agents"] = agents
            self.send_json({"project": project})
        else:
            self.send_json({"error": f"Project not found: {project_id}"}, 404)

    def _get_agent_project(self, parsed, params):
        agent_id = params.get("agent_id", [None])[0]
        if not agent_id:
            self.send_json({"error": "Missing agent_id"}, 400)
        else:
            project_id = storage.get_agent_active_project(agent_id)
            self.send_json({"agent_id": agent_id, "project_id": project_id})

    def _get_files(self, parsed, params):
        import pathlib
        raw_path = params.get("path", ["/projects/aircp"])[0]
        base = pathlib.Path(raw_path).resolve()
        sandbox = pathlib.Path("/projects").resolve()
        if not _is_path_within(base, sandbox):
            self.send_json({"error": "Path outside sandbox"}, 403)
        elif not base.exists():
            self.send_json({"error": "Path not found"}, 404)
        elif base.is_file():
            self.send_json({"path": str(base), "type": "file"})
        else:
            entries = []
            try:
                for entry in sorted(base.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
                    name = entry.name
                    if name.startswith(".") or name == "__pycache__" or name == "node_modules":
                        continue
                    entries.append({
                        "name": name,
                        "path": str(entry),
                        "type": "dir" if entry.is_dir() else "file",
                    })
            except PermissionError:
                pass
            self.send_json({"path": str(base), "entries": entries})

    def _get_history(self, parsed, params):
        room = params.get("room", ["#general"])[0]
        limit = int(params.get("limit", [20])[0])
        project_filter = params.get("project", [None])[0]

        all_msgs = list(message_history)
        all_msgs.extend(load_alpha_memory(room))

        if project_filter and project_filter != "default":
            all_msgs = [m for m in all_msgs if m.get("project", "default") in (project_filter, "default", "")]

        seen = set()
        unique = []
        for m in all_msgs:
            ts_key = m.get("timestamp", 0) // 1_000_000_000
            key = (m.get("from", ""), m.get("content", "")[:50], ts_key)
            if key not in seen:
                seen.add(key)
                unique.append(m)
        unique.sort(key=lambda x: x.get("timestamp", 0))

        room_msgs = [m for m in unique if m["room"] == room][-limit:]
        self.send_json({"room": room, "count": len(room_msgs), "messages": room_msgs})

    def _get_rooms(self, parsed, params):
        rooms_list = [
            {"name": room, "type": "reserved" if room in RESERVED_CHANNELS else "user"}
            for room in sorted(joined_rooms)
        ]
        self.send_json({"rooms": rooms_list, "count": len(rooms_list)})

    def _get_dashboard(self, parsed, params):
        try:
            with open("/projects/aircp/dashboard.html", "r") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content.encode())
        except FileNotFoundError:
            self.send_json({"error": "dashboard.html not found"}, 404)

    def _get_presence(self, parsed, params):
        agent = params.get("agent", [None])[0]
        if agent:
            result = autonomy.presence_query(agent)
        else:
            result = autonomy.presence_query()
        self.send_json(result)

    def _get_claims(self, parsed, params):
        resource = params.get("resource", [None])[0]
        result = autonomy.claim_query(resource)
        self.send_json(result)

    def _get_locks(self, parsed, params):
        path = params.get("path", [None])[0]
        result = autonomy.lock_query(path)
        self.send_json(result)

    def _get_mute_status(self, parsed, params):
        self.send_json({
            "muted": autonomy.is_muted(),
            "remaining_seconds": autonomy.mute_remaining_seconds()
        })

    def _get_spam_stats(self, parsed, params):
        self.send_json(autonomy.get_spam_stats())

    def _get_mode(self, parsed, params):
        mode_state = autonomy.get_mode_state()
        self.send_json({
            "mode": mode_state.mode if mode_state else "neutral",
            "lead": mode_state.lead if mode_state else "",
            "started_at": mode_state.started_at.isoformat() if mode_state and mode_state.started_at else None,
            "timeout_at": mode_state.timeout_at.isoformat() if mode_state and mode_state.timeout_at else None,
            "time_remaining": str(mode_state.time_remaining()) if mode_state and mode_state.time_remaining() else None,
            "pending_asks": mode_state.pending_asks if mode_state else []
        })

    def _get_mode_history(self, parsed, params):
        try:
            limit = int(params.get("limit", [10])[0])
        except (ValueError, IndexError):
            self.send_json({"error": "Invalid limit parameter"}, 400)
            return
        history = autonomy.get_mode_history(limit)
        self.send_json({"history": history, "count": len(history)})

    def _get_tasks(self, parsed, params):
        agent = params.get("agent", [None])[0]
        status_filter = params.get("status", [None])[0]
        project_filter = params.get("project", [None])[0]

        if status_filter == "active":
            status_filter = "in_progress"

        if agent:
            tasks = storage.get_agent_tasks(agent, status_filter, project_id=project_filter)
        elif status_filter:
            tasks = storage.get_tasks_by_status(status_filter)
            if project_filter:
                tasks = [t for t in tasks if t.get("project_id") == project_filter]
        else:
            tasks = storage.get_active_tasks(project_id=project_filter)

        # v4.7: Normalize SQLite timestamps to ISO8601+Z (fixes "1h ago" TZ bug)
        tasks = self._normalize_timestamps(tasks)
        self.send_json({"tasks": tasks, "count": len(tasks)})

    def _get_agents_presence(self, parsed, params):
        agent = params.get("agent", [None])[0]
        if agent:
            dead_s = get_agent_dead_seconds(agent)
            away_s = get_agent_away_seconds(agent)
            state = storage.get_agent_state(agent, offline_threshold_override=dead_s)
            if state and state.get("status") != "unknown":
                presence = storage.get_agent_presence(agent)
                seconds_ago = storage._seconds_since(presence.get("last_seen", "")) if presence else float('inf')
                if seconds_ago < away_s:
                    state["health"] = "online"
                elif seconds_ago < dead_s:
                    state["health"] = "away"
                else:
                    state["health"] = "dead"
                state["seconds_since_heartbeat"] = int(seconds_ago) if seconds_ago != float('inf') else 9999
                state["agent_id"] = agent
                if state["health"] == "dead":
                    state["status"] = "offline"
                elif state["health"] == "away":
                    state["status"] = "away"
                self.send_json(state)
            else:
                self.send_json({"error": "Agent not found"}, 404)
        else:
            all_presence = storage.get_all_agent_presence()
            enriched = []
            for p in all_presence:
                agent_id = p.get("agent_id", "")
                if not agent_id.startswith("@"):
                    continue
                dead_s = get_agent_dead_seconds(agent_id)
                away_s = get_agent_away_seconds(agent_id)
                state = storage.get_agent_state(agent_id, offline_threshold_override=dead_s) if agent_id else None
                seconds_ago = storage._seconds_since(p.get("last_seen", ""))
                if state:
                    if seconds_ago < away_s:
                        state["health"] = "online"
                    elif seconds_ago < dead_s:
                        state["health"] = "away"
                    else:
                        state["health"] = "dead"
                    state["seconds_since_heartbeat"] = int(seconds_ago)
                    state["agent_id"] = agent_id
                    if state["health"] == "dead":
                        state["status"] = "offline"
                    elif state["health"] == "away":
                        state["status"] = "away"
                    enriched.append(state)
                else:
                    p["status"] = "idle"
                    p["health"] = "dead" if seconds_ago > dead_s else ("away" if seconds_ago > away_s else "online")
                    p["seconds_since_heartbeat"] = int(seconds_ago)
                    enriched.append(p)
            self.send_json({"agents": enriched, "count": len(enriched)})

    def _get_brainstorm_active(self, parsed, params):
        sessions = storage.get_active_brainstorm_sessions()
        self.send_json({"sessions": sessions, "count": len(sessions)})

    def _get_brainstorm_history(self, parsed, params):
        limit = int(params.get("limit", [20])[0])
        sessions = storage.get_brainstorm_history(limit)
        self.send_json({"sessions": sessions, "count": len(sessions)})

    def _get_brainstorm_config(self, parsed, params):
        config = get_brainstorm_config()
        self.send_json(config)

    def _get_brainstorm_by_id(self, parsed, params):
        try:
            session_id = int(parsed.path.split("/")[-1])
            session = storage.get_brainstorm_session(session_id)
            if session:
                self.send_json(session)
            else:
                self.send_json({"error": "Session not found"}, 404)
        except ValueError:
            self.send_json({"error": "Invalid session ID"}, 400)

    def _normalize_timestamps(self, items):
        """v4.7: Convert SQLite timestamps to ISO8601+Z for browser UTC parsing."""
        _TS_FIELDS = ("created_at", "updated_at", "last_activity",
                       "claimed_at", "completed_at", "last_pinged_at",
                       "deadline_at", "closed_at")
        normalized = []
        for item in items:
            item2 = dict(item)
            for f in _TS_FIELDS:
                if item2.get(f):
                    item2[f] = _sqlite_to_iso8601(item2[f])
            normalized.append(item2)
        return normalized

    def _get_review_list(self, parsed, params):
        status_filter = params.get("status", ["pending"])[0]
        if status_filter == "pending":
            reviews = storage.get_active_review_requests()
        else:
            reviews = storage.get_review_history(limit=50)
        reviews = self._normalize_timestamps(reviews)
        self.send_json({"reviews": reviews, "count": len(reviews)})

    def _get_review_history(self, parsed, params):
        limit = int(params.get("limit", [20])[0])
        reviews = storage.get_review_history(limit)
        reviews = self._normalize_timestamps(reviews)
        self.send_json({"reviews": reviews, "count": len(reviews)})

    def _get_review_by_id(self, parsed, params):
        try:
            request_id = int(parsed.path.split("/")[-1])
            review = storage.get_review_request(request_id)
            if review:
                responses = review.get("responses", [])
                approvals = sum(1 for r in responses if r.get("vote") == "approve")
                changes = sum(1 for r in responses if r.get("vote") == "changes")
                comments = sum(1 for r in responses if r.get("vote") == "comment")
                review["summary"] = {
                    "approvals": approvals,
                    "changes_requested": changes,
                    "comments": comments,
                    "total_responses": len(responses),
                    "reviewers_pending": [r for r in review.get("reviewers", [])
                                           if r not in {resp.get("reviewer") for resp in responses}]
                }
                self.send_json(review)
            else:
                self.send_json({"error": "Review not found"}, 404)
        except ValueError:
            self.send_json({"error": "Invalid review ID"}, 400)

    def _get_tips(self, parsed, params):
        if tip_system:
            current = tip_system.get_current_tip()
            limit = int(params.get("limit", [10])[0])
            history = tip_system.get_history(limit)
            self.send_json({
                "current": current,
                "history": history,
                "enabled": tip_system.enabled,
                "interval_seconds": tip_system.interval,
                "total_shown": len(tip_system.tip_history)
            })
        else:
            self.send_json({"error": "Tips system not initialized"}, 503)

    def _get_tips_all(self, parsed, params):
        self.send_json({
            "general": GENERAL_TIPS,
            "contextual": CONTEXTUAL_TIPS,
            "general_count": len(GENERAL_TIPS),
            "contextual_count": len(CONTEXTUAL_TIPS)
        })

    def _get_progress_by_agent(self, parsed, params):
        agent_id = parsed.path.split("/")[-1]
        if not agent_id.startswith("@"):
            agent_id = f"@{agent_id}"

        state = storage.get_agent_state(agent_id)
        status = state.get("status", "unknown")
        task = state.get("task")

        if status == "unknown":
            message = f"? {agent_id} has never been seen (no heartbeat)"
        elif status == "offline":
            message = f"X {agent_id} appears offline (last heartbeat: {state.get('last_activity_human', 'unknown')})"
        elif status == "stale":
            task_desc = task.get("description", "")[:50] if task else ""
            pings = state.get("watchdog", {}).get("pings", 0)
            message = f"! {agent_id} appears stuck on task #{task.get('id')} ({task_desc}...) -- {pings}/3 pings, last activity: {state.get('last_activity_human')}"
        elif status == "idle":
            message = f"- {agent_id} is idle (no active task) -- last activity: {state.get('last_activity_human')}"
        elif status == "working":
            if task:
                step = task.get("step", 0)
                task_desc = task.get("description", "")[:50]
                message = f"~ {agent_id} working on task #{task.get('id')} ({task_desc}...) -- step: {step}"
            else:
                message = f"~ {agent_id} is active (no tracked task)"
        else:
            message = f"? {agent_id} -- status: {status}"

        state["message"] = message
        self.send_json(state)

    def _get_workflow(self, parsed, params):
        if workflow_scheduler:
            status = workflow_scheduler.get_workflow_status()
            self.send_json(status)
        else:
            self.send_json({"error": "Workflow scheduler not initialized"}, 500)

    def _get_workflow_history(self, parsed, params):
        limit = int(params.get("limit", [20])[0])
        if workflow_scheduler:
            history = workflow_scheduler.get_history(limit)
            self.send_json({"history": history, "count": len(history)})
        else:
            self.send_json({"error": "Workflow scheduler not initialized"}, 500)

    def _get_workflow_config(self, parsed, params):
        if workflow_scheduler:
            config = workflow_scheduler.get_config()
            self.send_json({"phases": WORKFLOW_PHASES, "config": config})
        else:
            self.send_json({"error": "Workflow scheduler not initialized"}, 500)

    def _get_agents_activity(self, parsed, params):
        agents = storage.get_all_agent_activity()
        active_count = sum(1 for a in agents if a.get("activity") not in ("idle", "away"))
        idle_count = sum(1 for a in agents if a.get("activity") == "idle")
        away_count = sum(1 for a in agents if a.get("activity") == "away")
        self.send_json({
            "agents": agents,
            "count": len(agents),
            "summary": {"active": active_count, "idle": idle_count, "away": away_count}
        })

    def _get_daemon_can_restart(self, parsed, params):
        check = storage.can_safely_restart()
        if workflow_scheduler:
            wf = workflow_scheduler.get_active_workflow()
            if wf:
                check["safe"] = False
                check["blockers"].append({
                    "type": "active_workflow",
                    "phase": wf.get("current_phase", "unknown"),
                    "feature": wf.get("feature", "")
                })
                check["reason"] = (check.get("reason", "") +
                    f"; Workflow active (phase: {wf.get('current_phase', '?')})")
        status_code = 200 if check["safe"] else 409
        self.send_json(check, status_code)

    def _get_compact_status(self, parsed, params):
        room = params.get("room", ["#general"])[0]
        counter = _compact_msg_counter.get(room, 0)
        last_compact = _last_compact_time.get(room, 0)
        db_stats = storage.get_compaction_stats(room) if storage else {}
        self.send_json({
            "room": room,
            "active_messages": db_stats.get("active_messages", 0),
            "pending_gc": db_stats.get("pending_gc", 0),
            "summaries": db_stats.get("summaries", 0),
            "msgs_since_last_compact": counter,
            "auto_threshold": COMPACT_AUTO_THRESHOLD,
            "last_compact_time": last_compact,
            "seconds_since_compact": int(time.time() - last_compact) if last_compact else None,
            "recent_compactions": db_stats.get("recent_compactions", []),
            "profiles_available": list(PROFILES.keys()),
            "agent_profile_map": AGENT_PROFILE_MAP,
        })

    def _get_retention_status(self, parsed, params):
        stats = storage.get_compaction_stats() if storage else {}
        self.send_json({
            "retention_days": 7,
            "gc_interval_hours": 6,
            "active_messages": stats.get("active_messages", 0),
            "pending_gc": stats.get("pending_gc", 0),
            "summaries": stats.get("summaries", 0),
            "total_in_db": stats.get("total", 0),
        })

    def _get_usage(self, parsed, params):
        agent_id = params.get("agent_id", [None])[0]
        minutes = params.get("minutes", [None])[0]
        minutes = int(minutes) if minutes else None
        group_by = params.get("group_by", ["agent"])[0]
        stats = storage.get_llm_usage_stats(
            agent_id=agent_id, minutes=minutes, group_by=group_by
        )
        self.send_json({"stats": stats})

    def _get_usage_timeline(self, parsed, params):
        agent_id = params.get("agent_id", [None])[0]
        minutes = int(params.get("minutes", ["60"])[0])
        bucket = int(params.get("bucket", ["1"])[0])
        timeline = storage.get_llm_usage_timeline(
            agent_id=agent_id, minutes=minutes, bucket_minutes=bucket
        )
        self.send_json({"timeline": timeline})

    def _get_memory_search(self, parsed, params):
        q = params.get("q", [""])[0]
        if not q:
            self.send_json({"error": "Missing 'q' param"}, 400)
            return
        room = params.get("room", [None])[0]
        agent = params.get("agent", [None])[0]
        day = params.get("day", [None])[0]
        limit = int(params.get("limit", ["50"])[0])
        results = storage.search_messages(q, room=room, agent=agent, day=day, limit=min(limit, 200))
        self.send_json({"query": q, "count": len(results), "results": results})

    def _get_memory_get(self, parsed, params):
        msg_id = params.get("id", [None])[0]
        if msg_id:
            msg = storage.get_message_by_id(msg_id)
            self.send_json(msg or {"error": "Not found"}, 200 if msg else 404)
        else:
            day = params.get("day", [None])[0]
            hour_str = params.get("hour", [None])[0]
            hour = int(hour_str) if hour_str else None
            room = params.get("room", [None])[0]
            agent = params.get("agent", [None])[0]
            limit = int(params.get("limit", ["100"])[0])
            results = storage.get_messages_by_date(day=day, hour=hour, room=room, agent=agent, limit=min(limit, 500))
            self.send_json({"count": len(results), "messages": results})

    def _get_memory_stats(self, parsed, params):
        stats = storage.get_stats()
        self.send_json(stats)

    # ----- Notification endpoints (Telegram bridge) -----

    def _get_notifications_stats(self, parsed, params):
        """GET /notifications/stats — Telegram notifier statistics."""
        try:
            notifier = TelegramNotifier()
            self.send_json(notifier.get_stats())
        except Exception as e:
            self.send_json({"error": str(e), "enabled": False})

    def _post_notifications_test(self, body):
        """POST /notifications/test — Send a test Telegram notification."""
        try:
            notifier = TelegramNotifier()
            if not notifier.enabled or not notifier.bot_token:
                self.send_json({"success": False, "error": "Telegram not configured"}, 503)
                return
            ok = notifier.test()
            self.send_json({"success": ok})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)

    def _post_notifications_fire(self, body):
        """POST /notifications/fire — Webhook for external services (forum) to fire notifications.

        Body: {"event": "trust/drop", "data": {"agent_id": "@x", ...}}
        Allowed events: trust/drop, agent/registered, moderation/reject
        """
        event = body.get("event", "")
        data = body.get("data", {})
        allowed_events = {
            "trust/drop", "agent/registered", "moderation/reject",
            "review/approved", "review/changes", "review/closed",
            "workflow/phase", "workflow/complete",
            "task/stale", "agent/dead",
        }
        if not event or event not in allowed_events:
            self.send_json({
                "error": f"Invalid event: {event}",
                "allowed": sorted(allowed_events),
            }, 400)
            return
        if not isinstance(data, dict):
            self.send_json({"error": "data must be a dict"}, 400)
            return
        telegram_notify(event, data)
        self.send_json({"success": True, "event": event, "queued": True})

    # =================================================================
    # v2.0 Passive Observability: Inferred activity mapping
    # =================================================================
    # Maps API path → (inferred_activity, context_body_key)
    _ACTIVITY_MAP = {
        "/send":             ("chatting", None),
        "/task/claim":       ("coding", "resource"),
        "/task/create":      ("coding", "description"),
        "/task/activity":    ("coding", "task_id"),
        "/review/request":   ("reviewing", "file"),
        "/review/approve":   ("reviewing", "request_id"),
        "/review/comment":   ("reviewing", "request_id"),
        "/review/changes":   ("reviewing", "request_id"),
        "/review/close":     ("reviewing", "request_id"),
        "/brainstorm/vote":  ("brainstorming", "session_id"),
        "/brainstorm/create":("brainstorming", "topic"),
        "/workflow/start":   ("coding", "feature"),
        "/workflow/next":    ("coding", None),
    }

    def _infer_activity(self, path: str, body: dict):
        """Hook: passively infer agent activity from API call."""
        try:
            mapping = self._ACTIVITY_MAP.get(path)
            if not mapping:
                return

            activity, context_key = mapping
            # Extract agent_id from body (various field names)
            agent_id = (body.get("from") or body.get("agent_id") or
                        body.get("requested_by") or body.get("created_by") or
                        getattr(transport, 'agent_id', None))
            if not agent_id:
                return

            # Build context string
            context = ""
            if context_key and context_key in body:
                val = body[context_key]
                context = f"{context_key}: {str(val)[:60]}"

            storage.update_inferred_activity(agent_id, activity, context)
        except Exception:
            pass  # Never block request on inference failure

    MAX_BODY_SIZE = 64 * 1024  # 64 KB — reject oversized payloads (OOM protection)

    POST_ROUTES = {
        "/projects":         "_post_projects",
        "/projects/delete":  "_post_projects_delete",
        "/agent/project":    "_post_agent_project",
        "/send":             "_post_send",
        "/claim":            "_post_claim",
        "/lock":             "_post_lock",
        "/heartbeat":        "_post_heartbeat",
        "/activity":         "_post_activity",
        "/stfu":             "_post_stfu",
        "/talk":             "_post_talk",
        "/reset-leader":     "_post_reset_leader",
        "/leader-mode":      "_post_leader_mode",
        "/mode":             "_post_mode",
        "/ask":              "_post_ask",
        "/stop":             "_post_stop",
        "/handover":         "_post_handover",
        "/task":             "_post_task",
        "/task/claim":       "_post_task_claim",
        "/task/activity":    "_post_task_activity",
        "/task/complete":    "_post_task_complete",
        "/agent/heartbeat":  "_post_agent_heartbeat",
        "/brainstorm/create":"_post_brainstorm_create",
        "/brainstorm/vote":  "_post_brainstorm_vote",
        "/idea":             "_post_idea",
        "/review/request":   "_post_review_request",
        "/review/approve":   "_post_review_approve",
        "/review/comment":   "_post_review_comment",
        "/review/changes":   "_post_review_changes",
        "/review/close":     "_post_review_close",
        "/workflow/start":   "_post_workflow_start",
        "/workflow/next":    "_post_workflow_next",
        "/workflow/extend":  "_post_workflow_extend",
        "/workflow/skip":    "_post_workflow_skip",
        "/workflow/abort":   "_post_workflow_abort",
        "/workflow/decompose": "_post_workflow_decompose",
        "/workflow/chunk/done": "_post_workflow_chunk_done",
        "/compact":          "_post_compact",
        "/retention/gc":     "_post_retention_gc",
        "/daemon/restart":   "_post_daemon_restart",
        "/upload":           "_post_upload",
        "/usage/report":     "_post_usage_report",
        "/notifications/test": "_post_notifications_test",
        "/notifications/fire": "_post_notifications_fire",
    }

    def _parse_body(self):
        """Parse JSON body from POST request. Returns None on error (response already sent)."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > self.MAX_BODY_SIZE:
                self.send_json({"error": f"Payload too large ({length} bytes, max {self.MAX_BODY_SIZE})"}, 413)
                return None
            return json.loads(self.rfile.read(length)) if length > 0 else {}
        except Exception as e:
            self.send_json({"error": f"Invalid JSON: {e}"}, 400)
            return None

    def do_POST(self):
        if not self._require_auth():
            return

        # Upload route needs special handling (larger body, multipart)
        if self.path == "/upload":
            if not _check_quota(self.path, "POST"):
                self.send_json({"error": "Quota exceeded"}, 429)
                return
            self._post_upload_dispatch()
            return

        body = self._parse_body()
        if body is None:
            return

        # v2.0: Passive activity inference hook (fire-and-forget)
        self._infer_activity(self.path, body)

        if not _check_quota(self.path, "POST"):
            self.send_json({"error": "Quota exceeded"}, 429)
            return

        handler_name = self.POST_ROUTES.get(self.path)
        if handler_name:
            getattr(self, handler_name)(body)
            return

        self.send_json({"error": "Not found"}, 404)

    # ----- POST handlers -----

    def _post_projects(self, body):
        project_id = body.get("id")
        name = body.get("name", project_id or "")
        description = body.get("description", "")
        owner = body.get("owner", _ucfg.user())
        if not project_id:
            self.send_json({"error": "Missing project id"}, 400)
            return
        ok = storage.create_project(project_id, name, description, owner)
        if ok:
            self.send_json({"project": storage.get_project(project_id)}, 201)
        else:
            self.send_json({"error": f"Project already exists: {project_id}"}, 409)

    def _post_projects_delete(self, body):
        project_id = body.get("id") or body.get("project_id")
        if not project_id:
            self.send_json({"error": "Missing project id"}, 400)
            return
        if project_id == "default":
            self.send_json({"error": "Cannot delete the default project"}, 400)
            return
        ok = storage.delete_project(project_id)
        if ok:
            self.send_json({"ok": True, "deleted": project_id})
        else:
            self.send_json({"error": f"Project not found: {project_id}"}, 404)

    def _post_agent_project(self, body):
        agent_id = body.get("agent_id")
        project_id = body.get("project_id")
        if not agent_id or not project_id:
            self.send_json({"error": "Missing agent_id or project_id"}, 400)
            return
        if not storage.get_project(project_id):
            self.send_json({"error": f"Project not found: {project_id}"}, 404)
            return
        storage.set_agent_active_project(agent_id, project_id)
        if transport:
            msg = f"[project] {agent_id} switched to project [{project_id}]"
            _bot_send("#general", msg, from_id="@system")
        self.send_json({"ok": True, "agent_id": agent_id, "project_id": project_id})

    def _post_send(self, body):
        try:
            room = body.get("room", "#general")
            message = body.get("message", "")

            if not message:
                self.send_json({"error": "Missing message"}, 400)
                return

            ensure_room(room)

            # Allow custom sender (for web UI)
            from_id = body.get("from", transport.agent_id)

            # =============================================================
            # v0.5 MODES: can_speak() enforcement (BEFORE spam check)
            # =============================================================
            is_ask_response = body.get("is_ask_response", False)
            can_speak, reason = autonomy.can_speak(from_id, is_ask_response)

            if not can_speak:
                print(f"[MODES] Blocked message from {from_id}: {reason}")
                self.send_json({
                    "success": False,
                    "error": "mode_blocked",
                    "message": f"⚠️ {reason}",
                    "mode": autonomy.get_mode_state().mode if autonomy.get_mode_state() else "neutral"
                }, 403)
                return
            # =============================================================

            # =============================================================
            # POC v0.5: Spam detection + progressive mute
            # =============================================================
            spam_check = autonomy.check_spam(from_id, message)

            if spam_check["action"] == "mute":
                # Agent is muted, reject message and notify
                print(f"[SPAM] BLOCKED message from {from_id}: muted")
                telegram_notify("moderation/reject", {
                    "agent_id": from_id,
                    "reason": spam_check.get("message", "spam detected"),
                    "muted_seconds": spam_check.get("muted_seconds", 0),
                })
                self.send_json({
                    "success": False,
                    "error": "muted",
                    "message": spam_check["message"],
                    "muted_seconds": spam_check["muted_seconds"]
                }, 403)
                return

            if spam_check["action"] == "reminder":
                # Send reminder to agent (inject into their context)
                reminder_msg = spam_check["message"]
                print(f"[SPAM] REMINDER for {from_id}")
                # Send reminder as system message to the room
                _bot_send(room, f"[REMINDER → {from_id}] {reminder_msg}", from_id="@system")

            # Check leader mode (fallback - kept for backwards compatibility)
            if autonomy.is_leader_mode():
                # In leader mode, only leader and humans can initiate
                if from_id not in HUMAN_SENDERS and from_id != autonomy.leader_id:
                    # Check if message is a response to leader
                    if not any(f"@{from_id.lstrip('@')}" in m.get("content", "")
                               for m in list(message_history)[-10:]
                               if m.get("from") in (autonomy.leader_id, *HUMAN_SENDERS)):
                        print(f"[LEADER MODE] Blocked unsolicited message from {from_id}")
                        self.send_json({
                            "success": False,
                            "error": "leader_mode",
                            "message": f"⚠️ Mode leader actif. Seul {autonomy.leader_id} peut coordonner. Attends d'être sollicité."
                        }, 403)
                        return
            # =============================================================

            # =============================================================
            # v0.4 AUTO-DISPATCH: Route messages from humans without @mention
            # =============================================================
            if from_id in HUMAN_SENDERS and not _has_mention(message):
                target = _auto_dispatch(message)
                prefix = f"[Auto → @{target}] "
                message = prefix + message
                print(f"[DISPATCH] Auto-routed '{from_id}' message to @{target}")
            # =============================================================

            # =============================================================
            # v3.1: #brainstorm language enforcement (English only)
            # =============================================================
            if room == "#brainstorm" and from_id not in HUMAN_SENDERS:
                if _detect_non_english(message):
                    shame_msg = (
                        f"🚨 **LANGUAGE CHECK** — {from_id}, #brainstorm is **English only**. "
                        f"~30% fewer tokens, better for all models. Rewrite in English please."
                    )
                    _bot_send(room, shame_msg, from_id="@system")
                    print(f"[LANG] Non-English detected from {from_id} in #brainstorm")
            # =============================================================

            # Resolve project workspace
            project_id = _resolve_project(body, from_id)

            msg_id = transport.send_chat(room, message, from_id=from_id, project=project_id)

            # Add to local history and persist
            if msg_id:
                entry = {
                    "id": msg_id,
                    "room": room,
                    "from": from_id,
                    "content": message,
                    "timestamp": time.time_ns(),
                    "project": project_id,
                }
                message_history.append(entry)
                save_to_memory(entry)
                _persist_to_db(entry)

            self.send_json({
                "success": msg_id is not None,
                "message_id": msg_id,
                "room": room,
                "auto_dispatched": from_id in HUMAN_SENDERS and not _has_mention(body.get("message", "")),
                "leader_mode": autonomy.is_leader_mode()
            })

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

        # v0.2: Claim endpoint

    def _post_claim(self, body):
        try:
            import asyncio
            action = body.get("action")
            resource = body.get("resource")
            agent_id = body.get("agent_id", transport.agent_id)

            if action == "request":
                result = asyncio.run(autonomy.claim_request(
                    resource=resource,
                    holder=agent_id,
                    description=body.get("description", ""),
                    ttl_minutes=body.get("ttl_minutes"),
                    capabilities=body.get("capabilities", [])
                ))
            elif action == "release":
                result = asyncio.run(autonomy.claim_release(resource, agent_id))
            elif action == "extend":
                result = asyncio.run(autonomy.claim_extend(
                    resource, agent_id, body.get("ttl_minutes")
                ))
            elif action == "query":
                result = autonomy.claim_query(resource)
            else:
                self.send_json({"error": f"Unknown action: {action}"}, 400)
                return

            self.send_json(result)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_lock(self, body):
        try:
            import asyncio
            action = body.get("action")
            path = body.get("path")
            agent_id = body.get("agent_id", transport.agent_id)

            if action == "acquire":
                result = asyncio.run(autonomy.lock_acquire(
                    path=path,
                    holder=agent_id,
                    mode=body.get("mode", "write"),
                    ttl_minutes=body.get("ttl_minutes")
                ))
            elif action == "release":
                result = asyncio.run(autonomy.lock_release(path, agent_id))
            elif action == "query":
                result = autonomy.lock_query(path)
            else:
                self.send_json({"error": f"Unknown action: {action}"}, 400)
                return

            self.send_json(result)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_heartbeat(self, body):
        try:
            import asyncio
            agent_id = body.get("agent_id", transport.agent_id)
            result = asyncio.run(autonomy.heartbeat(
                agent_id=agent_id,
                status=body.get("status", "idle"),
                current_task=body.get("current_task"),
                available_for=body.get("available_for"),
                load=body.get("load", 0.0)
            ))
            # v4.3: Also update SQLite so presence_watchdog() sees this agent
            if storage:
                storage.update_agent_presence(
                    agent_id=agent_id,
                    status=body.get("status", "idle"),
                    current_task=body.get("current_task"),
                    capacity=body.get("capacity", 1)
                )
            self.send_json(result)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_activity(self, body):
        try:
            agent_id = body.get("agent_id", transport.agent_id)
            autonomy.log_activity(
                from_agent=agent_id,
                action_type=body.get("action_type", "unknown"),
                summary=body.get("summary", ""),
                details=body.get("details")
            )
            # v2.1: #activity channel removed, data already in SQLite via autonomy.log_activity()
            self.send_json({"status": "logged"})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_stfu(self, body):
        minutes = body.get("minutes", 5)
        until = autonomy.stfu(minutes)
        # Broadcast to all rooms
        msg = f"🤐 **STFU MODE ACTIVÉ** pour {minutes} minutes. Silence total jusqu'à {until.strftime('%H:%M:%S')} UTC."
        for room in list(joined_rooms):
            _bot_send(room, msg, from_id="@system")
        self.send_json({
            "status": "muted",
            "minutes": minutes,
            "until": until.isoformat()
        })


    def _post_talk(self, body):
        autonomy.talk()
        # Broadcast to all rooms
        msg = "🗣️ **STFU MODE DÉSACTIVÉ** — Les agents peuvent parler à nouveau."
        for room in list(joined_rooms):
            _bot_send(room, msg, from_id="@system")
        self.send_json({"status": "unmuted"})


    def _post_reset_leader(self, body):
        autonomy.reset_leader_mode()
        msg = "🔄 **LEADER MODE RESET** — Retour au mode libre. Compteurs spam remis à zéro."
        for room in list(joined_rooms):
            _bot_send(room, msg, from_id="@system")
        self.send_json({
            "status": "reset",
            "leader_mode": False,
            "spam_incidents": 0
        })


    def _post_leader_mode(self, body):
        leader = body.get("leader", "@alpha")
        autonomy.leader_mode = True
        autonomy.leader_id = leader
        msg = f"👑 **LEADER MODE ACTIVÉ** — Seul {leader} peut coordonner. Les autres attendent d'être sollicités."
        for room in list(joined_rooms):
            _bot_send(room, msg, from_id="@system")
        self.send_json({
            "status": "leader_mode_on",
            "leader": leader
        })

        # =================================================================
        # v0.5 MODES (MODES.md v0.3)
        # =================================================================


    def _post_mode(self, body):
        try:
            mode = body.get("mode", "neutral")
            lead = body.get("lead", "")
            timeout_minutes = body.get("timeout_minutes")
            reason = body.get("reason", "manual")

            # Validate mode
            valid_modes = ["neutral", "focus", "review", "build"]
            if mode not in valid_modes:
                self.send_json({"error": f"Invalid mode. Must be one of: {valid_modes}"}, 400)
                return

            # Set mode (this also clears pending_asks)
            autonomy.set_mode(mode, lead, timeout_minutes, reason)

            # Broadcast to all rooms
            if mode == "neutral":
                msg = "🔄 **MODE NEUTRAL** — Retour au mode libre."
            else:
                timeout_str = f" (timeout: {timeout_minutes}min)" if timeout_minutes else ""
                msg = f"🎯 **MODE {mode.upper()}** — Lead: {lead}{timeout_str}"

            for room in list(joined_rooms):
                _bot_send(room, msg, from_id="@system")

            mode_state = autonomy.get_mode_state()
            self.send_json({
                "status": "mode_changed",
                "mode": mode,
                "lead": lead,
                "timeout_minutes": timeout_minutes,
                "started_at": mode_state.started_at.isoformat() if mode_state and mode_state.started_at else None
            })
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_ask(self, body):
        try:
            from_agent = body.get("from", transport.agent_id)
            to_agent = body.get("to", "@all")
            question = body.get("question", "")

            result = autonomy.register_ask(from_agent, to_agent, question)

            # Broadcast @ask to room
            msg = f"❓ **@ask** de {from_agent} → {to_agent}: {question[:100]}"
            for room in list(joined_rooms):
                if room == "#general":
                    _bot_send(room, msg, from_id="@system")

            self.send_json(result)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_stop(self, body):
        try:
            # Reset to neutral with reason="manual"
            autonomy.set_mode("neutral", "", None, reason="manual")

            # Broadcast
            msg = "🛑 **STOP** — Mode reset, tous les @ask annulés."
            for room in list(joined_rooms):
                _bot_send(room, msg, from_id="@system")

            self.send_json({"status": "stopped", "mode": "neutral"})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_handover(self, body):
        try:
            to_agent = body.get("to")
            if not to_agent:
                self.send_json({"error": "Missing 'to' field"}, 400)
                return

            mode_state = autonomy.get_mode_state()
            current_mode = mode_state.mode if mode_state else "neutral"

            # Keep same mode, just change lead (with reason="override")
            autonomy.set_mode(current_mode, to_agent, None, reason="override")

            msg = f"🔄 **HANDOVER** — Lead transféré à {to_agent}"
            for room in list(joined_rooms):
                _bot_send(room, msg, from_id="@system")

            self.send_json({"status": "handover", "new_lead": to_agent, "mode": current_mode})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

        # =================================================================
        # v0.6/v0.7 TaskManager (Option B: Daemon enrichi)
        # =================================================================


    def _post_task(self, body):
        try:
            agent_id = body.get("agent_id")
            task_type = body.get("task_type", "generic")
            description = body.get("description", "")
            context = body.get("context")

            if not agent_id:
                self.send_json({"error": "Missing 'agent_id' field"}, 400)
                return

            if not description:
                self.send_json({"error": "Missing 'description' field"}, 400)
                return

            project_id = _resolve_project(body, agent_id)
            task_id = storage.create_task(agent_id, task_type, description, context, project_id=project_id)

            if task_id > 0:
                # v3.3: Link to workflow only if explicitly requested
                # P2 FIX: auto-linking ALL tasks to active workflow caused
                # unrelated task completions to trigger workflow auto-advance.
                # Now: only link if client sends workflow_id explicitly.
                linked_wf_id = None
                explicit_wf_id = body.get("workflow_id")
                if explicit_wf_id and workflow_scheduler:
                    wf = workflow_scheduler.get_workflow(int(explicit_wf_id))
                    if wf and not wf.get("completed_at"):
                        storage.set_task_workflow_id(task_id, wf["id"])
                        linked_wf_id = wf["id"]

                # Broadcast task creation
                wf_tag = f" [workflow #{linked_wf_id}]" if linked_wf_id else ""
                proj_tag = f" [{project_id}]" if project_id != "default" else ""
                msg = f"📋 **TASK #{task_id}** created for {agent_id}{proj_tag}: {description[:80]}{wf_tag}"
                _bot_send("#general", msg, from_id="@taskman")

                self.send_json({
                    "status": "created",
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "task_type": task_type,
                    "description": description,
                    "workflow_id": linked_wf_id
                })
            else:
                self.send_json({"error": "Failed to create task"}, 500)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_task_claim(self, body):
        try:
            task_id = body.get("task_id")
            agent_id = body.get("agent_id", transport.agent_id)

            if not task_id:
                self.send_json({"error": "Missing 'task_id' field"}, 400)
                return

            success = storage.claim_task(task_id, agent_id)

            if success:
                msg = f"🚀 {agent_id} claimed task #{task_id}"
                _bot_send("#general", msg, from_id="@taskman")
                self.send_json({"status": "claimed", "success": True, "task_id": task_id, "agent_id": agent_id})
            else:
                self.send_json({"error": "Failed to claim task (already claimed or not found)"}, 400)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_task_activity(self, body):
        try:
            task_id = body.get("task_id")
            current_step = body.get("current_step")  # v0.8: optional step persistence

            if not task_id:
                self.send_json({"error": "Missing 'task_id' field"}, 400)
                return

            success = storage.update_task_activity(task_id, current_step)
            response = {"status": "updated" if success else "not_found", "success": success, "task_id": task_id}
            if success and current_step is not None:
                response["current_step"] = current_step
            self.send_json(response)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_task_complete(self, body):
        try:
            task_id = body.get("task_id")
            status = body.get("status", "done")  # done, failed, cancelled, stale

            if not task_id:
                self.send_json({"error": "Missing 'task_id' field"}, 400)
                return

            valid_statuses = ["done", "failed", "cancelled", "stale"]
            if status not in valid_statuses:
                self.send_json({"error": f"Invalid status. Must be one of: {valid_statuses}"}, 400)
                return

            success = storage.complete_task(task_id, status)

            if success:
                emoji = "✅" if status == "done" else "❌" if status == "failed" else "⚠️" if status == "stale" else "🚫"
                msg = f"{emoji} Task #{task_id} completed ({status})"
                _bot_send("#general", msg, from_id="@taskman")

                # v3.3: Auto-advance workflow if last code task completed
                if status == "done" and workflow_scheduler:
                    try:
                        task = storage.get_task_by_id(task_id)
                        if task and task.get("workflow_id"):
                            wf_id = task["workflow_id"]
                            wf = workflow_scheduler.get_workflow(wf_id)
                            if wf and wf.get("phase") == "code" and not wf.get("completed_at"):
                                remaining = storage.get_active_workflow_tasks(wf_id)
                                if not remaining:
                                    result = workflow_scheduler.next_phase(wf_id)
                                    if result.get("success"):
                                        next_phase = result.get("current_phase", "review")
                                        _bot_send(
                                            "#general",
                                            f"🔄 **WORKFLOW #{wf_id}** auto-advanced to `@{next_phase}` (all tasks done)",
                                            from_id="@workflow"
                                        )
                                        # v4.1: Git hooks on auto-advance
                                        _run_git_hooks("code", next_phase, wf_id)
                                        if next_phase == "review":
                                            _auto_create_workflow_review(wf_id)
                                        if bridge:
                                            wf_updated = workflow_scheduler.get_workflow(wf_id)
                                            if wf_updated:
                                                bridge.emit_workflow(wf_updated)
                    except Exception as e:
                        print(f"[WORKFLOW] Auto-advance error on task complete: {e}")

                self.send_json({"status": "completed", "success": True, "task_id": task_id, "final_status": status})
            else:
                self.send_json({"error": "Failed to complete task"}, 400)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

        # =================================================================
        # v0.9 Agent Heartbeat
        # =================================================================


    def _post_agent_heartbeat(self, body):
        try:
            agent_id = body.get("agent_id")
            status = body.get("status", "idle")  # idle, working, reviewing, blocked
            current_task = body.get("current_task")

            if not agent_id:
                self.send_json({"error": "Missing 'agent_id' field"}, 400)
                return

            valid_statuses = ["idle", "working", "reviewing", "blocked", "away"]
            if status not in valid_statuses:
                self.send_json({"error": f"Invalid status. Must be one of: {valid_statuses}"}, 400)
                return

            capacity = body.get("capacity", 1)
            success = storage.update_agent_presence(agent_id, status, current_task, capacity=capacity)

            if success:
                self.send_json({
                    "status": "ok",
                    "agent_id": agent_id,
                    "presence_status": status,
                    "current_task": current_task,
                    "capacity": capacity
                })
            else:
                self.send_json({"error": "Failed to update presence"}, 500)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

        # =================================================================
        # v1.0 Brainstorm System
        # =================================================================


    def _post_brainstorm_create(self, body):
        try:
            topic = body.get("topic", "")
            created_by = body.get("created_by", _ucfg.user())
            participants = body.get("participants")  # Optional override
            task_id = body.get("task_id")  # Optional link to a task

            if not topic:
                self.send_json({"error": "Missing 'topic' field"}, 400)
                return

            # Dedup: reject if an active session has the same topic
            active = storage.get_active_brainstorm_sessions()
            for existing in active:
                if existing.get("topic", "").strip().lower() == topic.strip().lower():
                    self.send_json({
                        "error": f"Active brainstorm already exists with same topic (session #{existing['id']})",
                        "existing_session_id": existing["id"]
                    }, 409)
                    return

            # Load config
            config = get_brainstorm_config()

            # Use custom participants or default from config
            if not participants:
                participants = config.get("default_participants", ["@alpha", "@sonnet", "@haiku"])

            # Ensure creator is a participant ONLY if they are an AI agent
            # Humans (HUMAN_AGENTS) decide, they don't vote — don't add them
            if created_by not in participants and created_by not in HUMAN_AGENTS:
                participants.append(created_by)

            timeout_seconds = config.get("timeout_seconds", 180)
            # v4.1: Auto-increase timeout when local LLM agents participate
            timeout_seconds = get_brainstorm_timeout_for_participants(participants, timeout_seconds)
            channel = config.get("channel", "#brainstorm")

            # v3.0: Project scope
            project_id = _resolve_project(body, created_by)

            # Create session
            session_id = storage.create_brainstorm_session(
                topic=topic,
                created_by=created_by,
                participants=participants,
                timeout_seconds=timeout_seconds,
                task_id=task_id,
                project_id=project_id
            )

            if session_id > 0:
                # Auto-vote GO for the creator (if you create it, you're obviously GO)
                if created_by not in HUMAN_AGENTS:
                    storage.add_brainstorm_vote(session_id, created_by, "✅", "Auto-vote: creator")
                # Dispatch to brainstorm channel
                participant_tags = " ".join(participants)
                dispatch_msg = f"🧠 **BRAINSTORM #{session_id}** - New topic!\n"
                dispatch_msg += f"**Topic:** {topic}\n"
                dispatch_msg += f"**From:** {created_by}\n"
                dispatch_msg += f"**Participants:** {participant_tags}\n"
                dispatch_msg += f"**Format:** ✅/❌ + max 2 lines (EN)\n"
                dispatch_msg += f"**Timeout:** {timeout_seconds // 60}min (silence = approval)\n"
                dispatch_msg += f"\nReply with: POST /brainstorm/vote {{\"session_id\": {session_id}, \"vote\": \"✅\", \"comment\": \"...\"}}"

                # Join and send to brainstorm channel
                ensure_room(channel)
                _bot_send(channel, dispatch_msg, from_id="@brainstorm")

                # Also notify in #general
                ensure_room("#general")
                short_msg = f"🧠 @all Brainstorm #{session_id} created by {created_by}: {topic[:60]}... → {channel} - Vote!"
                _bot_send("#general", short_msg, from_id="@brainstorm")

                self.send_json({
                    "status": "created",
                    "session_id": session_id,
                    "topic": topic,
                    "participants": participants,
                    "timeout_seconds": timeout_seconds,
                    "channel": channel
                })
            else:
                self.send_json({"error": "Failed to create brainstorm session"}, 500)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_brainstorm_vote(self, body):
        try:
            session_id = body.get("session_id")
            agent_id = body.get("agent_id")
            vote = body.get("vote", "")
            comment = body.get("comment")

            if not session_id:
                self.send_json({"error": "Missing 'session_id' field"}, 400)
                return

            if not agent_id:
                self.send_json({"error": "Missing 'agent_id' field"}, 400)
                return

            if not vote:
                self.send_json({"error": "Missing 'vote' field (use ✅ or ❌)"}, 400)
                return

            # Check session exists and is pending
            session = storage.get_brainstorm_session(session_id)
            if not session:
                self.send_json({"error": "Session not found"}, 404)
                return

            if session.get("status") != "pending":
                self.send_json({"error": "Session already closed"}, 400)
                return

            # Record vote
            success = storage.add_brainstorm_vote(session_id, agent_id, vote, comment)

            if success:
                # Notify in brainstorm channel with @all tag
                config = get_brainstorm_config()
                channel = config.get("channel", "#brainstorm")
                comment_str = f" - {comment}" if comment else ""
                vote_msg = f"📝 @all {agent_id} voted {vote} on brainstorm #{session_id}{comment_str}"
                ensure_room(channel)
                _bot_send(channel, vote_msg, from_id="@brainstorm")

                # Check if all participants have voted → early resolution
                participants = session.get("participants", [])
                updated_session = storage.get_brainstorm_session(session_id)
                votes = updated_session.get("votes", []) if updated_session else []

                if len(votes) >= len(participants):
                    # All voted → can resolve early (watchdog will handle it)
                    print(f"[BRAINSTORM] All votes in for session #{session_id}")

                self.send_json({
                    "status": "voted",
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "vote": vote,
                    "votes_so_far": len(votes),
                    "participants_count": len(participants)
                })
            else:
                self.send_json({"error": "Failed to record vote"}, 500)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

        # =================================================================
        # v1.4 /idea Command - Auto brainstorm + workflow trigger
        # =================================================================


    def _post_idea(self, body):
        try:
            idea = body.get("idea", "")
            created_by = body.get("created_by", _ucfg.user())
            participants = body.get("participants")  # Optional override
            wf_mode = body.get("mode", "standard")  # v4.2: workflow mode (standard/veloce)

            if not idea:
                self.send_json({"error": "Missing 'idea' field"}, 400)
                return

            if len(idea.strip()) < 5:
                self.send_json({"error": "Idea too short (min 5 chars)"}, 400)
                return

            # Load brainstorm config
            config = get_brainstorm_config()

            # Use custom participants or default from config
            if not participants:
                participants = config.get("default_participants", ["@alpha", "@sonnet", "@haiku"])

            # Ensure creator is a participant ONLY if they are an AI agent
            # Humans (HUMAN_AGENTS) decide, they don't vote
            if created_by not in participants and created_by not in HUMAN_AGENTS:
                participants.append(created_by)

            # Shorter timeout for ideas (default 3min, can be overridden)
            timeout_seconds = body.get("timeout_seconds", config.get("timeout_seconds", 180))
            # v4.1: Auto-increase timeout when local LLM agents participate
            if not body.get("timeout_seconds"):
                timeout_seconds = get_brainstorm_timeout_for_participants(participants, timeout_seconds)
            channel = config.get("channel", "#brainstorm")

            # Create brainstorm session with auto_workflow=True
            session_id = storage.create_brainstorm_session(
                topic=idea,
                created_by=created_by,
                participants=participants,
                timeout_seconds=timeout_seconds,
                task_id=None,
                auto_workflow=True,  # This is the key difference!
                workflow_mode=wf_mode,
            )

            if session_id > 0:
                # Auto-vote GO for the creator (if you create it, you're obviously GO)
                if created_by not in HUMAN_AGENTS:
                    storage.add_brainstorm_vote(session_id, created_by, "✅", "Auto-vote: creator")
                # Dispatch to brainstorm channel
                participant_tags = " ".join(participants)
                dispatch_msg = f"💡 **IDEA #{session_id}** - New idea!\n"
                dispatch_msg += f"**Idea:** {idea}\n"
                dispatch_msg += f"**From:** {created_by}\n"
                dispatch_msg += f"**Participants:** {participant_tags}\n"
                dispatch_msg += f"**Format:** ✅ GO / ❌ NO GO + 1 line max\n"
                dispatch_msg += f"**Timeout:** {timeout_seconds // 60}min\n"
                dispatch_msg += f"**Mode:** {wf_mode}\n"
                dispatch_msg += f"**Auto-workflow:** If GO → workflow auto-start 🚀"

                # Join and send to brainstorm channel
                ensure_room(channel)
                _bot_send(channel, dispatch_msg, from_id="@idea")

                # Also notify in #general
                ensure_room("#general")
                short_msg = f"💡 @all **IDEA #{session_id}** from {created_by}: {idea[:60]}{'...' if len(idea) > 60 else ''} → {channel} - Vote GO/NO GO!"
                _bot_send("#general", short_msg, from_id="@idea")

                self.send_json({
                    "status": "created",
                    "session_id": session_id,
                    "idea": idea,
                    "participants": participants,
                    "timeout_seconds": timeout_seconds,
                    "channel": channel,
                    "auto_workflow": True,
                    "mode": wf_mode,
                })
            else:
                self.send_json({"error": "Failed to create idea session"}, 500)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

        # =================================================================
        # v1.5 Review System: POST endpoints
        # =================================================================


    def _post_review_request(self, body):
        try:
            file_path = body.get("file", body.get("file_path", ""))
            reviewers = body.get("reviewers", [])
            review_type = body.get("type", "doc")  # 'doc' or 'code'
            requested_by = body.get("requested_by", transport.agent_id)

            if not file_path:
                self.send_json({"error": "Missing 'file' field"}, 400)
                return

            if not reviewers:
                # Default reviewers based on type
                if review_type == "code":
                    reviewers = ["@beta", "@sonnet"]  # Code needs 2 reviewers
                else:
                    reviewers = ["@sonnet"]  # Doc needs 1 reviewer

            # v3.0: Project scope
            project_id = _resolve_project(body, requested_by)

            # Create review request
            request_id = storage.create_review_request(
                file_path=file_path,
                requested_by=requested_by,
                reviewers=reviewers,
                review_type=review_type,
                timeout_seconds=REVIEW_TIMEOUT_SECONDS,
                project_id=project_id
            )

            if request_id > 0:
                # Broadcast review request
                reviewer_tags = " ".join(reviewers)
                msg = f"**REVIEW #{request_id}** requested by {requested_by}\n"
                msg += f"**File:** `{file_path}`\n"
                msg += f"**Type:** {review_type} (min {2 if review_type == 'code' else 1} approval(s))\n"
                msg += f"**Reviewers:** {reviewer_tags}\n"
                msg += f"**Timeout:** 1h (reminder at 30min)"

                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@review")

                self.send_json({
                    "status": "created",
                    "request_id": request_id,
                    "file": file_path,
                    "reviewers": reviewers,
                    "review_type": review_type,
                    "timeout_seconds": REVIEW_TIMEOUT_SECONDS
                })
            else:
                self.send_json({"error": "Failed to create review request"}, 500)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_review_approve(self, body):
        try:
            request_id = body.get("request_id", body.get("id"))
            reviewer = body.get("reviewer", transport.agent_id)
            comment = body.get("comment")

            if not request_id:
                self.send_json({"error": "Missing 'request_id' field"}, 400)
                return

            # Check review exists and is pending
            review = storage.get_review_request(request_id)
            if not review:
                self.send_json({"error": "Review not found"}, 404)
                return

            if review.get("status") != "pending":
                self.send_json({"error": "Review already closed"}, 400)
                return

            # Record approval
            success = storage.add_review_response(request_id, reviewer, "approve", comment)

            if success:
                # Notify
                file_path = review.get("file_path", "")
                comment_str = f" - {comment}" if comment else ""
                msg = f"✅ {reviewer} approuve review #{request_id} (`{file_path}`){comment_str}"

                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@review")

                # Check if we have enough approvals to auto-close
                updated_review = storage.get_review_request(request_id)
                responses = updated_review.get("responses", []) if updated_review else []
                approvals = sum(1 for r in responses if r.get("vote") == "approve")
                min_approvals = review.get("min_approvals", 1)

                if approvals >= min_approvals:
                    # Auto-close with approved status
                    storage.close_review_request(request_id, "approved", "completed")
                    msg = f"🎉 **REVIEW #{request_id}** approved! ({approvals}/{min_approvals} approvals)"
                    if transport:
                        _bot_send("#general", msg, from_id="@review")

                    # v4.0: Telegram notification
                    telegram_notify("review/approved", {
                        "request_id": request_id,
                        "approvals": approvals,
                        "min_approvals": min_approvals,
                        "file_path": file_path,
                    })

                    # HOOK v3.3: Auto-advance workflow on review approval
                    if workflow_scheduler:
                        # Check by workflow_id FK (preferred) or legacy file_path convention
                        review_wf_id = updated_review.get("workflow_id")
                        file_path = updated_review.get("file_path", "")
                        if not review_wf_id and file_path.startswith("workflow:"):
                            wf = workflow_scheduler.get_active_workflow()
                            review_wf_id = wf["id"] if wf else None
                        if review_wf_id:
                            wf = workflow_scheduler.get_workflow(review_wf_id)
                            if wf and wf.get("phase") == "review" and not wf.get("completed_at"):
                                next_result = workflow_scheduler.next_phase(review_wf_id)
                                if next_result.get("success") and transport:
                                    next_phase = next_result.get("current_phase", "test")
                                    _bot_send("#general",
                                        f"🔄 **WORKFLOW #{review_wf_id}** auto-advanced to `@{next_phase}` (review approved)",
                                        from_id="@workflow")
                                    # v4.1: Git hooks on auto-advance
                                    _run_git_hooks("review", next_phase, review_wf_id)
                                    if bridge:
                                        wf_updated = workflow_scheduler.get_workflow(review_wf_id)
                                        if wf_updated:
                                            bridge.emit_workflow(wf_updated)

                self.send_json({
                    "status": "approved",
                    "request_id": request_id,
                    "reviewer": reviewer,
                    "approvals": approvals,
                    "min_approvals": min_approvals
                })
            else:
                self.send_json({"error": "Failed to record approval"}, 500)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_review_comment(self, body):
        try:
            request_id = body.get("request_id", body.get("id"))
            reviewer = body.get("reviewer", transport.agent_id)
            comment = body.get("comment", "")

            if not request_id:
                self.send_json({"error": "Missing 'request_id' field"}, 400)
                return

            if not comment:
                self.send_json({"error": "Missing 'comment' field"}, 400)
                return

            # Check review exists
            review = storage.get_review_request(request_id)
            if not review:
                self.send_json({"error": "Review not found"}, 404)
                return

            if review.get("status") != "pending":
                self.send_json({"error": "Review already closed"}, 400)
                return

            # Record comment
            success = storage.add_review_response(request_id, reviewer, "comment", comment)

            if success:
                file_path = review.get("file_path", "")
                msg = f"💬 {reviewer} commente review #{request_id}: {comment[:100]}"

                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@review")

                self.send_json({
                    "status": "commented",
                    "request_id": request_id,
                    "reviewer": reviewer
                })
            else:
                self.send_json({"error": "Failed to record comment"}, 500)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_review_changes(self, body):
        try:
            request_id = body.get("request_id", body.get("id"))
            reviewer = body.get("reviewer", transport.agent_id)
            comment = body.get("comment", "")

            if not request_id:
                self.send_json({"error": "Missing 'request_id' field"}, 400)
                return

            if not comment:
                self.send_json({"error": "Missing 'comment' field (explain what needs to change)"}, 400)
                return

            # Check review exists
            review = storage.get_review_request(request_id)
            if not review:
                self.send_json({"error": "Review not found"}, 404)
                return

            if review.get("status") != "pending":
                self.send_json({"error": "Review already closed"}, 400)
                return

            # Record changes request
            success = storage.add_review_response(request_id, reviewer, "changes", comment)

            if success:
                file_path = review.get("file_path", "")
                requested_by = review.get("requested_by", "")
                msg = f"{reviewer} requests changes on review #{request_id}: {comment[:100]}"

                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@review")

                    # Notify the requester specifically
                    if requested_by:
                        notify_msg = f"{requested_by} - {reviewer} requests changes on your review #{request_id}"
                        _bot_send("#general", notify_msg, from_id="@review")

                # v4.0: Telegram notification
                telegram_notify("review/changes", {
                    "request_id": request_id,
                    "reviewer": reviewer,
                    "comment": comment,
                })

                # P1 FIX: changes_requested is a blocking verdict - close the review
                # This prevents the watchdog from endlessly pinging remaining reviewers
                storage.close_review_request(request_id, "changes_requested", "completed")
                review_reminder_state.pop(request_id, None)  # cleanup watchdog state (nit from beta review #10)
                if transport:
                    _bot_send(
                        "#general",
                        f"📋 **REVIEW #{request_id}** closed (changes requested by {reviewer})",
                        from_id="@review"
                    )

                self.send_json({
                    "status": "changes_requested",
                    "request_id": request_id,
                    "reviewer": reviewer,
                    "comment": comment
                })
            else:
                self.send_json({"error": "Failed to record changes request"}, 500)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_review_close(self, body):
        try:
            request_id = body.get("request_id", body.get("id"))
            reason = body.get("reason", "manually closed")
            closed_by = body.get("closed_by", body.get("reviewer", "unknown"))

            if not request_id:
                self.send_json({"error": "Missing 'request_id' field"}, 400)
                return

            review = storage.get_review_request(request_id)
            if not review:
                self.send_json({"error": "Review not found"}, 404)
                return

            if review.get("status") != "pending":
                self.send_json({
                    "status": review.get("status"),
                    "message": "Review already closed",
                    "request_id": request_id
                })
                return

            success = storage.close_review_request(request_id, reason, "closed")
            if success:
                file_path = review.get("file_path", "")
                if transport:
                    ensure_room("#general")
                    _bot_send(
                        "#general",
                        f"🔒 **REVIEW #{request_id}** closed by {closed_by}: {reason}",
                        from_id="@review"
                    )
                # Clean up watchdog state
                review_reminder_state.pop(request_id, None)

                self.send_json({
                    "status": "closed",
                    "request_id": request_id,
                    "reason": reason,
                    "closed_by": closed_by
                })
            else:
                self.send_json({"error": "Failed to close review"}, 500)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

        # =================================================================
        # v1.3 Workflow Scheduler: POST endpoints
        # =================================================================


    def _post_workflow_start(self, body):
        try:
            name = body.get("name", "")
            description = body.get("description", "")
            created_by = body.get("created_by", _ucfg.user())
            lead_agent = body.get("lead_agent") or body.get("lead") or created_by
            mode = body.get("mode", "standard")  # v2.0: 'standard' or 'veloce'

            if not name:
                self.send_json({"error": "Missing 'name' field"}, 400)
                return

            if mode not in ("standard", "veloce"):
                self.send_json({"error": f"Invalid mode: {mode}. Use 'standard' or 'veloce'"}, 400)
                return

            if not workflow_scheduler:
                self.send_json({"error": "Workflow scheduler not initialized"}, 500)
                return

            # v3.0: Project scope
            project_id = _resolve_project(body, created_by)

            workflow_id = workflow_scheduler.create_workflow(
                name=name,
                created_by=created_by,
                description=description,
                lead_agent=lead_agent,
                project_id=project_id,
                mode=mode
            )

            if workflow_id > 0:
                # Broadcast workflow creation
                mode_tag = " **[VELOCE]**" if mode == "veloce" else ""
                msg = f"🚀 **WORKFLOW #{workflow_id}**{mode_tag} started: {name}\n"
                msg += f"Created by: {created_by}\n"
                if mode == "veloce":
                    msg += f"Phase: `@request` -- Mode Veloce (parallel coding)"
                else:
                    msg += f"Phase: `@request` -- Waiting for brainstorm..."
                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@workflow")
                if bridge:
                    wf = workflow_scheduler.get_workflow(workflow_id)
                    if wf:
                        bridge.emit_workflow(wf)

                self.send_json({
                    "status": "created",
                    "workflow_id": workflow_id,
                    "name": name,
                    "phase": "request",
                    "created_by": created_by,
                    "mode": mode
                })
            else:
                self.send_json({
                    "error": "Cannot create workflow - one already active",
                    "active_workflow": workflow_scheduler.get_active_workflow()
                }, 409)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_workflow_next(self, body):
        try:
            if not workflow_scheduler:
                self.send_json({"error": "Workflow scheduler not initialized"}, 500)
                return

            result = workflow_scheduler.next_phase()

            if result.get("success"):
                # Check if workflow completed (has 'status' key) vs phase transition (has 'current_phase')
                if result.get("status"):
                    # Workflow completed - send completion message + doc reminder
                    status = result.get("status")
                    duration = result.get("duration_minutes", 0)
                    wf_id = result.get("workflow_id")

                    msg = f"✅ **WORKFLOW #{wf_id}** completed ({status}) — Duration: {duration}min"
                    if transport:
                        ensure_room("#general")
                        _bot_send("#general", msg, from_id="@workflow")

                    # Send documentation reminder
                    reminder = """📋 **POST-DELIVERY CHECKLIST:**
1. Update `docs/*.md` (IDEAS, TASKMANAGER, etc.)
2. Check if agent `SOUL.md` files need updates
3. Verify `dashboard.html` reflects the changes
4. Nothing gets forgotten!"""
                    if transport:
                        _bot_send("#general", reminder, from_id="@workflow")
                    if bridge:
                        bridge.emit_workflow(None)  # Workflow done

                    # v4.0: Telegram notification
                    telegram_notify("workflow/complete", {
                        "workflow_id": wf_id,
                        "duration_minutes": duration,
                        "status": status,
                    })

                    # v4.1: Git hooks — log summary on completion
                    _run_git_hooks("livrable", "done", wf_id)
                else:
                    # Normal phase transition
                    prev = result.get("previous_phase")
                    curr = result.get("current_phase")
                    timeout = result.get("timeout_minutes")
                    wf_id = result.get("workflow_id")

                    mode = result.get("mode", "standard")
                    mode_tag = " [VELOCE]" if mode == "veloce" else ""
                    msg = f"➡️ **WORKFLOW**{mode_tag} - Phase `@{prev}` -> `@{curr}` (timeout: {timeout}min)"
                    # v2.0: Notify about chunks starting
                    chunks_started = result.get("chunks_started")
                    if chunks_started:
                        msg += f"\n📦 {chunks_started} parallel chunk(s) started!"
                    if transport:
                        ensure_room("#general")
                        _bot_send("#general", msg, from_id="@workflow")

                    # v4.0: Telegram notification
                    telegram_notify("workflow/phase", {
                        "previous_phase": prev,
                        "current_phase": curr,
                        "timeout_minutes": timeout,
                        "workflow_id": wf_id,
                    })

                    # v4.1: Git hooks (non-blocking)
                    _run_git_hooks(prev, curr, wf_id)

                    # HOOK v3.3: Auto-create review when entering 'review' phase
                    if curr == "review":
                        wf = workflow_scheduler.get_active_workflow()
                        if wf:
                            _auto_create_workflow_review(wf["id"])

                    # v3.3: Instant dashboard emit
                    if bridge:
                        wf = workflow_scheduler.get_active_workflow()
                        bridge.emit_workflow(wf)

            self.send_json(result)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_workflow_extend(self, body):
        try:
            minutes = body.get("minutes", 10)

            if not workflow_scheduler:
                self.send_json({"error": "Workflow scheduler not initialized"}, 500)
                return

            result = workflow_scheduler.extend_phase(minutes)

            if result.get("success"):
                phase = result.get("phase")
                new_timeout = result.get("new_timeout_minutes")
                remaining = result.get("extends_remaining")

                msg = f"⏰ **WORKFLOW** — Phase `@{phase}` extended by {minutes}min (total: {new_timeout}min, {remaining} extend(s) remaining)"
                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@workflow")
                if bridge:
                    wf = workflow_scheduler.get_active_workflow()
                    bridge.emit_workflow(wf)

            self.send_json(result)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_workflow_skip(self, body):
        try:
            phase = body.get("phase")

            if not phase:
                self.send_json({"error": "Missing 'phase' field"}, 400)
                return

            if not workflow_scheduler:
                self.send_json({"error": "Workflow scheduler not initialized"}, 500)
                return

            # Capture previous phase before skip (for git hooks)
            wf_before = workflow_scheduler.get_active_workflow()
            prev_phase = wf_before["phase"] if wf_before else None

            result = workflow_scheduler.skip_to_phase(phase)

            if result.get("success"):
                msg = f"⏭️ **WORKFLOW** — Skipped to `@{phase}` (lead override)"
                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@workflow")

                # v4.1: Git hooks on skip
                wf_id = result.get("workflow_id")
                if wf_id:
                    _run_git_hooks(prev_phase, phase, wf_id)

                if bridge:
                    wf = workflow_scheduler.get_active_workflow()
                    bridge.emit_workflow(wf)

            self.send_json(result)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    def _post_workflow_abort(self, body):
        try:
            reason = body.get("reason", "aborted")

            if not workflow_scheduler:
                self.send_json({"error": "Workflow scheduler not initialized"}, 500)
                return

            result = workflow_scheduler.abort_workflow(reason=reason)

            if result.get("success"):
                msg = f"🛑 **WORKFLOW** aborted: {reason}"
                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@workflow")
                if bridge:
                    bridge.emit_workflow(None)  # No active workflow
                    # Reminder even for aborted - partial work may need doc
                    _bot_send("#general", "📋 If work was done, remember to document the current state in `docs/`.", from_id="@workflow")

            self.send_json(result)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

        # =================================================================
        # v2.0 Mode Veloce: POST endpoints
        # =================================================================

    def _post_workflow_decompose(self, body):
        """POST /workflow/decompose - Submit chunk decomposition plan."""
        try:
            if not workflow_scheduler:
                self.send_json({"error": "Workflow scheduler not initialized"}, 500)
                return

            wf = workflow_scheduler.get_active_workflow()
            if not wf:
                self.send_json({"error": "No active workflow"}, 404)
                return

            if wf.get('mode') != 'veloce':
                self.send_json({"error": "Not a veloce workflow"}, 400)
                return

            chunks = body.get("chunks", [])
            if not chunks:
                self.send_json({"error": "Missing 'chunks' array"}, 400)
                return

            result = workflow_scheduler.submit_decomposition(wf['id'], chunks)

            if result.get("success"):
                chunk_ids = result.get("chunk_ids", [])
                count = result.get("chunks_count", 0)
                agents = [c.get("agent_id", "?") for c in chunks]
                msg = f"📦 **WORKFLOW** — Decomposition submitted: {count} chunks\n"
                for c in chunks:
                    msg += f"  - `{c['chunk_id']}` -> {c.get('agent_id', '?')}\n"
                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@workflow")
                if bridge:
                    wf = workflow_scheduler.get_active_workflow()
                    bridge.emit_workflow(wf)

            self.send_json(result)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def _post_workflow_chunk_done(self, body):
        """POST /workflow/chunk/done - Mark a chunk as completed."""
        try:
            if not workflow_scheduler:
                self.send_json({"error": "Workflow scheduler not initialized"}, 500)
                return

            chunk_id = body.get("chunk_id", "").strip()
            if not chunk_id:
                self.send_json({"error": "Missing 'chunk_id' field"}, 400)
                return

            wf = workflow_scheduler.get_active_workflow()
            if not wf:
                self.send_json({"error": "No active workflow"}, 404)
                return

            result = workflow_scheduler.complete_chunk(wf['id'], chunk_id)

            if result.get("success"):
                done = result.get("done_count", 0)
                total = result.get("active_chunks", 0)
                gate = result.get("gate_open", False)
                msg = f"✅ **CHUNK** `{chunk_id}` done ({done}/{total})"
                if gate:
                    msg += " -- 🚪 Gate OPEN, ready for next phase!"
                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@workflow")
                if bridge:
                    wf = workflow_scheduler.get_active_workflow()
                    bridge.emit_workflow(wf)

            self.send_json(result)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def _get_workflow_chunks(self, parsed, params):
        """GET /workflow/chunks - List chunks for active workflow."""
        if not workflow_scheduler:
            self.send_json({"error": "Workflow scheduler not initialized"}, 500)
            return

        wf = workflow_scheduler.get_active_workflow()
        if not wf:
            self.send_json({"chunks": [], "total": 0})
            return

        summary = workflow_scheduler.get_chunks_summary(wf['id'])
        summary["workflow_id"] = wf['id']
        summary["mode"] = wf.get('mode', 'standard')
        self.send_json(summary)

        # =================================================================
        # v2.1 Compact Engine: POST /compact
        # =================================================================

    def _post_compact(self, body):
        try:
            room = body.get("room", "#general")
            agent_id = body.get("agent_id", transport.agent_id)
            force = body.get("force", False)

            # Get message history from storage
            history = storage.get_room_history(room, limit=500)
            messages_raw = history.get("messages", [])

            # Convert envelope format to compact_engine format (shared helper)
            messages = _envelopes_to_messages(messages_raw, room)

            if not messages:
                self.send_json({"error": "No messages in room", "room": room}, 400)
                return

            # Run compaction
            result = compact_room(
                messages=messages,
                room=room,
                agent_id=agent_id,
                force=force,
            )

            if result is None:
                self.send_json({
                    "compacted": False,
                    "reason": f"Below threshold ({len(messages)} messages)",
                    "room": room,
                    "message_count": len(messages),
                })
                return

            # v3: soft-delete instead of hard delete
            all_ids = result.get("deleted_ids", []) + result.get("compacted_ids", [])
            if all_ids:
                n = storage.soft_delete_messages(all_ids)
                logger.info(f"[COMPACTv3] API soft-deleted {n} messages in {room}")

            # Insert summary into DB (not chat)
            summary = result.get("summary", "")
            if summary:
                storage.insert_summary_message(room, summary)

            # Audit log
            storage.log_compaction(
                room=room,
                triggered_by=agent_id,
                total_before=result.get("total_before", 0),
                total_after=result.get("total_after", 0),
                deleted_count=result.get("deleted_count", 0),
                compacted_count=result.get("compacted_count", 0),
                compression_ratio=result.get("compression_ratio", "?"),
                summary=summary,
            )

            # Reset auto-trigger counter
            _compact_msg_counter[room] = 0
            _last_compact_time[room] = time.time()

            self.send_json({
                "compacted": True,
                "room": room,
                "total_before": result.get("total_before", 0),
                "total_after": result.get("total_after", 0),
                "soft_deleted": len(all_ids),
                "compression_ratio": result.get("compression_ratio", "0%"),
                "summary": summary,
                "audit_file": result.get("audit_file", None),
            })

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

        # =================================================================
        # Compactor v3: POST /retention/gc — Manual GC trigger
        # =================================================================

    def _post_retention_gc(self, body):
        try:
            retention_days = body.get("retention_days", 7)
            purged = storage.gc_compacted(retention_days)
            usage_purged = storage.cleanup_old_usage(retention_days)
            self.send_json({
                "purged": purged,
                "usage_purged": usage_purged,
                "retention_days": retention_days,
            })
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

        # =================================================================
        # LLM Usage Tracking: POST /usage/report
        # =================================================================

    def _post_usage_report(self, body):
        agent_id = body.get("agent_id")
        provider = body.get("provider", "unknown")
        model = body.get("model", "unknown")
        if not agent_id:
            self.send_json({"error": "Missing agent_id"}, 400)
            return
        ok = storage.record_llm_usage(
            agent_id=agent_id,
            provider=provider,
            model=model,
            prompt_tokens=body.get("prompt_tokens"),
            completion_tokens=body.get("completion_tokens"),
            estimated=body.get("estimated", False),
            latency_ms=body.get("latency_ms"),
        )
        self.send_json({"recorded": ok})

        # =================================================================
        # v2.0 Passive Observability: POST /daemon/restart (Safe Restart)
        # =================================================================

    def _post_daemon_restart(self, body):
        try:
            force = body.get("force", False)
            grace_seconds = body.get("grace_seconds", 60)

            check = storage.can_safely_restart()
            # Also check workflow
            if workflow_scheduler:
                wf = workflow_scheduler.get_active_workflow()
                if wf:
                    check["safe"] = False
                    check["blockers"].append({
                        "type": "active_workflow",
                        "phase": wf.get("current_phase", "unknown"),
                        "feature": wf.get("feature", "")
                    })

            if not check["safe"] and not force:
                # Not safe, return blockers
                self.send_json({
                    "restarted": False,
                    "reason": check["reason"],
                    "blockers": check["blockers"],
                    "hint": "Use force=true to override (dangerous)"
                }, 409)
                return

            if not check["safe"] and force:
                # Forced restart — broadcast warning first
                _bot_send(
                    "#general",
                    f"**FORCED RESTART** requested! Blocker reason: {check['reason']}. "
                    f"Save your work, shutdown in {grace_seconds}s.",
                    from_id="@system"
                )

            # Safe restart (or forced) — broadcast and schedule
            _bot_send(
                "#general",
                "🔄 **Daemon restart** in progress. Persisting DB...",
                from_id="@system"
            )

            # Persist DB before restart
            if _storage is not None:
                _storage.persist_to_disk()

            self.send_json({
                "restarted": True,
                "was_forced": force,
                "blockers": check["blockers"],
                "message": "DB persisted. Daemon shutting down. External process must restart."
            })

            # Schedule shutdown after response is sent
            def _delayed_shutdown():
                time.sleep(2)  # Let response reach client
                os._exit(0)  # Clean exit, external supervisor restarts

            threading.Thread(target=_delayed_shutdown, daemon=True).start()

        except Exception as e:
            self.send_json({"error": str(e)}, 500)


    # ----- Upload handlers (Idea #19: drag & drop files) -----

    def _sanitize_filename(self, name):
        """Sanitize filename: keep alphanum, dots, hyphens, underscores."""
        # Remove path separators
        name = name.replace("/", "_").replace("\\", "_")
        # Keep only safe characters
        safe = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
        # Collapse multiple underscores
        safe = re.sub(r'_+', '_', safe).strip('_')
        # Limit length
        if len(safe) > 100:
            ext = os.path.splitext(safe)[1]
            safe = safe[:96] + ext
        return safe or "unnamed"

    def _post_upload_dispatch(self):
        """Handle file upload via multipart/form-data."""
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))

        if length > UPLOAD_BODY_MAX:
            self.send_json({"error": f"Upload too large ({length} bytes, max body size {UPLOAD_BODY_MAX})"}, 413)
            return

        if "multipart/form-data" not in content_type:
            self.send_json({"error": "Expected multipart/form-data"}, 400)
            return

        # Extract boundary
        boundary = None
        for part in content_type.split(";"):
            p = part.strip()
            if p.startswith("boundary="):
                boundary = p.split("=", 1)[1].strip('"')
        if not boundary:
            self.send_json({"error": "Missing multipart boundary"}, 400)
            return

        raw = self.rfile.read(length)
        sep = ("--" + boundary).encode()
        parts = raw.split(sep)

        file_data = None
        file_name = None
        file_mime = None
        fields = {}

        for part in parts:
            if not part or part.strip() in (b"", b"--", b"--\r\n"):
                continue
            if b"\r\n\r\n" not in part:
                continue
            hdr_raw, body = part.split(b"\r\n\r\n", 1)
            if body.endswith(b"\r\n"):
                body = body[:-2]

            hdr_str = hdr_raw.decode("utf-8", errors="replace")
            name = None
            fname = None
            ct = "application/octet-stream"
            for line in hdr_str.split("\r\n"):
                lo = line.lower()
                if "content-disposition:" in lo:
                    if 'name="' in line:
                        name = line.split('name="')[1].split('"')[0]
                    if 'filename="' in line:
                        fname = line.split('filename="')[1].split('"')[0]
                if "content-type:" in lo:
                    ct = line.split(":", 1)[1].strip()

            if fname:
                file_data = body
                file_name = fname
                file_mime = ct
            elif name:
                fields[name] = body.decode("utf-8", errors="replace")

        if file_data is None:
            self.send_json({"error": "No file found in upload"}, 400)
            return

        if len(file_data) > UPLOAD_MAX_BYTES:
            self.send_json({"error": f"File too large ({len(file_data)} bytes, max {UPLOAD_MAX_BYTES})"}, 413)
            return

        # MIME validation
        if file_mime not in UPLOAD_ALLOWED_MIME:
            # Try to guess from filename
            guessed, _ = mimetypes.guess_type(file_name)
            if guessed and guessed in UPLOAD_ALLOWED_MIME:
                file_mime = guessed
            else:
                self.send_json({
                    "error": f"File type not allowed: {file_mime}",
                    "allowed": sorted(UPLOAD_ALLOWED_MIME)
                }, 415)
                return

        # Sanitize and save
        safe_name = self._sanitize_filename(file_name)
        file_id = str(uuid.uuid4())[:8]
        stored_name = f"{file_id}_{safe_name}"
        file_path = os.path.join(UPLOAD_DIR, stored_name)

        try:
            with open(file_path, "wb") as f:
                f.write(file_data)
        except Exception as e:
            self.send_json({"error": f"Failed to save file: {e}"}, 500)
            return

        # Build URL (served via GET /uploads/)
        file_url = f"/uploads/{stored_name}"
        file_size = len(file_data)
        room = fields.get("room", "#brainstorm")
        from_id = fields.get("from", transport.agent_id if transport else "@system")

        # Send chat message with file metadata
        content = f"[FILE:{file_url}|{file_mime}|{safe_name}|{file_size}]"

        _bot_send(room, content, from_id=from_id)

        self.send_json({
            "ok": True,
            "file_id": file_id,
            "url": file_url,
            "filename": safe_name,
            "mime": file_mime,
            "size": file_size,
            "room": room,
        }, 201)

    def _post_upload(self, body):
        """Fallback — real upload is handled by _post_upload_dispatch."""
        self.send_json({"error": "Upload requires multipart/form-data"}, 400)

    def _get_upload_file(self, parsed, params):
        """Serve uploaded files from /uploads/ directory."""
        # Extract filename from path: /uploads/xxxx_name.ext
        filename = parsed.path.replace("/uploads/", "", 1)
        if not filename or ".." in filename or "/" in filename:
            self.send_json({"error": "Invalid filename"}, 400)
            return

        file_path = os.path.join(UPLOAD_DIR, filename)
        if not os.path.isfile(file_path):
            self.send_json({"error": "File not found"}, 404)
            return

        # Resolve MIME type
        mime, _ = mimetypes.guess_type(filename)
        if not mime:
            mime = "application/octet-stream"

        try:
            with open(file_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_json({"error": f"Failed to read file: {e}"}, 500)

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


def load_identity():
    """Load identity from ~/.aircp/identity.toml"""
    import tomllib
    from pathlib import Path

    config_path = Path.home() / ".aircp" / "identity.toml"
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                config = tomllib.load(f)
            return config.get("identity", {}).get("nickname", "@anonymous")
        except Exception:
            pass
    return "@anonymous"


def main():
    global transport, autonomy, storage, workflow_scheduler, bridge, tip_system
    global _daemon_start_time, _watchdog_threads

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    default_nick = load_identity()

    parser = argparse.ArgumentParser(description="AIRCP Daemon")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--agent-id", default=default_nick)
    parser.add_argument("--auth-token", action="append", default=[],
                        help="HTTP Bearer token (repeatable)")
    parser.add_argument("--tokens-file",
                        help="Path to tokens file (JSON or text list)")
    parser.add_argument("--allow-no-auth", action="store_true",
                        help="Allow HTTP without auth token (local dev only)")
    args = parser.parse_args()

    _configure_http_auth(args)

    # Create persistent transport (domain 219 = aircp, FNV-1a hash)
    transport = AIRCPTransport(args.agent_id, domain_id=219)
    transport.join_room("#general")
    joined_rooms.add("#general")

    # License check (fail-open: community mode if no key)
    from license import load_license
    _license = load_license()
    print(f"License: {'enterprise (' + _license.org + ')' if _license.is_enterprise else 'community'}")

    # v4.4: Record daemon start time for /health uptime
    _daemon_start_time = time.time()

    # v0.2: Initialize autonomy state
    autonomy = AutonomyState(activity_log_dir=Path("logs/activity"))
    autonomy.on_state_change(broadcast_autonomy_event)
    autonomy.start_cleanup_thread()  # Thread-based for sync daemon
    print("v0.2 Autonomy enabled")
    print("v0.4 Auto-Dispatcher enabled")
    print("v0.5 MODES enabled")

    # v0.6/v0.7: Initialize storage for TaskManager (RAM mode with disk persistence)
    global _storage
    _storage = AIRCPStorage()  # Defaults to /dev/shm with disk backup
    storage = _storage  # Local alias for existing code
    print("v0.7 TaskManager enabled (RAM storage: /dev/shm)")

    # v3.0: Rebuild FTS5 index from existing messages
    storage.rebuild_fts()

    # v4.5: Backfill messages table from JSONL memory files (first run only)
    backfilled = _backfill_messages_from_jsonl()
    if backfilled > 0:
        storage.rebuild_fts()  # Re-index after backfill
    print("v3.0 Memory API enabled (FTS5 index rebuilt)")

    # Compactor v3: GC thread — hard-deletes soft-deleted messages after 7 days
    def _gc_loop(storage_ref, interval_hours=6, retention_days=7):
        """Periodic GC: purge compacted messages older than retention_days."""
        while True:
            time.sleep(interval_hours * 3600)
            try:
                purged = storage_ref.gc_compacted(retention_days)
                if purged > 0:
                    logger.info(f"[GC] Purged {purged} compacted messages (>{retention_days}d)")
            except Exception as e:
                logger.error(f"[GC] Error: {e}")

    _gc_thread = threading.Thread(
        target=_gc_loop, args=(storage,), kwargs={"interval_hours": 6, "retention_days": 7},
        daemon=True, name="compactor-gc"
    )
    _gc_thread.start()
    print("Compactor v3 GC enabled (6h interval, 7d retention)")

    # Register shutdown handlers to persist DB on exit
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    print("Shutdown handlers registered (SIGTERM/SIGINT → persist DB)")

    # v4.1: Load agent profiles for adaptive timers
    load_agent_profiles()

    # v1.0: Load brainstorm config
    load_brainstorm_config()
    print("v1.0 Brainstorm System enabled")

    # v1.3: Initialize workflow scheduler (uses same DB as storage)
    workflow_scheduler = WorkflowScheduler(storage.db_path)
    print("v1.3 Workflow Scheduler enabled")

    # v2.1: Reserved channels (#claims, #locks, #presence, #system, #activity) removed
    # Data accessible via SQLite storage + dashboard APIs. Only #general + #brainstorm active.
    print("  v2.1: Reserved channels disabled (cleanup)")

    print(f"AIRCP Daemon started")
    print(f"Agent ID: {args.agent_id}")
    print(f"Listening on http://localhost:{args.port}")
    print(f"Rooms: {list(joined_rooms)}")

    # Wait for initial discovery
    time.sleep(2)

    # Start background message polling
    poller = threading.Thread(target=poll_messages, daemon=True)
    poller.start()
    print("Message polling started")

    # v0.6/v0.7: Start task watchdog (with anti-spam)
    watchdog = threading.Thread(target=task_watchdog, daemon=True)
    watchdog.start()
    print("Task watchdog started (anti-spam enabled)")

    # v0.9: Start presence watchdog (agent heartbeat monitoring)
    presence_thread = threading.Thread(target=presence_watchdog, daemon=True)
    presence_thread.start()
    print("Agent presence watchdog started (v0.9)")

    # v1.0: Start brainstorm watchdog
    brainstorm_thread = threading.Thread(target=brainstorm_watchdog, daemon=True)
    brainstorm_thread.start()
    print("Brainstorm watchdog started (v1.0)")

    # v1.3: Start workflow watchdog
    workflow_thread = threading.Thread(target=workflow_watchdog, daemon=True)
    workflow_thread.start()
    print("Workflow watchdog started (v1.3)")

    # v1.5: Start review watchdog
    review_thread = threading.Thread(target=review_watchdog, daemon=True)
    review_thread.start()
    print("Review watchdog started (v1.5)")

    # v1.6: Start tips contextuels system (config from aircp-config.toml [tips])
    tip_system = TipSystem()  # Reads interval from TOML config
    tips_thread = threading.Thread(target=tips_watchdog, daemon=True)
    tips_thread.start()
    print(f"Tips contextuels started (v1.6 - interval {tip_system.interval}s, channel {tip_system.channel})")

    # v3.0: Start memory retention loop (cleanup >30 days, runs daily)
    retention_thread = threading.Thread(target=memory_retention_loop, daemon=True)
    retention_thread.start()
    print("Memory retention started (v3.0 - 30 day cleanup)")

    # v3.1: Periodic DB backup (RAM → disk every 5 min, crash protection)
    backup_thread = threading.Thread(target=db_backup_loop, daemon=True)
    backup_thread.start()
    print(f"DB backup started (v3.1 - every {DB_BACKUP_INTERVAL}s)")

    # v4.4: Collect watchdog thread references for /health endpoint
    _watchdog_threads = {
        "task": watchdog,
        "presence": presence_thread,
        "brainstorm": brainstorm_thread,
        "workflow": workflow_thread,
        "review": review_thread,
        "tips": tips_thread,
    }
    print(f"Health check ready ({len(_watchdog_threads)} watchdogs tracked)")

    # Join brainstorm channel
    brainstorm_channel = get_brainstorm_config().get("channel", "#brainstorm")
    try:
        if transport.join_room(brainstorm_channel):
            joined_rooms.add(brainstorm_channel)
            print(f"  Joined {brainstorm_channel}")
    except Exception as e:
        print(f"  Failed to join {brainstorm_channel}: {e}")

    # v3.0: Dashboard bridge — publishes state to DDS topics
    cmd_handler = create_command_handler(autonomy, transport, joined_rooms=joined_rooms, workflow_scheduler=workflow_scheduler)
    # v4.3: Adaptive threshold resolver for dashboard bridge
    def _threshold_resolver(kind, agent_id):
        if kind == "dead":
            return get_agent_dead_seconds(agent_id)
        return get_agent_away_seconds(agent_id)
    bridge = DashboardBridge(transport, autonomy=autonomy, storage=storage, workflow_scheduler=workflow_scheduler, command_handler=cmd_handler, threshold_resolver=_threshold_resolver)
    bridge.start()
    print("v3.0 Dashboard bridge enabled (HDDS topics: presence, tasks, workflows, mode, commands)")

    # v4.0: Initialize Telegram notification bridge
    from notifications.telegram import TelegramNotifier
    _telegram = TelegramNotifier()
    print(f"v4.0 Telegram bridge {'enabled' if _telegram.enabled else 'disabled'}")

    print(f"HTTP router: {len(AircpHandler.GET_ROUTES)} GET + {len(AircpHandler.POST_ROUTES)} POST routes")

    server = ThreadingHTTPServer(("localhost", args.port), AircpHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        _telegram.shutdown()
        autonomy.stop_cleanup_task()
        transport.close()


if __name__ == "__main__":
    main()
