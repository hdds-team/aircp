"""Background watchdog threads for aircp daemon.

Phase 2 extraction: 9 background loops moved from aircp_daemon.py.
Each function runs in an infinite loop with time.sleep() and is spawned
as a daemon thread. poll_messages() stays in aircp_daemon (Phase 3).

All functions import helpers from aircp_daemon (dual mode — globals
still live there until Phase 5 migration).
"""

import json
import logging
import threading
import time

from aircp_daemon import (
    storage, transport, workflow_scheduler, bridge, tip_system,
    autonomy, agent_profiles, joined_rooms,
    brainstorm_reminder_state, review_reminder_state,
    _bot_send, ensure_room, telegram_notify,
    get_brainstorm_config, get_agent_dead_seconds, get_agent_away_seconds,
    TASK_STALE_SECONDS, TASK_MIN_PING_INTERVAL, TASK_MAX_PINGS,
    TASK_LEAD_ID, TASK_LEAD_WAKEUP_PINGS,
    TASK_PENDING_WARN_SECONDS, TASK_PENDING_ESCALATE_SECONDS,
    TASK_PENDING_MAX_PINGS, TASK_PENDING_MIN_PING_INTERVAL,
    AGENT_AWAY_SECONDS, AGENT_DEAD_SECONDS, AGENT_HEARTBEAT_CHECK_INTERVAL,
    BRAINSTORM_WATCHDOG_INTERVAL, BRAINSTORM_REMINDER_INTERVAL,
    BRAINSTORM_MAX_REMINDERS, HUMAN_AGENTS,
    WORKFLOW_WATCHDOG_INTERVAL,
    REVIEW_WATCHDOG_INTERVAL, REVIEW_PING_DELAY, REVIEW_PING_INTERVAL,
    REVIEW_PING_MAX, REVIEW_ESCALATE_SECONDS,
    TASK_WATCHDOG_INTERVAL,
    _SYSTEM_BOTS,
)
from tip_system import TIPS_WATCHDOG_INTERVAL
from workflow_scheduler import MAX_TIMEOUT_NOTIFS

logger = logging.getLogger("aircp_daemon")


# =============================================================================
# v0.7 TaskManager: Watchdog Thread (with anti-spam)
# =============================================================================

