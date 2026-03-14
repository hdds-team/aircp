"""GitHub Agent Mode routes: /api/github/*  (Phase 1 + Phase 2 MVP + Multi-repo)

Phase 1 (read-only MVP):
- GET  /api/github/issues  -- cached issue list
- GET  /api/github/queue   -- pending actions awaiting approval
- POST /api/github/assign  -- assign agent(s) to issue
- POST /api/github/approve -- approve a queued action
- POST /api/github/reject  -- reject a queued action

Phase 2 (write MVP -- Brainstorm #9):
- POST /api/github/comment  -- queue a comment (via DryRunGate)
- POST /api/github/execute  -- execute an approved action

Multi-repo (Idea #13 -- Phase 1 switch-mode):
- GET  /api/github/repos         -- list registered repos + active repo
- POST /api/github/repos/switch  -- switch active repo

Reference: docs/_private/WIP_GIT_REPO_MODE-IDEA.md
"""

import logging
import os

logger = logging.getLogger("handlers.github")

# Lazy import to avoid circular deps -- same pattern as tasks.py
from aircp_daemon import storage, _bot_send

# DryRunGate is initialized lazily alongside the provider
_gate = None
_gate_init_attempted = False

# Provider is initialized lazily on first use
_provider = None
_provider_init_attempted = False

# Multi-repo state (Idea #13 -- Phase 1 switch-mode)
_repos_initialized = False
_active_repo_name = None  # None = use first registered repo


# ---------------------------------------------------------------------------
# Multi-repo registry (Idea #13)
# ---------------------------------------------------------------------------

def _init_repos():
    """Initialize repo registry from GITHUB_REPOS env var.

    Format: "name:owner/repo,name2:owner2/repo2"
    Example: "aircp:hdds-team/aircp,hdds:hdds-team/hdds"

    Backward compat: if GITHUB_REPOS is not set, registers the default
    "hdds-team/aircp" repo (same as Phase 1 behavior).
    """
    global _repos_initialized, _active_repo_name
    if _repos_initialized:
        return
    _repos_initialized = True

    repos_env = os.environ.get("GITHUB_REPOS", "")
    if not repos_env:
        # Backward compat -- register default repo only
        _ensure_repo("aircp", "hdds-team", "https://github.com/hdds-team/aircp")
        _active_repo_name = "aircp"
        logger.info("Multi-repo: backward compat mode (single repo: aircp)")
        return

    first_name = None
    for entry in repos_env.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            logger.warning(
                "Multi-repo: skipping malformed entry '%s' "
                "(expected name:owner/repo)", entry
            )
            continue
        name, owner_repo = entry.split(":", 1)
        name = name.strip()
        owner_repo = owner_repo.strip()
        if "/" not in owner_repo:
            logger.warning(
                "Multi-repo: skipping '%s' -- owner/repo must contain '/'",
                entry
            )
            continue
        owner = owner_repo.split("/")[0]
        html_url = f"https://github.com/{owner_repo}"
        _ensure_repo(name, owner, html_url)
        if first_name is None:
            first_name = name

    _active_repo_name = first_name
    count = len(storage.get_all_git_repos())
    logger.info(
        "Multi-repo: registered %d repos, active=%s", count, _active_repo_name
    )


def _ensure_repo(name: str, owner: str, html_url: str = "") -> int | None:
    """Ensure a repo is registered in storage. Returns repo_id or None."""
    repo = storage.get_git_repo(name=name)
    if repo:
        return repo["id"]
    repo_id = storage.add_git_repo(
        name=name,
        owner=owner,
        source="github",
        api_url="https://api.github.com",
        html_url=html_url,
        default_branch="main",
    )
    return repo_id if repo_id and repo_id > 0 else None


def _get_repo(name: str = None):
    """Get repo info by name, or use the active repo.

    Returns (repo_id, "owner/repo") or (None, None).
    """
    _init_repos()
    target = name or _active_repo_name
    if not target:
        return None, None
    repo = storage.get_git_repo(name=target)
    if not repo:
        return None, None
    owner_repo = f"{repo['owner']}/{repo['name']}"
    return repo["id"], owner_repo


def _get_active_repo_name() -> str | None:
    """Return the currently active repo name."""
    _init_repos()
    return _active_repo_name


