"""Brainstorm routes: /brainstorm/*, /idea"""

import aircp_user_config as _ucfg
from aircp_daemon import (
    storage, _bot_send, ensure_room, _resolve_project,
    get_brainstorm_config, get_brainstorm_timeout_for_participants,
    HUMAN_AGENTS,
)


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_brainstorm_active(handler, parsed, params):
    sessions = storage.get_active_brainstorm_sessions()
    handler.send_json({"sessions": sessions, "count": len(sessions)})


def get_brainstorm_history(handler, parsed, params):
    limit = int(params.get("limit", [20])[0])
    sessions = storage.get_brainstorm_history(limit)
    handler.send_json({"sessions": sessions, "count": len(sessions)})


def get_brainstorm_config_handler(handler, parsed, params):
    config = get_brainstorm_config()
    handler.send_json(config)


def get_brainstorm_by_id(handler, parsed, params):
    try:
        session_id = int(parsed.path.split("/")[-1])
        session = storage.get_brainstorm_session(session_id)
        if session:
            handler.send_json(session)
        else:
            handler.send_json({"error": "Session not found"}, 404)
    except ValueError:
        handler.send_json({"error": "Invalid session ID"}, 400)


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def post_brainstorm_create(handler, body):
    try:
        topic = body.get("topic", "")
        created_by = body.get("created_by", _ucfg.user())
        participants = body.get("participants")
        task_id = body.get("task_id")

        if not topic:
            handler.send_json({"error": "Missing 'topic' field"}, 400)
            return

        # Dedup: reject if an active session has the same topic
        active = storage.get_active_brainstorm_sessions()
        for existing in active:
            if existing.get("topic", "").strip().lower() == topic.strip().lower():
                handler.send_json({
                    "error": f"Active brainstorm already exists with same topic (session #{existing['id']})",
                    "existing_session_id": existing["id"]
                }, 409)
                return

        config = get_brainstorm_config()

        if not participants:
            participants = config.get("default_participants", ["@alpha", "@sonnet", "@haiku"])

        if created_by not in participants and created_by not in HUMAN_AGENTS:
            participants.append(created_by)

        timeout_seconds = config.get("timeout_seconds", 180)
        timeout_seconds = get_brainstorm_timeout_for_participants(participants, timeout_seconds)
        channel = config.get("channel", "#brainstorm")

        project_id = _resolve_project(body, created_by)

        session_id = storage.create_brainstorm_session(
            topic=topic,
            created_by=created_by,
            participants=participants,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            project_id=project_id
        )

        if session_id > 0:
            if created_by not in HUMAN_AGENTS:
                storage.add_brainstorm_vote(session_id, created_by, "\u2705", "Auto-vote: creator")
            participant_tags = " ".join(participants)
            dispatch_msg = f"\U0001f9e0 **BRAINSTORM #{session_id}** - New topic!\n"
            dispatch_msg += f"**Topic:** {topic}\n"
            dispatch_msg += f"**From:** {created_by}\n"
            dispatch_msg += f"**Participants:** {participant_tags}\n"
            dispatch_msg += f"**Format:** \u2705/\u274c + max 2 lines (EN)\n"
            dispatch_msg += f"**Timeout:** {timeout_seconds // 60}min (silence = approval)\n"
            dispatch_msg += f"\nReply with: POST /brainstorm/vote {{\"session_id\": {session_id}, \"vote\": \"\u2705\", \"comment\": \"...\"}}"

            ensure_room(channel)
            _bot_send(channel, dispatch_msg, from_id="@brainstorm")

            ensure_room("#general")
            short_msg = f"\U0001f9e0 @all Brainstorm #{session_id} created by {created_by}: {topic[:60]}... \u2192 {channel} - Vote!"
            _bot_send("#general", short_msg, from_id="@brainstorm")

            handler.send_json({
                "status": "created",
                "session_id": session_id,
                "topic": topic,
                "participants": participants,
                "timeout_seconds": timeout_seconds,
                "channel": channel
            })
        else:
            handler.send_json({"error": "Failed to create brainstorm session"}, 500)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_brainstorm_vote(handler, body):
    try:
        session_id = body.get("session_id")
        agent_id = body.get("agent_id")
        vote = body.get("vote", "")
        comment = body.get("comment")

        if not session_id:
            handler.send_json({"error": "Missing 'session_id' field"}, 400)
            return

        if not agent_id:
            handler.send_json({"error": "Missing 'agent_id' field"}, 400)
            return

        if not vote:
            handler.send_json({"error": "Missing 'vote' field (use \u2705 or \u274c)"}, 400)
            return

        session = storage.get_brainstorm_session(session_id)
        if not session:
            handler.send_json({"error": "Session not found"}, 404)
            return

        if session.get("status") != "pending":
            handler.send_json({"error": "Session already closed"}, 400)
            return

        success = storage.add_brainstorm_vote(session_id, agent_id, vote, comment)

        if success:
            config = get_brainstorm_config()
            channel = config.get("channel", "#brainstorm")
            comment_str = f" - {comment}" if comment else ""
            vote_msg = f"\U0001f4dd @all {agent_id} voted {vote} on brainstorm #{session_id}{comment_str}"
            ensure_room(channel)
            _bot_send(channel, vote_msg, from_id="@brainstorm")

            participants = session.get("participants", [])
            updated_session = storage.get_brainstorm_session(session_id)
            votes = updated_session.get("votes", []) if updated_session else []

            if len(votes) >= len(participants):
                print(f"[BRAINSTORM] All votes in for session #{session_id}")

            handler.send_json({
                "status": "voted",
                "session_id": session_id,
                "agent_id": agent_id,
                "vote": vote,
                "votes_so_far": len(votes),
                "participants_count": len(participants)
            })
        else:
            handler.send_json({"error": "Failed to record vote"}, 500)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_idea(handler, body):
    try:
        idea = body.get("idea", "")
        created_by = body.get("created_by", _ucfg.user())
        participants = body.get("participants")
        wf_mode = body.get("mode", "standard")

        if not idea:
            handler.send_json({"error": "Missing 'idea' field"}, 400)
            return

        if len(idea.strip()) < 5:
            handler.send_json({"error": "Idea too short (min 5 chars)"}, 400)
            return

        config = get_brainstorm_config()

        if not participants:
            participants = config.get("default_participants", ["@alpha", "@sonnet", "@haiku"])

        if created_by not in participants and created_by not in HUMAN_AGENTS:
            participants.append(created_by)

        timeout_seconds = body.get("timeout_seconds", config.get("timeout_seconds", 180))
        if not body.get("timeout_seconds"):
            timeout_seconds = get_brainstorm_timeout_for_participants(participants, timeout_seconds)
        channel = config.get("channel", "#brainstorm")

        session_id = storage.create_brainstorm_session(
            topic=idea,
            created_by=created_by,
            participants=participants,
            timeout_seconds=timeout_seconds,
            task_id=None,
            auto_workflow=True,
            workflow_mode=wf_mode,
        )

        if session_id > 0:
            if created_by not in HUMAN_AGENTS:
                storage.add_brainstorm_vote(session_id, created_by, "\u2705", "Auto-vote: creator")
            participant_tags = " ".join(participants)
            dispatch_msg = f"\U0001f4a1 **IDEA #{session_id}** - New idea!\n"
            dispatch_msg += f"**Idea:** {idea}\n"
            dispatch_msg += f"**From:** {created_by}\n"
            dispatch_msg += f"**Participants:** {participant_tags}\n"
            dispatch_msg += f"**Format:** \u2705 GO / \u274c NO GO + 1 line max\n"
            dispatch_msg += f"**Timeout:** {timeout_seconds // 60}min\n"
            dispatch_msg += f"**Mode:** {wf_mode}\n"
            dispatch_msg += f"**Auto-workflow:** If GO \u2192 workflow auto-start \U0001f680"

            ensure_room(channel)
            _bot_send(channel, dispatch_msg, from_id="@idea")

            ensure_room("#general")
            short_msg = f"\U0001f4a1 @all **IDEA #{session_id}** from {created_by}: {idea[:60]}{'...' if len(idea) > 60 else ''} \u2192 {channel} - Vote GO/NO GO!"
            _bot_send("#general", short_msg, from_id="@idea")

            handler.send_json({
                "status": "created",
                "session_id": session_id,
                "idea": idea,
                "participants": participants,
                "timeout_seconds": timeout_seconds,
                "channel": channel,
                "auto_workflow": True,
                "mode": wf_mode,
            })
        else:
            handler.send_json({"error": "Failed to create idea session"}, 500)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/brainstorm/active":  get_brainstorm_active,
    "/brainstorm/history": get_brainstorm_history,
    "/brainstorm/config":  get_brainstorm_config_handler,
}

POST_ROUTES = {
    "/brainstorm/create": post_brainstorm_create,
    "/brainstorm/vote":   post_brainstorm_vote,
    "/idea":              post_idea,
}

GET_PREFIX_ROUTES = [
    ("/brainstorm/", get_brainstorm_by_id),
]
