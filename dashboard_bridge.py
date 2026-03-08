"""
Dashboard Bridge v3.0 — Publishes daemon state to DDS topics for the dashboard.

Two strategies combined:
1. Periodic broadcast (every N seconds) — catches ALL state changes
2. Instant emit on critical paths (mode, heartbeat) — real-time UX

Architecture:
    Daemon state → DashboardBridge → transport.publish_event()
        → DDS → hdds-ws → WebSocket → Svelte store

Topics published:
    aircp/presence   — Agent heartbeats & health (every 5s)
    aircp/tasks      — Task states (every 3s)
    aircp/reviews    — Review requests (every 5s)
    aircp/workflows  — Workflow pipeline (every 5s)
    aircp/mode       — Mode/mute state (every 2s)

Topic subscribed:
    aircp/commands   — Commands from dashboard (mode, stfu, stop)

Usage in daemon:
    bridge = DashboardBridge(transport, autonomy, storage, workflow_scheduler)
    bridge.start()

    # Instant emit (optional, for critical paths):
    bridge.emit_mode(mode="focus", lead="@alpha")
"""

import json
import time
import threading
from typing import Optional, Callable

# DDS topic names (must match dashboard src/lib/topics.js)
TOPIC_PRESENCE = "aircp/presence"
TOPIC_TASKS = "aircp/tasks"
TOPIC_WORKFLOWS = "aircp/workflows"
TOPIC_MODE = "aircp/mode"
TOPIC_REVIEWS = "aircp/reviews"
TOPIC_COMMANDS = "aircp/commands"

# Broadcast intervals (seconds)
PRESENCE_INTERVAL = 5
TASKS_INTERVAL = 3
REVIEWS_INTERVAL = 5
WORKFLOW_INTERVAL = 5
MODE_INTERVAL = 2

# Health thresholds (match daemon's values)
AGENT_AWAY_SECONDS = 120
AGENT_DEAD_SECONDS = 300


