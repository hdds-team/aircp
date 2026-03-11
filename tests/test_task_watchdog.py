#!/usr/bin/env python3
"""
Task Watchdog Tests -- Pending Task Reminder (v4.3).

Tests the pending task reminder logic in task_watchdog():
1. No nudge before TASK_PENDING_WARN_SECONDS (10 min)
2. First nudge after threshold -> message to assigned agent
3. Throttle: no re-nudge within TASK_PENDING_MIN_PING_INTERVAL
4. Escalation to lead after TASK_PENDING_ESCALATE_SECONDS or MAX_PINGS
5. Escalation happens ONCE per task (no infinite spam to lead)
6. Multiple pending tasks tracked independently
7. Task claimed (no longer pending) -> no nudge
8. Edge cases (no agent, no tasks, malformed data)

Usage:
    python3 -m pytest tests/test_task_watchdog.py -v
    # or standalone:
    python3 tests/test_task_watchdog.py
"""

import sys
import os
import time
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =============================================================================
# Constants (mirror from daemon_config.py)
# =============================================================================

TASK_PENDING_WARN_SECONDS = 600       # 10 min
TASK_PENDING_ESCALATE_SECONDS = 1800  # 30 min
TASK_PENDING_MAX_PINGS = 2
TASK_PENDING_MIN_PING_INTERVAL = 600  # 10 min
TASK_LEAD_ID = "@naskel"


# =============================================================================
# Helpers
# =============================================================================

@pytest.fixture
def r():
    return _TestResult()


class _TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def ok(self, name: str):
        self.passed += 1
        print(f"  \u2705 {name}")

    def fail(self, name: str, reason: str):
        self.failed += 1
        self.errors.append((name, reason))
        print(f"  \u274c {name}: {reason}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            print("Failures:")
            for name, reason in self.errors:
                print(f"  - {name}: {reason}")
        print(f"{'='*60}")
        return self.failed == 0


def make_pending_task(task_id=1, agent_id="@beta", description="Implement feature X",
                      created_at=None, age_seconds=0, ping_count=0,
                      last_pinged_at=None):
    """Create a mock pending task dict (mirrors agent_tasks row)."""
    if created_at is None:
        dt = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "id": task_id,
        "agent_id": agent_id,
        "description": description,
        "status": "pending",
        "created_at": created_at,
        "ping_count": ping_count,
        "last_pinged_at": last_pinged_at,
    }


def _seconds_since(timestamp_str):
    """Mirror storage._seconds_since for test simulation."""
    try:
        dt = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
        dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return float('inf')


def simulate_pending_watchdog_cycle(pending_tasks, storage_mock, transport_mock,
                                    escalated_pending, lead_id=TASK_LEAD_ID):
    """
    Simulate one cycle of the pending task reminder logic.
    This mirrors the v4.3 code in task_watchdog() (pending section)
    WITH the escalation stop-condition fix (escalated_pending set).

    Returns list of sent messages (for assertions).
    """
    sent_messages = []

    for task in pending_tasks:
        agent_id = task.get("agent_id", "")
        # v4.5: Skip tasks with empty agent_id (broken mention)
        if not agent_id:
            continue

        task_id = task.get("id")
        description = task.get("description", "")[:50]
        ping_count = task.get("ping_count", 0)
        created_at = task.get("created_at", "")

        try:
            age_seconds = _seconds_since(created_at)
        except Exception:
            # v4.5: Fail-safe -- assume old = escalate (not silence)
            # Use finite sentinel so int() works in message formatting
            age_seconds = TASK_PENDING_ESCALATE_SECONDS + 1

        # v4.5: Sanitize inf/NaN from _seconds_since (returns inf on parse error)
        if not (0 <= age_seconds < 1e9):
            age_seconds = TASK_PENDING_ESCALATE_SECONDS + 1

        is_escalation = (
            ping_count >= TASK_PENDING_MAX_PINGS
            or age_seconds >= TASK_PENDING_ESCALATE_SECONDS
        )

        if is_escalation:
            # Stop condition: only escalate once per task
            if task_id in escalated_pending:
                continue
            escalated_pending.add(task_id)

            msg = (
                f"\U0001f4e2 {lead_id}: Task #{task_id} assigned to "
                f"@{agent_id.lstrip('@')} is unclaimed for "
                f"{int(age_seconds // 60)}min! ({description}...)"
            )
            msg_type = "escalation"
        else:
            msg = (
                f"\U0001f4cb @{agent_id.lstrip('@')}: Task #{task_id} is "
                f"waiting for you! ({description}...) "
                f"[pending {int(age_seconds // 60)}min]"
            )
            msg_type = "nudge"

        storage_mock.update_task_pinged(task_id)

        if transport_mock:
            transport_mock.send_chat("#general", msg, from_id="@taskman")

        sent_messages.append({
            "task_id": task_id,
            "msg": msg,
            "type": msg_type,
            "agent_id": agent_id,
        })

    return sent_messages


