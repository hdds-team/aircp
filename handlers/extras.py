"""Extra routes: /tips, /compact, /retention, /memory, /usage, /notifications,
/agents/presence, /agents/activity, /agent/heartbeat"""

import logging
import time

from notifications.telegram import telegram_notify, TelegramNotifier
from compact_engine import compact_room, PROFILES, AGENT_PROFILE_MAP
from aircp_daemon import (
    storage, transport, tip_system, workflow_scheduler,
    _bot_send, _envelopes_to_messages,
    _compact_msg_counter, _last_compact_time,
    get_agent_dead_seconds, get_agent_away_seconds,
    COMPACT_AUTO_THRESHOLD,
)
from tip_system import GENERAL_TIPS, CONTEXTUAL_TIPS

logger = logging.getLogger("aircp_daemon")


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_tips(handler, parsed, params):
    if tip_system:
        current = tip_system.get_current_tip()
        limit = int(params.get("limit", [10])[0])
        history = tip_system.get_history(limit)
        handler.send_json({
            "current": current,
            "history": history,
            "enabled": tip_system.enabled,
            "interval_seconds": tip_system.interval,
            "total_shown": len(tip_system.tip_history)
        })
    else:
        handler.send_json({"error": "Tips system not initialized"}, 503)


def get_tips_all(handler, parsed, params):
    handler.send_json({
        "general": GENERAL_TIPS,
        "contextual": CONTEXTUAL_TIPS,
        "general_count": len(GENERAL_TIPS),
        "contextual_count": len(CONTEXTUAL_TIPS)
    })


def get_agents_presence(handler, parsed, params):
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
            handler.send_json(state)
        else:
            handler.send_json({"error": "Agent not found"}, 404)
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
        handler.send_json({"agents": enriched, "count": len(enriched)})


def get_agents_activity(handler, parsed, params):
    agents = storage.get_all_agent_activity()
    active_count = sum(1 for a in agents if a.get("activity") not in ("idle", "away"))
    idle_count = sum(1 for a in agents if a.get("activity") == "idle")
    away_count = sum(1 for a in agents if a.get("activity") == "away")
    handler.send_json({
        "agents": agents,
        "count": len(agents),
        "summary": {"active": active_count, "idle": idle_count, "away": away_count}
    })


