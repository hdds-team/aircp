"""Contextual tip system for aircp daemon.

Phase 4 extraction: TipSystem class and tip-related constants moved from
aircp_daemon.py. Broadcasts helpful tips to agents based on workflow phase
or on a time-based rotation.
"""

import os
import random
import threading
import time
import tomllib
from pathlib import Path

_AIRCP_HOME = os.environ.get("AIRCP_HOME", os.path.dirname(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "aircp_daemon.py"))
))
TIPS_CONFIG_PATH = Path(_AIRCP_HOME) / "aircp-config.toml"

# Watchdog / broadcast intervals (seconds)
TIPS_WATCHDOG_INTERVAL = 60   # Check every 60s
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
    "The idea button in the dashboard automatically creates a brainstorm session with agent voting.",
    "Use `memory/search` to find past conversations. Ex: `devit_aircp command=\"memory/search\" query=\"forum refactor\"` -- no more scrolling through history.",
    "The `memory/get` command lets you re-read a specific day. Ex: `devit_aircp command=\"memory/get\" day=\"2026-02-08\" room=\"#brainstorm\"` -- useful before resuming a topic.",
    "Your messages are full-text indexed (FTS5). `memory/search` is faster than reading 500 history messages.",
]

# Contextual tips: shown based on current workflow phase
CONTEXTUAL_TIPS = {
    "request": "Phase **request**: Clearly describe the need. Use `workflow/next` when the spec is ready.",
    "brainstorm": "Phase **brainstorm**: Discussions and votes in **#brainstorm** (not #general). Vote via `brainstorm/vote`. Final summary only in #general.",
    "code": "Phase **code**: Code and commit. Don't forget `task/activity` to show your progress to the watchdog.",
    "review": "Phase **review**: Use `review/request` (not just a chat message!). Code needs 2 MCP approvals.",
    "done": "Workflow complete! Remember to update `docs/*.md` and check if `dashboard.html` reflects the changes.",
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
        config = load_tips_config()
        self.general_tips = list(GENERAL_TIPS)
        self.shown_indices = set()
        self.interval = config.get("interval_minutes", 30) * 60
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

    def broadcast(self, transport_ref, ensure_room_fn, workflow_phase: str = None):
        """Check and broadcast a tip if needed. Called from watchdog loop.

        Args:
            transport_ref: HDDS transport for sending messages.
            ensure_room_fn: Function to ensure room exists before sending.
            workflow_phase: Current workflow phase (for contextual tips).
        """
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
                if len(self.tip_history) > 50:
                    self.tip_history = self.tip_history[-50:]

            msg = f"{self.prefix} **Tip:** {tip_text}"
            if transport_ref:
                try:
                    ensure_room_fn(self.channel)
                    transport_ref.send_chat(self.channel, msg, from_id="@tips")
                except Exception as e:
                    print(f"[TIPS] Failed to broadcast: {e}")
            print(f"[TIPS] Broadcast ({tip_type}): {tip_text[:60]}...")