def task_watchdog():
    """Background thread to ping agents with stale tasks (anti-spam enabled).

    v0.8: Added lead wake-up feature - notifies TASK_LEAD_ID when:
    - A task has been pinged TASK_LEAD_WAKEUP_PINGS times without response
    - A task is about to be marked as stale
    """
    print("[WATCHDOG] Task watchdog started (v4.5 with lead wake-up + pending reminder)")
    print(f"[WATCHDOG] Lead wake-up: {TASK_LEAD_ID} after {TASK_LEAD_WAKEUP_PINGS} pings")

    escalated_pending = set()  # v4.5: track task_ids already escalated to lead (no infinite spam)
    while True:
        try:
            if storage:
                # Get stale tasks that haven't been pinged recently
                stale_tasks = storage.get_stale_tasks(
                    stale_seconds=TASK_STALE_SECONDS,
                    min_ping_interval=TASK_MIN_PING_INTERVAL
                )

                # v3.2: Get recently active agents (DDS messages + HTTP activity)
                # to avoid false-pinging agents that are actively working
                recently_active = set()
                try:
                    active_list = storage.get_agents_active_since(TASK_STALE_SECONDS)
                    recently_active = {a.lstrip("@").lower() for a in active_list}
                except Exception:
                    pass

                for task in stale_tasks:
                    agent_id = task.get("agent_id", "")
                    task_id = task.get("id")
                    description = task.get("description", "")[:50]
                    ping_count = task.get("ping_count", 0)

                    # v3.2: Skip ping if agent has recent activity (DDS or HTTP)
                    agent_normalized = agent_id.lstrip("@").lower()
                    if agent_normalized in recently_active:
                        print(f"[WATCHDOG] Skipping ping for {agent_id} on task #{task_id} -- agent has recent activity")
                        # Auto-refresh task last_activity so it doesn't keep appearing as stale
                        try:
                            storage.update_task_activity(task_id)
                        except Exception:
                            pass
                        continue

                    # Ping the agent
                    msg = f"\u23f0 @{agent_id.lstrip('@')}: ping! Status update on task #{task_id} ({description}...)? [ping {ping_count + 1}/{TASK_MAX_PINGS}]"
                    print(f"[WATCHDOG] Pinging {agent_id} for stale task #{task_id} (ping {ping_count + 1})")

                    # Mark as pinged BEFORE broadcasting (to prevent re-ping race)
                    storage.update_task_pinged(task_id)

                    # Broadcast to #general
                    if transport:
                        try:
                            ensure_room("#general")
                            _bot_send("#general", msg, from_id="@watchdog", context_agent=agent_id)
                        except Exception as e:
                            print(f"[WATCHDOG] Failed to send ping: {e}")

                    # v0.8 Lead wake-up: notify lead after TASK_LEAD_WAKEUP_PINGS
                    if ping_count + 1 >= TASK_LEAD_WAKEUP_PINGS:
                        lead_msg = f"\U0001f440 {TASK_LEAD_ID}: Task #{task_id} ({agent_id}) appears stuck ({ping_count + 1} pings without response). Desc: {description}..."
                        print(f"[WATCHDOG] Lead wake-up: notifying {TASK_LEAD_ID} about task #{task_id}")
                        if transport:
                            try:
                                _bot_send("#general", lead_msg, from_id="@watchdog", context_agent=agent_id)
                            except Exception as e:
                                print(f"[WATCHDOG] Failed to notify lead: {e}")
                        time.sleep(0.3)  # Small delay to avoid message spam

                # Auto-release locks for tasks about to be marked stale (Brainstorm #7)
                if autonomy and stale_tasks:
                    from handlers.tasks import _auto_release_locks  # deferred: circular import guard
                    for t in stale_tasks:
                        if t.get("ping_count", 0) + 1 >= TASK_MAX_PINGS:
                            agent = t.get("agent_id", "")
                            tid = t.get("id", 0)
                            _auto_release_locks(agent, tid)

                # Mark tasks that exceeded max pings as stale + final lead notification
                marked = storage.mark_stale_tasks_as_stale(TASK_MAX_PINGS)
                if marked > 0:
                    msg = f"\u26a0\ufe0f {marked} task(s) marked as 'stale' (no response after {TASK_MAX_PINGS} pings)"
                    lead_msg = f"\U0001f6a8 {TASK_LEAD_ID}: {marked} task(s) now STALE. Action needed?"
                    print(f"[WATCHDOG] {msg}")
                    if transport:
                        try:
                            _bot_send("#general", msg, from_id="@watchdog")
                            _bot_send("#general", lead_msg, from_id="@watchdog")
                        except Exception:
                            pass

                    # v4.0: Telegram notification
                    telegram_notify("task/stale", {
                        "count": marked,
                        "max_pings": TASK_MAX_PINGS,
                    })

                # =============================================================
                # v4.3: Pending task reminder (unclaimed tasks)
                # Pings the assigned agent if a task sits in 'pending' too long.
                # Escalates to lead after TASK_PENDING_ESCALATE_SECONDS.
                # =============================================================
                pending_tasks = storage.get_stale_pending_tasks(
                    pending_seconds=TASK_PENDING_WARN_SECONDS,
                    min_ping_interval=TASK_PENDING_MIN_PING_INTERVAL,
                )
                # v4.6: Prune escalated_pending -- remove IDs no longer pending (fixes leak)
                current_pending_ids = {t.get("id") for t in pending_tasks}
                escalated_pending &= current_pending_ids

                for task in pending_tasks:
                    agent_id = task.get("agent_id", "")
                    task_id = task.get("id")

                    # v4.5: Guard against empty agent_id (broken mention)
                    if not agent_id:
                        continue

                    description = task.get("description", "")[:50]
                    ping_count = task.get("ping_count", 0)
                    created_at = task.get("created_at", "")

                    # Check if past escalation threshold
                    try:
                        age_seconds = storage._seconds_since(created_at)
                    except Exception:
                        # v4.5: Fail-safe -- assume old = escalate (not new = silence)
                        # Use finite sentinel (> ESCALATE threshold) so int() works in msgs
                        age_seconds = TASK_PENDING_ESCALATE_SECONDS + 1

                    # v4.5: Sanitize inf/NaN from _seconds_since (returns inf on parse error)
                    if not (0 <= age_seconds < 1e9):
                        age_seconds = TASK_PENDING_ESCALATE_SECONDS + 1

                    if ping_count >= TASK_PENDING_MAX_PINGS or age_seconds >= TASK_PENDING_ESCALATE_SECONDS:
                        # v4.5: Stop condition -- only escalate once per task
                        if task_id in escalated_pending:
                            continue
                        escalated_pending.add(task_id)
                        # Escalate to lead
                        msg = f"\U0001f4e2 {TASK_LEAD_ID}: Task #{task_id} assigned to @{agent_id.lstrip('@')} is unclaimed for {int(age_seconds // 60)}min! ({description}...)"
                        print(f"[WATCHDOG] Pending escalation: task #{task_id} -> {TASK_LEAD_ID}")
                    else:
                        # NOTE: No recently_active guard here -- pending tasks have no
                        # task/activity by definition (not claimed yet), so the concept
                        # of "recent activity" doesn't apply.
                        # Nudge the assigned agent
                        msg = f"\U0001f4cb @{agent_id.lstrip('@')}: Task #{task_id} is waiting for you! ({description}...) [pending {int(age_seconds // 60)}min]"
                        print(f"[WATCHDOG] Pending nudge: task #{task_id} -> {agent_id}")

                    storage.update_task_pinged(task_id)

                    if transport:
                        try:
                            ensure_room("#general")
                            _bot_send("#general", msg, from_id="@taskman", context_agent=agent_id)
                        except Exception as e:
                            print(f"[WATCHDOG] Failed to send pending reminder: {e}")

        except Exception as e:
            print(f"[WATCHDOG] Error: {e}")

        time.sleep(TASK_WATCHDOG_INTERVAL)


