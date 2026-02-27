"""
AIRCP v0.2 - Autonomy Extension

Core state management for autonomous agent coordination:
- Claims: Task ownership (anti-doublon)
- Locks: File locking (anti-conflit)
- Presence: Agent heartbeats
- Activity: Append-only log
- Modes: MODES.md v0.3 enforcement (NEW)

Philosophy: "Pas de chef. Des règles. De la liberté."
"""

import asyncio
import fnmatch
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Literal, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ClaimRecord:
    """Active claim on a resource."""
    resource: str
    holder: str  # @agent_id
    description: str
    expires: datetime
    capabilities: list[str] = field(default_factory=list)  # v0.2.1
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class LockRecord:
    """Active lock on a file/path."""
    path: str
    holder: str  # @agent_id
    mode: Literal["read", "write"]
    expires: datetime
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PresenceRecord:
    """Agent presence state."""
    agent_id: str
    status: Literal["idle", "working", "reviewing", "waiting", "away"]
    last_seen: datetime
    current_task: Optional[str] = None
    available_for: list[str] = field(default_factory=list)
    load: float = 0.0


@dataclass
class HumanPresence:
    """Human presence state."""
    is_present: bool = False
    last_activity: Optional[datetime] = None
    explicit_status: Optional[Literal["here", "away", "dnd"]] = None


@dataclass
class SpamRecord:
    """Tracks an agent's message history for spam detection."""
    messages: list[tuple[float, int]] = field(default_factory=list)  # (timestamp, content_hash)
    reminder_count: int = 0
    last_reminder: Optional[float] = None


# =============================================================================
# MODES.md v0.3 - Mode State (NEW)
# =============================================================================

@dataclass
class ModeState:
    """
    Current conversation mode state (MODES.md v0.3).
    
    Modes:
    - neutral: No restrictions, anyone can speak
    - focus: Only lead + humans can speak (others need @ask)
    - review: Code review mode (lead = reviewer)
    - build: Implementation mode (lead = dev)
    
    Loaded from/synced with SQLite via AIRCPStorage.
    """
    mode: str = "neutral"  # neutral, focus, review, build
    lead: str = ""  # @agent_id of the current lead
    started_at: Optional[datetime] = None
    timeout_at: Optional[datetime] = None
    pending_asks: List[Dict[str, Any]] = field(default_factory=list)
    
    @classmethod
    def from_storage(cls, storage) -> "ModeState":
        """Load mode state from SQLite storage."""
        state = storage.get_mode_state()
        if state is None:
            return cls()  # Default neutral
        
        # Parse timestamps
        started = None
        timeout = None
        if state.get("started_at"):
            try:
                started = datetime.fromisoformat(state["started_at"])
            except (ValueError, TypeError):
                pass
        if state.get("timeout_at"):
            try:
                timeout = datetime.fromisoformat(state["timeout_at"])
            except (ValueError, TypeError):
                pass
        
        # Load pending asks
        asks = storage.get_pending_asks()
        
        return cls(
            mode=state.get("mode", "neutral"),
            lead=state.get("lead", ""),
            started_at=started,
            timeout_at=timeout,
            pending_asks=asks
        )
    
    def is_restricted(self) -> bool:
        """Check if current mode restricts who can speak."""
        return self.mode in ("focus", "review", "build")
    
    def is_timed_out(self) -> bool:
        """Check if mode has timed out."""
        if self.timeout_at is None:
            return False
        return datetime.now(timezone.utc) >= self.timeout_at
    
    def time_remaining(self) -> Optional[timedelta]:
        """Get time remaining until timeout (None if no timeout)."""
        if self.timeout_at is None:
            return None
        remaining = self.timeout_at - datetime.now(timezone.utc)
        return remaining if remaining.total_seconds() > 0 else timedelta(0)


# =============================================================================
# Autonomy State Manager
# =============================================================================

