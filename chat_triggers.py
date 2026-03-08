"""Chat trigger functions for aircp daemon.

Phase 3 extraction: poll_messages() and all chat-triggered parsers/processors
moved from aircp_daemon.py. This is the riskiest extraction because
_detect_implicit_review has side-effects (DB writes + auto-close reviews).

Functions:
- poll_messages() — main DDS polling loop (background thread)
- parse_aircp_vote() / process_brainstorm_vote() — brainstorm vote detection
- parse_task_command() / process_task_command() — @task chat commands
- parse_compact_command() / process_compact_command() — @compact chat commands
- _detect_implicit_review() / _check_review_consensus() — auto-detect review votes
- _run_auto_compact() — threshold-triggered background compaction
"""

import logging
import re
import threading
import time
from datetime import datetime, timezone

from compact_engine import compact_room
from aircp_daemon import (
    storage, transport, joined_rooms, message_history,
    _compact_msg_counter, _last_compact_time, _compact_lock,
    _envelopes_to_messages,
    _bot_send, save_to_memory, _persist_to_db,
    get_brainstorm_config,
    _SYSTEM_BOTS, HUMAN_SENDERS,
    COMPACT_AUTO_THRESHOLD, COMPACT_AUTO_INTERVAL,
)

logger = logging.getLogger("aircp_daemon")


# =============================================================================
# Regex patterns for chat trigger detection
# =============================================================================

# Brainstorm vote: ✅ <decision> or ❌ <reason>
VOTE_PATTERN = re.compile(r'^([\u2705\u274c])\s*(.*)$', re.MULTILINE)

# @task commands: create, list, done, complete, activity, claim
TASK_COMMAND_PATTERN = re.compile(
    r'@task\s+(create|list|done|complete|activity|claim)\s*(.*)',
    re.IGNORECASE
)
# Helper to extract key=value or key="value with spaces" pairs
TASK_KV_PATTERN = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([\S]+))')

# @compact commands: compact, status, force
COMPACT_COMMAND_PATTERN = re.compile(
    r'^@compact\s*(status|force|#\w+)?\s*(force)?\s*$',
    re.IGNORECASE
)

# Implicit review vote detection patterns
_REVIEW_APPROVE_PATTERNS = [
    re.compile(r"\bLGTM\b", re.IGNORECASE),
    re.compile(r"\bapproved?\b", re.IGNORECASE),
    re.compile(r"\bno\s*issues?\s*(found|detected|bloqu)", re.IGNORECASE),
    re.compile(r"\bQA\s*pass(ed)?\b", re.IGNORECASE),
    re.compile(r"\bcode\s*(is\s*)?(clean|good|solid)\b", re.IGNORECASE),
    re.compile(r"\bGO\s*!?\s*$", re.MULTILINE),
]

_REVIEW_CHANGES_PATTERNS = [
    re.compile(r"\bchanges?\s*request", re.IGNORECASE),
    re.compile(r"\bNO[- ]GO\b", re.IGNORECASE),
    re.compile(r"\bblocking\s*(issue|problem|bug)", re.IGNORECASE),
    re.compile(r"\bshowstopper\b", re.IGNORECASE),
    re.compile(r"\breject(ed)?\b", re.IGNORECASE),
]


# =============================================================================
# Auto-compaction (threshold-triggered, non-blocking)
# =============================================================================

