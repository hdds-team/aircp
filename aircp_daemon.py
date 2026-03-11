#!/usr/bin/env python3
"""AIRCP Daemon - Persistent HDDS bridge + HTTP API.

Modules:
    handlers/       HTTP route handlers (Phase 1)
    watchdogs.py    Background watchdog threads (Phase 2)
    chat_triggers.py  DDS message polling + chat commands (Phase 3)
    daemon_config.py  Timer/threshold constants (Phase 4)
    tip_system.py     TipSystem class + tips (Phase 4)

Usage:  python aircp_daemon.py [--port 5555] [--agent-id @name] [--auth-token TOKEN]
"""

import sys
# Prevent double-import: when run as __main__, ensure 'from aircp_daemon import X'
# resolves to THIS module instance (not a second copy with storage=None).
sys.modules.setdefault("aircp_daemon", sys.modules[__name__])

import os
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
from workflow_scheduler import WorkflowScheduler
from dashboard_bridge import DashboardBridge, create_command_handler
from app_context import AppContext

# v4.0: Telegram notification bridge
from notifications.telegram import telegram_notify, TelegramNotifier

# v4.1: Git hooks for workflow transitions
import git_hooks

# =============================================================================
# Global storage reference (for signal handler)
# =============================================================================
_storage: AIRCPStorage = None

# Phase 4: Config constants moved to daemon_config.py
from daemon_config import (
    BRAINSTORM_CONFIG_PATH,
    COMPACT_AUTO_THRESHOLD, COMPACT_AUTO_INTERVAL,
    TASK_STALE_SECONDS, TASK_WATCHDOG_INTERVAL, TASK_MIN_PING_INTERVAL, TASK_MAX_PINGS,
    TASK_LEAD_WAKEUP_PINGS, TASK_LEAD_ID, TASK_LEAD_STALE_MINUTES,
    TASK_PENDING_WARN_SECONDS, TASK_PENDING_ESCALATE_SECONDS,
    TASK_PENDING_MAX_PINGS, TASK_PENDING_MIN_PING_INTERVAL,
    AGENT_AWAY_SECONDS, AGENT_DEAD_SECONDS, AGENT_HEARTBEAT_CHECK_INTERVAL,
    BRAINSTORM_WATCHDOG_INTERVAL, BRAINSTORM_REMINDER_INTERVAL, BRAINSTORM_MAX_REMINDERS,
    HUMAN_AGENTS,
    WORKFLOW_WATCHDOG_INTERVAL,
    REVIEW_WATCHDOG_INTERVAL, REVIEW_TIMEOUT_SECONDS,
    REVIEW_PING_DELAY, REVIEW_PING_INTERVAL, REVIEW_PING_MAX, REVIEW_ESCALATE_SECONDS,
    UPLOAD_DIR, UPLOAD_MAX_BYTES, UPLOAD_BODY_MAX, UPLOAD_ALLOWED_MIME,
    HUMAN_SENDERS, DISPATCH_RULES, _FR_STOPWORDS, _FR_THRESHOLD,
    ALLOWED_ORIGINS,
)

# Compact engine: mutable state (stays here)
_compact_msg_counter = {}  # room -> message count since last compact
_last_compact_time = {}    # room -> last compact timestamp
_compact_lock = threading.Lock()


def _normalize_ts(ts) -> float:
    """Normalize a timestamp to float seconds since epoch.

    Handles mixed types found in envelopes:
    - int nanoseconds (>1e18, from time.time_ns())
    - int microseconds (>1e15)
    - int milliseconds (>1e12)
    - int/float seconds
    - str ISO8601 (from datetime.isoformat())
    - str SQLite format (from _sqlite_now(), "YYYY-MM-DD HH:MM:SS")

    Thresholds aligned with compact_engine._parse_timestamp().
    """
    if isinstance(ts, (int, float)):
        if ts > 1e18:
            return ts / 1e9   # nanoseconds
        if ts > 1e15:
            return ts / 1e6   # microseconds
        if ts > 1e12:
            return ts / 1e3   # milliseconds
        return float(ts)      # seconds
    if isinstance(ts, str) and ts:
        try:
            from datetime import datetime, timezone
            clean = ts.replace('Z', '+00:00')
            if 'T' in clean:
                dt = datetime.fromisoformat(clean)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            # SQLite format: "YYYY-MM-DD HH:MM:SS" (assume UTC)
            return datetime.strptime(clean, '%Y-%m-%d %H:%M:%S').replace(
                tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            pass
    return 0.0


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
            "timestamp": _normalize_ts(env.get("ts", 0)),
            "room": room,
        })
    return messages


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

    # Close DB (WAL checkpoint + close connection)
    print("[SHUTDOWN] Closing database...")
    if _storage is not None:
        _storage.close()
        print("[SHUTDOWN] Database closed")

    print("[SHUTDOWN] Goodbye!")
    sys.exit(0)


os.makedirs(UPLOAD_DIR, exist_ok=True)


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


# Mutable state (stays here, constants moved to daemon_config.py)
brainstorm_reminder_state = {}     # {session_id: {"count": int, "last_sent": float}}
brainstorm_config = None           # Loaded at startup
workflow_scheduler: WorkflowScheduler = None
review_reminder_state = {}         # {request_id: {"count": int, "last_sent": float, "escalated": bool}}

