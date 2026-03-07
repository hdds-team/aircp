"""Autonomy routes: /claim, /lock, /heartbeat, /mode, /ask, /stop, /handover, ..."""

import asyncio

from aircp_daemon import (
    autonomy, transport, storage,
    joined_rooms, _bot_send,
)


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_presence(handler, parsed, params):
    agent = params.get("agent", [None])[0]
    if agent:
        result = autonomy.presence_query(agent)
    else:
        result = autonomy.presence_query()
    handler.send_json(result)


def get_claims(handler, parsed, params):
    resource = params.get("resource", [None])[0]
    result = autonomy.claim_query(resource)
    handler.send_json(result)


def get_locks(handler, parsed, params):
    path = params.get("path", [None])[0]
    result = autonomy.lock_query(path)
    handler.send_json(result)


def get_mute_status(handler, parsed, params):
    handler.send_json({
        "muted": autonomy.is_muted(),
        "remaining_seconds": autonomy.mute_remaining_seconds()
    })


def get_spam_stats(handler, parsed, params):
    handler.send_json(autonomy.get_spam_stats())


def get_mode(handler, parsed, params):
    mode_state = autonomy.get_mode_state()
    handler.send_json({
        "mode": mode_state.mode if mode_state else "neutral",
        "lead": mode_state.lead if mode_state else "",
        "started_at": mode_state.started_at.isoformat() if mode_state and mode_state.started_at else None,
        "timeout_at": mode_state.timeout_at.isoformat() if mode_state and mode_state.timeout_at else None,
        "time_remaining": str(mode_state.time_remaining()) if mode_state and mode_state.time_remaining() else None,
        "pending_asks": mode_state.pending_asks if mode_state else []
    })


def get_mode_history(handler, parsed, params):
    try:
        limit = int(params.get("limit", [10])[0])
    except (ValueError, IndexError):
        handler.send_json({"error": "Invalid limit parameter"}, 400)
        return
    history = autonomy.get_mode_history(limit)
    handler.send_json({"history": history, "count": len(history)})


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def post_claim(handler, body):
    try:
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
            handler.send_json({"error": f"Unknown action: {action}"}, 400)
            return

        handler.send_json(result)
    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_lock(handler, body):
    try:
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
            handler.send_json({"error": f"Unknown action: {action}"}, 400)
            return

        handler.send_json(result)
    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_heartbeat(handler, body):
    try:
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
        handler.send_json(result)
    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_activity(handler, body):
    try:
        agent_id = body.get("agent_id", transport.agent_id)
        autonomy.log_activity(
            from_agent=agent_id,
            action_type=body.get("action_type", "unknown"),
            summary=body.get("summary", ""),
            details=body.get("details")
        )
        handler.send_json({"status": "logged"})
    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_stfu(handler, body):
    minutes = body.get("minutes", 5)
    until = autonomy.stfu(minutes)
    msg = f"\U0001f910 **STFU MODE ACTIV\u00c9** pour {minutes} minutes. Silence total jusqu'\u00e0 {until.strftime('%H:%M:%S')} UTC."
    for room in list(joined_rooms):
        _bot_send(room, msg, from_id="@system")
    handler.send_json({
        "status": "muted",
        "minutes": minutes,
        "until": until.isoformat()
    })


def post_talk(handler, body):
    autonomy.talk()
    msg = "\U0001f5e3\ufe0f **STFU MODE D\u00c9SACTIV\u00c9** \u2014 Les agents peuvent parler \u00e0 nouveau."
    for room in list(joined_rooms):
        _bot_send(room, msg, from_id="@system")
    handler.send_json({"status": "unmuted"})


def post_reset_leader(handler, body):
    autonomy.reset_leader_mode()
    msg = "\U0001f504 **LEADER MODE RESET** \u2014 Retour au mode libre. Compteurs spam remis \u00e0 z\u00e9ro."
    for room in list(joined_rooms):
        _bot_send(room, msg, from_id="@system")
    handler.send_json({
        "status": "reset",
        "leader_mode": False,
        "spam_incidents": 0
    })