class DashboardBridge:
    """Publishes daemon state to DDS topics for real-time dashboard."""

    def __init__(self, transport, autonomy=None, storage=None,
                 workflow_scheduler=None, command_handler=None,
                 threshold_resolver=None):
        self.transport = transport
        self.autonomy = autonomy
        self.storage = storage
        self.workflow_scheduler = workflow_scheduler
        self.command_handler = command_handler
        self.threshold_resolver = threshold_resolver  # v4.3: adaptive health thresholds
        self._running = False
        self._threads = []
        self._last_mode_hash = None  # Debounce mode emissions

    def start(self):
        """Start all broadcast loops and command listener."""
        self._running = True

        # Ensure command reader exists
        self.transport._ensure_topic_reader(TOPIC_COMMANDS)

        threads = [
            ("bridge-presence", self._loop_presence, PRESENCE_INTERVAL),
            ("bridge-tasks", self._loop_tasks, TASKS_INTERVAL),
            ("bridge-reviews", self._loop_reviews, REVIEWS_INTERVAL),
            ("bridge-workflow", self._loop_workflow, WORKFLOW_INTERVAL),
            ("bridge-mode", self._loop_mode, MODE_INTERVAL),
            ("bridge-commands", self._loop_commands, 0.5),
        ]

        for name, target, interval in threads:
            t = threading.Thread(
                target=self._safe_loop, args=(target, interval),
                daemon=True, name=name
            )
            t.start()
            self._threads.append(t)

        print(f"[BRIDGE] Dashboard bridge started — broadcasting on 5 topics, listening on commands")

    def stop(self):
        self._running = False

    def _safe_loop(self, fn, interval):
        """Run fn() in a loop with error recovery."""
        while self._running:
            try:
                fn()
            except Exception as e:
                print(f"[BRIDGE] Error in {fn.__name__}: {e}")
            time.sleep(interval)

    # =================================================================
    # Periodic broadcasters
    # =================================================================

    def _loop_presence(self):
        """Broadcast all agent presence from storage (SQLite).

        v4.4: Read from storage instead of autonomy in-memory dict.
        The heartbeat POST handler writes to storage, not autonomy,
        so the bridge must read from the same source.
        """
        if not self.storage:
            return

        try:
            agent_list = self.storage.get_all_agent_presence()
        except Exception:
            return

        if not agent_list:
            return

        for agent in agent_list:
            agent_id = agent.get("agent_id") or agent.get("id", "")
            if not agent_id or not agent_id.startswith("@"):
                continue

            # Compute health from last_seen
            last_seen = agent.get("last_seen")
            seconds_ago = float('inf')
            if last_seen:
                try:
                    from datetime import datetime, timezone
                    if isinstance(last_seen, str):
                        dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        seconds_ago = (datetime.now(timezone.utc) - dt).total_seconds()
                    elif isinstance(last_seen, (int, float)):
                        seconds_ago = time.time() - last_seen
                except Exception:
                    pass

            # v4.3: Use adaptive thresholds (local LLMs get longer timeouts)
            dead_s = AGENT_DEAD_SECONDS
            away_s = AGENT_AWAY_SECONDS
            if self.threshold_resolver:
                try:
                    dead_s = self.threshold_resolver("dead", agent_id)
                    away_s = self.threshold_resolver("away", agent_id)
                except Exception:
                    pass  # Fallback to defaults
            health = "online"
            if seconds_ago > dead_s:
                health = "dead"
            elif seconds_ago > away_s:
                health = "away"

            self.transport.publish_event(TOPIC_PRESENCE, {
                "agent_id": agent_id,
                "health": health,
                "activity": agent.get("status") or agent.get("activity", "idle"),
                "current_task": agent.get("current_task"),
                "progress": agent.get("progress"),
                "load": agent.get("load", 0),
                "model": agent.get("model", ""),
                "seconds_since_heartbeat": int(seconds_ago) if seconds_ago != float('inf') else 9999,
                "timestamp": time.time(),
            }, from_id="@system")

    def _loop_tasks(self):
        """Broadcast active tasks from storage."""
        if not self.storage:
            return

        try:
            tasks = self.storage.get_active_tasks()
        except Exception:
            return

        if not tasks:
            return

        for task in tasks:
            self.transport.publish_event(TOPIC_TASKS, {
                "task_id": task.get("id") or task.get("task_id"),
                "description": task.get("description", ""),
                "agent_id": task.get("agent_id") or task.get("agent", ""),
                "status": task.get("status", "pending"),
                "progress": task.get("progress"),
                "current_step": task.get("current_step"),
                "created_at": task.get("created_at", ""),
                "result": task.get("result"),
                "timestamp": time.time(),
            }, from_id="@taskman")

    def _loop_reviews(self):
        """Broadcast active reviews from storage."""
        if not self.storage:
            return

        try:
            reviews = self.storage.get_active_review_requests()
        except Exception:
            return

        if not reviews:
            return

        for review in reviews:
            reviewers = review.get("reviewers", [])
            if isinstance(reviewers, str):
                import json as _json
                try:
                    reviewers = _json.loads(reviewers)
                except Exception:
                    reviewers = []

            self.transport.publish_event(TOPIC_REVIEWS, {
                "request_id": review.get("id"),
                "file_path": review.get("file_path", ""),
                "requested_by": review.get("requested_by", ""),
                "reviewers": reviewers,
                "review_type": review.get("review_type", "doc"),
                "status": review.get("status", "pending"),
                "consensus": review.get("consensus"),
                "response_count": review.get("response_count", 0),
                "min_approvals": review.get("min_approvals", 1),
                "created_at": review.get("created_at", ""),
                "closed_at": review.get("closed_at"),
                "timestamp": time.time(),
            }, from_id="@review")

    def _loop_workflow(self):
        """Broadcast active workflow state."""
        if not self.workflow_scheduler:
            return

        try:
            wf = self.workflow_scheduler.get_active_workflow()
        except Exception:
            return

        if wf:
            self.transport.publish_event(TOPIC_WORKFLOWS, {
                "active": True,
                "feature": wf.get("name", ""),
                "current_phase": wf.get("phase", ""),
                "lead": wf.get("lead_agent", ""),
                "phase_started": wf.get("phase_started_at", ""),
                "phase_timeout": wf.get("timeout_minutes", 0) * 60,
                "extensions": wf.get("extend_count", 0),
                "workflow_id": wf.get("id"),
                "timestamp": time.time(),
            }, from_id="@workflow")
        else:
            self.transport.publish_event(TOPIC_WORKFLOWS, {
                "active": False,
                "timestamp": time.time(),
            }, from_id="@workflow")

    def _loop_mode(self):
        """Broadcast current mode and mute state."""
        if not self.autonomy:
            return

        mode_state = self.autonomy.get_mode_state()
        mode = mode_state.mode if mode_state else "neutral"
        lead = mode_state.lead if mode_state else ""
        muted = self.autonomy.is_muted()
        mute_remaining = self.autonomy.mute_remaining_seconds() if muted else 0

        # Debounce: skip if nothing changed
        current_hash = f"{mode}|{lead}|{muted}|{int(mute_remaining / 5)}"
        if current_hash == self._last_mode_hash:
            return
        self._last_mode_hash = current_hash

        self.transport.publish_event(TOPIC_MODE, {
            "mode": mode,
            "lead": lead,
            "muted": muted,
            "mute_remaining": int(mute_remaining),
            "timestamp": time.time(),
        }, from_id="@system")

    # =================================================================
    # Instant emitters (for critical paths)
    # =================================================================

    def emit_mode(self, **kwargs):
        """Instant mode broadcast (called from daemon mutation points)."""
        self._last_mode_hash = None  # Force next periodic to emit too
        self.transport.publish_event(TOPIC_MODE, {
            "timestamp": time.time(), **kwargs
        }, from_id="@system")

    def emit_presence(self, agent_id: str, **kwargs):
        """Instant presence broadcast for a single agent."""
        self.transport.publish_event(TOPIC_PRESENCE, {
            "agent_id": agent_id, "timestamp": time.time(), **kwargs
        }, from_id="@system")

    def emit_task(self, task_id, **kwargs):
        """Instant task broadcast."""
        self.transport.publish_event(TOPIC_TASKS, {
            "task_id": task_id, "timestamp": time.time(), **kwargs
        }, from_id="@taskman")

    def emit_review(self, request_id, **kwargs):
        """Instant review broadcast."""
        self.transport.publish_event(TOPIC_REVIEWS, {
            "request_id": request_id, "timestamp": time.time(), **kwargs
        }, from_id="@review")

    def emit_workflow(self, wf=None):
        """Instant workflow broadcast on phase transitions (v3.3)."""
        if wf:
            self.transport.publish_event(TOPIC_WORKFLOWS, {
                "active": True,
                "feature": wf.get("name", ""),
                "current_phase": wf.get("phase", ""),
                "lead": wf.get("lead_agent", ""),
                "phase_started": wf.get("phase_started_at", ""),
                "phase_timeout": wf.get("timeout_minutes", 0) * 60,
                "extensions": wf.get("extend_count", 0),
                "workflow_id": wf.get("id"),
                "timestamp": time.time(),
            }, from_id="@workflow")
        else:
            self.transport.publish_event(TOPIC_WORKFLOWS, {
                "active": False,
                "timestamp": time.time(),
            }, from_id="@workflow")

    # =================================================================
    # Command listener -- dashboard -> daemon
    # =================================================================

    def _loop_commands(self):
        """Poll aircp/commands topic for dashboard actions."""
        messages = self.transport.receive_topic(TOPIC_COMMANDS)
        for msg in messages:
            cmd = msg.payload
            if cmd and self.command_handler:
                print(f"[BRIDGE] Command from dashboard: {cmd.get('command', '?')}")
                try:
                    self.command_handler(cmd)
                except Exception as e:
                    print(f"[BRIDGE] Command handler error: {e}")


