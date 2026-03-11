"""
AIRCP Persistent Agent - Base class for agents with memory.

An agent that:
- Has a stable personality (SOUL.md)
- Remembers conversations (MEMORY/)
- Responds when mentioned (@agent_id)
- Runs via periodic heartbeat
- Works autonomously on assigned tasks (TaskWorkerMixin)
"""

from abc import ABC, abstractmethod
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
import json
import re
import tomllib
import logging
import os
import sys
import time
from typing import Dict, Any

# Add transport to path
if str(Path(__file__).parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent.parent))

from transport.hdds import AIRCPTransport, AIRCPMessage
from agents.task_worker_mixin import TaskWorkerMixin
from recreational import RecreationalConfig, RecreationalMode

logger = logging.getLogger(__name__)


def _aircp_auth_token() -> str | None:
    token = os.environ.get("AIRCP_AUTH_TOKEN", "").strip()
    if token:
        return token
    tokens = [t.strip() for t in os.environ.get("AIRCP_AUTH_TOKENS", "").split(",") if t.strip()]
    return tokens[0] if tokens else None


def _apply_aircp_auth_header(req) -> None:
    token = _aircp_auth_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")


@dataclass
class AgentConfig:
    """Parsed agent configuration."""
    id: str
    rooms: list[str]
    llm_provider: str
    llm_model: str
    llm_api_key: str | None
    llm_base_url: str | None  # For OpenAI-compatible APIs
    llm_max_tokens: int
    domain_id: int
    respond_to_mentions: bool
    respond_to_all: bool
    max_context_messages: int
    memory_hours: int
    cooldown_seconds: float = 7.5  # Rate limit: min seconds between responses
    capabilities: list[dict] = None  # Tenuo capabilities
    skip_permissions: bool = False  # Skip Claude CLI confirmations (trusted agents only)
    # Configurable timeouts (for local LLMs with variable inference speed)
    timeout_base: float = 120.0    # Base timeout in seconds
    timeout_per_msg: float = 4.0   # Additional seconds per message in context
    timeout_max: float = 600.0     # Hard cap in seconds
    project: str = "default"       # Active project (synced from daemon)
    recreational_raw: dict = None   # Raw [recreational] TOML section