def _set_active_repo(name: str) -> bool:
    """Switch the active repo. Returns True if valid."""
    global _active_repo_name
    _init_repos()
    repo = storage.get_git_repo(name=name)
    if not repo:
        return False
    if not repo.get("enabled", True):
        return False
    _active_repo_name = name
    logger.info("Multi-repo: switched active repo to '%s'", name)
    return True


# ---------------------------------------------------------------------------
# Provider + DryRunGate initialization (unchanged from Phase 1/2)
# ---------------------------------------------------------------------------

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


def _get_gate():
    """Lazily initialize the DryRunGate.

    Requires both a valid provider and a configured repo.
    Returns None if prerequisites are missing.  Note: initialization is
    attempted only once -- if GITHUB_TOKEN is absent at startup, a daemon
    restart is required after setting it.  This is intentional (avoids
    re-checking env on every request).
    """
    global _gate, _gate_init_attempted
    if _gate_init_attempted:
        return _gate
    _gate_init_attempted = True

    provider = _get_provider()
    if provider is None:
        logger.warning("DryRunGate unavailable -- no provider")
        return None

    repo_id, _ = _get_repo()
    if repo_id is None:
        logger.warning("DryRunGate unavailable -- no repo configured")
        return None

    try:
        from dry_run_gate import DryRunGate
        # Phase 2 MVP: always dry_run=True.  @naskel flips to False
        # once the queue has been validated in production.
        live = os.environ.get("GITHUB_LIVE_MODE", "") == "1"
        _gate = DryRunGate(
            provider=provider, storage=storage,
            repo_id=repo_id, dry_run=not live,
        )
        mode = "LIVE" if live else "DRY-RUN"
        logger.info("DryRunGate initialized (%s mode)", mode)
    except Exception as e:
        logger.error(f"Failed to initialize DryRunGate: {e}")
    return _gate


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_github_issues(handler, parsed, params):
    """GET /api/github/issues -- return cached issue list for active/specified repo.

    Query params:
        state: "open" (default), "closed", "all"
        refresh: "1" to force refresh from GitHub API
        repo: repo name to query (default: active repo)
    """
    state = params.get("state", ["open"])[0]
    refresh = params.get("refresh", ["0"])[0] == "1"
    repo_name = params.get("repo", [None])[0]

    repo_id, owner_repo = _get_repo(repo_name)
    if repo_id is None:
        msg = f"Repo '{repo_name}' not found" if repo_name else "No repo configured"
        handler.send_json({"error": msg}, 404 if repo_name else 500)
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
            issues = provider.list_issues(owner_repo, state=state)
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
        "repo": owner_repo,
        "repo_name": repo_name or _active_repo_name,
        "state": state,
    })


def get_github_queue(handler, parsed, params):
    """GET /api/github/queue -- pending actions awaiting approval.

    Query params:
        repo: repo name (default: active repo)
    """
    repo_name = params.get("repo", [None])[0]
    repo_id, owner_repo = _get_repo(repo_name)
    if repo_id is None:
        handler.send_json({"error": "No repo configured"}, 404)
        return

    pending = storage.get_pending_git_actions(repo_id=repo_id)
    handler.send_json({
        "queue": pending,
        "count": len(pending),
        "repo": owner_repo,
    })


def get_github_repos(handler, parsed, params):
    """GET /api/github/repos -- list registered repos + active repo."""
    _init_repos()
    repos = storage.get_all_git_repos(enabled_only=False)
    result = []
    for r in repos:
        result.append({
            "name": r["name"],
            "owner": r["owner"],
            "owner_repo": f"{r['owner']}/{r['name']}",
            "source": r["source"],
            "html_url": r.get("html_url", ""),
            "enabled": bool(r.get("enabled", 1)),
            "active": r["name"] == _active_repo_name,
        })
    handler.send_json({
        "repos": result,
        "active": _active_repo_name,
        "count": len(result),
    })


# ---------------------------------------------------------------------------
# POST handlers -- Multi-repo (Idea #13)
# ---------------------------------------------------------------------------

def post_github_switch_repo(handler, body):
    """POST /api/github/repos/switch -- switch active repo.

    Body:
        name: str (required) -- repo name to switch to
    """
    name = body.get("name", "")
    if not name:
        handler.send_json({"error": "Missing 'name'"}, 400)
        return
    if _set_active_repo(name):
        handler.send_json({"status": "switched", "active": name})
    else:
        handler.send_json({"error": f"Repo '{name}' not found or disabled"}, 404)


