"""Messaging routes: /send, /history, /files

Upload handlers (/upload, /uploads/) stay on AircpHandler (raw rfile reads).
"""

import pathlib
import time

from aircp_daemon import (
    transport, storage, autonomy,
    joined_rooms, message_history,
    _bot_send, ensure_room, _auto_dispatch, _has_mention,
    _detect_non_english, _resolve_project, _is_path_within,
    save_to_memory, _persist_to_db, telegram_notify,
    load_alpha_memory, HUMAN_SENDERS,
)


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_files(handler, parsed, params):
    raw_path = params.get("path", ["/projects/aircp"])[0]
    base = pathlib.Path(raw_path).resolve()
    sandbox = pathlib.Path("/projects").resolve()
    if not _is_path_within(base, sandbox):
        handler.send_json({"error": "Path outside sandbox"}, 403)
    elif not base.exists():
        handler.send_json({"error": "Path not found"}, 404)
    elif base.is_file():
        handler.send_json({"path": str(base), "type": "file"})
    else:
        entries = []
        try:
            for entry in sorted(base.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
                name = entry.name
                if name.startswith(".") or name == "__pycache__" or name == "node_modules":
                    continue
                entries.append({
                    "name": name,
                    "path": str(entry),
                    "type": "dir" if entry.is_dir() else "file",
                })
        except PermissionError:
            pass
        handler.send_json({"path": str(base), "entries": entries})


def get_history(handler, parsed, params):
    room = params.get("room", ["#general"])[0]
    limit = int(params.get("limit", [20])[0])
    project_filter = params.get("project", [None])[0]

    all_msgs = list(message_history)
    all_msgs.extend(load_alpha_memory(room))

    if project_filter and project_filter != "default":
        all_msgs = [m for m in all_msgs if m.get("project", "default") in (project_filter, "default", "")]

    seen = set()
    unique = []
    for m in all_msgs:
        ts_key = m.get("timestamp", 0) // 1_000_000_000
        key = (m.get("from", ""), m.get("content", "")[:50], ts_key)
        if key not in seen:
            seen.add(key)
            unique.append(m)
    unique.sort(key=lambda x: x.get("timestamp", 0))

    room_msgs = [m for m in unique if m["room"] == room][-limit:]
    handler.send_json({"room": room, "count": len(room_msgs), "messages": room_msgs})


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def post_send(handler, body):
    try:
        room = body.get("room", "#general")
        message = body.get("message", "")

        if not message:
            handler.send_json({"error": "Missing message"}, 400)
            return

        ensure_room(room)

        from_id = body.get("from", transport.agent_id)

        # v0.5 MODES: can_speak() enforcement (BEFORE spam check)
        is_ask_response = body.get("is_ask_response", False)
        can_speak, reason = autonomy.can_speak(from_id, is_ask_response)

        if not can_speak:
            print(f"[MODES] Blocked message from {from_id}: {reason}")
            handler.send_json({
                "success": False,
                "error": "mode_blocked",
                "message": f"\u26a0\ufe0f {reason}",
                "mode": autonomy.get_mode_state().mode if autonomy.get_mode_state() else "neutral"
            }, 403)
            return

        # POC v0.5: Spam detection + progressive mute
        spam_check = autonomy.check_spam(from_id, message)

        if spam_check["action"] == "mute":
            print(f"[SPAM] BLOCKED message from {from_id}: muted")
            telegram_notify("moderation/reject", {
                "agent_id": from_id,
                "reason": spam_check.get("message", "spam detected"),
                "muted_seconds": spam_check.get("muted_seconds", 0),
            })
            handler.send_json({
                "success": False,
                "error": "muted",
                "message": spam_check["message"],
                "muted_seconds": spam_check["muted_seconds"]
            }, 403)
            return

        if spam_check["action"] == "reminder":
            reminder_msg = spam_check["message"]
            print(f"[SPAM] REMINDER for {from_id}")
            _bot_send(room, f"[REMINDER \u2192 {from_id}] {reminder_msg}", from_id="@system")

        # Check leader mode (fallback - kept for backwards compatibility)
        if autonomy.is_leader_mode():
            if from_id not in HUMAN_SENDERS and from_id != autonomy.leader_id:
                if not any(f"@{from_id.lstrip('@')}" in m.get("content", "")
                           for m in list(message_history)[-10:]
                           if m.get("from") in (autonomy.leader_id, *HUMAN_SENDERS)):
                    print(f"[LEADER MODE] Blocked unsolicited message from {from_id}")
                    handler.send_json({
                        "success": False,
                        "error": "leader_mode",
                        "message": "\u26a0\ufe0f Mode leader actif. Seul "
                                   f"{autonomy.leader_id} peut coordonner. "
                                   "Attends d'\u00eatre sollicit\u00e9."
                    }, 403)
                    return

        # v0.4 AUTO-DISPATCH: Route messages from humans without @mention
        if from_id in HUMAN_SENDERS and not _has_mention(message):
            target = _auto_dispatch(message)
            prefix = f"[Auto \u2192 @{target}] "
            message = prefix + message
            print(f"[DISPATCH] Auto-routed '{from_id}' message to @{target}")

        # v3.1: #brainstorm language enforcement (English only)
        if room == "#brainstorm" and from_id not in HUMAN_SENDERS:
            if _detect_non_english(message):
                shame_msg = (
                    f"\U0001f6a8 **LANGUAGE CHECK** \u2014 {from_id}, #brainstorm is **English only**. "
                    f"~30% fewer tokens, better for all models. Rewrite in English please."
                )
                _bot_send(room, shame_msg, from_id="@system")
                print(f"[LANG] Non-English detected from {from_id} in #brainstorm")

        # Resolve project workspace
        project_id = _resolve_project(body, from_id)

        msg_id = transport.send_chat(room, message, from_id=from_id, project=project_id)

        # Add to local history and persist
        if msg_id:
            entry = {
                "id": msg_id,
                "room": room,
                "from": from_id,
                "content": message,
                "timestamp": time.time_ns(),
                "project": project_id,
            }
            message_history.append(entry)
            save_to_memory(entry)
            _persist_to_db(entry)

        handler.send_json({
            "success": msg_id is not None,
            "message_id": msg_id,
            "room": room,
            "auto_dispatched": from_id in HUMAN_SENDERS and not _has_mention(body.get("message", "")),
            "leader_mode": autonomy.is_leader_mode()
        })

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/files":   get_files,
    "/history": get_history,
}

POST_ROUTES = {
    "/send": post_send,
}