def _run_auto_compact(room: str, transport_ref, storage_ref):
    """Run auto-compaction in a separate thread (non-blocking).
    Compactor v3: soft-delete + summary insertion. No chat noise.
    """
    if not _compact_lock.acquire(blocking=False):
        return
    try:
        # Fetch visible (non-compacted) messages for classification
        history = storage_ref.get_room_history(room, limit=500)
        messages_raw = history.get("messages", [])
        messages = _envelopes_to_messages(messages_raw, room)
        result = compact_room(messages, room, "@system", force=True)
        if result:
            all_ids = result.get("deleted_ids", []) + result.get("compacted_ids", [])
            if all_ids:
                # v3: soft-delete instead of hard delete
                n = storage_ref.soft_delete_messages(all_ids)
                logger.info(f"[COMPACTv3] Soft-deleted {n} messages in {room}")
            # Insert summary as a DB record (not a chat message)
            summary = result.get("summary", "")
            if summary:
                storage_ref.insert_summary_message(room, summary)
            # Audit log
            storage_ref.log_compaction(
                room=room,
                triggered_by="@system/auto",
                total_before=result.get("total_before", 0),
                total_after=result.get("total_after", 0),
                deleted_count=result.get("deleted_count", 0),
                compacted_count=result.get("compacted_count", 0),
                compression_ratio=result.get("compression_ratio", "?"),
                summary=summary,
            )
        ratio = result.get("compression_ratio", "?") if result else "skip"
        logger.info(f"[COMPACTv3] Auto-compacted {room}: {ratio}")
    except Exception as e:
        logger.error(f"[COMPACTv3] Auto-compact error for {room}: {e}")
    finally:
        # Always reset counter/timer to prevent infinite retry on failure
        _compact_msg_counter[room] = 0
        _last_compact_time[room] = time.time()
        _compact_lock.release()


# =============================================================================
# Brainstorm vote parsing (DDS-based voting)
# =============================================================================

def parse_aircp_vote(message: str, from_id: str, room: str) -> dict | None:
    """Parse a vote from an AIRCP message on #brainstorm or #general channel.

    v1.2: Also accepts votes from #general (agents vote where they chat).
    Returns dict with agent_id, vote, comment or None if not a vote.
    """
    brainstorm_channel = get_brainstorm_config().get("channel", "#brainstorm")
    # v1.2: Accept votes from both #brainstorm AND #general
    if room not in (brainstorm_channel, "#general"):
        return None

    # Skip system messages
    if from_id in ("@brainstorm", "@system", "@watchdog", "@hub"):
        return None

    match = VOTE_PATTERN.search(message)
    if not match:
        return None

    vote = match.group(1)  # ✅ or ❌
    comment = match.group(2).strip() if match.group(2) else None

    return {
        "agent_id": from_id,
        "vote": vote,
        "comment": comment
    }


def process_brainstorm_vote(vote_data: dict) -> bool:
    """Process a parsed vote and record it in SQLite.

    Finds the most recent active brainstorm session and records the vote.
    """
    if not storage:
        return False

    agent_id = vote_data.get("agent_id")
    vote = vote_data.get("vote")
    comment = vote_data.get("comment")

    # Get active brainstorm sessions
    active_sessions = storage.get_active_brainstorm_sessions()
    if not active_sessions:
        print(f"[BRAINSTORM] Vote from {agent_id} but no active session")
        return False

    # Use the most recent active session
    session = active_sessions[0]
    session_id = session.get("id")

    # Check if agent is a participant
    participants = session.get("participants", [])
    if agent_id not in participants:
        print(f"[BRAINSTORM] Vote from {agent_id} ignored - not a participant")
        return False

    # Record the vote
    success = storage.add_brainstorm_vote(session_id, agent_id, vote, comment)
    if success:
        print(f"[BRAINSTORM] Vote recorded via AIRCP: {agent_id} = {vote} on session #{session_id}")
    return success


# =============================================================================
# @compact chat commands
# =============================================================================