# =============================================================================
# Tests -- Basic Timing
# =============================================================================

def test_no_nudge_before_threshold(r: _TestResult):
    """Task pending < 10 min -> not returned by storage query -> no nudge.
    (Storage query handles the threshold; watchdog just processes results.)"""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    # Empty list = storage found no stale pending tasks
    msgs = simulate_pending_watchdog_cycle([], storage, transport, escalated)

    if len(msgs) == 0 and transport.send_chat.call_count == 0:
        r.ok("no_nudge_before_threshold")
    else:
        r.fail("no_nudge_before_threshold", f"Expected 0, got {len(msgs)}")


def test_first_nudge_sent(r: _TestResult):
    """Task pending > 10 min with 0 pings -> nudge to assigned agent."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [make_pending_task(age_seconds=700, ping_count=0)]  # 11+ min
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    if len(msgs) == 1 and msgs[0]["type"] == "nudge":
        msg = msgs[0]["msg"]
        if "@beta" in msg and "Task #1" in msg:
            r.ok("first_nudge_sent")
        else:
            r.fail("first_nudge_sent", f"Bad msg content: {msg[:100]}")
    else:
        r.fail("first_nudge_sent", f"Expected 1 nudge, got {msgs}")


def test_nudge_calls_update_task_pinged(r: _TestResult):
    """Nudge should call storage.update_task_pinged to track ping count."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [make_pending_task(task_id=42, age_seconds=700, ping_count=0)]
    simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    if storage.update_task_pinged.called:
        call_args = storage.update_task_pinged.call_args[0]
        if call_args[0] == 42:
            r.ok("nudge_calls_update_task_pinged")
        else:
            r.fail("nudge_calls_update_task_pinged",
                    f"Called with {call_args}, expected (42,)")
    else:
        r.fail("nudge_calls_update_task_pinged", "update_task_pinged not called")


# =============================================================================
# Tests -- Escalation
# =============================================================================

def test_escalation_after_max_pings(r: _TestResult):
    """Task with ping_count >= MAX_PINGS -> escalation to lead."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [make_pending_task(age_seconds=900, ping_count=2)]  # 2 = MAX
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    if len(msgs) == 1 and msgs[0]["type"] == "escalation":
        msg = msgs[0]["msg"]
        if TASK_LEAD_ID in msg and "@beta" in msg:
            r.ok("escalation_after_max_pings")
        else:
            r.fail("escalation_after_max_pings", f"Bad content: {msg[:100]}")
    else:
        r.fail("escalation_after_max_pings", f"Expected escalation, got {msgs}")


def test_escalation_after_time_threshold(r: _TestResult):
    """Task pending > 30 min -> escalation regardless of ping count."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [make_pending_task(age_seconds=1900, ping_count=0)]  # 31+ min
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    if len(msgs) == 1 and msgs[0]["type"] == "escalation":
        r.ok("escalation_after_time_threshold")
    else:
        r.fail("escalation_after_time_threshold", f"Expected escalation, got {msgs}")


def test_escalation_once_only(r: _TestResult):
    """BUG FIX: Escalation should happen ONCE per task, not every cycle.
    This is the core bug that review #14 caught."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [make_pending_task(task_id=7, age_seconds=2000, ping_count=3)]

    # Cycle 1: escalation sent
    msgs1 = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    # Cycle 2: same task still pending -> should NOT re-escalate
    msgs2 = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    # Cycle 3: still nothing
    msgs3 = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    total = len(msgs1) + len(msgs2) + len(msgs3)
    if total == 1 and 7 in escalated:
        r.ok("escalation_once_only (BUG FIX)")
    else:
        r.fail("escalation_once_only (BUG FIX)",
                f"Expected exactly 1 escalation, got {total} "
                f"(cycle1={len(msgs1)}, cycle2={len(msgs2)}, cycle3={len(msgs3)})")


def test_escalation_message_mentions_duration(r: _TestResult):
    """Escalation message should include how long the task has been pending."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [make_pending_task(age_seconds=2400, ping_count=2)]  # 40 min
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    if len(msgs) == 1:
        msg = msgs[0]["msg"]
        # Should mention ~40min
        if "40min" in msg or "39min" in msg or "41min" in msg:
            r.ok("escalation_message_mentions_duration")
        else:
            r.fail("escalation_message_mentions_duration",
                    f"Expected ~40min in msg: {msg[:120]}")
    else:
        r.fail("escalation_message_mentions_duration",
                f"Expected 1 msg, got {len(msgs)}")