# Phase 4: TipSystem class + tip constants moved to tip_system.py
from tip_system import TipSystem
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

# Phase 2: Watchdog functions moved to watchdogs.py
# Phase 3: Chat triggers (poll_messages, parse/process pairs) moved to chat_triggers.py


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

    @property
    def ctx(self):
        """Phase 0: access AppContext via server reference."""
        return self.server.ctx

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
    # Phase 1: Function-based routes (populated by init_routes)
    # =================================================================
    _get_routes = {}
    _post_routes = {}
    _get_prefix_routes = []
    _post_raw_routes = {}  # Raw body routes (multipart uploads)

    @classmethod
    def init_routes(cls):
        """Load extracted handler modules and merge route tables."""
        from handlers import collect_routes
        cls._get_routes, cls._post_routes, cls._get_prefix_routes, cls._post_raw_routes = collect_routes()

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

        # Function-based dispatch (extracted handlers)
        handler_fn = self._get_routes.get(parsed.path)
        if handler_fn:
            handler_fn(self, parsed, params)
            return
        for prefix, handler_fn in self._get_prefix_routes:
            if parsed.path.startswith(prefix):
                handler_fn(self, parsed, params)
                return

        self.send_json({"error": "Not found"}, 404)

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

        # Raw routes (multipart uploads — no JSON body parsing)
        raw_fn = self._post_raw_routes.get(self.path)
        if raw_fn:
            if not _check_quota(self.path, "POST"):
                self.send_json({"error": "Quota exceeded"}, 429)
                return
            raw_fn(self)
            return

        body = self._parse_body()
        if body is None:
            return

        # v2.0: Passive activity inference hook (fire-and-forget)
        self._infer_activity(self.path, body)

        if not _check_quota(self.path, "POST"):
            self.send_json({"error": "Quota exceeded"}, 429)
            return

        # Function-based dispatch (extracted handlers)
        handler_fn = self._post_routes.get(self.path)
        if handler_fn:
            handler_fn(self, body)
            return

        self.send_json({"error": "Not found"}, 404)


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
    _storage = AIRCPStorage()  # Disk-based SQLite with WAL mode
    storage = _storage  # Local alias for existing code
    print("v0.7 TaskManager enabled (disk SQLite + WAL)")

    # v3.0: Rebuild FTS5 index from existing messages
    storage.rebuild_fts()

    # v4.5: Backfill messages table from JSONL memory files (first run only)
    backfilled = _backfill_messages_from_jsonl()
    if backfilled > 0:
        storage.rebuild_fts()  # Re-index after backfill
    print("v3.0 Memory API enabled (FTS5 index rebuilt)")

    # Register shutdown handlers to persist DB on exit
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    print("Shutdown handlers registered (SIGTERM/SIGINT)")

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

    # Phase 3: Start background message polling (extracted to chat_triggers.py)
    from chat_triggers import poll_messages
    poller = threading.Thread(target=poll_messages, daemon=True)
    poller.start()
    print("Message polling started")

    # v1.6: Initialize tips system (config from aircp-config.toml [tips])
    tip_system = TipSystem()  # Reads interval from TOML config
    print(f"Tips contextuels started (v1.6 - interval {tip_system.interval}s, channel {tip_system.channel})")

    # Phase 2: Start all watchdog threads (extracted to watchdogs.py)
    from watchdogs import start_watchdogs
    _watchdog_threads = start_watchdogs(storage)

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

    # Load extracted handler modules (all routes now function-based)
    AircpHandler.init_routes()
    _n_get = len(AircpHandler._get_routes)
    _n_post = len(AircpHandler._post_routes)
    _n_prefix = len(AircpHandler._get_prefix_routes)
    _n_raw = len(AircpHandler._post_raw_routes)
    print(f"HTTP router: {_n_get} GET + {_n_post} POST + {_n_raw} raw POST + {_n_prefix} prefix routes")

    # Phase 0: Build AppContext — single source of truth for daemon state.
    # Globals remain in parallel (dual mode) until Phase 5 migration completes.
    ctx = AppContext(
        transport=transport,
        storage=storage,
        autonomy=autonomy,
        workflow_scheduler=workflow_scheduler,
        bridge=bridge,
        tip_system=tip_system,
        license=_license,
        joined_rooms=joined_rooms,
        message_history=message_history,
        agent_profiles=agent_profiles,
        brainstorm_config=brainstorm_config or {},
        compact_msg_counter=_compact_msg_counter,
        last_compact_time=_last_compact_time,
        compact_lock=_compact_lock,
        review_reminder_state=review_reminder_state,
        brainstorm_reminder_state=brainstorm_reminder_state,
        watchdog_threads=_watchdog_threads,
        daemon_start_time=_daemon_start_time,
    )

    server = ThreadingHTTPServer(("localhost", args.port), AircpHandler)
    server.ctx = ctx
    print("Phase 0: AppContext attached to server (dual mode)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        _telegram.shutdown()
        autonomy.stop_cleanup_task()
        transport.close()


if __name__ == "__main__":
    main()