def parse_compact_command(message: str, from_id: str, room: str) -> dict | None:
    """Parse a @compact command from an AIRCP chat message.

    v2.0: Chat-triggered compaction.
    Formats:
      @compact              -> compact current room
      @compact force        -> force compact even below threshold
      @compact #general     -> compact specific room
      @compact status       -> show compaction status

    Returns dict with action + params, or None if not a @compact command.
    """
    # Only parse messages that START with @compact (avoid false positives)
    if not message.strip().lower().startswith("@compact"):
        return None

    # Ignore messages from system bots (prevent loops)
    if from_id in ("@compactor", "@system", "@watchdog", "@taskman"):
        return None

    match = COMPACT_COMMAND_PATTERN.match(message.strip())
    if not match:
        return None

    arg1 = (match.group(1) or "").strip().lower()
    arg2 = (match.group(2) or "").strip().lower()

    # Determine action
    if arg1 == "status":
        return {"action": "status", "room": room, "from": from_id}

    target_room = room  # default: current room
    force = False

    if arg1.startswith("#"):
        target_room = arg1
    elif arg1 == "force":
        force = True

    if arg2 == "force":
        force = True

    return {"action": "compact", "room": target_room, "force": force, "from": from_id}


def process_compact_command(cmd: dict) -> bool:
    """Process a parsed @compact command.

    Triggers compaction via the engine and posts results to chat.
    Returns True if command was processed, False otherwise.
    """
    if not transport or not storage:
        return False

    action = cmd.get("action", "")
    room = cmd.get("room", "#general")
    from_id = cmd.get("from", "@system")

    try:
        if action == "status":
            # Show compaction status for the room
            counter = _compact_msg_counter.get(room, 0)
            last_time = _last_compact_time.get(room, 0)
            since = int(time.time() - last_time) if last_time else None

            try:
                import requests as _req_check
                llm_available = True
            except ImportError:
                llm_available = False

            msg = (
                f"\U0001f4ca **Compaction status for {room}:**\n"
                f"  Messages since last compact: **{counter}**\n"
                f"  Auto-trigger threshold: **{COMPACT_AUTO_THRESHOLD}**\n"
                f"  Last compaction: **{f'{since}s ago' if since else 'never'}**\n"
                f"  LLM summary: **{'available (Ollama)' if llm_available else 'unavailable'}**"
            )
            _bot_send(room, msg, from_id="@compactor")
            return True

        elif action == "compact":
            force = cmd.get("force", False)

            # Fetch visible (non-compacted) history
            history = storage.get_room_history(room, limit=500)
            messages_raw = history.get("messages", [])

            if not messages_raw:
                _bot_send(room, "No messages to compact in {}.".format(room), from_id="@compactor")
                return True

            messages = _envelopes_to_messages(messages_raw, room)
            result = compact_room(messages, room, from_id, force=force)

            if result is None:
                _bot_send(
                    room,
                    "Compaction not needed for {} ({} msgs, below threshold). "
                    "Use `@compact force` to override.".format(room, len(messages)),
                    from_id="@compactor"
                )
            else:
                # v3: soft-delete + summary insertion
                all_ids = result.get("deleted_ids", []) + result.get("compacted_ids", [])
                if all_ids:
                    n = storage.soft_delete_messages(all_ids)
                    logger.info(f"[COMPACTv3] Soft-deleted {n} messages in {room}")
                summary = result.get("summary", "")
                if summary:
                    storage.insert_summary_message(room, summary)
                # Audit log
                storage.log_compaction(
                    room=room,
                    triggered_by=from_id,
                    total_before=result.get("total_before", 0),
                    total_after=result.get("total_after", 0),
                    deleted_count=result.get("deleted_count", 0),
                    compacted_count=result.get("compacted_count", 0),
                    compression_ratio=result.get("compression_ratio", "?"),
                    summary=summary,
                )
                _compact_msg_counter[room] = 0
                _last_compact_time[room] = time.time()
                # Brief confirmation (not the full summary -- that's in DB now)
                _bot_send(
                    room,
                    "Compacted {}: {} soft-deleted, {} kept, ratio {}".format(
                        room, len(all_ids),
                        result.get("total_after", 0),
                        result.get("compression_ratio", "?")
                    ),
                    from_id="@compactor"
                )

            return True

    except Exception as e:
        _bot_send(room, f"\u274c @compact error: {e}", from_id="@compactor")
        logger.error(f"Compact command error: {e}")
        return False

    return False