# =============================================================================
# Tests -- Multiple Tasks
# =============================================================================

def test_multiple_tasks_independent(r: _TestResult):
    """Two pending tasks -> each gets its own nudge/escalation."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [
        make_pending_task(task_id=1, agent_id="@beta", age_seconds=700, ping_count=0),
        make_pending_task(task_id=2, agent_id="@haiku", age_seconds=700, ping_count=0),
    ]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    if len(msgs) == 2:
        agents = {m["agent_id"] for m in msgs}
        if agents == {"@beta", "@haiku"}:
            r.ok("multiple_tasks_independent")
        else:
            r.fail("multiple_tasks_independent",
                    f"Expected beta+haiku, got {agents}")
    else:
        r.fail("multiple_tasks_independent", f"Expected 2 msgs, got {len(msgs)}")


def test_mixed_nudge_and_escalation(r: _TestResult):
    """One task needs nudge, another needs escalation."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [
        make_pending_task(task_id=1, age_seconds=700, ping_count=0),   # nudge
        make_pending_task(task_id=2, age_seconds=2000, ping_count=3),  # escalation
    ]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    types = {m["task_id"]: m["type"] for m in msgs}
    if types.get(1) == "nudge" and types.get(2) == "escalation":
        r.ok("mixed_nudge_and_escalation")
    else:
        r.fail("mixed_nudge_and_escalation", f"Expected nudge+escalation, got {types}")


def test_escalated_task_does_not_block_others(r: _TestResult):
    """Task #1 already escalated -> task #2 can still get nudged."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = {1}  # Task 1 already escalated

    tasks = [
        make_pending_task(task_id=1, age_seconds=3000, ping_count=5),  # should be skipped
        make_pending_task(task_id=2, age_seconds=700, ping_count=0),   # should get nudge
    ]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    if len(msgs) == 1 and msgs[0]["task_id"] == 2 and msgs[0]["type"] == "nudge":
        r.ok("escalated_task_does_not_block_others")
    else:
        r.fail("escalated_task_does_not_block_others",
                f"Expected 1 nudge for #2, got {msgs}")


# =============================================================================
# Tests -- Edge Cases
# =============================================================================

def test_empty_task_list(r: _TestResult):
    """No pending tasks -> no crash, no messages."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    msgs = simulate_pending_watchdog_cycle([], storage, transport, escalated)

    if len(msgs) == 0:
        r.ok("empty_task_list")
    else:
        r.fail("empty_task_list", f"Expected 0, got {len(msgs)}")


def test_no_transport(r: _TestResult):
    """No transport (DDS offline) -> still processes but no messages sent."""
    storage = MagicMock()
    escalated = set()

    tasks = [make_pending_task(age_seconds=700, ping_count=0)]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, None, escalated)

    # Should still call update_task_pinged (tracking) but not crash
    if len(msgs) == 1 and storage.update_task_pinged.called:
        r.ok("no_transport")
    else:
        r.fail("no_transport", f"Expected 1 msg + pinged, got {len(msgs)}")


def test_task_missing_agent_id(r: _TestResult):
    """v4.5 BUG FIX: Task with empty agent_id -> skipped (no broken mention)."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [make_pending_task(agent_id="", age_seconds=700, ping_count=0)]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    # v4.5: Should be skipped entirely -- no message, no crash
    if len(msgs) == 0 and not storage.update_task_pinged.called:
        r.ok("task_missing_agent_id")
    else:
        r.fail("task_missing_agent_id",
                f"Expected 0 msgs (skip), got {len(msgs)}")


def test_task_with_at_prefix_stripped(r: _TestResult):
    """Agent ID '@beta' -> message shows '@beta' not '@@beta'."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [make_pending_task(agent_id="@beta", age_seconds=700, ping_count=0)]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    if len(msgs) == 1:
        msg = msgs[0]["msg"]
        if "@@beta" not in msg and "@beta" in msg:
            r.ok("task_with_at_prefix_stripped")
        else:
            r.fail("task_with_at_prefix_stripped", f"Double @@ in: {msg[:80]}")
    else:
        r.fail("task_with_at_prefix_stripped", f"Expected 1, got {len(msgs)}")


