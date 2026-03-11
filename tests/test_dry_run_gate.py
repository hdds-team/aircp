"""Unit tests for dry_run_gate.py -- Phase 2 DryRunGate with storage backend.

Tests cover:
1. comment() queues action in storage (dry-run mode)
2. comment() input validation
3. execute_approved() dispatches to provider
4. execute_approved() rejects non-approved actions
5. execute_approved() handles missing actions
6. Full flow: queue -> approve -> execute
7. Audit trail (git_events logged)
8. get_pending() / get_action() wrappers
9. Unknown action type rejection

Uses a real SQLite in-memory database via AIRCPStorage (no mocks for storage).
Provider is mocked via a fake class.
"""

import json
import sqlite3

import pytest

# We need the storage to be importable -- add project root to path
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from git_provider import (
    Comment,
    GitProviderError,
    NotApprovedError,
    User,
)
from dry_run_gate import DryRunGate, QueuedAction, ExecutedAction


# ---------------------------------------------------------------------------
# Fake provider (no HTTP, controllable)
# ---------------------------------------------------------------------------

class FakeProvider:
    """Minimal IssueProvider fake for testing DryRunGate dispatch."""

    def __init__(self):
        self.calls: list[dict] = []
        self.next_comment = Comment(
            id=999, body="fake", user=User(login="bot"),
            created_at="2026-03-11T00:00:00Z",
        )

    def list_issues(self, repo, **kw):
        return []

    def get_issue(self, repo, number, **kw):
        raise NotImplementedError

    def comment(self, repo: str, number: int, body: str) -> Comment:
        self.calls.append({"action": "comment", "repo": repo,
                           "number": number, "body": body})
        return self.next_comment

    def add_label(self, repo, number, labels):
        self.calls.append({"action": "add_label", "repo": repo,
                           "number": number, "labels": labels})

    def create_pr(self, repo, head, base, title, body, draft=False):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Minimal storage (real SQLite, in-memory)
# ---------------------------------------------------------------------------