# =============================================================================
# v0.9 Agent Heartbeat: Presence Watchdog Thread
# =============================================================================

def presence_watchdog():
    """Background thread to detect agents that stopped sending heartbeats.

    v0.9: Agent presence monitoring
    v4.1: Adaptive thresholds -- local LLM agents get relaxed timers
    - Away (>120s cloud, >timeout_base local): Agent status shown as yellow in dashboard
    - Dead (>300s cloud, >timeout_max+60 local): Alert sent to lead, status shown as red
    """
    print("[PRESENCE] Agent presence watchdog started (v4.1 adaptive)")
    print(f"[PRESENCE] Defaults -- Away: {AGENT_AWAY_SECONDS}s, Dead: {AGENT_DEAD_SECONDS}s")
    for aid, prof in agent_profiles.items():
        if prof["is_local"]:
            print(f"[PRESENCE] {aid}: adaptive -- away={get_agent_away_seconds(aid):.0f}s, dead={get_agent_dead_seconds(aid):.0f}s")

    # Track which agents we've already alerted about (to avoid spam)
    alerted_dead_agents = set()

    while True:
        try:
            if storage:
                # v4.1: Per-agent adaptive check instead of global threshold
                all_presence = storage.get_all_agent_presence()
                dead_agents = []
                away_agents = []
                online_agents = set()

                for agent in all_presence:
                    agent_id = agent.get("agent_id", "")
                    last_seen = agent.get("last_seen", "")
                    if not last_seen:
                        continue

                    seconds_ago = storage._seconds_since(last_seen)
                    # v4.6: Guard against inf/NaN from _seconds_since (parse error)
                    if not (0 <= seconds_ago < 1e9):
                        continue
                    dead_threshold = get_agent_dead_seconds(agent_id)
                    away_threshold = get_agent_away_seconds(agent_id)

                    if seconds_ago >= dead_threshold:
                        dead_agents.append(agent)
                    elif seconds_ago >= away_threshold:
                        away_agents.append(agent)
                    else:
                        online_agents.add(agent_id)

                # Handle dead agents (notify lead once)
                for agent in dead_agents:
                    agent_id = agent.get("agent_id", "")
                    if agent_id not in alerted_dead_agents:
                        last_seen = agent.get("last_seen", "unknown")
                        threshold = get_agent_dead_seconds(agent_id)
                        msg = f"\U0001f480 {TASK_LEAD_ID}: Agent {agent_id} appears down (last heartbeat: {last_seen})"
                        print(f"[PRESENCE] Agent {agent_id} marked as DEAD (threshold: {threshold:.0f}s)")

                        if transport:
                            try:
                                ensure_room("#general")
                                _bot_send("#general", msg, from_id="@watchdog", context_agent=agent_id)
                            except Exception as e:
                                print(f"[PRESENCE] Failed to notify lead: {e}")

                        alerted_dead_agents.add(agent_id)

                        # v4.0: Telegram notification
                        telegram_notify("agent/dead", {
                            "agent_id": agent_id,
                            "last_seen": last_seen,
                        })

                # Remove from alerted set if agent came back
                came_back = alerted_dead_agents & online_agents
                if came_back:
                    for agent_id in came_back:
                        print(f"[PRESENCE] Agent {agent_id} is back online!")
                        if transport:
                            try:
                                msg = f"\u2705 Agent {agent_id} is back online!"
                                _bot_send("#general", msg, from_id="@watchdog", context_agent=agent_id)
                            except Exception:
                                pass
                    alerted_dead_agents -= came_back

        except Exception as e:
            print(f"[PRESENCE] Error: {e}")

        time.sleep(AGENT_HEARTBEAT_CHECK_INTERVAL)


# =============================================================================
# v1.0 Brainstorm System: Watchdog Thread
# =============================================================================

