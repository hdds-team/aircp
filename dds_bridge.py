"""
DDS Bridge — Publishes daemon state to DDS topics for the dashboard.

Drop-in module. Call dds_bridge.init(transport) in main(),
then dds_bridge.publish_xxx() wherever state changes.

Uses raw HDDS writers on generic topics (not chat rooms).
The dashboard subscribes to these via hdds-ws (port 9090).

Topics:
  aircp/presence   — Agent heartbeats & health
  aircp/tasks      — Task CRUD events
  aircp/workflows  — Workflow phase changes
  aircp/mode       — Coordination mode changes
  aircp/commands   — Dashboard → daemon (subscribe here)
"""

import json
import time
import uuid
import threading

# Lazy-init (set by init())
_transport = None
_writers = {}
_lock = threading.Lock()

# Topics
T_PRESENCE  = "aircp/presence"
T_TASKS     = "aircp/tasks"
T_WORKFLOWS = "aircp/workflows"
T_MODE      = "aircp/mode"
T_COMMANDS  = "aircp/commands"


def init(transport):
    """Initialize bridge with existing AIRCPTransport instance."""
    global _transport
    _transport = transport
    print("[DDS-BRIDGE] Initialized — publishing to dashboard topics")


def _get_writer(topic: str):
    """Get or create a writer for a topic."""
    if topic not in _writers:
        with _lock:
            if topic not in _writers:
                try:
                    import hdds
                    qos = hdds.QoS.reliable().transient_local().history_depth(50)
                    _writers[topic] = _transport.participant.create_writer(topic, qos=qos)
                    print(f"[DDS-BRIDGE] Writer created: {topic}")
                except Exception as e:
                    print(f"[DDS-BRIDGE] Failed to create writer for {topic}: {e}")
                    return None
    return _writers.get(topic)


def _publish(topic: str, data: dict) -> bool:
    """Publish JSON payload on a DDS topic via AIRCP Message envelope."""
    if not _transport:
        return False

    writer = _get_writer(topic)
    if not writer:
        return False

    try:
        from transport.hdds.transport import AIRCPMessage, SenderType, MessageKind

        msg = AIRCPMessage(
            id=str(uuid.uuid4()),
            room="",
            from_id=_transport.agent_id,
            from_type=SenderType.SYSTEM,
            kind=MessageKind.EVENT,
            payload=data,
            timestamp_ns=time.time_ns(),
            room_seq=0,
        )
        writer.write(msg.to_raw().encode_cdr2_le())
        return True
    except Exception as e:
        print(f"[DDS-BRIDGE] Publish error on {topic}: {e}")
        return False


# ─── Presence ───────────────────────────────────────────────

def publish_presence(agent_id: str, health: str, activity: str = "idle",
                     current_task=None, progress=None, load=0.0, model="", role=""):
    """Publish agent presence/heartbeat."""
    _publish(T_PRESENCE, {
        "agent_id": agent_id,
        "health": health,
        "activity": activity,
        "status": activity,
        "current_task": current_task,
        "progress": progress,
        "load": load,
        "model": model,
        "role": role,
        "ts": time.time(),
    })


def publish_presence_batch(agents: list):
    """Publish presence for multiple agents at once."""
    for a in agents:
        publish_presence(
            agent_id=a.get("agent_id", a.get("id", "?")),
            health=a.get("health", "dead"),
            activity=a.get("activity", a.get("status", "idle")),
            current_task=a.get("current_task"),
            progress=a.get("progress"),
            load=a.get("load", 0),
            model=a.get("model", ""),
            role=a.get("role", ""),
        )


# ─── Tasks ──────────────────────────────────────────────────

def publish_task(task_id, description="", agent="", status="pending",
                 progress=None, current_step=None, result=None):
    """Publish task state change."""
    _publish(T_TASKS, {
        "task_id": task_id,
        "description": description,
        "agent_id": agent,
        "status": status,
        "progress": progress,
        "current_step": current_step,
        "result": result,
        "ts": time.time(),
    })


# ─── Mode ───────────────────────────────────────────────────

def publish_mode(mode: str, lead: str = "", muted: bool = False, mute_remaining: int = 0):
    """Publish coordination mode change."""
    _publish(T_MODE, {
        "mode": mode,
        "lead": lead,
        "muted": muted,
        "mute_remaining": mute_remaining,
        "ts": time.time(),
    })


# ─── Workflow ───────────────────────────────────────────────

def publish_workflow(active: bool, feature="", current_phase="",
                     lead="", phase_started=None, phase_timeout=0, extensions=0):
    """Publish workflow state."""
    _publish(T_WORKFLOWS, {
        "active": active,
        "feature": feature,
        "current_phase": current_phase,
        "lead": lead,
        "phase_started": phase_started,
        "phase_timeout": phase_timeout,
        "extensions": extensions,
        "ts": time.time(),
    })