class MiniStorage:
    """Minimal storage implementing the methods DryRunGate needs.

    Uses a real SQLite in-memory DB with the git_actions_queue table.
    """

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
        self.events: list[dict] = []  # Capture git_events

    def _init_tables(self):
        self.conn.execute("""
            CREATE TABLE git_actions_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL,
                issue_number INTEGER,
                action_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                params TEXT NOT NULL DEFAULT '{}',
                preview TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                approved_by TEXT,
                rejected_by TEXT,
                result TEXT,
                queued_at TEXT NOT NULL,
                decided_at TEXT,
                executed_at TEXT
            )
        """)
        self.conn.commit()

    def queue_git_action(self, repo_id, action_type, actor_id,
                         params=None, issue_number=None, preview=""):
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO git_actions_queue
            (repo_id, issue_number, action_type, actor_id, params, preview,
             status, queued_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', datetime('now'))
        """, (repo_id, issue_number, action_type, actor_id,
              json.dumps(params or {}), preview))
        self.conn.commit()
        return c.lastrowid

    def get_pending_git_actions(self, repo_id=None):
        c = self.conn.cursor()
        if repo_id is not None:
            c.execute(
                "SELECT * FROM git_actions_queue WHERE status='pending' AND repo_id=?",
                (repo_id,))
        else:
            c.execute("SELECT * FROM git_actions_queue WHERE status='pending'")
        rows = [dict(r) for r in c.fetchall()]
        for d in rows:
            if d.get("params"):
                try:
                    d["params"] = json.loads(d["params"])
                except (json.JSONDecodeError, TypeError):
                    pass
        return rows

    def get_git_action(self, action_id):
        c = self.conn.cursor()
        c.execute("SELECT * FROM git_actions_queue WHERE id=?", (action_id,))
        row = c.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("params"):
            try:
                d["params"] = json.loads(d["params"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def approve_git_action(self, action_id, approved_by):
        c = self.conn.cursor()
        c.execute("""
            UPDATE git_actions_queue
            SET status='approved', approved_by=?, decided_at=datetime('now')
            WHERE id=? AND status='pending'
        """, (approved_by, action_id))
        self.conn.commit()
        return c.rowcount > 0

    def reject_git_action(self, action_id, rejected_by):
        c = self.conn.cursor()
        c.execute("""
            UPDATE git_actions_queue
            SET status='rejected', rejected_by=?, decided_at=datetime('now')
            WHERE id=? AND status='pending'
        """, (rejected_by, action_id))
        self.conn.commit()
        return c.rowcount > 0

    def mark_git_action_executed(self, action_id, result=""):
        c = self.conn.cursor()
        c.execute("""
            UPDATE git_actions_queue
            SET status='executed', result=?, executed_at=datetime('now')
            WHERE id=? AND status='approved'
        """, (result, action_id))
        self.conn.commit()
        return c.rowcount > 0

    def log_git_event(self, event_type, actor_id="", repo_id=None,
                      issue_number=None, details=None):
        self.events.append({
            "event_type": event_type,
            "actor_id": actor_id,
            "repo_id": repo_id,
            "issue_number": issue_number,
            "details": details or {},
        })


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REPO = "hdds-team/aircp"
REPO_ID = 1


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def storage():
    return MiniStorage()


@pytest.fixture
def gate(provider, storage):
    """DryRunGate in dry-run mode (default)."""
    return DryRunGate(provider=provider, storage=storage,
                      repo_id=REPO_ID, dry_run=True)


@pytest.fixture
def live_gate(provider, storage):
    """DryRunGate in live mode."""
    return DryRunGate(provider=provider, storage=storage,
                      repo_id=REPO_ID, dry_run=False)


# ===========================================================================
# 1. comment() queues action (dry-run)
# ===========================================================================

class TestCommentQueue:

    def test_comment_returns_queued_action(self, gate):
        """comment() returns a QueuedAction with correct fields."""
        result = gate.comment(REPO, 42, "Hello world", actor_id="@alpha")
        assert isinstance(result, QueuedAction)
        assert result.action_type == "comment"
        assert result.status == "pending"
        assert result.action_id > 0
        assert "Hello world" in result.preview

    def test_comment_persisted_in_storage(self, gate, storage):
        """comment() creates a row in git_actions_queue."""
        result = gate.comment(REPO, 42, "Test body", actor_id="@alpha")
        row = storage.get_git_action(result.action_id)
        assert row is not None
        assert row["status"] == "pending"
        assert row["action_type"] == "comment"
        assert row["actor_id"] == "@alpha"
        assert row["issue_number"] == 42
        assert row["params"]["body"] == "Test body"
        assert row["params"]["repo"] == REPO

    def test_comment_no_http_in_dry_run(self, gate, provider):
        """Dry-run comment does NOT call provider.comment()."""
        gate.comment(REPO, 42, "Should not call provider")
        assert len(provider.calls) == 0

    def test_comment_preview_truncated(self, gate):
        """Long comment body is truncated in preview."""
        long_body = "x" * 200
        result = gate.comment(REPO, 1, long_body)
        assert len(result.preview) < 200
        assert result.preview.endswith("...")

    def test_comment_short_preview_no_ellipsis(self, gate):
        """Short comment body has no ellipsis in preview."""
        result = gate.comment(REPO, 1, "Short")
        assert "..." not in result.preview

    def test_multiple_comments_queued(self, gate, storage):
        """Multiple comments create separate queue entries."""
        r1 = gate.comment(REPO, 1, "First")
        r2 = gate.comment(REPO, 2, "Second")
        assert r1.action_id != r2.action_id
        pending = storage.get_pending_git_actions(repo_id=REPO_ID)
        assert len(pending) == 2

    def test_comment_params_contain_all_fields(self, gate):
        """QueuedAction.params has repo, number, body."""
        result = gate.comment(REPO, 42, "test body")
        assert result.params == {
            "repo": REPO,
            "number": 42,
            "body": "test body",
        }

    def test_comment_to_dict(self, gate):
        """QueuedAction.to_dict() returns serializable dict."""
        result = gate.comment(REPO, 42, "test")
        d = result.to_dict()
        assert d["action_type"] == "comment"
        assert d["status"] == "pending"
        assert isinstance(d["action_id"], int)
        # Should be JSON-serializable
        json.dumps(d)


# ===========================================================================
# 2. Audit trail
# ===========================================================================

class TestAuditTrail:

    def test_comment_logs_git_event(self, gate, storage):
        """comment() logs a queue_comment event."""
        gate.comment(REPO, 42, "test", actor_id="@haiku")
        events = [e for e in storage.events if e["event_type"] == "queue_comment"]
        assert len(events) == 1
        assert events[0]["actor_id"] == "@haiku"
        assert events[0]["issue_number"] == 42
        assert events[0]["details"]["dry_run"] is True

    def test_execute_logs_git_event(self, live_gate, storage):
        """execute_approved() logs an execute_comment event."""
        result = live_gate.comment(REPO, 42, "will execute")
        storage.approve_git_action(result.action_id, "@naskel")
        live_gate.execute_approved(result.action_id)
        events = [e for e in storage.events
                  if e["event_type"] == "execute_comment"]
        assert len(events) == 1
        assert events[0]["details"]["approved_by"] == "@naskel"


# ===========================================================================
# 3. execute_approved() -- happy path
# ===========================================================================

class TestExecuteApproved:

    def test_execute_calls_provider(self, live_gate, storage, provider):
        """execute_approved() dispatches to provider.comment()."""
        result = live_gate.comment(REPO, 42, "Hello GitHub")
        storage.approve_git_action(result.action_id, "@naskel")
        executed = live_gate.execute_approved(result.action_id)

        assert isinstance(executed, ExecutedAction)
        assert executed.status == "executed"
        assert executed.action_type == "comment"
        assert len(provider.calls) == 1
        assert provider.calls[0]["body"] == "Hello GitHub"

    def test_execute_marks_storage_executed(self, live_gate, storage):
        """After execution, storage row status is 'executed'."""
        result = live_gate.comment(REPO, 42, "test")
        storage.approve_git_action(result.action_id, "@naskel")
        live_gate.execute_approved(result.action_id)

        row = storage.get_git_action(result.action_id)
        assert row["status"] == "executed"
        assert row["executed_at"] is not None

    def test_execute_returns_provider_result(self, live_gate, storage, provider):
        """ExecutedAction.result contains the Comment from provider."""
        provider.next_comment = Comment(
            id=123, body="real comment", user=User(login="bot"),
        )
        result = live_gate.comment(REPO, 42, "test")
        storage.approve_git_action(result.action_id, "@naskel")
        executed = live_gate.execute_approved(result.action_id)
        assert isinstance(executed.result, Comment)
        assert executed.result.id == 123

    def test_executed_to_dict(self, live_gate, storage):
        """ExecutedAction.to_dict() is JSON-serializable."""
        result = live_gate.comment(REPO, 42, "test")
        storage.approve_git_action(result.action_id, "@naskel")
        executed = live_gate.execute_approved(result.action_id)
        d = executed.to_dict()
        json.dumps(d)  # Must not raise


# ===========================================================================
# 4. execute_approved() -- error cases
# ===========================================================================

class TestExecuteErrors:

    def test_execute_pending_raises(self, gate, storage):
        """Cannot execute a pending (unapproved) action."""
        result = gate.comment(REPO, 42, "pending comment")
        with pytest.raises(NotApprovedError, match="pending"):
            gate.execute_approved(result.action_id)

    def test_execute_rejected_raises(self, gate, storage):
        """Cannot execute a rejected action."""
        result = gate.comment(REPO, 42, "will be rejected")
        storage.reject_git_action(result.action_id, "@naskel")
        with pytest.raises(NotApprovedError, match="rejected"):
            gate.execute_approved(result.action_id)

    def test_execute_nonexistent_raises(self, gate):
        """Executing a non-existent action_id raises GitProviderError."""
        with pytest.raises(GitProviderError, match="not found"):
            gate.execute_approved(99999)

    def test_execute_already_executed_raises(self, live_gate, storage):
        """Cannot execute an already-executed action."""
        result = live_gate.comment(REPO, 42, "once only")
        storage.approve_git_action(result.action_id, "@naskel")
        live_gate.execute_approved(result.action_id)
        # Second execution should fail
        with pytest.raises(NotApprovedError, match="executed"):
            live_gate.execute_approved(result.action_id)


# ===========================================================================
# 5. Full flow: queue -> approve -> execute
# ===========================================================================

class TestFullFlow:

    def test_dry_run_queue_approve_execute(self, gate, storage, provider):
        """Complete flow: queue in dry-run, approve, execute."""
        # Step 1: Queue
        queued = gate.comment(REPO, 42, "Full flow test", actor_id="@alpha")
        assert queued.status == "pending"
        assert len(provider.calls) == 0

        # Step 2: Verify pending
        pending = gate.get_pending()
        assert len(pending) == 1
        assert pending[0]["action_type"] == "comment"

        # Step 3: Approve (simulates dashboard action)
        storage.approve_git_action(queued.action_id, "@naskel")
        action = gate.get_action(queued.action_id)
        assert action["status"] == "approved"

        # Step 4: Execute
        executed = gate.execute_approved(queued.action_id)
        assert executed.status == "executed"
        assert len(provider.calls) == 1
        assert provider.calls[0]["body"] == "Full flow test"

        # Step 5: No more pending
        pending = gate.get_pending()
        assert len(pending) == 0

    def test_reject_flow(self, gate, storage, provider):
        """Queue then reject -- no execution."""
        queued = gate.comment(REPO, 42, "Will be rejected")
        storage.reject_git_action(queued.action_id, "@naskel")

        with pytest.raises(NotApprovedError):
            gate.execute_approved(queued.action_id)

        assert len(provider.calls) == 0


# ===========================================================================
# 6. Unknown action type
# ===========================================================================

class TestUnknownAction:

    def test_dispatch_unknown_type_raises(self, live_gate, storage):
        """Manually inserting an unknown action_type and executing raises."""
        action_id = storage.queue_git_action(
            repo_id=REPO_ID,
            action_type="delete_repo",  # Not supported
            actor_id="@evil",
            params={"repo": REPO},
        )
        storage.approve_git_action(action_id, "@naskel")
        with pytest.raises(GitProviderError, match="Unknown action type"):
            live_gate.execute_approved(action_id)


# ===========================================================================
# 7. get_pending() / get_action() wrappers
# ===========================================================================

class TestQueryHelpers:

    def test_get_pending_filters_by_repo(self, gate, storage):
        """get_pending() returns only actions for gate's repo_id."""
        gate.comment(REPO, 1, "our repo")
        # Manually insert for different repo_id
        storage.queue_git_action(
            repo_id=999, action_type="comment",
            actor_id="@other", params={},
        )
        pending = gate.get_pending()
        assert len(pending) == 1
        assert pending[0]["repo_id"] == REPO_ID

    def test_get_action_returns_none_for_missing(self, gate):
        """get_action() returns None for non-existent ID."""
        assert gate.get_action(12345) is None

    def test_get_action_returns_dict(self, gate):
        """get_action() returns a dict with parsed params."""
        result = gate.comment(REPO, 42, "test")
        action = gate.get_action(result.action_id)
        assert isinstance(action, dict)
        assert action["action_type"] == "comment"
        assert isinstance(action["params"], dict)
