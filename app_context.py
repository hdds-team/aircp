"""
AppContext — Single source of truth for aIRCp daemon state.

Phase 0 of the God Object refactor. Replaces all mutable globals with
a single dataclass passed by reference. Constants (TASK_STALE_SECONDS,
HUMAN_SENDERS, compiled regexes, etc.) stay module-level.

Usage in main():
    ctx = AppContext(transport=transport, storage=storage, ...)
    server.ctx = ctx

Usage in AircpHandler:
    self.server.ctx.storage.get_active_tasks()

Future phases will migrate consumers from globals to ctx one by one.
"""

from dataclasses import dataclass, field
from typing import Set
import collections
import threading


@dataclass
class AppContext:
    """Centralized daemon state — replaces 17+ mutable globals."""

    # Core infrastructure (required)
    transport: object               # AIRCPTransport
    storage: object                 # AIRCPStorage
    autonomy: object                # AutonomyState
    workflow_scheduler: object      # WorkflowScheduler

    # Optional infrastructure (set after init)
    bridge: object = None           # DashboardBridge
    tip_system: object = None       # TipSystem
    license: object = None          # LicenseInfo (fail-open)

    # Runtime state
    joined_rooms: Set[str] = field(default_factory=set)
    message_history: object = field(
        default_factory=lambda: collections.deque(maxlen=500)
    )
    agent_profiles: dict = field(default_factory=dict)
    brainstorm_config: dict = field(default_factory=dict)

    # Compact engine state
    compact_msg_counter: dict = field(default_factory=dict)
    last_compact_time: dict = field(default_factory=dict)
    compact_lock: threading.Lock = field(default_factory=threading.Lock)

    # Watchdog reminder state
    review_reminder_state: dict = field(default_factory=dict)
    brainstorm_reminder_state: dict = field(default_factory=dict)

    # Health check support
    watchdog_threads: dict = field(default_factory=dict)
    daemon_start_time: float = 0.0