def brainstorm_watchdog():
    """Background thread to check brainstorm deadlines and resolve sessions.

    v1.1: Added reminder loop - pings non-voters every 15s until vote or timeout.

    v1.0: Handles:
    - Expired sessions -> auto-resolve based on votes + silent_mode
    - Consensus calculation (majority rule)
    - Notification dispatch to participants
    """
    print("[BRAINSTORM] Watchdog started (v1.1 with reminder loop)")

    while True:
        try:
            if storage:
                config = get_brainstorm_config()
                silent_mode = config.get("silent_mode", True)
                min_votes_rule = config.get("min_votes", "majority")
                synthesizer = config.get("synthesizer", "@alpha")
                channel = config.get("channel", "#brainstorm")

                # =============================================================
                # v1.2: Reminder loop - throttled (max every 60s, max 3 per session)
                # Fix: v1.1 spammed every 15s with no limit -> infinite loop
                # =============================================================
                active_sessions = storage.get_active_brainstorm_sessions()
                now = time.time()
                for session in active_sessions:
                    session_id = session.get("id")
                    full_topic = session.get("topic", "")
                    topic = full_topic[:100] + ("..." if len(full_topic) > 100 else "")
                    participants = session.get("participants", [])

                    # Get current votes
                    full_session = storage.get_brainstorm_session(session_id)
                    votes = full_session.get("votes", []) if full_session else []
                    # Normalize @ prefix for comparison (agents may vote as "alpha" or "@alpha")
                    voted_normalized = {v.get("agent_id", "").lstrip("@") for v in votes}

                    # Find non-voters (exclude humans - they decide, they don't vote)
                    non_voters = [p for p in participants if p.lstrip("@") not in voted_normalized and p not in HUMAN_AGENTS]

                    if non_voters and transport:
                        # v1.2: Throttle reminders - check state
                        state = brainstorm_reminder_state.get(session_id, {"count": 0, "last_sent": 0})
                        elapsed = now - state["last_sent"]

                        if state["count"] >= BRAINSTORM_MAX_REMINDERS:
                            # Max reminders reached, stop spamming
                            continue

                        if elapsed < BRAINSTORM_REMINDER_INTERVAL:
                            # Too soon since last reminder, skip
                            continue

                        # Build targeted reminder with @mentions
                        tags = " ".join(non_voters)
                        reminder_count = state["count"] + 1
                        reminder_msg = f"\U0001f5f3\ufe0f Reminder brainstorm #{session_id} ({reminder_count}/{BRAINSTORM_MAX_REMINDERS}) - {tags} : vote! (Topic: {topic}...)"
                        try:
                            brainstorm_ch = config.get("channel", "#brainstorm")
                            ensure_room(brainstorm_ch)
                            _bot_send(brainstorm_ch, reminder_msg, from_id="@brainstorm")
                            brainstorm_reminder_state[session_id] = {"count": reminder_count, "last_sent": now}
                            print(f"[BRAINSTORM] Reminder {reminder_count}/{BRAINSTORM_MAX_REMINDERS} sent for #{session_id} -> {non_voters}")
                        except Exception as e:
                            print(f"[BRAINSTORM] Failed to send reminder: {e}")
                    elif not non_voters and session_id in brainstorm_reminder_state:
                        # Everyone voted, clean up state
                        del brainstorm_reminder_state[session_id]
                # =============================================================

                # Get expired sessions
                expired = storage.get_expired_brainstorm_sessions()
                if expired:
                    print(f"[BRAINSTORM] Watchdog found {len(expired)} expired session(s): {[s.get('id') for s in expired]}")

                for session in expired:
                    session_id = session.get("id")
                    full_topic = session.get("topic", "")
                    topic = full_topic[:100] + ("..." if len(full_topic) > 100 else "")
                    participants = session.get("participants", [])
                    created_by = session.get("created_by", "@system")

                    # Get votes for this session
                    full_session = storage.get_brainstorm_session(session_id)
                    votes = full_session.get("votes", []) if full_session else []

                    # Count votes
                    go_votes = sum(1 for v in votes if v.get("vote", "").startswith("\u2705"))
                    block_votes = sum(1 for v in votes if v.get("vote", "").startswith("\u274c"))
                    voted_normalized = {v.get("agent_id", "").lstrip("@") for v in votes}
                    non_voters = [p for p in participants if p.lstrip("@") not in voted_normalized]

                    # Silent mode: non-voters count as implicit approval
                    if silent_mode:
                        go_votes += len(non_voters)

                    # Calculate consensus
                    total_votes = go_votes + block_votes
                    required_majority = len(participants) // 2 + 1

                    if min_votes_rule == "majority":
                        consensus = "GO" if go_votes >= required_majority else "BLOCKED"
                    elif min_votes_rule == "all":
                        consensus = "GO" if block_votes == 0 and go_votes == len(participants) else "BLOCKED"
                    else:  # "any"
                        consensus = "GO" if go_votes > 0 else "BLOCKED"

                    # Close session
                    storage.close_brainstorm_session(session_id, consensus, "completed")
                    # v1.2: Clean up reminder state
                    brainstorm_reminder_state.pop(session_id, None)

                    # Build result message
                    vote_summary = f"\u2705 {go_votes} / \u274c {block_votes}"
                    if non_voters and silent_mode:
                        vote_summary += f" (implicit: {len(non_voters)})"

                    # Check if this was an auto-workflow idea
                    auto_workflow = session.get("auto_workflow", 0) == 1
                    is_idea = auto_workflow  # Ideas have auto_workflow flag

                    if is_idea:
                        result_msg = f"\U0001f4a1 **IDEA #{session_id}** - {consensus}\n"
                    else:
                        result_msg = f"\U0001f9e0 **BRAINSTORM #{session_id}** - {consensus}\n"
                    result_msg += f"Topic: {topic}...\n"
                    result_msg += f"Votes: {vote_summary}\n"

                    # v3.3: Auto-trigger workflow on ALL brainstorm GO (not just auto_workflow)
                    workflow_triggered = False
                    if consensus == "GO" and workflow_scheduler:
                        try:
                            label = "Idea" if is_idea else "Brainstorm"
                            # v4.2: Forward workflow_mode from brainstorm session
                            wf_mode = session.get("workflow_mode", "standard")
                            workflow_id = workflow_scheduler.create_workflow(
                                name=session.get("topic", "Feature")[:100],
                                created_by=created_by,
                                description=f"Auto-triggered from {label} #{session_id}",
                                lead_agent=synthesizer,
                                mode=wf_mode,
                            )
                            if workflow_id > 0:
                                # Skip to 'code' phase -- brainstorm+vote already done
                                workflow_scheduler.skip_to_phase('code', workflow_id)
                                # Back-link brainstorm -> workflow
                                try:
                                    conn = storage._get_conn()
                                    conn.execute(
                                        "UPDATE brainstorm_sessions SET workflow_id = ? WHERE id = ?",
                                        (workflow_id, session_id))
                                    conn.commit()
                                except Exception:
                                    pass
                                workflow_triggered = True
                                mode_tag = f" **[{wf_mode.upper()}]**" if wf_mode == "veloce" else ""
                                result_msg += f"\U0001f680 **WORKFLOW #{workflow_id}**{mode_tag} auto-started at `@code`!\n"
                                result_msg += f"Lead: {synthesizer}\n"
                                print(f"[WORKFLOW] Auto-triggered workflow #{workflow_id} mode={wf_mode} from {label.lower()} #{session_id}")
                                # Dashboard instant emit
                                if bridge:
                                    wf = workflow_scheduler.get_workflow(workflow_id)
                                    if wf:
                                        bridge.emit_workflow(wf)
                            else:
                                result_msg += f"\u26a0\ufe0f Workflow not started (one may already be active)\n"
                                print(f"[WORKFLOW] Failed to auto-trigger -- one may already be active")
                        except Exception as e:
                            result_msg += f"\u26a0\ufe0f Auto-workflow error: {e}\n"
                            print(f"[WORKFLOW] Error auto-triggering workflow: {e}")
                    elif consensus != "GO":  # Only show rejection for non-GO
                        if is_idea:
                            result_msg += f"\u274c Idea rejected -- needs refinement."
                        else:
                            result_msg += f"\u26a0\ufe0f Needs clarification before GO."

                    print(f"[{'IDEA' if is_idea else 'BRAINSTORM'}] Session #{session_id} resolved: {consensus}")

                    # Broadcast result
                    if transport:
                        try:
                            ensure_room(channel)
                            _bot_send(channel, result_msg, from_id="@idea" if is_idea else "@brainstorm")

                            # Also notify creator in #general
                            ensure_room("#general")
                            if workflow_triggered:
                                short_msg = f"\U0001f4a1 Idea #{session_id} -> **GO** ({vote_summary}) -> \U0001f680 Workflow auto-started!"
                            else:
                                short_msg = f"{'\U0001f4a1 Idea' if is_idea else '\U0001f9e0 Brainstorm'} #{session_id} resolved: **{consensus}** ({vote_summary})"
                            _bot_send("#general", short_msg, from_id="@idea" if is_idea else "@brainstorm")

                            # Notify creator specifically if idea was approved/rejected
                            if is_idea and created_by and created_by != "@system":
                                notify_msg = f"\U0001f4a1 {created_by} -- Your idea #{session_id} was {'\u2705 approved' if consensus == 'GO' else '\u274c rejected'}"
                                if workflow_triggered:
                                    notify_msg += f" -> Workflow #{workflow_id} in progress!"
                                _bot_send("#general", notify_msg, from_id="@idea")
                        except Exception as e:
                            print(f"[{'IDEA' if is_idea else 'BRAINSTORM'}] Failed to send result: {e}")

        except Exception as e:
            print(f"[BRAINSTORM] Watchdog error: {e}")
            __import__('traceback').print_exc()

        time.sleep(BRAINSTORM_WATCHDOG_INTERVAL)