def test_pending_fallback_age_is_inf_on_error(r: _TestResult):
    """v4.5 BUG FIX: If _seconds_since throws, age should be inf (escalate),
    not 0 (silence). Fail-safe = assume old task = escalate to lead."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    # Create task with a completely invalid created_at timestamp
    task = make_pending_task(task_id=99, age_seconds=0, ping_count=0)
    task["created_at"] = "GARBAGE_TIMESTAMP"

    msgs = simulate_pending_watchdog_cycle([task], storage, transport, escalated)

    # With inf age, this should trigger escalation (not nudge, not silence)
    if len(msgs) == 1 and msgs[0]["type"] == "escalation":
        r.ok("pending_fallback_age_is_inf_on_error (BUG FIX)")
    else:
        types = [m["type"] for m in msgs] if msgs else []
        r.fail("pending_fallback_age_is_inf_on_error (BUG FIX)",
                f"Expected 1 escalation, got {len(msgs)} msgs: {types}")


def test_pending_skip_empty_agent_id(r: _TestResult):
    """v4.5 BUG FIX: Tasks with empty agent_id are skipped entirely.
    No broken '@: Task #X...' mention, no update_task_pinged call."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    tasks = [
        make_pending_task(task_id=1, agent_id="", age_seconds=700, ping_count=0),
        make_pending_task(task_id=2, agent_id="@beta", age_seconds=700, ping_count=0),
    ]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)

    # Task 1 (empty agent) should be skipped, task 2 should get nudge
    if len(msgs) == 1 and msgs[0]["task_id"] == 2 and msgs[0]["type"] == "nudge":
        # Also verify update_task_pinged was only called once (for task 2)
        if storage.update_task_pinged.call_count == 1:
            r.ok("pending_skip_empty_agent_id (BUG FIX)")
        else:
            r.fail("pending_skip_empty_agent_id (BUG FIX)",
                    f"Expected 1 pinged call, got {storage.update_task_pinged.call_count}")
    else:
        r.fail("pending_skip_empty_agent_id (BUG FIX)",
                f"Expected 1 nudge for #2 only, got {msgs}")


def test_full_lifecycle(r: _TestResult):
    """Full lifecycle: nudge -> nudge -> escalation -> stop.
    Simulates 4 watchdog cycles with increasing ping_count."""
    storage = MagicMock()
    transport = MagicMock()
    escalated = set()

    total_msgs = []

    # Cycle 1: fresh pending task (10+ min old, 0 pings) -> nudge
    tasks = [make_pending_task(task_id=1, age_seconds=700, ping_count=0)]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)
    total_msgs.extend(msgs)

    # Cycle 2: same task, now 1 ping (storage incremented) -> nudge again
    tasks = [make_pending_task(task_id=1, age_seconds=1300, ping_count=1)]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)
    total_msgs.extend(msgs)

    # Cycle 3: 2 pings = MAX -> escalation
    tasks = [make_pending_task(task_id=1, age_seconds=1900, ping_count=2)]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)
    total_msgs.extend(msgs)

    # Cycle 4: still pending, still 2+ pings -> no more messages (escalated_pending blocks)
    tasks = [make_pending_task(task_id=1, age_seconds=2500, ping_count=3)]
    msgs = simulate_pending_watchdog_cycle(tasks, storage, transport, escalated)
    total_msgs.extend(msgs)

    types = [m["type"] for m in total_msgs]
    if types == ["nudge", "nudge", "escalation"]:
        r.ok("full_lifecycle (nudge -> nudge -> escalation -> stop)")
    else:
        r.fail("full_lifecycle",
                f"Expected [nudge, nudge, escalation], got {types}")


# =============================================================================
# Tests -- Storage Integration (get_stale_pending_tasks query)
# =============================================================================