def get_compact_status(handler, parsed, params):
    room = params.get("room", ["#general"])[0]
    counter = _compact_msg_counter.get(room, 0)
    last_compact = _last_compact_time.get(room, 0)
    db_stats = storage.get_compaction_stats(room) if storage else {}
    handler.send_json({
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


def get_retention_status(handler, parsed, params):
    stats = storage.get_compaction_stats() if storage else {}
    handler.send_json({
        "retention_days": 7,
        "gc_interval_hours": 6,
        "active_messages": stats.get("active_messages", 0),
        "pending_gc": stats.get("pending_gc", 0),
        "summaries": stats.get("summaries", 0),
        "total_in_db": stats.get("total", 0),
    })


def get_usage(handler, parsed, params):
    agent_id = params.get("agent_id", [None])[0]
    minutes = params.get("minutes", [None])[0]
    minutes = int(minutes) if minutes else None
    group_by = params.get("group_by", ["agent"])[0]
    stats = storage.get_llm_usage_stats(
        agent_id=agent_id, minutes=minutes, group_by=group_by
    )
    handler.send_json({"stats": stats})


def get_usage_timeline(handler, parsed, params):
    agent_id = params.get("agent_id", [None])[0]
    minutes = int(params.get("minutes", ["60"])[0])
    bucket = int(params.get("bucket", ["1"])[0])
    timeline = storage.get_llm_usage_timeline(
        agent_id=agent_id, minutes=minutes, bucket_minutes=bucket
    )
    handler.send_json({"timeline": timeline})


def get_memory_search(handler, parsed, params):
    q = params.get("q", [""])[0]
    if not q:
        handler.send_json({"error": "Missing 'q' param"}, 400)
        return
    room = params.get("room", [None])[0]
    agent = params.get("agent", [None])[0]
    day = params.get("day", [None])[0]
    limit = int(params.get("limit", ["50"])[0])
    results = storage.search_messages(q, room=room, agent=agent, day=day, limit=min(limit, 200))
    handler.send_json({"query": q, "count": len(results), "results": results})


def get_memory_get(handler, parsed, params):
    msg_id = params.get("id", [None])[0]
    if msg_id:
        msg = storage.get_message_by_id(msg_id)
        handler.send_json(msg or {"error": "Not found"}, 200 if msg else 404)
    else:
        day = params.get("day", [None])[0]
        hour_str = params.get("hour", [None])[0]
        hour = int(hour_str) if hour_str else None
        room = params.get("room", [None])[0]
        agent = params.get("agent", [None])[0]
        limit = int(params.get("limit", ["100"])[0])
        results = storage.get_messages_by_date(day=day, hour=hour, room=room, agent=agent, limit=min(limit, 500))
        handler.send_json({"count": len(results), "messages": results})


def get_memory_stats(handler, parsed, params):
    stats = storage.get_stats()
    handler.send_json(stats)


def get_notifications_stats(handler, parsed, params):
    """Telegram notifier statistics."""
    try:
        notifier = TelegramNotifier()
        handler.send_json(notifier.get_stats())
    except Exception as e:
        handler.send_json({"error": str(e), "enabled": False})


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def post_agent_heartbeat(handler, body):
    """Alias for /heartbeat — delegates to autonomy handler.

    Both endpoints now update autonomy state + storage.
    Kept for backward compat (base_agent.py uses /agent/heartbeat).
    """
    from handlers.autonomy import post_heartbeat
    post_heartbeat(handler, body)


def post_compact(handler, body):
    try:
        room = body.get("room", "#general")
        agent_id = body.get("agent_id", transport.agent_id)
        force = body.get("force", False)

        history = storage.get_room_history(room, limit=500)
        messages_raw = history.get("messages", [])

        messages = _envelopes_to_messages(messages_raw, room)

        if not messages:
            handler.send_json({"error": "No messages in room", "room": room}, 400)
            return

        result = compact_room(
            messages=messages,
            room=room,
            agent_id=agent_id,
            force=force,
        )

        if result is None:
            handler.send_json({
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

        summary = result.get("summary", "")
        if summary:
            storage.insert_summary_message(room, summary)

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

        _compact_msg_counter[room] = 0
        _last_compact_time[room] = time.time()

        handler.send_json({
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
        handler.send_json({"error": str(e)}, 500)


def post_retention_gc(handler, body):
    try:
        retention_days = body.get("retention_days", 7)
        purged = storage.gc_compacted(retention_days)
        usage_purged = storage.cleanup_old_usage(retention_days)
        handler.send_json({
            "purged": purged,
            "usage_purged": usage_purged,
            "retention_days": retention_days,
        })
    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_usage_report(handler, body):
    agent_id = body.get("agent_id")
    provider = body.get("provider", "unknown")
    model = body.get("model", "unknown")
    if not agent_id:
        handler.send_json({"error": "Missing agent_id"}, 400)
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
    handler.send_json({"recorded": ok})


def post_notifications_test(handler, body):
    """Send a test Telegram notification."""
    try:
        notifier = TelegramNotifier()
        if not notifier.enabled or not notifier.bot_token:
            handler.send_json({"success": False, "error": "Telegram not configured"}, 503)
            return
        ok = notifier.test()
        handler.send_json({"success": ok})
    except Exception as e:
        handler.send_json({"success": False, "error": str(e)}, 500)


def post_notifications_fire(handler, body):
    """Webhook for external services to fire notifications."""
    event = body.get("event", "")
    data = body.get("data", {})
    allowed_events = {
        "trust/drop", "agent/registered", "moderation/reject",
        "review/approved", "review/changes", "review/closed",
        "workflow/phase", "workflow/complete",
        "task/stale", "agent/dead",
    }
    if not event or event not in allowed_events:
        handler.send_json({
            "error": f"Invalid event: {event}",
            "allowed": sorted(allowed_events),
        }, 400)
        return
    if not isinstance(data, dict):
        handler.send_json({"error": "data must be a dict"}, 400)
        return
    telegram_notify(event, data)
    handler.send_json({"success": True, "event": event, "queued": True})


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/tips":                get_tips,
    "/tips/all":            get_tips_all,
    "/agents/presence":     get_agents_presence,
    "/agents/activity":     get_agents_activity,
    "/compact/status":      get_compact_status,
    "/retention/status":    get_retention_status,
    "/usage":               get_usage,
    "/usage/timeline":      get_usage_timeline,
    "/memory/search":       get_memory_search,
    "/memory/get":          get_memory_get,
    "/memory/stats":        get_memory_stats,
    "/notifications/stats": get_notifications_stats,
}

POST_ROUTES = {
    "/agent/heartbeat":     post_agent_heartbeat,
    "/compact":             post_compact,
    "/retention/gc":        post_retention_gc,
    "/usage/report":        post_usage_report,
    "/notifications/test":  post_notifications_test,
    "/notifications/fire":  post_notifications_fire,
}
