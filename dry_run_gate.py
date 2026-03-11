"""DryRunGate -- safety layer for all Git write operations.

Separate module (Brainstorm #9 decision A): keeps gating logic out of
git_provider.py.  All write ops are queued in the git_actions_queue table
(decision B: reuse existing table, add approved_at/pending flow).

Phase 2 MVP scope (decision C): comment() only.

Flow:
  1. Agent calls gate.comment(repo, number, body)
  2. Gate inserts a 'pending' row in git_actions_queue (dry-run mode)
     -- OR checks approval + executes immediately (live mode)
  3. Dashboard shows pending queue, human approves/rejects
  4. POST /api/github/execute picks up approved actions and runs them

Architecture:
  - DryRunGate wraps an IssueProvider (GitHubProvider)
  - Storage-backed queue (SQLite via aircp_storage)
  - Audit trail via git_events for every action
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from git_provider import (
    Comment,
    GitProviderError,
    IssueProvider,
    NotApprovedError,
)

__version__ = "0.1.0"  # Phase 2 MVP: comment() only

logger = logging.getLogger("dry_run_gate")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QueuedAction:
    """Returned when an action is queued (dry-run mode)."""
    action_id: int
    action_type: str
    status: str  # "pending"
    preview: str
    params: dict

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "status": self.status,
            "preview": self.preview,
            "params": self.params,
        }


@dataclass(frozen=True)
class ExecutedAction:
    """Returned when an approved action is executed (live mode)."""
    action_id: int
    action_type: str
    status: str  # "executed"
    result: Any

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "status": self.status,
            "result": repr(self.result)[:500],
        }


# ---------------------------------------------------------------------------
# DryRunGate
# ---------------------------------------------------------------------------

class DryRunGate:
    """Safety gate for Git write operations.

    dry_run=True (default):
        All write calls are queued as 'pending' in git_actions_queue.
        Nothing touches the remote Git host.  The dashboard shows a
        "would have done X" preview queue.

    dry_run=False (live mode):
        Write calls check for prior dashboard approval (status='approved'
        in the queue).  If approved, executes and marks 'executed'.
        If not approved, raises NotApprovedError.

    Every call is logged to the audit trail regardless of mode.
    """

    def __init__(
        self,
        provider: IssueProvider,
        storage,  # aircp_storage.AIRCPStorage instance
        repo_id: int,
        dry_run: bool = True,
    ):
        """
        Args:
            provider: The IssueProvider to delegate write calls to.
            storage: AIRCPStorage instance for queue persistence.
            repo_id: Default repo ID (from git_repos table).
            dry_run: If True, queue actions without executing.
        """
        self.provider = provider
        self.storage = storage
        self.repo_id = repo_id
        self.dry_run = dry_run

    # -- Public write operations (Phase 2 MVP: comment only) ---------------

    def comment(
        self,
        repo: str,
        number: int,
        body: str,
        actor_id: str = "@system",
    ) -> QueuedAction:
        """Post a comment on an issue -- gated.

        Always queues the action and returns a QueuedAction (pending).
        Execution happens later via execute_approved() after dashboard approval.

        Args:
            repo: Owner/repo (e.g. "hdds-team/aircp").
            number: Issue number.
            body: Comment body (markdown).
            actor_id: Agent requesting the action.

        Returns:
            QueuedAction with the queue entry details.
        """
        params = {"repo": repo, "number": number, "body": body}
        preview = (
            f"Comment on #{number}: "
            f"{body[:120]}{'...' if len(body) > 120 else ''}"
        )

        action_id = self.storage.queue_git_action(
            repo_id=self.repo_id,
            action_type="comment",
            actor_id=actor_id,
            params=params,
            issue_number=number,
            preview=preview,
        )

        if action_id < 0:
            raise GitProviderError("Failed to queue comment action")

        # Audit trail
        self.storage.log_git_event(
            "queue_comment",
            actor_id=actor_id,
            repo_id=self.repo_id,
            issue_number=number,
            details={"action_id": action_id, "dry_run": self.dry_run,
                      "body_len": len(body)},
        )

        logger.info(
            "[%s] Queued comment on #%d by %s (action_id=%d)",
            "DRY-RUN" if self.dry_run else "LIVE",
            number, actor_id, action_id,
        )

        return QueuedAction(
            action_id=action_id,
            action_type="comment",
            status="pending",
            preview=preview,
            params=params,
        )

    # -- Execution of approved actions -------------------------------------

    def execute_approved(self, action_id: int) -> ExecutedAction:
        """Execute a previously approved action.

        Called by POST /api/github/execute after dashboard approval.

        Args:
            action_id: ID from git_actions_queue.

        Returns:
            ExecutedAction with the result.

        Raises:
            NotApprovedError: Action is not in 'approved' status.
            GitProviderError: Execution failed.
        """
        action = self.storage.get_git_action(action_id)
        if not action:
            raise GitProviderError(f"Action {action_id} not found")

        if action["status"] != "approved":
            raise NotApprovedError(
                f"Action {action_id} is '{action['status']}', "
                f"expected 'approved'"
            )

        action_type = action["action_type"]
        params = action.get("params", {})
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}

        # Dispatch to provider method
        result = self._dispatch(action_type, params)

        # Mark executed in storage
        result_str = repr(result)[:500]
        self.storage.mark_git_action_executed(action_id, result=result_str)

        # Audit trail
        self.storage.log_git_event(
            f"execute_{action_type}",
            actor_id=action.get("actor_id", "@system"),
            repo_id=self.repo_id,
            issue_number=action.get("issue_number"),
            details={
                "action_id": action_id,
                "approved_by": action.get("approved_by", ""),
            },
        )

        logger.info(
            "Executed action %d (%s) approved by %s",
            action_id, action_type, action.get("approved_by", "?"),
        )

        return ExecutedAction(
            action_id=action_id,
            action_type=action_type,
            status="executed",
            result=result,
        )

    # -- Internal dispatch -------------------------------------------------

    def _dispatch(self, action_type: str, params: dict) -> Any:
        """Route an action_type to the correct provider method.

        Phase 2 MVP: only 'comment' is supported.
        Phase 2+: add 'add_label', 'create_pr' here.
        """
        if action_type == "comment":
            repo = params.get("repo")
            number = params.get("number")
            body = params.get("body")
            if not repo or number is None or not body:
                raise GitProviderError(
                    f"Incomplete params for 'comment' action: "
                    f"need repo, number, body -- got {list(params.keys())}"
                )
            return self.provider.comment(
                repo=repo,
                number=number,
                body=body,
            )

        # Future: add_label, create_pr
        raise GitProviderError(
            f"Unknown action type: {action_type}. "
            f"Phase 2 MVP only supports: comment"
        )

    # -- Query helpers (convenience wrappers around storage) ----------------

    def get_pending(self) -> list[dict]:
        """Get all pending actions for this repo."""
        return self.storage.get_pending_git_actions(repo_id=self.repo_id)

    def get_action(self, action_id: int) -> Optional[dict]:
        """Get a single action by ID."""
        return self.storage.get_git_action(action_id)