# =============================================================================
# v1.4 Workflow Scheduler: Watchdog Thread
# =============================================================================

# NOTE: MAX_TIMEOUT_NOTIFS imported from workflow_scheduler (single source of truth)
# Design: Auto-abort after N timeout notifications (no auto-transition to next phase)
# This gives users time to react while preventing infinite spam.

def workflow_watchdog():
    """Background thread to check workflow phase timeouts and send reminders.

    v1.4: Handles:
    - Reminder at 80% of phase timeout
    - Timeout notification when phase expires
    - Auto-abort after MAX_TIMEOUT_NOTIFS notifications (fixes spam bug)
    """
    print("[WORKFLOW] Watchdog started (v1.4 - auto-abort after 3 notifs)")

    while True:
        try:
            if workflow_scheduler:
                check = workflow_scheduler.check_timeout()

                if check:
                    action = check.get("action")
                    workflow_id = check.get("workflow_id")
                    phase = check.get("phase")

                    if action == "reminder":
                        remaining = check.get("remaining_minutes", 0)
                        msg = f"\u23f0 **WORKFLOW #{workflow_id}** - Phase `@{phase}`: {remaining}min remaining!"
                        print(f"[WORKFLOW] Reminder for #{workflow_id} phase {phase}")

                        if transport:
                            try:
                                ensure_room("#general")
                                _bot_send("#general", msg, from_id="@workflow")
                            except Exception as e:
                                print(f"[WORKFLOW] Failed to send reminder: {e}")

                    elif action == "timeout":
                        # v1.4: Increment counter and check for auto-abort
                        notif_count = workflow_scheduler.increment_timeout_notif(workflow_id)
                        elapsed = check.get("elapsed_minutes", 0)
                        timeout = check.get("timeout_minutes", 0)

                        if notif_count >= MAX_TIMEOUT_NOTIFS:
                            # Auto-abort after 3 notifications
                            result = workflow_scheduler.abort_workflow(
                                workflow_id,
                                f"Extended timeout phase {phase} - cleanup"
                            )
                            msg = f"\U0001f6d1 **WORKFLOW** aborted: Extended timeout phase {phase} - cleanup"
                            print(f"[WORKFLOW] Auto-abort #{workflow_id} after {notif_count} timeout notifs")
                        else:
                            # Normal timeout notification
                            remaining_notifs = MAX_TIMEOUT_NOTIFS - notif_count
                            msg = f"\u26a0\ufe0f **WORKFLOW #{workflow_id}** - Phase `@{phase}` timed out! ({elapsed}/{timeout}min)\n"
                            msg += f"\u27a1\ufe0f `@extend 10` to extend or `@next` to move on"
                            if remaining_notifs <= 1:
                                msg += f"\n\u26a0\ufe0f Auto-abort in {remaining_notifs} notification(s)!"
                            print(f"[WORKFLOW] Timeout #{notif_count}/{MAX_TIMEOUT_NOTIFS} for #{workflow_id} phase {phase}")

                        if transport:
                            try:
                                ensure_room("#general")
                                _bot_send("#general", msg, from_id="@workflow")
                            except Exception as e:
                                print(f"[WORKFLOW] Failed to send timeout: {e}")

        except Exception as e:
            print(f"[WORKFLOW] Watchdog error: {e}")

        time.sleep(WORKFLOW_WATCHDOG_INTERVAL)