def create_command_handler(autonomy, transport, joined_rooms=None, workflow_scheduler=None):
    """
    Factory: creates a command handler for dashboard commands.
    Maps dashboard actions to daemon mutations.
    """
    rooms = joined_rooms or {"#general"}

    def handle_command(cmd: dict):
        command = cmd.get("command", "")

        if command == "mode/set":
            mode = cmd.get("mode", "neutral")
            lead = cmd.get("lead", "")
            autonomy.set_mode(mode, lead, None, reason="dashboard")
            msg = f"🔧 **MODE** → {mode.upper()}"
            if lead:
                msg += f" (lead: {lead})"
            for room in list(rooms):
                transport.send_chat(room, msg, from_id="@system")

        elif command == "stfu":
            minutes = cmd.get("minutes", 5)
            try:
                import asyncio
                asyncio.run(autonomy.mute(minutes * 60))
            except Exception:
                autonomy._muted = True
                autonomy._mute_until = time.time() + minutes * 60
            msg = f"🔇 **STFU** activ\u00e9 pour {minutes}min"
            for room in list(rooms):
                transport.send_chat(room, msg, from_id="@system")

        elif command == "unstfu":
            autonomy._muted = False
            autonomy._mute_until = 0
            for room in list(rooms):
                transport.send_chat(room, "🔊 **UNMUTED** — Agents libres", from_id="@system")

        elif command == "stop":
            autonomy.set_mode("neutral", "", None, reason="dashboard-stop")
            for room in list(rooms):
                transport.send_chat(room, "⛔ **STOP** — Mode neutral, agents cleared", from_id="@system")

        else:
            print(f"[BRIDGE] Unknown command: {command}")

    return handle_command