# ---------------------------------------------------------------------------
# POST handlers -- Phase 2 write operations
# ---------------------------------------------------------------------------

def post_github_comment(handler, body):
    """POST /api/github/comment -- queue a comment via DryRunGate.

    Body:
        issue_number: int (required)
        body: str (required) -- comment markdown
        actor_id: str (default: "@system")
    """
    try:
        issue_number = body.get("issue_number")
        comment_body = body.get("body", "")
        actor_id = body.get("actor_id", "@system")

        if not issue_number:
            handler.send_json({"error": "Missing 'issue_number'"}, 400)
            return
        try:
            issue_number = int(issue_number)
        except (ValueError, TypeError):
            handler.send_json({"error": "Invalid 'issue_number': must be an integer"}, 400)
            return
        if not comment_body:
            handler.send_json({"error": "Missing 'body'"}, 400)
            return
        if len(comment_body) > 65536:
            handler.send_json({"error": "Comment body too long (max 64KB)"}, 400)
            return

        gate = _get_gate()
        if gate is None:
            handler.send_json(
                {"error": "DryRunGate not available (check GITHUB_TOKEN)"},
                503,
            )
            return

        repo_id, owner_repo = _get_repo()
        if repo_id is None:
            handler.send_json({"error": "No repo configured"}, 500)
            return

        queued = gate.comment(
            repo=owner_repo,
            number=issue_number,
            body=comment_body,
            actor_id=actor_id,
        )

        _bot_send(
            "#general",
            f"\U0001f4dd **GitHub** {actor_id} queued comment on "
            f"#{issue_number} (action #{queued.action_id}, "
            f"{'dry-run' if gate.dry_run else 'pending approval'})",
            from_id="@github",
        )

        handler.send_json(queued.to_dict(), 201)

    except Exception as e:
        logger.error(f"GitHub comment error: {e}")
        handler.send_json({"error": str(e)}, 500)


def post_github_execute(handler, body):
    """POST /api/github/execute -- execute an approved action.

    Body:
        action_id: int (required)
    """
    try:
        action_id = body.get("action_id")
        if not action_id:
            handler.send_json({"error": "Missing 'action_id'"}, 400)
            return

        gate = _get_gate()
        if gate is None:
            handler.send_json(
                {"error": "DryRunGate not available (check GITHUB_TOKEN)"},
                503,
            )
            return

        executed = gate.execute_approved(int(action_id))

        _bot_send(
            "#general",
            f"\u2705 **GitHub** action #{action_id} "
            f"({executed.action_type}) executed successfully",
            from_id="@github",
        )

        handler.send_json(executed.to_dict())

    except Exception as e:
        logger.error(f"GitHub execute error: {e}")
        from git_provider import GitProviderError, NotApprovedError
        if isinstance(e, NotApprovedError):
            status = 409
        elif isinstance(e, GitProviderError) and "not found" in str(e).lower():
            status = 404
        else:
            status = 500
        handler.send_json({"error": str(e)}, status)


# ---------------------------------------------------------------------------
# POST handlers -- Phase 1
# ---------------------------------------------------------------------------

def post_github_assign(handler, body):
    """POST /api/github/assign -- assign agent(s) to an issue.

    Body:
        issue_number: int (required)
        agent_id: str (required) -- agent to assign
        role: str -- "triage", "investigate", "code", "review" (default: "investigate")
        auto_task: bool -- create AIRCP task automatically (default: true)
        repo: str -- repo name (default: active repo)
    """
    try:
        issue_number = body.get("issue_number")
        agent_id = body.get("agent_id")
        role = body.get("role", "investigate")
        repo_name = body.get("repo")

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

        repo_id, owner_repo = _get_repo(repo_name)
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
                          "repo": owner_repo},
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
    "/api/github/repos": get_github_repos,
}

POST_ROUTES = {
    "/api/github/assign": post_github_assign,
    "/api/github/approve": post_github_approve,
    "/api/github/reject": post_github_reject,
    "/api/github/comment": post_github_comment,
    "/api/github/execute": post_github_execute,
    "/api/github/repos/switch": post_github_switch_repo,
}