# =============================================================================
# @task chat commands
# =============================================================================

def parse_task_command(message: str, from_id: str, room: str) -> dict | None:
    """Parse a @task command from an AIRCP chat message.

    v1.3: Allows agents to manage tasks directly from chat.
    Supported commands:
      @task create description="Fix login bug" agent="@alpha"
      @task list [agent=@beta] [status=active]
      @task done id=1
      @task complete id=1 [status=failed]
      @task activity id=1 [step="Working on auth"]
      @task claim id=1

    Returns dict with action + params, or None if not a @task command.
    """
    # Skip system/bot messages to avoid loops
    if from_id in ("@taskman", "@system", "@watchdog", "@hub", "@brainstorm", "@idea", "@review"):
        return None

    match = TASK_COMMAND_PATTERN.match(message)
    if not match:
        return None

    action = match.group(1).lower()
    args_str = match.group(2).strip()

    # Parse key=value pairs from args
    params = {}
    for kv_match in TASK_KV_PATTERN.finditer(args_str):
        key = kv_match.group(1)
        value = kv_match.group(2) if kv_match.group(2) is not None else kv_match.group(3)
        params[key] = value

    # Normalize "done" -> "complete"
    if action == "done":
        action = "complete"

    return {
        "action": action,
        "params": params,
        "from_id": from_id,
        "room": room,
        "raw_args": args_str,
    }