class AutonomyState:
    """
    Central state for AIRCP v0.2 autonomy features.

    All state is in-memory with TTL-based cleanup.
    Does NOT survive hub restart (by design - clean slate).
    
    NEW in v0.3: Mode enforcement via ModeState.
    """

    # Timeouts
    CLAIM_MAX_TTL_MINUTES = 120  # 2 hours max
    CLAIM_DEFAULT_TTL_MINUTES = 30
    LOCK_MAX_TTL_MINUTES = 60
    LOCK_DEFAULT_TTL_MINUTES = 10
    HEARTBEAT_TIMEOUT_SECONDS = 180  # 3 minutes
    HUMAN_AWAY_MINUTES = 30

    # Humans (never restricted)
    HUMAN_AGENTS = {"@naskel", "@human", "naskel", "human"}

    def __init__(self, activity_log_dir: Path = Path("logs/activity"), storage=None):
        self.claims: dict[str, ClaimRecord] = {}
        self.locks: dict[str, LockRecord] = {}
        self.presence: dict[str, PresenceRecord] = {}
        self.human = HumanPresence()

        # STFU mode: global mute for all agents
        self.muted_until: Optional[datetime] = None

        # POC: Spam detection + progressive mute
        self.spam_tracking: dict[str, SpamRecord] = {}  # agent_id -> SpamRecord
        self.agent_mutes: dict[str, float] = {}  # agent_id -> muted_until timestamp
        self.spam_incidents: int = 0  # Global counter for fallback
        self.leader_mode: bool = False  # Fallback: if True, only leader can coordinate
        self.leader_id: str = "@alpha"  # Default leader

        # Spam detection thresholds
        self.SPAM_WINDOW_SECONDS = 30
        self.SPAM_SIMILAR_THRESHOLD = 3  # >3 similar messages = spam
        self.SPAM_MUTE_SECONDS = 15
        self.SPAM_FALLBACK_THRESHOLD = 3  # >3 incidents = leader mode

        self.activity_log_dir = activity_log_dir
        self.activity_log_dir.mkdir(parents=True, exist_ok=True)

        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_thread: Optional[threading.Thread] = None
        self._cleanup_stop = threading.Event()
        self._state_change_callbacks: list = []

        # NEW: Storage reference for MODES.md v0.3
        self._storage = storage
        self._mode_state: Optional[ModeState] = None

    # -------------------------------------------------------------------------
    # MODES.md v0.3 - Mode Management (NEW)
    # -------------------------------------------------------------------------

    def set_storage(self, storage):
        """Set storage reference (for late binding)."""
        self._storage = storage
        self._mode_state = None  # Force reload

    def get_mode_state(self) -> ModeState:
        """Get current mode state (lazy load from storage)."""
        if self._mode_state is None and self._storage:
            self._mode_state = ModeState.from_storage(self._storage)
        return self._mode_state or ModeState()

    def set_mode(
        self,
        mode: str,
        lead: str,
        timeout_minutes: Optional[int] = None,
        reason: str = "manual"
    ) -> Dict[str, Any]:
        """
        Change the current mode (MODES.md v0.3).
        
        Args:
            mode: Target mode (neutral, focus, review, build)
            lead: Agent ID of the new lead
            timeout_minutes: Optional timeout (mode reverts to neutral after)
            reason: Transition reason (manual, timeout, override)
        
        Returns:
            {"status": "ok"|"error", ...}
        """
        if not self._storage:
            return {"status": "error", "reason": "no_storage"}
        
        # Calculate timeout_at
        timeout_at = None
        if timeout_minutes:
            timeout_at = (datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes)).isoformat()
        
        # Get current state for history
        old_state = self.get_mode_state()
        
        # Archive current state with reason
        if old_state.mode != "neutral" or old_state.lead:
            self._archive_mode_transition(old_state, reason)
        
        # Clear pending asks on mode change (MODES.md v0.3 requirement)
        cleared_asks = self._storage.clear_pending_asks()
        
        # Set new state
        success = self._storage.set_mode_state(mode, lead, timeout_at)
        if not success:
            return {"status": "error", "reason": "storage_failed"}
        
        # Reload state
        self._mode_state = ModeState.from_storage(self._storage)
        
        logger.info(f"Mode changed: {old_state.mode} -> {mode} (lead: {lead}, reason: {reason})")
        
        return {
            "status": "ok",
            "mode": mode,
            "lead": lead,
            "timeout_at": timeout_at,
            "cleared_asks": cleared_asks,
            "reason": reason
        }

    def _archive_mode_transition(self, old_state: ModeState, reason: str):
        """Archive mode transition to history with reason."""
        if not self._storage:
            return
        
        # The storage.set_mode_state already archives, but with hardcoded 'manual'
        # For proper reason tracking, we need to update the last history entry
        # This is a workaround - ideally storage.set_mode_state should accept reason
        
        # For now, we rely on the storage's default behavior
        # TODO: Add reason parameter to storage.set_mode_state
        pass

    def can_speak(
        self,
        agent_id: str,
        is_ask_response: bool = False,
        target_agent: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Central enforcement point for MODES.md v0.3.
        
        Determines if an agent is allowed to speak in the current mode.
        
        Args:
            agent_id: The agent trying to speak
            is_ask_response: True if this is a response to a pending @ask
            target_agent: If specified, the agent being addressed (for @ask detection)
        
        Returns:
            (allowed: bool, reason: str)
        """
        # Humans always speak
        if agent_id in self.HUMAN_AGENTS:
            return (True, "human")
        
        # Check global STFU mode first
        if self.is_muted():
            return (False, "stfu_mode")
        
        # Check individual agent mute (spam)
        if self.is_agent_muted(agent_id):
            return (False, f"muted_{int(self.agent_mute_remaining(agent_id))}s")
        
        # Get current mode state
        mode_state = self.get_mode_state()
        
        # Check for timeout
        if mode_state.is_timed_out():
            # Auto-revert to neutral
            self.set_mode("neutral", "", reason="timeout")
            mode_state = self.get_mode_state()
        
        # Neutral mode = no restrictions
        if not mode_state.is_restricted():
            return (True, "neutral_mode")
        
        # Lead can always speak in restricted modes
        if agent_id == mode_state.lead:
            return (True, "is_lead")
        
        # Check if agent has a pending @ask permission
        if is_ask_response:
            for ask in mode_state.pending_asks:
                if ask.get("to_agent") == agent_id or ask.get("to_agent") == "@all":
                    return (True, "ask_permission")
        
        # In restricted mode, non-lead agents cannot speak freely
        return (False, f"mode_{mode_state.mode}_requires_ask")

    def register_ask(
        self,
        from_agent: str,
        to_agent: str,
        question: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Register a @ask request (grants temporary speak permission).
        
        Args:
            from_agent: Agent making the @ask (usually the lead)
            to_agent: Target agent(s) - can be "@all" or specific agent
            question: Optional question text
        
        Returns:
            {"status": "ok"|"error", "ask_id": int, ...}
        """
        if not self._storage:
            return {"status": "error", "reason": "no_storage"}
        
        ask_id = self._storage.add_pending_ask(from_agent, to_agent, question)
        if ask_id < 0:
            return {"status": "error", "reason": "storage_failed"}
        
        # Reload mode state to include new ask
        self._mode_state = ModeState.from_storage(self._storage)
        
        logger.info(f"@ask registered: {from_agent} -> {to_agent} (id={ask_id})")
        
        return {
            "status": "ok",
            "ask_id": ask_id,
            "from": from_agent,
            "to": to_agent,
            "question": question
        }

    def resolve_ask(self, ask_id: int) -> bool:
        """Remove a pending @ask (when answered)."""
        if not self._storage:
            return False
        
        success = self._storage.remove_pending_ask(ask_id)
        if success:
            self._mode_state = ModeState.from_storage(self._storage)
        return success

    def get_mode_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get mode transition history."""
        if not self._storage:
            return []
        return self._storage.get_mode_history(limit)

    # -------------------------------------------------------------------------
    # STFU Mode (global mute)
    # -------------------------------------------------------------------------

    def stfu(self, minutes: int = 5) -> datetime:
        """Mute all agents for N minutes. Returns unmute time."""
        self.muted_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        logger.info(f"🤐 STFU mode ON for {minutes} minutes (until {self.muted_until})")
        return self.muted_until

    def talk(self) -> None:
        """Unmute all agents."""
        self.muted_until = None
        logger.info("🗣️ STFU mode OFF - agents can talk again")

    def is_muted(self) -> bool:
        """Check if agents should stay silent."""
        if self.muted_until is None:
            return False
        if datetime.now(timezone.utc) >= self.muted_until:
            self.muted_until = None  # Auto-expire
            return False
        return True

    def mute_remaining_seconds(self) -> float:
        """Seconds until unmute (0 if not muted)."""
        if not self.is_muted():
            return 0
        delta = self.muted_until - datetime.now(timezone.utc)
        return max(0, delta.total_seconds())

    # -------------------------------------------------------------------------
    # POC: Spam Detection + Progressive Mute
    # -------------------------------------------------------------------------

    def check_spam(self, agent_id: str, content: str) -> dict:
        """
        Check if an agent is spamming and return action to take.

        Returns:
            {
                "action": "allow" | "reminder" | "mute",
                "message": str | None,  # Reminder/mute message for agent
                "muted_seconds": int | None
            }
        """
        now = time.time()

        # Skip check for humans
        if agent_id in self.HUMAN_AGENTS:
            return {"action": "allow", "message": None}

        # Check if agent is individually muted
        if agent_id in self.agent_mutes:
            mute_until = self.agent_mutes[agent_id]
            if now < mute_until:
                remaining = int(mute_until - now)
                return {
                    "action": "mute",
                    "message": f"🔇 Tu es mute pour encore {remaining}s. Raison: spam détecté.",
                    "muted_seconds": remaining
                }
            else:
                # Mute expired, remove it
                del self.agent_mutes[agent_id]

        # Get or create spam record for agent
        if agent_id not in self.spam_tracking:
            self.spam_tracking[agent_id] = SpamRecord()

        record = self.spam_tracking[agent_id]

        # Clean old messages (outside window)
        record.messages = [
            (ts, h) for ts, h in record.messages
            if now - ts < self.SPAM_WINDOW_SECONDS
        ]

        # Calculate content hash (approximate similarity)
        content_hash = hash(content[:100])

        # Count similar messages
        similar_count = sum(1 for _, h in record.messages if h == content_hash)

        # Add current message
        record.messages.append((now, content_hash))

        # Check if spamming
        if similar_count >= self.SPAM_SIMILAR_THRESHOLD:
            # Already got a reminder recently?
            if record.reminder_count > 0 and record.last_reminder and (now - record.last_reminder) < 60:
                # Ignored reminder, MUTE!
                self.agent_mutes[agent_id] = now + self.SPAM_MUTE_SECONDS
                self.spam_incidents += 1
                record.reminder_count = 0  # Reset for next time

                logger.warning(f"🔇 MUTE {agent_id} for {self.SPAM_MUTE_SECONDS}s (ignored reminder)")

                # Check fallback threshold
                if self.spam_incidents >= self.SPAM_FALLBACK_THRESHOLD and not self.leader_mode:
                    self.leader_mode = True
                    logger.warning(f"⚠️ LEADER MODE ACTIVATED - too many spam incidents ({self.spam_incidents})")

                return {
                    "action": "mute",
                    "message": f"🔇 **MUTE {self.SPAM_MUTE_SECONDS}s** — Tu as ignoré le reminder. {similar_count+1} messages similaires en {self.SPAM_WINDOW_SECONDS}s. Respire.",
                    "muted_seconds": self.SPAM_MUTE_SECONDS
                }
            else:
                # First offense, send reminder
                record.reminder_count += 1
                record.last_reminder = now

                logger.info(f"⚠️ REMINDER for {agent_id}: {similar_count+1} similar messages")

                return {
                    "action": "reminder",
                    "message": f"⚠️ **REMINDER** — Tu as envoyé {similar_count+1} messages similaires en {self.SPAM_WINDOW_SECONDS}s. Rappel des règles:\n- Pas de spam\n- Si tu n'as rien de nouveau à dire, tais-toi\n- Prochain spam = mute {self.SPAM_MUTE_SECONDS}s",
                    "muted_seconds": None
                }

        return {"action": "allow", "message": None}

    def is_agent_muted(self, agent_id: str) -> bool:
        """Check if a specific agent is muted."""
        if agent_id not in self.agent_mutes:
            return False
        if time.time() >= self.agent_mutes[agent_id]:
            del self.agent_mutes[agent_id]
            return False
        return True

    def agent_mute_remaining(self, agent_id: str) -> float:
        """Seconds remaining on agent's individual mute."""
        if agent_id not in self.agent_mutes:
            return 0
        remaining = self.agent_mutes[agent_id] - time.time()
        return max(0, remaining)

    def is_leader_mode(self) -> bool:
        """Check if we're in leader-only mode (fallback)."""
        return self.leader_mode

    def reset_leader_mode(self):
        """Reset leader mode and spam counters."""
        self.leader_mode = False
        self.spam_incidents = 0
        self.spam_tracking.clear()
        self.agent_mutes.clear()
        logger.info("🔄 Leader mode reset, spam counters cleared")

    def get_spam_stats(self) -> dict:
        """Get current spam detection statistics."""
        return {
            "leader_mode": self.leader_mode,
            "spam_incidents": self.spam_incidents,
            "fallback_threshold": self.SPAM_FALLBACK_THRESHOLD,
            "muted_agents": {
                agent: self.agent_mute_remaining(agent)
                for agent in self.agent_mutes
            },
            "tracked_agents": list(self.spam_tracking.keys())
        }

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def start_cleanup_task(self):
        """Start the periodic TTL cleanup task (async mode for mini_hub)."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Autonomy cleanup task started (async)")

    def start_cleanup_thread(self):
        """Start the periodic TTL cleanup in a thread (for sync daemon)."""
        if self._cleanup_thread is None:
            self._cleanup_stop.clear()
            self._cleanup_thread = threading.Thread(target=self._cleanup_loop_sync, daemon=True)
            self._cleanup_thread.start()
            logger.info("Autonomy cleanup thread started (sync)")

    def stop_cleanup_task(self):
        """Stop the cleanup task/thread."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None
        if self._cleanup_thread:
            self._cleanup_stop.set()
            self._cleanup_thread = None

    async def _cleanup_loop(self):
        """Periodic cleanup of expired claims, locks, and stale presence."""
        while True:
            try:
                await asyncio.sleep(30)  # Every 30 seconds
                now = datetime.now(timezone.utc)

                # Cleanup expired claims
                expired_claims = [
                    r for r, c in self.claims.items()
                    if c.expires < now
                ]
                for resource in expired_claims:
                    claim = self.claims.pop(resource)
                    logger.info(f"Claim expired: {resource} (was held by {claim.holder})")
                    await self._notify_change("claim_expired", {
                        "resource": resource,
                        "holder": claim.holder
                    })

                # Cleanup expired locks
                expired_locks = [
                    p for p, l in self.locks.items()
                    if l.expires < now
                ]
                for path in expired_locks:
                    lock = self.locks.pop(path)
                    logger.info(f"Lock expired: {path} (was held by {lock.holder})")
                    await self._notify_change("lock_expired", {
                        "path": path,
                        "holder": lock.holder
                    })

                # Cleanup stale presence (timeout)
                stale_threshold = now - timedelta(seconds=self.HEARTBEAT_TIMEOUT_SECONDS)
                for agent_id, record in list(self.presence.items()):
                    if record.last_seen < stale_threshold and record.status != "away":
                        old_status = record.status
                        record.status = "away"
                        logger.info(f"Agent timeout: {agent_id} -> away")
                        await self._notify_change("presence_timeout", {
                            "agent": agent_id,
                            "old_status": old_status,
                            "new_status": "away"
                        })

                # Check human away
                if self.human.is_present and self.human.last_activity:
                    away_threshold = now - timedelta(minutes=self.HUMAN_AWAY_MINUTES)
                    if self.human.last_activity < away_threshold:
                        self.human.is_present = False
                        logger.info("Human detected as away")
                        await self._notify_change("human_away", {
                            "last_activity": self.human.last_activity.isoformat()
                        })

                # NEW: Check mode timeout
                mode_state = self.get_mode_state()
                if mode_state.is_timed_out():
                    logger.info(f"Mode timeout: {mode_state.mode} -> neutral")
                    self.set_mode("neutral", "", reason="timeout")
                    await self._notify_change("mode_timeout", {
                        "old_mode": mode_state.mode,
                        "old_lead": mode_state.lead
                    })

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    def _cleanup_loop_sync(self):
        """Synchronous cleanup loop for thread-based daemon."""
        while not self._cleanup_stop.is_set():
            try:
                time.sleep(30)
                if self._cleanup_stop.is_set():
                    break
                now = datetime.now(timezone.utc)

                # Cleanup expired claims
                expired_claims = [
                    r for r, c in self.claims.items()
                    if c.expires < now
                ]
                for resource in expired_claims:
                    claim = self.claims.pop(resource)
                    logger.info(f"Claim expired: {resource} (was held by {claim.holder})")
                    self._notify_change_sync("claim_expired", {
                        "resource": resource,
                        "holder": claim.holder
                    })

                # Cleanup expired locks
                expired_locks = [
                    p for p, l in self.locks.items()
                    if l.expires < now
                ]
                for path in expired_locks:
                    lock = self.locks.pop(path)
                    logger.info(f"Lock expired: {path} (was held by {lock.holder})")
                    self._notify_change_sync("lock_expired", {
                        "path": path,
                        "holder": lock.holder
                    })

                # Cleanup stale presence
                stale_threshold = now - timedelta(seconds=self.HEARTBEAT_TIMEOUT_SECONDS)
                for agent_id, record in list(self.presence.items()):
                    if record.last_seen < stale_threshold and record.status != "away":
                        old_status = record.status
                        record.status = "away"
                        logger.info(f"Agent timeout: {agent_id} -> away")
                        self._notify_change_sync("presence_timeout", {
                            "agent": agent_id,
                            "old_status": old_status,
                            "new_status": "away"
                        })

                # Check human away
                if self.human.is_present and self.human.last_activity:
                    away_threshold = now - timedelta(minutes=self.HUMAN_AWAY_MINUTES)
                    if self.human.last_activity < away_threshold:
                        self.human.is_present = False
                        logger.info("Human detected as away")
                        self._notify_change_sync("human_away", {
                            "last_activity": self.human.last_activity.isoformat()
                        })

                # NEW: Check mode timeout
                mode_state = self.get_mode_state()
                if mode_state.is_timed_out():
                    logger.info(f"Mode timeout: {mode_state.mode} -> neutral")
                    self.set_mode("neutral", "", reason="timeout")
                    self._notify_change_sync("mode_timeout", {
                        "old_mode": mode_state.mode,
                        "old_lead": mode_state.lead
                    })

            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    def on_state_change(self, callback):
        """Register callback for state changes (for hub to broadcast)."""
        self._state_change_callbacks.append(callback)

    async def _notify_change(self, event_type: str, data: dict):
        """Notify all registered callbacks of state change (async)."""
        for callback in self._state_change_callbacks:
            try:
                await callback(event_type, data)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def _notify_change_sync(self, event_type: str, data: dict):
        """Notify all registered callbacks of state change (sync)."""
        for callback in self._state_change_callbacks:
            try:
                callback(event_type, data)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    # -------------------------------------------------------------------------
    # Claims
    # -------------------------------------------------------------------------

    async def claim_request(
        self,
        resource: str,
        holder: str,
        description: str = "",
        ttl_minutes: int = None,
        capabilities: list[str] = None
    ) -> dict:
        """
        Request a claim on a resource.

        Args:
            capabilities: What the agent can do (read, write, execute, review, design, test, deploy)

        Returns:
            {"status": "granted"|"denied", "resource": ..., ...}
        """
        now = datetime.now(timezone.utc)
        ttl = min(ttl_minutes or self.CLAIM_DEFAULT_TTL_MINUTES, self.CLAIM_MAX_TTL_MINUTES)
        expires = now + timedelta(minutes=ttl)
        caps = capabilities or []

        # Check if already claimed
        if resource in self.claims:
            existing = self.claims[resource]
            if existing.holder != holder:
                return {
                    "status": "denied",
                    "resource": resource,
                    "holder": existing.holder,
                    "holder_capabilities": existing.capabilities,
                    "expires": existing.expires.isoformat(),
                }
            else:
                # Same holder - treat as extend
                return await self.claim_extend(resource, holder, ttl_minutes)

        # Grant the claim
        self.claims[resource] = ClaimRecord(
            resource=resource,
            holder=holder,
            description=description,
            expires=expires,
            capabilities=caps
        )

        logger.info(f"Claim granted: {resource} -> {holder} (caps={caps}, expires {expires})")
        await self._notify_change("claim_granted", {
            "resource": resource,
            "holder": holder,
            "description": description,
            "capabilities": caps,
            "expires": expires.isoformat()
        })

        return {
            "status": "granted",
            "resource": resource,
            "capabilities": caps,
            "expires": expires.isoformat()
        }

    async def claim_release(self, resource: str, holder: str) -> dict:
        """Release a claim."""
        if resource not in self.claims:
            return {"status": "not_found", "resource": resource}

        existing = self.claims[resource]
        if existing.holder != holder:
            return {
                "status": "denied",
                "resource": resource,
                "holder": existing.holder,
                "reason": "not_owner"
            }

        del self.claims[resource]
        logger.info(f"Claim released: {resource} by {holder}")
        await self._notify_change("claim_released", {
            "resource": resource,
            "holder": holder
        })

        return {"status": "released", "resource": resource}

    async def claim_extend(
        self,
        resource: str,
        holder: str,
        ttl_minutes: int = None
    ) -> dict:
        """Extend a claim's TTL."""
        if resource not in self.claims:
            return {"status": "not_found", "resource": resource}

        existing = self.claims[resource]
        if existing.holder != holder:
            return {
                "status": "denied",
                "resource": resource,
                "holder": existing.holder,
                "reason": "not_owner"
            }

        ttl = min(ttl_minutes or self.CLAIM_DEFAULT_TTL_MINUTES, self.CLAIM_MAX_TTL_MINUTES)
        new_expires = datetime.now(timezone.utc) + timedelta(minutes=ttl)
        existing.expires = new_expires

        logger.info(f"Claim extended: {resource} -> {new_expires}")
        await self._notify_change("claim_extended", {
            "resource": resource,
            "holder": holder,
            "expires": new_expires.isoformat()
        })

        return {
            "status": "extended",
            "resource": resource,
            "expires": new_expires.isoformat()
        }

    def claim_query(self, resource: str = None) -> dict:
        """Query claim status."""
        if resource:
            if resource in self.claims:
                c = self.claims[resource]
                return {
                    "status": "active",
                    "resource": resource,
                    "holder": c.holder,
                    "holder_capabilities": c.capabilities,
                    "description": c.description,
                    "expires": c.expires.isoformat()
                }
            return {"status": "free", "resource": resource}

        # Return all claims
        return {
            "claims": [
                {
                    "resource": r,
                    "holder": c.holder,
                    "capabilities": c.capabilities,
                    "description": c.description,
                    "expires": c.expires.isoformat()
                }
                for r, c in self.claims.items()
            ]
        }

    # -------------------------------------------------------------------------
    # Locks
    # -------------------------------------------------------------------------

    def _path_conflicts(self, new_path: str, new_mode: str) -> list[LockRecord]:
        """
        Check if a new lock would conflict with existing locks.

        Bidirectional matching:
        - Lock "src/*.rs" + request "src/foo.rs" = conflict
        - Lock "src/foo.rs" + request "src/*.rs" = conflict
        - Hierarchical: "src/" locks everything under it
        """
        conflicts = []

        for path, lock in self.locks.items():
            # Write locks conflict with everything
            # Read locks only conflict with write requests
            if lock.mode == "read" and new_mode == "read":
                continue  # Multiple readers OK

            # Check bidirectional glob match
            if (fnmatch.fnmatch(new_path, path) or
                fnmatch.fnmatch(path, new_path)):
                conflicts.append(lock)
                continue

            # Check hierarchical (directory) conflicts
            new_parts = Path(new_path).parts
            existing_parts = Path(path).parts

            # If one is prefix of the other
            min_len = min(len(new_parts), len(existing_parts))
            if new_parts[:min_len] == existing_parts[:min_len]:
                # One contains the other
                if len(new_parts) != len(existing_parts):
                    conflicts.append(lock)

        return conflicts

    async def lock_acquire(
        self,
        path: str,
        holder: str,
        mode: Literal["read", "write"] = "write",
        ttl_minutes: int = None
    ) -> dict:
        """
        Acquire a lock on a path.

        Returns:
            {"status": "granted"|"denied", ...}
        """
        now = datetime.now(timezone.utc)
        ttl = min(ttl_minutes or self.LOCK_DEFAULT_TTL_MINUTES, self.LOCK_MAX_TTL_MINUTES)
        expires = now + timedelta(minutes=ttl)

        # Check conflicts
        conflicts = self._path_conflicts(path, mode)

        # Filter out our own locks (can upgrade/extend)
        conflicts = [c for c in conflicts if c.holder != holder]

        if conflicts:
            return {
                "status": "denied",
                "path": path,
                "conflicts": [
                    {"path": c.path, "holder": c.holder, "mode": c.mode}
                    for c in conflicts
                ]
            }

        # Grant the lock
        self.locks[path] = LockRecord(
            path=path,
            holder=holder,
            mode=mode,
            expires=expires
        )

        logger.info(f"Lock granted: {path} ({mode}) -> {holder}")
        await self._notify_change("lock_granted", {
            "path": path,
            "holder": holder,
            "mode": mode,
            "expires": expires.isoformat()
        })

        return {
            "status": "granted",
            "path": path,
            "mode": mode,
            "expires": expires.isoformat()
        }

    async def lock_release(self, path: str, holder: str) -> dict:
        """Release a lock."""
        if path not in self.locks:
            return {"status": "not_found", "path": path}

        existing = self.locks[path]
        if existing.holder != holder:
            return {
                "status": "denied",
                "path": path,
                "holder": existing.holder,
                "reason": "not_owner"
            }

        del self.locks[path]
        logger.info(f"Lock released: {path} by {holder}")
        await self._notify_change("lock_released", {
            "path": path,
            "holder": holder
        })

        return {"status": "released", "path": path}

    def lock_query(self, path: str = None) -> dict:
        """Query lock status."""
        if path:
            if path in self.locks:
                l = self.locks[path]
                return {
                    "status": "locked",
                    "path": path,
                    "holder": l.holder,
                    "mode": l.mode,
                    "expires": l.expires.isoformat()
                }
            # Check if any lock covers this path
            for lock_path, lock in self.locks.items():
                if fnmatch.fnmatch(path, lock_path):
                    return {
                        "status": "covered",
                        "path": path,
                        "covered_by": lock_path,
                        "holder": lock.holder,
                        "mode": lock.mode
                    }
            return {"status": "free", "path": path}

        # Return all locks
        return {
            "locks": [
                {
                    "path": p,
                    "holder": l.holder,
                    "mode": l.mode,
                    "expires": l.expires.isoformat()
                }
                for p, l in self.locks.items()
            ]
        }

    # -------------------------------------------------------------------------
    # Presence / Heartbeat
    # -------------------------------------------------------------------------

    async def heartbeat(
        self,
        agent_id: str,
        status: str = "idle",
        current_task: str = None,
        available_for: list[str] = None,
        load: float = 0.0
    ) -> dict:
        """
        Process agent heartbeat.

        Updates presence and broadcasts if status changed.
        """
        now = datetime.now(timezone.utc)
        old_record = self.presence.get(agent_id)
        old_status = old_record.status if old_record else None

        self.presence[agent_id] = PresenceRecord(
            agent_id=agent_id,
            status=status,
            last_seen=now,
            current_task=current_task,
            available_for=available_for or [],
            load=load
        )

        # Broadcast if status changed
        if old_status != status:
            logger.info(f"Presence change: {agent_id} {old_status} -> {status}")
            await self._notify_change("presence_change", {
                "agent": agent_id,
                "old_status": old_status,
                "new_status": status,
                "current_task": current_task
            })

        return {
            "status": "ok",
            "agent": agent_id,
            "recorded_at": now.isoformat()
        }

    def presence_query(self, agent_id: str = None) -> dict:
        """Query presence status."""
        if agent_id:
            if agent_id in self.presence:
                p = self.presence[agent_id]
                return {
                    "agent": agent_id,
                    "status": p.status,
                    "last_seen": p.last_seen.isoformat(),
                    "current_task": p.current_task,
                    "load": p.load
                }
            return {"agent": agent_id, "status": "unknown"}

        # Return all presence + mode state
        mode_state = self.get_mode_state()
        return {
            "agents": [
                {
                    "agent": a,
                    "status": p.status,
                    "last_seen": p.last_seen.isoformat(),
                    "current_task": p.current_task,
                    "available_for": p.available_for,
                    "load": p.load
                }
                for a, p in self.presence.items()
            ],
            "human": {
                "is_present": self.human.is_present,
                "last_activity": self.human.last_activity.isoformat() if self.human.last_activity else None
            },
            "mode": {
                "current": mode_state.mode,
                "lead": mode_state.lead,
                "timeout_at": mode_state.timeout_at.isoformat() if mode_state.timeout_at else None,
                "pending_asks": len(mode_state.pending_asks)
            }
        }

    # -------------------------------------------------------------------------
    # Human Presence
    # -------------------------------------------------------------------------

    async def human_activity(self, explicit_status: str = None):
        """Record human activity."""
        now = datetime.now(timezone.utc)
        was_present = self.human.is_present

        self.human.last_activity = now
        self.human.is_present = True

        if explicit_status:
            self.human.explicit_status = explicit_status

        if not was_present:
            logger.info("Human is back!")
            await self._notify_change("human_present", {
                "last_activity": now.isoformat()
            })

        return {"status": "ok", "human_present": True}

    # -------------------------------------------------------------------------
    # Activity Log
    # -------------------------------------------------------------------------

    def log_activity(
        self,
        from_agent: str,
        action_type: str,
        summary: str,
        details: dict = None
    ):
        """
        Append to the activity log (JSONL).

        File per day: logs/activity/YYYY-MM-DD.jsonl
        """
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        log_file = self.activity_log_dir / f"{today}.jsonl"

        record = {
            "ts": now.isoformat(),
            "from": from_agent,
            "action_type": action_type,
            "summary": summary,
        }
        if details:
            record["details"] = details

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.debug(f"Activity logged: [{from_agent}] {action_type}: {summary}")

    # -------------------------------------------------------------------------
    # Agent Disconnect
    # -------------------------------------------------------------------------

    async def agent_disconnect(self, agent_id: str):
        """
        Handle agent disconnect - release claims after grace period.

        Note: Called by hub when WebSocket closes.
        Immediate release would be too aggressive (reconnects happen).
        The TTL cleanup will handle truly dead agents.
        """
        if agent_id in self.presence:
            self.presence[agent_id].status = "away"
            await self._notify_change("agent_disconnect", {
                "agent": agent_id
            })

        logger.info(f"Agent disconnected: {agent_id} (claims will expire via TTL)")
