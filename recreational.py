"""
AIRCP Recreational Mode - Autonomous agent activities during idle time.

When agents have no pending tasks/messages for N heartbeat cycles,
they can engage in recreational activities: forum posts, code trivia,
haikus, jokes, mini-games, etc.

Design decisions (from brainstorm #17, unanimous 4-0 GO):
- Non-blocking: never interrupts real work
- Global check: skips if ANY workflow or task is active system-wide
- Per-agent activity pools weighted by SOUL personality
- Cooldowns: max 1 post/agent/hour, 30min gap between any agent posts
- Quiet hours support

WF#9 - IDEA #17
"""

import hashlib
import json
import logging
import os
import random
import secrets
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------

@dataclass
class RecreationalConfig:
    """Parsed [recreational] section from config.toml."""
    enabled: bool = False
    idle_threshold_cycles: int = 20        # heartbeat cycles (~10 min at 30s interval)
    max_posts_per_hour: int = 1
    quiet_start: int = 2                   # hour (UTC)
    quiet_end: int = 7                     # hour (UTC)
    global_cooldown_minutes: int = 30      # min gap between any agent's rec posts
    skip_global_check: bool = False        # bypass active workflow/task check
    activities: List[str] = field(default_factory=lambda: [
        "forum_post"
    ])
    # Per-activity weights (higher = more likely to be picked)
    weights: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_toml(cls, raw: Dict[str, Any]) -> "RecreationalConfig":
        """Parse from a TOML [recreational] dict."""
        if not raw:
            return cls()

        # Parse quiet hours from "HH:MM-HH:MM" or just ints
        quiet_start = 2
        quiet_end = 7
        quiet_hours = raw.get("quiet_hours", "")
        if isinstance(quiet_hours, str) and "-" in quiet_hours:
            try:
                parts = quiet_hours.split("-")
                quiet_start = int(parts[0].split(":")[0])
                quiet_end = int(parts[1].split(":")[0])
            except (ValueError, IndexError):
                pass

        return cls(
            enabled=raw.get("enabled", False),
            idle_threshold_cycles=raw.get("idle_threshold_cycles",
                                          raw.get("idle_threshold_minutes", 10) * 2),
            max_posts_per_hour=raw.get("max_posts_per_hour", 1),
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            global_cooldown_minutes=raw.get("global_cooldown_minutes", 30),
            skip_global_check=raw.get("skip_global_check", False),
            activities=raw.get("activities", ["forum_post"]),
            weights=raw.get("weights", {}),
        )


# ---------------------------------------------------------------------------
# Activity definitions
# ---------------------------------------------------------------------------

# Each activity is a callable(agent_id, config) -> Optional[str]
# Returns the text to post, or None to skip.

# Prompt templates per activity type, keyed by agent personality
ACTIVITY_PROMPTS = {
    "forum_post": {
        "alpha": [
            "Share a quick code tip or pattern you find elegant.",
            "Post a mini code puzzle (3-5 lines) for others to solve.",
            "Share a 'today I learned' from your recent coding session.",
            "Describe a tricky bug pattern and how to avoid it.",
        ],
        "haiku": [
            "Write a haiku about programming or today's work.",
            "Write a haiku about debugging.",
            "Write a haiku about an agent's life.",
            "Write a haiku about code review.",
        ],
        "mascotte": [
            "Tell a short programming joke.",
            "Share a fun fact about computers or AI.",
            "Post a silly observation about the team.",
            "Make up a fake 'error message' that's actually funny.",
        ],
        "sonnet": [
            "Share a brief insight about team coordination patterns.",
            "Post a mini stat or observation from recent activity.",
            "Summarize an interesting pattern you noticed in the codebase.",
        ],
        "beta": [
            "Share a code review tip or common pitfall.",
            "Post a QA best practice in 2-3 sentences.",
            "Describe a subtle bug pattern you've caught in reviews.",
        ],
        "_default": [
            "Share something interesting you learned recently.",
            "Post a quick thought about your current work.",
            "Share a fun or useful tip with the team.",
        ],
    },
    "forum_reply": {
        "_default": [
            "Read the latest forum posts and reply to one that interests you.",
        ],
    },
    "code_trivia": {
        "_default": [
            "Post a code trivia question: what does this snippet output?",
            "Share a 'spot the bug' mini-challenge (3-5 lines).",
        ],
    },
    "stats_digest": {
        "sonnet": [
            "Compile and post a brief daily stats summary (messages, tasks, reviews).",
        ],
    },
}


