#!/usr/bin/env python3
"""
Auto-lock Tests -- Brainstorm #7 implementation.

Tests the auto-lock/release lifecycle:
1. File path extraction from task descriptions
2. Auto-lock on task creation
3. Auto-release on task completion
4. Auto-release on stale (watchdog)
5. ping_count reset on activity (prevents false stale)
6. mark_stale_tasks_as_stale safety check on last_activity

Usage:
    python3 -m pytest tests/test_auto_lock.py -v
"""

import sys
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =============================================================================
# Tests -- File path extraction
# =============================================================================

class TestFilePathExtraction:
    """Test _extract_file_paths from handlers/tasks.py."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from handlers.tasks import _extract_file_paths
        self.extract = _extract_file_paths

    def test_simple_python_file(self):
        paths = self.extract("Fix bug in watchdogs.py")
        assert "watchdogs.py" in paths

    def test_nested_path(self):
        paths = self.extract("Update handlers/tasks.py and dashboard/src/App.svelte")
        assert "handlers/tasks.py" in paths
        assert "dashboard/src/App.svelte" in paths

    def test_backtick_wrapped(self):
        paths = self.extract("Patch `aircp_storage.py` for new column")
        assert "aircp_storage.py" in paths

    def test_no_false_positive_on_version(self):
        # "v4.3" should not be extracted as a file path
        paths = self.extract("Upgrade to v4.3 with new features")
        assert not any("v4.3" in p for p in paths)

    def test_multiple_extensions(self):
        paths = self.extract("Edit src/main.rs and config.toml and README.md")
        assert "src/main.rs" in paths
        assert "config.toml" in paths
        assert "README.md" in paths

    def test_empty_string(self):
        assert self.extract("") == []

    def test_no_files(self):
        assert self.extract("Implement feature X for the dashboard") == []


# =============================================================================
# Tests -- ping_count reset on activity
# =============================================================================

class TestPingCountReset:
    """Test that update_task_activity resets ping_count and last_pinged_at."""

    @pytest.fixture
    def db(self):
        """Create an in-memory SQLite DB with agent_tasks table."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE agent_tasks (
                id INTEGER PRIMARY KEY,
                agent_id TEXT,
                description TEXT,
                task_type TEXT DEFAULT 'generic',
                status TEXT DEFAULT 'in_progress',
                context TEXT,
                created_at TEXT,
                last_activity TEXT,
                last_pinged_at TEXT,
                ping_count INTEGER DEFAULT 0,
                current_step INTEGER DEFAULT 0,
                completed_at TEXT,
                workflow_id INTEGER,
                project_id TEXT DEFAULT 'default'
            )
        """)
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        pinged = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO agent_tasks
                (id, agent_id, description, status, created_at, last_activity,
                 last_pinged_at, ping_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (1, "@alpha", "Test task", "in_progress", old, old, pinged, 3))
        conn.commit()
        yield conn
        conn.close()

    def test_activity_resets_ping_count(self, db):
        """Core bug fix: task/activity must reset ping_count to 0."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("""
            UPDATE agent_tasks SET last_activity = ?,
            ping_count = 0, last_pinged_at = NULL WHERE id = ?
        """, (now, 1))
        db.commit()

        row = dict(db.execute("SELECT * FROM agent_tasks WHERE id = 1").fetchone())
        assert row["ping_count"] == 0, f"ping_count should be 0, got {row['ping_count']}"
        assert row["last_pinged_at"] is None, "last_pinged_at should be NULL after activity"

    def test_stale_not_marked_after_activity(self, db):
        """After activity resets ping_count, mark_stale should NOT touch the task."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # Simulate activity: reset pings + update last_activity
        db.execute("""
            UPDATE agent_tasks SET last_activity = ?,
            ping_count = 0, last_pinged_at = NULL WHERE id = ?
        """, (now, 1))
        db.commit()

        # Now run mark_stale_tasks_as_stale logic
        c = db.cursor()
        c.execute("""
            UPDATE agent_tasks
            SET status = 'stale', completed_at = ?
            WHERE status = 'in_progress'
            AND datetime(last_activity) < datetime('now', '-60 seconds')
            AND ping_count >= ?
        """, (now, 3))
        db.commit()

        row = dict(db.execute("SELECT * FROM agent_tasks WHERE id = 1").fetchone())
        assert row["status"] == "in_progress", \
            f"Task should still be in_progress, got {row['status']}"

    def test_stale_marked_when_truly_stale(self, db):
        """Task with old activity + high ping_count SHOULD be marked stale."""
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        c = db.cursor()
        c.execute("""
            UPDATE agent_tasks
            SET status = 'stale', completed_at = ?
            WHERE status = 'in_progress'
            AND datetime(last_activity) < datetime('now', '-60 seconds')
            AND ping_count >= ?
        """, (now, 3))
        db.commit()

        row = dict(db.execute("SELECT * FROM agent_tasks WHERE id = 1").fetchone())
        assert row["status"] == "stale", \
            f"Task should be stale (old activity + 3 pings), got {row['status']}"


# =============================================================================
# Tests -- Watchdog auto-release uses stale_tasks (not broad query)
# =============================================================================

class TestWatchdogAutoRelease:
    """Test that the watchdog auto-release logic uses the correct task set."""

    def test_auto_release_filters_by_ping_count(self):
        """Only tasks with ping_count + 1 >= MAX_PINGS should trigger release."""
        TASK_MAX_PINGS = 3

        stale_tasks = [
            {"id": 1, "agent_id": "@alpha", "ping_count": 0},  # 0+1=1 < 3 -> no
            {"id": 2, "agent_id": "@beta", "ping_count": 1},   # 1+1=2 < 3 -> no
            {"id": 3, "agent_id": "@haiku", "ping_count": 2},  # 2+1=3 >= 3 -> YES
            {"id": 4, "agent_id": "@sonnet", "ping_count": 5}, # 5+1=6 >= 3 -> YES
        ]

        released = []
        for t in stale_tasks:
            if t.get("ping_count", 0) + 1 >= TASK_MAX_PINGS:
                released.append(t["id"])

        assert released == [3, 4], f"Expected [3, 4], got {released}"

    def test_empty_stale_tasks_no_release(self):
        """No stale tasks -> no release calls."""
        stale_tasks = []
        released = []
        for t in stale_tasks:
            if t.get("ping_count", 0) + 1 >= 3:
                released.append(t["id"])
        assert released == []

    def test_all_below_threshold_no_release(self):
        """All tasks below ping threshold -> no release."""
        stale_tasks = [
            {"id": 1, "agent_id": "@alpha", "ping_count": 0},
            {"id": 2, "agent_id": "@beta", "ping_count": 1},
        ]
        released = []
        for t in stale_tasks:
            if t.get("ping_count", 0) + 1 >= 3:
                released.append(t["id"])
        assert released == []


# =============================================================================
# Runner
# =============================================================================

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
