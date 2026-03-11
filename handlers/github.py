"""GitHub Agent Mode routes: /api/github/*

Phase 1 (read-only MVP):
- GET  /api/github/issues  -- cached issue list
- GET  /api/github/queue   -- pending actions awaiting approval
- POST /api/github/assign  -- assign agent(s) to issue
- POST /api/github/approve -- approve a queued action
- POST /api/github/reject  -- reject a queued action

Reference: docs/_private/WIP_GIT_REPO_MODE-IDEA.md
"""

import logging
import os

logger = logging.getLogger("handlers.github")

# Lazy import to avoid circular deps -- same pattern as tasks.py
from aircp_daemon import storage, _bot_send

# Provider is initialized lazily on first use
_provider = None
_provider_init_attempted = False


def _get_provider():
    """Lazily initialize the GitHubProvider from env var.

    Returns None if GITHUB_TOKEN is not set (GitHub features disabled).
    Rejects classic PATs (ghp_*) per Beta's security spec.
    """
    global _provider, _provider_init_attempted
    if _provider_init_attempted:
        return _provider
    _provider_init_attempted = True

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        logger.warning("GITHUB_TOKEN not set -- GitHub features disabled")
        return None
    if token.startswith("ghp_"):
        logger.error(
            "GITHUB_TOKEN rejected: classic PAT (ghp_*) not allowed -- "
            "use a fine-grained PAT (github_pat_*) instead"
        )
        return None
    if not token.startswith("github_pat_"):
        logger.warning("GITHUB_TOKEN format unrecognized -- proceeding but verify")

    try:
        from git_provider import GitHubProvider
        _provider = GitHubProvider(token=token)
        logger.info("GitHub provider initialized (Phase 1 read-only)")
    except Exception as e:
        logger.error(f"Failed to initialize GitHub provider: {e}")
    return _provider


def _ensure_default_repo():
    """Ensure the default repo (hdds-team/aircp) is registered.

    Returns the repo_id or None.
    """
    repo = storage.get_git_repo(name="aircp")
    if repo:
        return repo["id"]

    repo_id = storage.add_git_repo(
        name="aircp",
        owner="hdds-team",
        source="github",
        api_url="https://api.github.com",
        html_url="https://github.com/hdds-team/aircp",
        default_branch="main",
    )
    return repo_id if repo_id > 0 else None


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_github_issues(handler, parsed, params):
    """GET /api/github/issues -- return cached issue list.

    Query params:
        state: "open" (default), "closed", "all"
        refresh: "1" to force refresh from GitHub API
    """
    state = params.get("state", ["open"])[0]
    refresh = params.get("refresh", ["0"])[0] == "1"

    repo_id = _ensure_default_repo()
    if repo_id is None:
        handler.send_json({"error": "No repo configured"}, 500)
        return

    # Refresh cache from GitHub if requested
    if refresh:
        provider = _get_provider()
        if provider is None:
            handler.send_json(
                {"error": "GitHub provider not configured (GITHUB_TOKEN not set)"},
                503,
            )
            return

        try:
            issues = provider.list_issues("hdds-team/aircp", state=state)
            # Convert Issue dataclasses to dicts for caching
            issue_dicts = []
            for iss in issues:
                issue_dicts.append({
                    "number": iss.number,
                    "title": iss.title,
                    "body": iss.body,
                    "state": iss.state,
                    "labels": [{"name": lb.name, "color": lb.color} for lb in iss.labels],
                    "assignees": [{"login": a.login} for a in iss.assignees],
                    "author_login": iss.user.login if iss.user else "",
                    "comments_count": iss.comments_count,
                    "html_url": iss.html_url,
                    "created_at": iss.created_at,
                    "updated_at": iss.updated_at,
                })
            storage.cache_issues(repo_id, issue_dicts)
            storage.log_git_event(
                "refresh_issues", actor_id="@system",
                repo_id=repo_id,
                details={"count": len(issue_dicts), "state": state},
            )
        except Exception as e:
            logger.error(f"Failed to refresh issues from GitHub: {e}")
            handler.send_json({"error": f"GitHub API error: {e}"}, 502)
            return

    # Return cached issues
    cached = storage.get_cached_issues(repo_id, state=state)

    # Enrich with assignments
    for issue in cached:
        assignments = storage.get_issue_assignments(repo_id, issue["issue_number"])
        issue["agents"] = [
            {"agent_id": a["agent_id"], "role": a["role"], "task_id": a.get("task_id")}
            for a in assignments
        ]

    handler.send_json({
        "issues": cached,
        "count": len(cached),
        "repo": "hdds-team/aircp",
        "state": state,
    })