def post_leader_mode(handler, body):
    leader = body.get("leader", "@alpha")
    autonomy.leader_mode = True
    autonomy.leader_id = leader
    msg = f"\U0001f451 **LEADER MODE ACTIV\u00c9** \u2014 Seul {leader} peut coordonner. Les autres attendent d'\u00eatre sollicit\u00e9s."
    for room in list(joined_rooms):
        _bot_send(room, msg, from_id="@system")
    handler.send_json({
        "status": "leader_mode_on",
        "leader": leader
    })


def post_mode(handler, body):
    try:
        mode = body.get("mode", "neutral")
        lead = body.get("lead", "")
        timeout_minutes = body.get("timeout_minutes")
        reason = body.get("reason", "manual")

        valid_modes = ["neutral", "focus", "review", "build"]
        if mode not in valid_modes:
            handler.send_json({"error": f"Invalid mode. Must be one of: {valid_modes}"}, 400)
            return

        autonomy.set_mode(mode, lead, timeout_minutes, reason)

        if mode == "neutral":
            msg = "\U0001f504 **MODE NEUTRAL** \u2014 Retour au mode libre."
        else:
            timeout_str = f" (timeout: {timeout_minutes}min)" if timeout_minutes else ""
            msg = f"\U0001f3af **MODE {mode.upper()}** \u2014 Lead: {lead}{timeout_str}"

        for room in list(joined_rooms):
            _bot_send(room, msg, from_id="@system")

        mode_state = autonomy.get_mode_state()
        handler.send_json({
            "status": "mode_changed",
            "mode": mode,
            "lead": lead,
            "timeout_minutes": timeout_minutes,
            "started_at": mode_state.started_at.isoformat() if mode_state and mode_state.started_at else None
        })
    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_ask(handler, body):
    try:
        from_agent = body.get("from", transport.agent_id)
        to_agent = body.get("to", "@all")
        question = body.get("question", "")

        result = autonomy.register_ask(from_agent, to_agent, question)

        msg = f"\u2753 **@ask** de {from_agent} \u2192 {to_agent}: {question[:100]}"
        for room in list(joined_rooms):
            if room == "#general":
                _bot_send(room, msg, from_id="@system")

        handler.send_json(result)
    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_stop(handler, body):
    try:
        autonomy.set_mode("neutral", "", None, reason="manual")

        msg = "\U0001f6d1 **STOP** \u2014 Mode reset, tous les @ask annul\u00e9s."
        for room in list(joined_rooms):
            _bot_send(room, msg, from_id="@system")

        handler.send_json({"status": "stopped", "mode": "neutral"})
    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_handover(handler, body):
    try:
        to_agent = body.get("to")
        if not to_agent:
            handler.send_json({"error": "Missing 'to' field"}, 400)
            return

        mode_state = autonomy.get_mode_state()
        current_mode = mode_state.mode if mode_state else "neutral"

        autonomy.set_mode(current_mode, to_agent, None, reason="override")

        msg = f"\U0001f504 **HANDOVER** \u2014 Lead transf\u00e9r\u00e9 \u00e0 {to_agent}"
        for room in list(joined_rooms):
            _bot_send(room, msg, from_id="@system")

        handler.send_json({"status": "handover", "new_lead": to_agent, "mode": current_mode})
    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/presence":         get_presence,
    "/claims":           get_claims,
    "/locks":            get_locks,
    "/mute-status":      get_mute_status,
    "/spam-stats":       get_spam_stats,
    "/mode":             get_mode,
    "/mode/history":     get_mode_history,
}

POST_ROUTES = {
    "/claim":         post_claim,
    "/lock":          post_lock,
    "/heartbeat":     post_heartbeat,
    "/activity":      post_activity,
    "/stfu":          post_stfu,
    "/talk":          post_talk,
    "/reset-leader":  post_reset_leader,
    "/leader-mode":   post_leader_mode,
    "/mode":          post_mode,
    "/ask":           post_ask,
    "/stop":          post_stop,
    "/handover":      post_handover,
}