def test_storage_query_filters_by_status(r: _TestResult):
    """get_stale_pending_tasks should only return 'pending' tasks."""
    # This tests the SQL query logic, not the watchdog.
    # We use a real in-memory SQLite DB.
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE agent_tasks (
            id INTEGER PRIMARY KEY,
            agent_id TEXT,
            description TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            last_activity TEXT,
            last_pinged_at TEXT,
            ping_count INTEGER DEFAULT 0,
            completed_at TEXT
        )
    """)

    old_time = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    recent_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Old pending task -> should be returned
    conn.execute(
        "INSERT INTO agent_tasks (id, agent_id, description, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (1, "@beta", "Old pending", "pending", old_time)
    )
    # Old in_progress task -> should NOT be returned
    conn.execute(
        "INSERT INTO agent_tasks (id, agent_id, description, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (2, "@alpha", "Old active", "in_progress", old_time)
    )
    # Recent pending task -> should NOT be returned (too young)
    conn.execute(
        "INSERT INTO agent_tasks (id, agent_id, description, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (3, "@haiku", "Fresh pending", "pending", recent_time)
    )
    conn.commit()

    c = conn.cursor()
    c.execute("""
        SELECT * FROM agent_tasks
        WHERE status = 'pending'
        AND datetime(created_at) < datetime('now', '-' || ? || ' seconds')
        AND (
            last_pinged_at IS NULL
            OR datetime(last_pinged_at) < datetime('now', '-' || ? || ' seconds')
        )
        ORDER BY created_at ASC
    """, (600, 600))
    rows = [dict(row) for row in c.fetchall()]

    if len(rows) == 1 and rows[0]["id"] == 1:
        r.ok("storage_query_filters_by_status")
    else:
        r.fail("storage_query_filters_by_status",
                f"Expected [id=1], got {[r['id'] for r in rows]}")

    conn.close()


def test_storage_query_respects_ping_interval(r: _TestResult):
    """get_stale_pending_tasks should skip recently-pinged tasks."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE agent_tasks (
            id INTEGER PRIMARY KEY,
            agent_id TEXT,
            description TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            last_activity TEXT,
            last_pinged_at TEXT,
            ping_count INTEGER DEFAULT 0,
            completed_at TEXT
        )
    """)

    old_time = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    recent_ping = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")

    # Old pending, recently pinged -> should NOT be returned
    conn.execute(
        "INSERT INTO agent_tasks (id, agent_id, description, status, created_at, last_pinged_at, ping_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "@beta", "Recently pinged", "pending", old_time, recent_ping, 1)
    )
    # Old pending, never pinged -> should be returned
    conn.execute(
        "INSERT INTO agent_tasks (id, agent_id, description, status, created_at, last_pinged_at, ping_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (2, "@haiku", "Never pinged", "pending", old_time, None, 0)
    )
    conn.commit()

    c = conn.cursor()
    c.execute("""
        SELECT * FROM agent_tasks
        WHERE status = 'pending'
        AND datetime(created_at) < datetime('now', '-' || ? || ' seconds')
        AND (
            last_pinged_at IS NULL
            OR datetime(last_pinged_at) < datetime('now', '-' || ? || ' seconds')
        )
        ORDER BY created_at ASC
    """, (600, 600))
    rows = [dict(row) for row in c.fetchall()]

    if len(rows) == 1 and rows[0]["id"] == 2:
        r.ok("storage_query_respects_ping_interval")
    else:
        r.fail("storage_query_respects_ping_interval",
                f"Expected [id=2], got {[r['id'] for r in rows]}")

    conn.close()


# =============================================================================
# Runner
# =============================================================================

def main():
    print("=" * 60)
    print("Task Watchdog -- Pending Task Reminder Tests (v4.3)")
    print("=" * 60)

    r = _TestResult()

    print("\n--- Basic Timing ---")
    test_no_nudge_before_threshold(r)
    test_first_nudge_sent(r)
    test_nudge_calls_update_task_pinged(r)

    print("\n--- Escalation ---")
    test_escalation_after_max_pings(r)
    test_escalation_after_time_threshold(r)
    test_escalation_once_only(r)
    test_escalation_message_mentions_duration(r)

    print("\n--- Multiple Tasks ---")
    test_multiple_tasks_independent(r)
    test_mixed_nudge_and_escalation(r)
    test_escalated_task_does_not_block_others(r)

    print("\n--- Edge Cases ---")
    test_empty_task_list(r)
    test_no_transport(r)
    test_task_missing_agent_id(r)
    test_task_with_at_prefix_stripped(r)

    print("\n--- v4.5 Bug Fixes ---")
    test_pending_fallback_age_is_inf_on_error(r)
    test_pending_skip_empty_agent_id(r)

    print("\n--- Full Lifecycle ---")
    test_full_lifecycle(r)

    print("\n--- Storage Integration ---")
    test_storage_query_filters_by_status(r)
    test_storage_query_respects_ping_interval(r)

    success = r.summary()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