def get_github_queue(handler, parsed, params):
    """GET /api/github/queue -- pending actions awaiting approval."""
    repo_id = _ensure_default_repo()
    if repo_id is None:
        handler.send_json({"error": "No repo configured"}, 500)
        return

    pending = storage.get_pending_git_actions(repo_id=repo_id)
    handler.send_json({
        "queue": pending,
        "count": len(pending),
    })


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def post_github_assign(handler, body):
    """POST /api/github/assign -- assign agent(s) to an issue.

    Body:
        issue_number: int (required)
        agent_id: str (required) -- agent to assign
        role: str -- "triage", "investigate", "code", "review" (default: "investigate")
        auto_task: bool -- create AIRCP task automatically (default: true)
    """
    try:
        issue_number = body.get("issue_number")
        agent_id = body.get("agent_id")
        role = body.get("role", "investigate")

        if not issue_number:
            handler.send_json({"error": "Missing 'issue_number'"}, 400)
            return
        if not agent_id:
            handler.send_json({"error": "Missing 'agent_id'"}, 400)
            return

        # Validate role
        valid_roles = ("triage", "investigate", "code", "review")
        if role not in valid_roles:
            handler.send_json(
                {"error": f"Invalid role '{role}'. Must be one of: {valid_roles}"},
                400,
            )
            return

        repo_id = _ensure_default_repo()
        if repo_id is None:
            handler.send_json({"error": "No repo configured"}, 500)
            return

        # Check for double-assign
        existing = storage.get_issue_assignments(repo_id, issue_number)
        for a in existing:
            if a["agent_id"] == agent_id:
                handler.send_json(
                    {"error": f"{agent_id} already assigned to issue #{issue_number}"},
                    409,
                )
                return

        # Auto-create AIRCP task if requested (default: yes)
        task_id = None
        auto_task = body.get("auto_task", True)
        if auto_task:
            # Get issue title for task description
            cached = storage.get_cached_issue(repo_id, issue_number)
            title = cached["title"] if cached else f"Issue #{issue_number}"
            task_desc = f"[GitHub #{issue_number}] {title} ({role})"

            task_id = storage.create_task(
                agent_id, "github_issue", task_desc,
                context={"issue_number": issue_number, "role": role,
                         "repo": "hdds-team/aircp"},
            )
            if task_id > 0:
                _bot_send(
                    "#general",
                    f"\U0001f4cb **TASK #{task_id}** created for {agent_id}: {task_desc}",
                    from_id="@taskman",
                )

        # Record assignment
        assignment_id = storage.assign_agent_to_issue(
            repo_id, issue_number, agent_id,
            role=role, task_id=task_id,
        )

        # Audit trail
        storage.log_git_event(
            "assign", actor_id="@naskel",
            repo_id=repo_id, issue_number=issue_number,
            details={"agent_id": agent_id, "role": role, "task_id": task_id},
        )

        handler.send_json({
            "status": "assigned",
            "assignment_id": assignment_id,
            "agent_id": agent_id,
            "issue_number": issue_number,
            "role": role,
            "task_id": task_id,
        })

    except Exception as e:
        logger.error(f"GitHub assign error: {e}")
        handler.send_json({"error": str(e)}, 500)


def post_github_approve(handler, body):
    """POST /api/github/approve -- approve a queued action.

    Body:
        action_id: int (required)
        approved_by: str (default: "@naskel")
    """
    try:
        action_id = body.get("action_id")
        approved_by = body.get("approved_by", "@naskel")

        if not action_id:
            handler.send_json({"error": "Missing 'action_id'"}, 400)
            return

        action = storage.get_git_action(action_id)
        if not action:
            handler.send_json({"error": f"Action {action_id} not found"}, 404)
            return
        if action["status"] != "pending":
            handler.send_json(
                {"error": f"Action {action_id} is '{action['status']}', not pending"},
                409,
            )
            return

        success = storage.approve_git_action(action_id, approved_by)
        if not success:
            handler.send_json({"error": "Failed to approve action"}, 500)
            return

        # Audit trail
        storage.log_git_event(
            "approve_action", actor_id=approved_by,
            repo_id=action.get("repo_id"),
            issue_number=action.get("issue_number"),
            details={"action_id": action_id, "action_type": action["action_type"]},
        )

        handler.send_json({
            "status": "approved",
            "action_id": action_id,
            "approved_by": approved_by,
        })

    except Exception as e:
        logger.error(f"GitHub approve error: {e}")
        handler.send_json({"error": str(e)}, 500)


def post_github_reject(handler, body):
    """POST /api/github/reject -- reject a queued action.

    Body:
        action_id: int (required)
        rejected_by: str (default: "@naskel")
    """
    try:
        action_id = body.get("action_id")
        rejected_by = body.get("rejected_by", "@naskel")

        if not action_id:
            handler.send_json({"error": "Missing 'action_id'"}, 400)
            return

        action = storage.get_git_action(action_id)
        if not action:
            handler.send_json({"error": f"Action {action_id} not found"}, 404)
            return
        if action["status"] != "pending":
            handler.send_json(
                {"error": f"Action {action_id} is '{action['status']}', not pending"},
                409,
            )
            return

        success = storage.reject_git_action(action_id, rejected_by)
        if not success:
            handler.send_json({"error": "Failed to reject action"}, 500)
            return

        # Audit trail
        storage.log_git_event(
            "reject_action", actor_id=rejected_by,
            repo_id=action.get("repo_id"),
            issue_number=action.get("issue_number"),
            details={"action_id": action_id, "action_type": action["action_type"]},
        )

        handler.send_json({
            "status": "rejected",
            "action_id": action_id,
            "rejected_by": rejected_by,
        })

    except Exception as e:
        logger.error(f"GitHub reject error: {e}")
        handler.send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# Route tables (collected by handlers/__init__.py)
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/api/github/issues": get_github_issues,
    "/api/github/queue": get_github_queue,
}

POST_ROUTES = {
    "/api/github/assign": post_github_assign,
    "/api/github/approve": post_github_approve,
    "/api/github/reject": post_github_reject,
}