def process_task_command(cmd: dict) -> bool:
    """Process a parsed @task command and execute via storage + broadcast.

    v1.3: Chat-driven task management.
    Returns True if command was processed successfully.
    """
    if not storage or not transport:
        return False

    action = cmd["action"]
    params = cmd["params"]
    from_id = cmd["from_id"]
    room = cmd.get("room", "#general")

    try:
        # -- @task create --
        if action == "create":
            description = params.get("description", cmd.get("raw_args", ""))
            agent_id = params.get("agent", from_id)  # Default: self-assign
            task_type = params.get("type", "generic")

            if not description or not description.strip():
                msg = f"\u274c @task create: description manquante. Usage: `@task create description=\"...\" [agent=\"@xxx\"]`"
                _bot_send(room, msg, from_id="@taskman")
                return False

            # Clean description if it still contains key=value pairs
            if "description=" not in cmd.get("raw_args", ""):
                # Raw text mode: @task create Fix the login bug
                description = cmd.get("raw_args", "").strip()
                # Remove agent= if present at end
                agent_match = re.search(r'\bagent\s*=\s*(?:"([^"]*)"|([\S]+))', description)
                if agent_match:
                    agent_id = agent_match.group(1) or agent_match.group(2)
                    description = description[:agent_match.start()].strip()

            if not description:
                msg = f"\u274c @task create: description vide."
                _bot_send(room, msg, from_id="@taskman")
                return False

            task_id = storage.create_task(agent_id, task_type, description, None)
            if task_id > 0:
                msg = f"\U0001f4cb **TASK #{task_id}** created for {agent_id}: {description[:80]}"
                _bot_send("#general", msg, from_id="@taskman")
                print(f"[TASK-CHAT] Created task #{task_id} for {agent_id} by {from_id}")
                return True
            else:
                _bot_send(room, "\u274c Task creation failed.", from_id="@taskman")
                return False

        # -- @task list --
        elif action == "list":
            agent_filter = params.get("agent")
            status_filter = params.get("status")

            if agent_filter:
                tasks = storage.get_agent_tasks(agent_filter, status_filter)
            elif status_filter:
                tasks = storage.get_tasks_by_status(status_filter)
            else:
                tasks = storage.get_active_tasks()

            if not tasks:
                _bot_send(room, "\U0001f4cb No active tasks.", from_id="@taskman")
                return True

            lines = [f"\U0001f4cb **{len(tasks)} task(s):**"]
            for t in tasks[:10]:  # Max 10 to avoid chat spam
                tid = t.get("id", "?")
                agent = t.get("agent_id", "?")
                desc = t.get("description", "")[:60]
                status = t.get("status", "?")
                emoji = {
                    "pending": "\u23f3", "in_progress": "\U0001f504",
                    "done": "\u2705", "failed": "\u274c", "stale": "\u26a0\ufe0f"
                }.get(status, "\u2753")
                lines.append(f"  {emoji} #{tid} [{agent}] {desc} ({status})")

            if len(tasks) > 10:
                lines.append(f"  ... et {len(tasks) - 10} autres")

            _bot_send(room, "\n".join(lines), from_id="@taskman")
            print(f"[TASK-CHAT] Listed {len(tasks)} tasks for {from_id}")
            return True

        # -- @task complete / @task done --
        elif action == "complete":
            task_id = params.get("id")
            if not task_id:
                _bot_send(room, "\u274c @task done: id manquant. Usage: `@task done id=1`", from_id="@taskman")
                return False

            try:
                task_id = int(task_id)
            except ValueError:
                _bot_send(room, "\u274c @task done: id must be a number.", from_id="@taskman")
                return False

            status = params.get("status", "done")
            valid_statuses = ["done", "failed", "cancelled"]
            if status not in valid_statuses:
                _bot_send(room, f"\u274c Invalid status. Valid: {valid_statuses}", from_id="@taskman")
                return False

            success = storage.complete_task(task_id, status)
            if success:
                emoji = {
                    "done": "\u2705", "failed": "\u274c", "cancelled": "\U0001f6ab"
                }.get(status, "\u26a0\ufe0f")
                msg = f"{emoji} Task #{task_id} completed ({status})"
                _bot_send("#general", msg, from_id="@taskman")
                print(f"[TASK-CHAT] Completed task #{task_id} as {status} by {from_id}")
                return True
            else:
                _bot_send(room, f"\u274c Task #{task_id} not found or already completed.", from_id="@taskman")
                return False

        # -- @task activity --
        elif action == "activity":
            task_id = params.get("id")
            if not task_id:
                _bot_send(room, "\u274c @task activity: missing id. Usage: `@task activity id=1 [step=\"...\"]`", from_id="@taskman")
                return False

            try:
                task_id = int(task_id)
            except ValueError:
                _bot_send(room, "\u274c @task activity: id must be a number.", from_id="@taskman")
                return False

            step = params.get("step")
            current_step = None
            if step:
                try:
                    current_step = int(step)
                except ValueError:
                    current_step = None  # step is text, ignore numeric conversion

            success = storage.update_task_activity(task_id, current_step)
            if success:
                msg = f"\U0001f504 Tache #{task_id}: activite mise a jour"
                if step:
                    msg += f" (step: {step})"
                _bot_send(room, msg, from_id="@taskman")
                print(f"[TASK-CHAT] Activity update for task #{task_id} by {from_id}")
                return True
            else:
                _bot_send(room, f"\u274c Task #{task_id} not found.", from_id="@taskman")
                return False

        # -- @task claim --
        elif action == "claim":
            task_id = params.get("id")
            if not task_id:
                _bot_send(room, "\u274c @task claim: id manquant. Usage: `@task claim id=1`", from_id="@taskman")
                return False

            try:
                task_id = int(task_id)
            except ValueError:
                _bot_send(room, "\u274c @task claim: id must be a number.", from_id="@taskman")
                return False

            success = storage.claim_task(task_id, from_id)
            if success:
                msg = f"\U0001f680 {from_id} claimed task #{task_id}"
                _bot_send("#general", msg, from_id="@taskman")
                print(f"[TASK-CHAT] Task #{task_id} claimed by {from_id}")
                return True
            else:
                _bot_send(room, f"\u274c Task #{task_id}: claim failed (already claimed or not found).", from_id="@taskman")
                return False

        else:
            _bot_send(room, f"\u274c Unknown @task command: `{action}`. Valid: create, list, done, activity, claim", from_id="@taskman")
            return False

    except Exception as e:
        print(f"[TASK-CHAT] Error processing command: {e}")
        _bot_send(room, f"\u274c @task error: {e}", from_id="@taskman")
        return False