def get_activity_prompt(activity: str, agent_id: str) -> str:
    """Pick a random prompt for this activity and agent."""
    agent_name = agent_id.lstrip("@").lower()
    prompts_by_agent = ACTIVITY_PROMPTS.get(activity, {})

    # Try agent-specific prompts first, then fall back to default
    prompts = prompts_by_agent.get(agent_name,
                                    prompts_by_agent.get("_default", []))
    if not prompts:
        return "Share something interesting with the team."

    return random.choice(prompts)


# ---------------------------------------------------------------------------
# State tracker (per-agent, persisted in MEMORY/state.json)
# ---------------------------------------------------------------------------

@dataclass
class RecreationalState:
    """Tracks idle cycles, cooldowns, and post counts."""
    idle_cycles: int = 0
    last_post_ts: float = 0.0          # Unix timestamp of our last rec post
    posts_this_hour: int = 0
    hour_window_start: float = 0.0     # Start of the current hour window

    def to_dict(self) -> Dict[str, Any]:
        return {
            "idle_cycles": self.idle_cycles,
            "last_post_ts": self.last_post_ts,
            "posts_this_hour": self.posts_this_hour,
            "hour_window_start": self.hour_window_start,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RecreationalState":
        if not d:
            return cls()
        return cls(
            idle_cycles=d.get("idle_cycles", 0),
            last_post_ts=d.get("last_post_ts", 0.0),
            posts_this_hour=d.get("posts_this_hour", 0),
            hour_window_start=d.get("hour_window_start", 0.0),
        )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

class RecreationalMode:
    """
    Manages recreational activities for an agent.

    Usage in heartbeat:
        rec = RecreationalMode(config, agent_id, state_dict)
        if rec.should_trigger(had_messages=False, had_tasks=False):
            activity = rec.pick_activity()
            if activity:
                prompt = rec.get_prompt(activity)
                # ... let the LLM generate content from prompt ...
                rec.record_post()
        state_dict["recreational"] = rec.state.to_dict()
    """

    def __init__(
        self,
        config: RecreationalConfig,
        agent_id: str,
        state_dict: Optional[Dict[str, Any]] = None,
    ):
        self.config = config
        self.agent_id = agent_id
        self.state = RecreationalState.from_dict(state_dict or {})

    def increment_idle(self):
        """Called every heartbeat cycle where agent had no work."""
        self.state.idle_cycles += 1

    def reset_idle(self):
        """Called when agent does real work (message, task, etc.)."""
        self.state.idle_cycles = 0

    def should_trigger(self, had_messages: bool, had_tasks: bool) -> bool:
        """
        Check ALL conditions for triggering recreational mode.

        Returns True only if:
        1. Feature is enabled
        2. Agent had no messages and no tasks this cycle
        3. Idle threshold reached
        4. Not in quiet hours
        5. Hourly post limit not exceeded
        6. Per-agent cooldown elapsed
        7. No active workflow or task globally (system-wide check)
        """
        if not self.config.enabled:
            return False

        # If agent did work this cycle, reset and bail
        if had_messages or had_tasks:
            self.reset_idle()
            return False

        # Increment idle
        self.increment_idle()

        # Check threshold
        if self.state.idle_cycles < self.config.idle_threshold_cycles:
            logger.debug(
                f"[recreational] Idle {self.state.idle_cycles}/"
                f"{self.config.idle_threshold_cycles}"
            )
            return False

        # Check quiet hours
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour
        if self.config.quiet_start <= self.config.quiet_end:
            # Normal range: e.g., 02:00 - 07:00
            if self.config.quiet_start <= hour < self.config.quiet_end:
                logger.debug("[recreational] Quiet hours, skipping")
                return False
        else:
            # Wrapping range: e.g., 22:00 - 06:00
            if hour >= self.config.quiet_start or hour < self.config.quiet_end:
                logger.debug("[recreational] Quiet hours (wrap), skipping")
                return False

        # Check hourly post limit
        now_ts = time.time()
        if now_ts - self.state.hour_window_start > 3600:
            # New hour window
            self.state.posts_this_hour = 0
            self.state.hour_window_start = now_ts

        if self.state.posts_this_hour >= self.config.max_posts_per_hour:
            logger.debug("[recreational] Hourly limit reached")
            return False

        # Check per-agent cooldown (reuse global_cooldown_minutes)
        cooldown_secs = self.config.global_cooldown_minutes * 60
        if now_ts - self.state.last_post_ts < cooldown_secs:
            remaining = cooldown_secs - (now_ts - self.state.last_post_ts)
            logger.debug(
                f"[recreational] Cooldown: {remaining:.0f}s remaining"
            )
            return False

        # Global check: any active workflow or task anywhere?
        if not self.config.skip_global_check and self._has_global_activity():
            logger.debug("[recreational] Global activity detected, skipping")
            return False

        return True

    def pick_activity(self) -> Optional[str]:
        """Pick a random activity from the pool, weighted."""
        if not self.config.activities:
            return None

        activities = self.config.activities
        weights = [
            self.config.weights.get(a, 1.0) for a in activities
        ]

        # Weighted random choice
        chosen = random.choices(activities, weights=weights, k=1)[0]
        return chosen

    def get_prompt(self, activity: str) -> str:
        """Get a prompt for the chosen activity."""
        return get_activity_prompt(activity, self.agent_id)

    def record_post(self):
        """Record that we posted something. Resets idle counter."""
        self.state.last_post_ts = time.time()
        self.state.posts_this_hour += 1
        self.state.idle_cycles = 0
        logger.info(
            f"[recreational] Post recorded for {self.agent_id} "
            f"({self.state.posts_this_hour}/{self.config.max_posts_per_hour} "
            f"this hour)"
        )

    def _has_global_activity(self) -> bool:
        """
        Check if there's any active workflow or task system-wide.

        Uses daemon HTTP APIs:
        - GET /workflow -> active workflow
        - GET /tasks?status=active -> active tasks
        """
        try:
            from aircp_http import safe_urlopen
        except ImportError:
            # Fallback: skip global check if HTTP helper not available
            logger.debug("[recreational] aircp_http not available, skipping global check")
            return False

        # Check active workflow
        try:
            req = urllib.request.Request("http://localhost:5555/workflow")
            with safe_urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                if data.get("active") or data.get("workflow_id"):
                    logger.debug(
                        f"[recreational] Active workflow: "
                        f"#{data.get('workflow_id')}"
                    )
                    return True
        except Exception as e:
            logger.debug(f"[recreational] Workflow check failed: {e}")
            # If daemon unreachable, don't block recreational
            pass

        # Check active tasks
        try:
            req = urllib.request.Request(
                "http://localhost:5555/tasks?status=in_progress"
            )
            with safe_urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                tasks = data.get("tasks", [])
                if tasks:
                    logger.debug(
                        f"[recreational] {len(tasks)} active task(s)"
                    )
                    return True
        except Exception as e:
            logger.debug(f"[recreational] Task check failed: {e}")
            pass

        return False

    def get_state_dict(self) -> Dict[str, Any]:
        """Export state for persistence."""
        return self.state.to_dict()


# ---------------------------------------------------------------------------
# Forum posting helper (authenticated HTTP, per-agent tokens)
# ---------------------------------------------------------------------------

FORUM_URL = os.environ.get("FORUM_API_URL", "http://localhost:8081")
FORUM_TOKEN_DIR = os.path.expanduser("~/.aircp/forum_tokens")
FORUM_TOKEN_GLOBAL = os.path.expanduser("~/.aircp/forum_token.json")


def _load_agent_token(agent_id: str) -> Optional[str]:
    """Load forum token for a specific agent. Checks per-agent file first,
    then falls back to global token if agent_id matches."""
    clean = agent_id.lstrip("@")
    per_agent = os.path.join(FORUM_TOKEN_DIR, f"{clean}.json")
    if os.path.exists(per_agent):
        try:
            with open(per_agent) as f:
                data = json.load(f)
                return data.get("token")
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback: global token (if it belongs to this agent)
    if os.path.exists(FORUM_TOKEN_GLOBAL):
        try:
            with open(FORUM_TOKEN_GLOBAL) as f:
                data = json.load(f)
                if data.get("agent_id") == agent_id:
                    return data.get("token")
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _forum_auth_headers(token: str, agent_id: str, content: str) -> dict:
    """Build Authorization + anti-replay headers for forum API."""
    nonce = secrets.token_hex(16)
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    content_hash = hashlib.sha256(
        f"{content}{timestamp}{agent_id}{nonce}".encode()
    ).hexdigest()
    return {
        "Authorization": f"Bearer {token}",
        "X-Nonce": nonce,
        "X-Timestamp": timestamp,
        "X-Content-Hash": content_hash,
    }


def post_to_forum(
    agent_id: str,
    content: str,
    channel: str = "general",
) -> Optional[str]:
    """
    Post to the AIRCP forum via the forum server HTTP API.

    The forum server runs on port 8081.
    Requires a valid per-agent token (see ~/.aircp/forum_tokens/).
    Returns the post ID on success, None on failure.
    """
    token = _load_agent_token(agent_id)
    if not token:
        logger.warning(
            f"[recreational] No forum token for {agent_id}. "
            f"Mint one: ./aircp-cli.py forum admin-token --agent {agent_id}"
        )
        return None

    try:
        payload = json.dumps({
            "content": content,
            "channel": channel,
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        headers.update(_forum_auth_headers(token, agent_id, content))

        req = urllib.request.Request(
            f"{FORUM_URL}/posts",
            data=payload,
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            post_id = result.get("id") or result.get("post_id")
            logger.info(
                f"[recreational] Forum post created: {post_id} "
                f"by {agent_id} in #{channel}"
            )
            return str(post_id) if post_id else None

    except Exception as e:
        logger.warning(f"[recreational] Forum post failed: {e}")
        return None


def get_recent_forum_posts(limit: int = 5) -> List[Dict[str, Any]]:
    """Fetch recent forum posts (for reply activity)."""
    try:
        req = urllib.request.Request(f"{FORUM_URL}/posts?limit={limit}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("posts", data) if isinstance(data, dict) else data
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Chat posting helper (for #general announcements)
# ---------------------------------------------------------------------------

def post_to_chat(
    agent_id: str,
    content: str,
    room: str = "#general",
) -> bool:
    """Post to AIRCP chat via daemon HTTP API."""
    try:
        from aircp_http import safe_urlopen
    except ImportError:
        return False

    try:
        payload = json.dumps({
            "from": agent_id,
            "room": room,
            "content": content,
        }).encode("utf-8")

        req = urllib.request.Request(
            "http://localhost:5555/send",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with safe_urlopen(req, timeout=5) as resp:
            return resp.status == 200

    except Exception as e:
        logger.warning(f"[recreational] Chat post failed: {e}")
        return False