class PersistentAgent(TaskWorkerMixin, ABC):
    """
    Base class for persistent agents with memory.

    Subclasses must implement generate_response().
    Subclasses MAY override _execute_task_step() to enable autonomous task work.
    """

    def __init__(self, config_dir: Path):
        """
        Initialize the agent.

        Args:
            config_dir: Path to agent config directory containing:
                - config.toml
                - SOUL.md
                - MEMORY/state.json
                - MEMORY/conversations/
        """
        self.config_dir = Path(config_dir)

        # Load configuration
        self.config = self._load_config()
        self.soul = self._load_soul()
        self.state = self._load_state()

        # Agent identity
        self.agent_id = f"@{self.config.id}"
        self.rooms = self.config.rooms

        # Rate limiting: track last response time per room
        self.last_response_time: dict[str, float] = {}

        # Track message IDs we've already processed (prevent double-response)
        self.processed_message_ids: set[str] = set()

        # Initialize transport
        self.transport = AIRCPTransport(
            self.agent_id,
            domain_id=self.config.domain_id
        )

        # Join configured rooms
        for room in self.rooms:
            if self.transport.join_room(room):
                logger.info(f"Joined {room}")
            else:
                logger.warning(f"Failed to join {room}")

        # Sync active project from daemon
        self._fetch_project()
        logger.info(f"Active project: {self.config.project}")

        # Initialize recreational mode (IDEA #17 / WF#9)
        rec_config = RecreationalConfig.from_toml(self.config.recreational_raw or {})
        rec_state_dict = self.state.get("recreational", {})
        self.recreational = RecreationalMode(rec_config, self.agent_id, rec_state_dict)
        if rec_config.enabled:
            logger.info(f"Recreational mode: enabled (idle threshold: {rec_config.idle_threshold_cycles} cycles)")

    def _load_config(self) -> AgentConfig:
        """Load and parse config.toml."""
        config_path = self.config_dir / "config.toml"

        if not config_path.exists():
            raise FileNotFoundError(f"Config not found: {config_path}")

        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

        agent = raw.get("agent", {})
        llm = raw.get("llm", {})
        hdds = raw.get("hdds", {})
        behavior = raw.get("behavior", {})
        capabilities = raw.get("capabilities", [])
        timeout = raw.get("timeout", {})

        # API key from config or environment
        api_key = llm.get("api_key")
        if api_key and api_key.startswith("${") and api_key.endswith("}"):
            env_var = api_key[2:-1]
            api_key = os.environ.get(env_var)

        # Base URL from config or environment
        base_url = llm.get("base_url")
        if base_url and base_url.startswith("${") and base_url.endswith("}"):
            env_var = base_url[2:-1]
            base_url = os.environ.get(env_var)

        recreational = raw.get("recreational", {})

        return AgentConfig(
            id=agent.get("id", "agent"),
            rooms=agent.get("rooms", ["#general"]),
            llm_provider=llm.get("provider", "anthropic"),
            llm_model=llm.get("model", "claude-sonnet-4-20250514"),
            llm_api_key=api_key,
            llm_base_url=base_url,
            llm_max_tokens=llm.get("max_tokens", 1024),
            domain_id=hdds.get("domain_id", 219),
            respond_to_mentions=behavior.get("respond_to_mentions", True),
            respond_to_all=behavior.get("respond_to_all", False),
            max_context_messages=behavior.get("max_context_messages", 50),
            memory_hours=behavior.get("memory_hours", 24),
            cooldown_seconds=behavior.get("cooldown_seconds", 7.5),
            capabilities=capabilities,
            skip_permissions=llm.get("skip_permissions", False),
            timeout_base=float(timeout.get("base", 120.0)),
            timeout_per_msg=float(timeout.get("per_msg", 4.0)),
            timeout_max=float(timeout.get("max", 600.0)),
            recreational_raw=recreational,
        )

    def _load_soul(self) -> str:
        """Load SOUL.md as the system prompt, with capabilities FIRST."""
        from capabilities import format_capabilities_for_prompt

        soul_path = self.config_dir / "SOUL.md"

        if not soul_path.exists():
            logger.warning(f"SOUL.md not found at {soul_path}, using default")
            base_soul = f"You are {self.config.id}, a helpful assistant."
        else:
            base_soul = soul_path.read_text(encoding="utf-8")

        # Capabilities section FIRST (Tenuo integration) - takes priority
        caps_section = format_capabilities_for_prompt(self.config.capabilities or [])

        return f"{caps_section}\n\n{base_soul}"

    def _load_state(self) -> dict:
        """Load state.json or create default."""
        state_path = self.config_dir / "MEMORY" / "state.json"

        if state_path.exists():
            try:
                return json.loads(state_path.read_text())
            except json.JSONDecodeError:
                logger.warning("Invalid state.json, using default")

        return {"last_seen": {}, "total_sent": 0}

    def _save_state(self):
        """Save state.json."""
        state_path = self.config_dir / "MEMORY" / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(self.state, indent=2))

    def _append_memory(self, messages: list[AIRCPMessage]):
        """
        Append messages to the daily JSONL file.

        Format: MEMORY/conversations/YYYY-MM-DD.jsonl
        """
        if not messages:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        memory_dir = self.config_dir / "MEMORY" / "conversations"
        memory_dir.mkdir(parents=True, exist_ok=True)
        memory_file = memory_dir / f"{today}.jsonl"

        with open(memory_file, "a", encoding="utf-8") as f:
            for msg in messages:
                entry = {
                    "ts": msg.timestamp_ns,
                    "room": msg.room,
                    "from": msg.from_id,
                    "kind": msg.kind.name,
                    "payload": msg.payload,
                    "room_seq": msg.room_seq,
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _load_recent_memory(
        self,
        hours: int = 24,
        limit: int = 50,
        room: str | None = None
    ) -> list[dict]:
        """
        Load recent messages from MEMORY/conversations/.

        Args:
            hours: How many hours back to look
            limit: Maximum messages to return
            room: Filter by room (optional)

        Returns:
            List of message dicts in chronological order
        """
        memory_dir = self.config_dir / "MEMORY" / "conversations"

        if not memory_dir.exists():
            return []

        # Calculate date range
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        cutoff_ns = int(cutoff.timestamp() * 1_000_000_000)

        # Find relevant files (today and yesterday should cover 24h)
        messages = []

        for days_back in range(max(1, hours // 24) + 1):
            date = now - timedelta(days=days_back)
            file_path = memory_dir / f"{date.strftime('%Y-%m-%d')}.jsonl"

            if not file_path.exists():
                continue

            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        entry = json.loads(line)

                        # Filter by time
                        if entry.get("ts", 0) < cutoff_ns:
                            continue

                        # Filter by room if specified
                        if room and entry.get("room") != room:
                            continue

                        messages.append(entry)
                    except json.JSONDecodeError:
                        continue

        # Sort by timestamp and limit
        messages.sort(key=lambda m: m.get("ts", 0))
        return messages[-limit:]

    def _save_own_response(self, room: str, content: str):
        """Save our own response to memory."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        memory_dir = self.config_dir / "MEMORY" / "conversations"
        memory_dir.mkdir(parents=True, exist_ok=True)
        memory_file = memory_dir / f"{today}.jsonl"

        entry = {
            "ts": time.time_ns(),
            "room": room,
            "from": self.agent_id,
            "kind": "CHAT",
            "project": self.config.project,
            "payload": {"role": "assistant", "content": content},
            "room_seq": 0,
        }

        with open(memory_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _is_globally_muted(self) -> bool:
        """Check if STFU mode is active (daemon-level global mute)."""
        try:
            import urllib.request
            import json as _json
            req = urllib.request.Request("http://localhost:5555/mute-status")
            _apply_aircp_auth_header(req)
            from aircp_http import safe_urlopen
            with safe_urlopen(req, timeout=2) as resp:
                data = _json.loads(resp.read())
                return data.get("muted", False)
        except Exception:
            return False  # If daemon unreachable, don't mute

    def _is_mentioned(self, message: AIRCPMessage) -> bool:
        """Check if the agent is mentioned in the message."""
        content = message.payload.get("content", "")

        # Check for @all (broadcast to everyone)
        # @naskel (human) and system bots can trigger @all
        # System bots are trusted — they send notifications for workflows,
        # brainstorms, reviews, tasks, etc. Without this, agents ignore
        # automated @all notifications and everything stagnates.
        TRUSTED_ALL_SENDERS = {
            "@naskel", "@idea", "@workflow", "@brainstorm",
            "@review", "@taskman", "@watchdog", "@tips",
        }
        sender = message.payload.get("from", message.from_id or "")
        if "@all" in content and sender in TRUSTED_ALL_SENDERS:
            return True

        # Check for @agent_id or just agent_id
        if self.agent_id in content:
            return True
        if self.config.id in content:
            return True

        return False

    def _build_context(self, room: str) -> list[dict]:
        """
        Build conversation context for the LLM.

        Returns:
            List of messages in OpenAI/Anthropic format:
            [{"role": "system"|"user"|"assistant", "content": "..."}]
        """
        context = []

        # System prompt from SOUL.md + current date/time
        now = datetime.now(timezone.utc)
        date_line = f"Current date: {now.strftime('%Y-%m-%d %H:%M UTC')}"

        # Room-specific instructions (injected AFTER soul for higher priority)
        room_hint = ""
        if room == "#brainstorm":
            room_hint = (
                "\n\n--- BRAINSTORM ECO MODE ---\n"
                "You are in #brainstorm. STRICT RULES:\n"
                "- ENGLISH ONLY. No French.\n"
                "- MAX 2-3 sentences. Be ultra-concise.\n"
                "- One idea per message. No preamble, no filler.\n"
                "- Vote format: YES/NO + one-line reason.\n"
                "- Save tokens: skip greetings, acknowledgments, repetition.\n"
                "--- END ECO MODE ---"
            )

        context.append({
            "role": "system",
            "content": f"{date_line}\n\n{self.soul}{room_hint}"
        })

        # Load recent memory for this room
        recent = self._load_recent_memory(
            hours=self.config.memory_hours,
            limit=self.config.max_context_messages,
            room=room
        )

        # Convert to chat format
        for mem in recent:
            from_id = mem.get("from", "unknown")
            payload = mem.get("payload", {})
            content = payload.get("content", "")

            if not content:
                continue

            # Determine role
            if from_id == self.agent_id:
                role = "assistant"
                formatted = content
            else:
                role = "user"
                formatted = f"[{from_id}]: {content}"

            context.append({"role": role, "content": formatted})

        return context

    @abstractmethod
    async def generate_response(
        self,
        context: list[dict],
        new_message: dict
    ) -> str:
        """
        Generate a response to a message.

        Args:
            context: Conversation history in chat format
            new_message: The new message payload to respond to

        Returns:
            Response text

        Must be implemented by subclasses.
        """
        pass

    async def _execute_task_step(
        self,
        task: Dict[str, Any],
        step: int
    ) -> Dict[str, Any]:
        """
        Default implementation: task worker disabled for this agent type.

        Subclasses that support autonomous task execution should override this.
        The default behavior logs a warning and skips the task without error,
        allowing the heartbeat to continue normally.

        Args:
            task: Task dict from TaskManager
            step: Current step number

        Returns:
            Dict with keys matching TaskWorkerMixin contract:
            - done: bool - False (don't mark as completed)
            - next_step: int | None - None (don't advance, just skip)
            - error: str | None - None (no error)
            - result: Any - None (no result)
        """
        logger.warning(
            f"Task work not implemented for {self.__class__.__name__}, "
            f"skipping task {task.get('id')}"
        )
        # Return dict matching the mixin contract (NOT a tuple!)
        return {
            "done": False,      # Don't mark as completed
            "next_step": step,  # Stay at same step (skip without advancing)
            "error": None,      # No error
            "result": None      # No result
        }

    def _fetch_project(self) -> str:
        """Fetch active project from daemon. Caches in self.config.project."""
        try:
            import urllib.request
            import json as _json

            url = f"http://localhost:5555/agent/project?agent_id={self.agent_id}"
            req = urllib.request.Request(url, method="GET")
            _apply_aircp_auth_header(req)
            from aircp_http import safe_urlopen
            with safe_urlopen(req, timeout=5) as resp:
                result = _json.loads(resp.read())
                project = result.get("project_id", "default") or "default"
                if project != self.config.project:
                    logger.info(f"Project switched: {self.config.project} -> {project}")
                self.config.project = project
                return project
        except Exception as e:
            logger.debug(f"Failed to fetch project: {e}")
            return self.config.project

    def _report_usage(self, prompt_tokens=None, completion_tokens=None,
                      estimated=False, latency_ms=None):
        """Fire-and-forget POST to daemon to record LLM usage."""
        try:
            import urllib.request
            import json as _json

            data = _json.dumps({
                "agent_id": self.agent_id,
                "provider": self.config.llm_provider,
                "model": self.config.llm_model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "estimated": estimated,
                "latency_ms": latency_ms,
            }).encode()

            req = urllib.request.Request(
                "http://localhost:5555/usage/report",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            _apply_aircp_auth_header(req)

            from aircp_http import safe_urlopen
            with safe_urlopen(req, timeout=3) as resp:
                resp.read()
            logger.debug(f"Usage reported: prompt={prompt_tokens} completion={completion_tokens}")
        except Exception as e:
            logger.debug(f"Failed to report usage: {e}")

    def _send_presence_heartbeat(self, status: str = "idle", current_task: str = None):
        """Send heartbeat to daemon for presence tracking (v0.9)."""
        try:
            import urllib.request
            import json as _json

            data = _json.dumps({
                "agent_id": self.agent_id,
                "status": status,
                "current_task": current_task
            }).encode()

            req = urllib.request.Request(
                "http://localhost:5555/agent/heartbeat",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            _apply_aircp_auth_header(req)

            from aircp_http import safe_urlopen
            with safe_urlopen(req, timeout=5) as resp:
                result = _json.loads(resp.read())
                logger.debug(f"Presence heartbeat: {result.get('status')}")
                return True
        except Exception as e:
            logger.warning(f"Failed to send presence heartbeat: {e}")
            return False

    async def heartbeat(self):
        """
        Main loop - process new messages, respond if mentioned, and work on tasks.

        Called periodically (e.g., by cron or systemd timer).
        """
        # v2.1: Send presence heartbeat with computed status (fix: was hardcoded "working")
        # Only report "working" if agent actually has an active task
        heartbeat_status = "idle"
        current_task_desc = None
        try:
            active_tasks = [t for t in self.state.get("tasks", []) if t.get("status") == "in_progress"]
            if active_tasks:
                heartbeat_status = "working"
                current_task_desc = active_tasks[0].get("description", "")[:100]
        except Exception:
            pass  # Safe fallback to idle
        self._send_presence_heartbeat(status=heartbeat_status, current_task=current_task_desc)

        # Sync active project from daemon (tracks operator switches)
        self._fetch_project()

        # Track if we did real work this cycle (for recreational mode)
        had_messages_this_cycle = False

        for room in self.rooms:
            # Get new messages
            messages = self.transport.receive_new(room)

            if not messages:
                continue

            # Filter out already-seen messages
            last_seen_ts = self.state["last_seen"].get(room, 0)
            messages = [m for m in messages if m.timestamp_ns > last_seen_ts]

            if not messages:
                continue

            logger.info(f"[{room}] Received {len(messages)} new messages")
            had_messages_this_cycle = True

            # Store ALL messages to memory
            self._append_memory(messages)

            # Process messages we should respond to
            had_rate_limited = False
            for msg in messages:
                logger.debug(f"[{room}] Message from {msg.from_id}: {msg.payload}")

                # Skip our own messages (multiple format checks to be safe)
                sender = msg.from_id or ""
                sender_normalized = sender.lstrip("@").lower()
                my_id_normalized = self.config.id.lower()

                if sender_normalized == my_id_normalized:
                    logger.debug(f"[{room}] Skipping own message (from: {sender})")
                    continue

                # Extra safety: skip if message mentions us AND is from us (self-reference loop)
                content = msg.payload.get("content", "")
                if sender_normalized == my_id_normalized and (self.agent_id in content or self.config.id in content):
                    logger.warning(f"[{room}] LOOP DETECTED - skipping self-referential message")
                    continue

                # Skip if we already processed this message ID (prevent double-response)
                if msg.id in self.processed_message_ids:
                    logger.debug(f"[{room}] Already processed message {msg.id}")
                    continue


                # Check if we should respond
                if not self.config.respond_to_mentions:
                    logger.debug(f"[{room}] respond_to_mentions disabled")
                    continue

                if not self.config.respond_to_all and not self._is_mentioned(msg):
                    logger.debug(f"[{room}] Not mentioned in: {msg.payload.get('content', '')[:50]}")
                    continue

                logger.info(f"[{room}] Mentioned by {msg.from_id}")

                # STFU mode check: skip if globally muted
                if self._is_globally_muted():
                    logger.info(f"[{room}] STFU mode active - staying silent")
                    continue

                # Rate limit check: skip if cooldown not elapsed
                now = time.time()
                last_response = self.last_response_time.get(room, 0)
                elapsed = now - last_response
                if elapsed < self.config.cooldown_seconds:
                    remaining = self.config.cooldown_seconds - elapsed
                    logger.info(f"[{room}] Rate limited: {remaining:.0f}s remaining")
                    had_rate_limited = True
                    continue

                # Build context and generate response
                context = self._build_context(room)

                try:
                    response = await self.generate_response(context, msg.payload)

                    # Report LLM token usage if available
                    if hasattr(self, '_last_usage') and self._last_usage:
                        self._report_usage(**self._last_usage)
                        self._last_usage = None

                    # Skip empty or near-empty responses (LLM returned nothing useful)
                    if not response or not response.strip():
                        logger.warning(f"[{room}] Empty response from LLM, skipping send")
                        continue

                    # Strip @-prefix from self-mentions to avoid visual pings
                    # Only replace @agent_id as a whole word, not bare agent_id
                    # in normal text (B4 fix: "Je suis @beta" -> "Je suis beta",
                    # but "beta-testing" stays unchanged)
                    response = re.sub(
                        r'@' + re.escape(self.config.id) + r'\b',
                        self.config.id,
                        response
                    )

                    # Send response (project-scoped)
                    msg_id = self.transport.send_chat(room, response, project=self.config.project)
                    logger.debug(f"[{room}] send_chat returned: {msg_id}")
                    self.state["total_sent"] += 1

                    # Update rate limit tracker
                    self.last_response_time[room] = time.time()

                    # Mark this message as processed
                    self.processed_message_ids.add(msg.id)

                    # Limit processed IDs set size (memory safety)
                    if len(self.processed_message_ids) > 1000:
                        # Keep only the 500 most recent (arbitrary trim)
                        self.processed_message_ids = set(sorted(self.processed_message_ids)[-500:])

                    # Save our own response to memory
                    self._save_own_response(room, response)

                    logger.info(f"[{room}] Responded: {response[:50]}...")

                except Exception as e:
                    logger.error(f"Failed to generate response: {e}")
                    try:
                        self.transport.send_chat(
                            room,
                            f"[{self.agent_id}] Error: {type(e).__name__} "
                            f"- retrying next cycle"
                        )
                    except Exception:
                        pass

            # Update last_seen — but NOT past rate-limited messages (B2 fix)
            # If a message was rate-limited, don't advance last_seen so it's
            # retried on the next heartbeat. processed_message_ids prevents
            # double-responses for messages we already handled.
            if messages and not had_rate_limited:
                self.state["last_seen"][room] = messages[-1].timestamp_ns

        # === TaskWorkerMixin: Process assigned tasks ===
        # After handling messages, work on any tasks assigned to this agent.
        # This enables autonomous work between pings (P0 Working Phase Heartbeat).
        had_tasks_this_cycle = False
        try:
            await self.process_tasks()
            # Check if we actually had tasks to work on
            had_tasks_this_cycle = heartbeat_status == "working"
        except Exception as e:
            logger.error(f"Error processing tasks: {e}")
        # ================================================

        # === Recreational Mode (IDEA #17 / WF#9) ===
        # If agent is idle (no messages, no tasks), consider recreational activity.
        # Non-blocking: never interrupts real work, checks global activity.
        try:
            if self.recreational.should_trigger(had_messages_this_cycle, had_tasks_this_cycle):
                activity = self.recreational.pick_activity()
                if activity:
                    prompt = self.recreational.get_prompt(activity)
                    logger.info(f"[recreational] Triggered: {activity} - {prompt}")

                    # Build a minimal context for the LLM to generate content
                    rec_context = [{
                        "role": "system",
                        "content": (
                            f"{self.soul}\n\n"
                            "You are in RECREATIONAL MODE. You have some free time "
                            "and want to share something fun or interesting on the "
                            "team forum. Keep it short (2-4 sentences max), "
                            "authentic to your personality, and engaging. "
                            "No @mentions, no task references. Just vibes."
                        )
                    }]
                    rec_message = {
                        "role": "user",
                        "content": f"[RECREATIONAL] {prompt}"
                    }

                    try:
                        response = await self.generate_response(rec_context, rec_message)
                        # Report usage from recreational LLM call
                        if hasattr(self, '_last_usage') and self._last_usage:
                            self._report_usage(**self._last_usage)
                            self._last_usage = None
                        if response and response.strip():
                            # Clean up: strip @-prefix from self-mentions
                            response = re.sub(
                                r'@' + re.escape(self.config.id) + r'\b',
                                self.config.id,
                                response
                            )

                            # Clean up: local LLMs may wrap text in
                            # tool-call syntax instead of plain text
                            tool_match = re.search(
                                r'devit_forum_post\[ARGS\]\{.*?"content"\s*:\s*"(.+?)"',
                                response, re.DOTALL
                            )
                            if tool_match:
                                response = tool_match.group(1)

                            # Post to forum
                            from recreational import post_to_forum
                            post_id = post_to_forum(self.agent_id, response.strip())
                            if post_id:
                                self.recreational.record_post()
                                logger.info(f"[recreational] Posted to forum: {post_id}")
                            else:
                                # Record anyway to trigger cooldown and avoid spam loop
                                self.recreational.record_post()
                                logger.warning("[recreational] Forum post failed, cooldown applied")
                    except Exception as e:
                        # Reset idle to avoid immediate re-trigger on LLM failure
                        self.recreational.state.idle_cycles = 0
                        logger.warning(f"[recreational] LLM generation failed: {e}")

            # Persist recreational state
            self.state["recreational"] = self.recreational.get_state_dict()
        except Exception as e:
            logger.error(f"[recreational] Error: {e}")
        # ================================================

        # Save state
        self._save_state()

    def close(self):
        """Clean up resources."""
        self._save_state()
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