# =============================================================================
# v1.5 Review System: Watchdog Thread
# =============================================================================

def review_watchdog():
    """Background thread to check review deadlines and send reminders.

    v1.5: Handles:
    - Reminder at 30min (REVIEW_REMINDER_SECONDS) -- legacy DB-level
    - Auto-close at 1h (REVIEW_TIMEOUT_SECONDS) with timeout status
    - Consensus calculation (approved if min_approvals reached, else timeout)

    v2.0 P7: Aggressive ping system (pattern: brainstorm watchdog)
    - First ping after REVIEW_PING_DELAY (2 min)
    - Subsequent pings every REVIEW_PING_INTERVAL (2 min), max REVIEW_PING_MAX (3)
    - Escalation to #general after REVIEW_ESCALATE_SECONDS (5 min)
    - In-memory state: review_reminder_state dict (no DB migration needed)
    - Message reminds reviewers to use MCP command (not just chat)
    """
    print("[REVIEW] Watchdog started (v2.0 P7 -- aggressive pings)")

    while True:
        try:
            if storage:
                now = time.time()

                # =============================================================
                # 1. P7: Aggressive ping system for pending reviews
                # =============================================================
                active_reviews = storage.get_active_review_requests()
                for review in active_reviews:
                    request_id = review.get("id")
                    file_path = review.get("file_path", "")
                    reviewers = review.get("reviewers", [])
                    if isinstance(reviewers, str):
                        try:
                            reviewers = json.loads(reviewers)
                        except (json.JSONDecodeError, TypeError):
                            reviewers = []
                    requested_by = review.get("requested_by", "")
                    created_at = review.get("created_at", "")

                    # Calculate review age in seconds
                    # v4.6: Use _seconds_since for consistency (fail-safe = escalate, not silence)
                    review_age = storage._seconds_since(created_at)
                    if not (0 <= review_age < 1e9):
                        review_age = REVIEW_ESCALATE_SECONDS + 1

                    # Too young for first ping?
                    if review_age < REVIEW_PING_DELAY:
                        continue

                    # Get current responses to find non-voters
                    full_review = storage.get_review_request(request_id)
                    responses = full_review.get("responses", []) if full_review else []
                    voted_reviewers = {r.get("reviewer") for r in responses}

                    # P1 FIX (backward compat): if any reviewer posted changes_requested,
                    # auto-close the review. This catches zombie reviews from before the fix.
                    has_changes_requested = any(
                        r.get("vote") == "changes" for r in responses
                    )
                    if has_changes_requested:
                        changer = next(
                            (r.get("reviewer") for r in responses if r.get("vote") == "changes"),
                            "unknown"
                        )
                        storage.close_review_request(request_id, "changes_requested", "completed")
                        review_reminder_state.pop(request_id, None)
                        if transport:
                            try:
                                _bot_send(
                                    "#general",
                                    f"\U0001f4cb **REVIEW #{request_id}** auto-closed (changes requested by {changer})",
                                    from_id="@review"
                                )
                            except Exception:
                                pass
                        print(f"[REVIEW] Auto-closed zombie review #{request_id} (changes_requested found)")
                        continue

                    non_voters = [r for r in reviewers if r not in voted_reviewers]

                    # Everyone voted -> clean up state and skip
                    if not non_voters:
                        review_reminder_state.pop(request_id, None)
                        continue

                    # Get or init ping state for this review
                    state = review_reminder_state.get(request_id, {
                        "count": 0, "last_sent": 0, "escalated": False
                    })

                    # Max pings reached -> stop spamming
                    if state["count"] >= REVIEW_PING_MAX:
                        continue

                    # Throttle: too soon since last ping?
                    elapsed_since_last = now - state["last_sent"]
                    if state["count"] > 0 and elapsed_since_last < REVIEW_PING_INTERVAL:
                        continue

                    # === SEND PING ===
                    ping_count = state["count"] + 1
                    tags = " ".join(non_voters)

                    # Escalation check: if review is older than REVIEW_ESCALATE_SECONDS
                    is_escalation = review_age >= REVIEW_ESCALATE_SECONDS and not state["escalated"]

                    if is_escalation:
                        msg = (
                            f"\U0001f6a8 **REVIEW #{request_id}** -- ESCALATION! "
                            f"{tags}: review pending for {int(review_age // 60)} min on `{file_path}`\n"
                            f"\u26a0\ufe0f Use `review/approve` or `review/changes` (not just chat!)"
                        )
                    else:
                        msg = (
                            f"\U0001f514 **REVIEW #{request_id}** ({ping_count}/{REVIEW_PING_MAX}) -- "
                            f"{tags} : review pending on `{file_path}`\n"
                            f"\U0001f4a1 Reminder: use MCP command `review/approve` or `review/changes`"
                        )

                    if transport:
                        try:
                            ensure_room("#general")
                            _bot_send("#general", msg, from_id="@review")
                            review_reminder_state[request_id] = {
                                "count": ping_count,
                                "last_sent": now,
                                "escalated": state["escalated"] or is_escalation
                            }
                            print(f"[REVIEW] P7 ping {ping_count}/{REVIEW_PING_MAX} for #{request_id} -> {non_voters}"
                                  f"{' (ESCALATION)' if is_escalation else ''}")
                        except Exception as e:
                            print(f"[REVIEW] Failed to send P7 ping: {e}")

                    # Also mark legacy DB reminder (backward compat)
                    if ping_count == 1:
                        storage.mark_review_reminder_sent(request_id)

                # =============================================================
                # 2. Auto-close expired reviews (1h) -- unchanged from v1.5
                # =============================================================
                expired_reviews = storage.get_expired_review_requests()
                for review in expired_reviews:
                    request_id = review.get("id")
                    file_path = review.get("file_path", "")
                    reviewers = review.get("reviewers", [])
                    if isinstance(reviewers, str):
                        try:
                            reviewers = json.loads(reviewers)
                        except (json.JSONDecodeError, TypeError):
                            reviewers = []
                    min_approvals = review.get("min_approvals", 1)
                    requested_by = review.get("requested_by", "")

                    # Get responses
                    full_review = storage.get_review_request(request_id)
                    responses = full_review.get("responses", []) if full_review else []

                    # Count approvals and changes_requested
                    approvals = sum(1 for r in responses if r.get("vote") == "approve")
                    changes_requested = sum(1 for r in responses if r.get("vote") == "changes")

                    # Determine consensus
                    if changes_requested > 0:
                        consensus = "changes_requested"
                    elif approvals >= min_approvals:
                        consensus = "approved"
                    else:
                        consensus = "timeout"

                    # Close review
                    storage.close_review_request(request_id, consensus, "completed")
                    # P7: Clean up ping state
                    review_reminder_state.pop(request_id, None)

                    # Build result message
                    emoji = "\u2705" if consensus == "approved" else "\u26a0\ufe0f" if consensus == "changes_requested" else "\u23f0"
                    msg = f"{emoji} **REVIEW #{request_id}** - `{file_path}` -> **{consensus.upper()}**\n"
                    msg += f"Votes: {approvals} approvals, {changes_requested} changes requested (min: {min_approvals})"

                    print(f"[REVIEW] #{request_id} closed: {consensus}")

                    if transport:
                        try:
                            ensure_room("#general")
                            _bot_send("#general", msg, from_id="@review")

                            # Notify requester
                            if requested_by:
                                notify_msg = f"\U0001f4cb {requested_by} - Ta review #{request_id} est terminee: **{consensus}**"
                                _bot_send("#general", notify_msg, from_id="@review")
                        except Exception as e:
                            print(f"[REVIEW] Failed to send result: {e}")

                    # v4.0: Telegram notification
                    telegram_notify("review/closed", {
                        "request_id": request_id,
                        "consensus": consensus,
                        "file_path": file_path,
                    })

        except Exception as e:
            print(f"[REVIEW] Watchdog error: {e}")

        time.sleep(REVIEW_WATCHDOG_INTERVAL)