# =============================================================================
# Implicit Review Detection (v3.3)
# =============================================================================
# When an assigned reviewer posts approval/rejection language in chat
# without using the formal MCP review/approve command, auto-trigger it.

def _detect_implicit_review(from_id: str, content: str, room: str):
    """Check if a chat message from an assigned reviewer implies a review vote.
    If so, auto-trigger the formal review/approve or review/changes action.

    v4.1 fixes:
    - Grace period: ignore reviews created less than 30s ago (prevents stale messages
      from being parsed as votes for a brand-new auto-review)
    - Cross-reference guard: if the message mentions a specific review number (#N),
      only apply to that review, not to any other pending review
    """
    if not storage or from_id in _SYSTEM_BOTS or from_id in HUMAN_SENDERS:
        return

    # Get all pending reviews where this agent is an assigned reviewer
    try:
        active = storage.get_active_review_requests()
    except Exception:
        return

    if not active:
        return

    # v4.1: Check if message references a specific review number
    ref_match = re.search(r"#(\d+)", content)
    referenced_review_id = int(ref_match.group(1)) if ref_match else None

    # Find reviews where from_id is a reviewer and hasn't voted yet
    pending_for_agent = []
    now = datetime.now(timezone.utc)
    for rev in active:
        reviewers = rev.get("reviewers", [])
        if from_id in reviewers or from_id.lstrip("@") in [r.lstrip("@") for r in reviewers]:
            # v4.1: Grace period -- skip reviews created less than 30s ago
            created_at = rev.get("created_at", "")
            if created_at:
                try:
                    created_dt = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                    if (now - created_dt).total_seconds() < 30:
                        continue  # Too fresh, skip
                except Exception:
                    pass

            # v4.1: Cross-reference guard -- if message mentions #N, skip if this review is not #N
            if referenced_review_id is not None and rev["id"] != referenced_review_id:
                continue

            # Check if already voted
            full_rev = storage.get_review_request(rev["id"])
            if full_rev:
                already_voted = any(
                    r.get("reviewer") == from_id for r in full_rev.get("responses", [])
                )
                if not already_voted:
                    pending_for_agent.append(full_rev)

    if not pending_for_agent:
        return

    # Detect vote from message content
    vote = None
    for pat in _REVIEW_APPROVE_PATTERNS:
        if pat.search(content):
            vote = "approve"
            break

    if not vote:
        for pat in _REVIEW_CHANGES_PATTERNS:
            if pat.search(content):
                vote = "changes"
                break

    if not vote:
        return

    # Auto-apply to the most recent pending review for this agent
    rev = pending_for_agent[0]
    req_id = rev["id"]
    comment_preview = content[:200] if len(content) > 200 else content

    ok = storage.add_review_response(req_id, from_id, vote, f"[auto-detected from chat] {comment_preview}")
    if ok:
        action_text = "approved" if vote == "approve" else "requested changes on"
        notify = f"Review #{req_id}: {from_id} {action_text} (auto-detected from chat message)"
        _bot_send(room, notify, from_id="@review")
        logger.info(f"[REVIEW] Auto-detected: {from_id} {vote} review #{req_id}")

        # Check if review is now complete (doc=1, code=2 approvals)
        _check_review_consensus(req_id)