# =============================================================================
# v1.6 Tips Contextuels: Watchdog Thread
# =============================================================================

def tips_watchdog():
    """Background thread to broadcast contextual tips to agents.

    v1.6: Two modes:
    - Contextual: Tip based on current workflow phase (immediate on phase change)
    - General: Random tip from rotation every TIPS_BROADCAST_INTERVAL seconds
    """
    print("[TIPS] Watchdog started (v1.6)")

    while True:
        try:
            if tip_system and transport:
                # Get current workflow phase for contextual tips
                current_phase = None
                if workflow_scheduler:
                    try:
                        wf_status = workflow_scheduler.get_active_workflow()
                        if wf_status:
                            current_phase = wf_status.get("phase")
                    except Exception:
                        pass

                tip_system.broadcast(transport, ensure_room, workflow_phase=current_phase)

        except Exception as e:
            print(f"[TIPS] Watchdog error: {e}")

        time.sleep(TIPS_WATCHDOG_INTERVAL)


# =============================================================================
# v3.0 Memory Retention -- cleanup messages older than 30 days
# =============================================================================

def memory_retention_loop():
    """Cleanup messages older than 30 days, runs daily"""
    while True:
        time.sleep(86400)  # 24h
        try:
            deleted = storage.cleanup_old_messages(days=30)
            if deleted > 0:
                print(f"[RETENTION] Cleaned up {deleted} messages older than 30 days")
        except Exception as e:
            print(f"[RETENTION] Error: {e}")