def _check_review_consensus(request_id: int):
    """Check if a review has enough approvals to close."""
    rev = storage.get_review_request(request_id)
    if not rev or rev.get("status") != "pending":
        return

    review_type = rev.get("review_type", "doc")
    needed = 2 if review_type == "code" else 1

    approvals = [r for r in rev.get("responses", []) if r.get("vote") == "approve"]
    changes = [r for r in rev.get("responses", []) if r.get("vote") == "changes"]

    if len(approvals) >= needed and not changes:
        storage.close_review_request(request_id, "approved", "completed")
        _bot_send(
            "#general",
            f"Review #{request_id} **approved** ({len(approvals)}/{needed} approvals)",
            from_id="@review"
        )
        logger.info(f"[REVIEW] #{request_id} closed: approved ({len(approvals)}/{needed})")


# =============================================================================
# Main polling loop (background thread)
# =============================================================================

def poll_messages():
    """Background thread to poll incoming messages.

    v1.1: Also detects and processes brainstorm votes from #brainstorm channel.
    v3.2: Tracks DDS message activity in agent_activity table for watchdog.
    """
    while True:
        for room in list(joined_rooms):
            try:
                msgs = transport.receive_new(room)
                for m in msgs:
                    content = m.payload.get("content", "")
                    project = getattr(m, 'project', '') or 'default'
                    entry = {
                        "id": m.id,
                        "room": room,
                        "from": m.from_id,
                        "content": content,
                        "timestamp": m.timestamp_ns,
                        "project": project,
                    }
                    message_history.append(entry)
                    save_to_memory(entry)  # Persist to MEMORY
                    _persist_to_db(entry)  # Persist to SQLite (FTS5)
                    print(f"[{room}] {m.from_id}: {content[:50]}")

                    # v3.2: Track DDS message activity in agent_activity table
                    # Without this, watchdog can't see agents that post via DDS
                    if storage and m.from_id and m.from_id not in _SYSTEM_BOTS:
                        try:
                            storage.update_inferred_activity(
                                m.from_id, "chatting", f"room: {room}"
                            )
                        except Exception:
                            pass  # Never block poll on activity tracking

                    # v1.2: Check for brainstorm votes on #brainstorm AND #general
                    vote_data = parse_aircp_vote(content, m.from_id, room)
                    if vote_data:
                        process_brainstorm_vote(vote_data)

                    # v1.3: Check for @task commands in chat
                    task_cmd = parse_task_command(content, m.from_id, room)
                    if task_cmd:
                        process_task_command(task_cmd)

                    # v2.0: Check for @compact commands in chat
                    compact_cmd = parse_compact_command(content, m.from_id, room)
                    if compact_cmd:
                        # Run in thread to avoid blocking poll loop
                        t = threading.Thread(
                            target=process_compact_command,
                            args=(compact_cmd,),
                            daemon=True,
                        )
                        t.start()

                    # v3.3: Implicit review detection from chat messages
                    try:
                        _detect_implicit_review(m.from_id, content, room)
                    except Exception:
                        pass  # Never block poll on review detection

                    # v2.1: Auto-compact trigger -- increment counter
                    # NOTE: race with _run_auto_compact resetting to 0 is benign;
                    # COMPACT_AUTO_INTERVAL cooldown prevents duplicate compactions.
                    _compact_msg_counter[room] = _compact_msg_counter.get(room, 0) + 1

            except Exception as e:
                logger.warning(f"[POLL] Error processing messages for {room}: {e}")

        # v2.1: Check auto-compact thresholds (non-blocking via thread)
        for room in list(_compact_msg_counter.keys()):
            count = _compact_msg_counter.get(room, 0)
            if count >= COMPACT_AUTO_THRESHOLD:
                last_time = _last_compact_time.get(room, 0)
                if time.time() - last_time > COMPACT_AUTO_INTERVAL:
                    # Run in background thread to avoid blocking HDDS poll loop
                    t = threading.Thread(
                        target=_run_auto_compact,
                        args=(room, transport, storage),
                        daemon=True,
                    )
                    t.start()

        # v4.3: Trim handled by deque(maxlen=500) -- no manual trim needed
        time.sleep(0.5)