# =============================================================================
# Compactor v3: GC thread -- hard-deletes soft-deleted messages after N days
# =============================================================================

def gc_loop(storage_ref, interval_hours=6, retention_days=7):
    """Periodic GC: purge compacted messages older than retention_days."""
    while True:
        time.sleep(interval_hours * 3600)
        try:
            purged = storage_ref.gc_compacted(retention_days)
            if purged > 0:
                logger.info(f"[GC] Purged {purged} compacted messages (>{retention_days}d)")
        except Exception as e:
            logger.error(f"[GC] Error: {e}")


# =============================================================================
# Orchestrator: start all watchdog threads
# =============================================================================

def start_watchdogs(storage_ref):
    """Spawn all background watchdog threads. Returns dict of thread references
    for /health endpoint monitoring.

    Called from main() after all globals are initialized.
    storage_ref is passed explicitly to gc_loop (it was a closure param).
    """
    threads = {}

    # Task watchdog (stale task detection + pinging)
    t = threading.Thread(target=task_watchdog, daemon=True)
    t.start()
    threads["task"] = t
    print("Task watchdog started (anti-spam enabled)")

    # Presence watchdog (agent heartbeat monitoring)
    t = threading.Thread(target=presence_watchdog, daemon=True)
    t.start()
    threads["presence"] = t
    print("Agent presence watchdog started (v0.9)")

    # Brainstorm watchdog (deadline enforcement)
    t = threading.Thread(target=brainstorm_watchdog, daemon=True)
    t.start()
    threads["brainstorm"] = t
    print("Brainstorm watchdog started (v1.0)")

    # Workflow watchdog (phase timeout monitoring)
    t = threading.Thread(target=workflow_watchdog, daemon=True)
    t.start()
    threads["workflow"] = t
    print("Workflow watchdog started (v1.3)")

    # Review watchdog (deadline & auto-close)
    t = threading.Thread(target=review_watchdog, daemon=True)
    t.start()
    threads["review"] = t
    print("Review watchdog started (v1.5)")

    # Tips watchdog (contextual tips broadcast)
    t = threading.Thread(target=tips_watchdog, daemon=True)
    t.start()
    threads["tips"] = t

    # Memory retention (daily cleanup >30 days)
    t = threading.Thread(target=memory_retention_loop, daemon=True)
    t.start()
    threads["memory_retention"] = t
    print("Memory retention started (v3.0 - 30 day cleanup)")

    # GC loop (compactor hard-delete after 7 days)
    t = threading.Thread(
        target=gc_loop, args=(storage_ref,),
        kwargs={"interval_hours": 6, "retention_days": 7},
        daemon=True, name="compactor-gc"
    )
    t.start()
    threads["gc"] = t
    print("Compactor v3 GC enabled (6h interval, 7d retention)")

    print(f"Health check ready ({len(threads)} watchdogs tracked)")
    return threads
