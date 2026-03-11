"""Git hosting issue provider -- Phase 1 (read-only MVP).

Provider abstraction for Git hosting platforms (GitHub, Gitea, etc.).
Phase 1: GitHubProvider with list_issues() + get_issue() only.
Phase 2: Write operations (comment, label, create_pr) via DryRunGate.

Architecture decisions (Brainstorm #6, IDEA #5):
- Direct REST API via httpx -- no gh CLI subprocess
- Sync client (daemon uses ThreadingHTTPServer, not asyncio)
- Provider interface via typing.Protocol (Gitea plugs in later)
- Fine-grained PAT authentication (repo-scoped)

Reference: docs/_private/WIP_GIT_REPO_MODE-IDEA.md
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import httpx

__version__ = "0.1.0"  # Phase 1 read-only MVP

logger = logging.getLogger("git_provider")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Label:
    """Issue label with color metadata."""
    name: str
    color: str = ""
    description: str = ""


@dataclass(frozen=True)
class User:
    """Minimal user identity from the hosting platform."""
    login: str
    avatar_url: str = ""


@dataclass(frozen=True)
class Comment:
    """Single comment on an issue or PR."""
    id: int
    body: str
    user: User
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Issue:
    """Normalized issue representation (GitHub + Gitea compatible)."""
    number: int
    title: str
    body: str = ""
    state: str = "open"
    labels: list[Label] = field(default_factory=list)
    assignees: list[User] = field(default_factory=list)
    user: User | None = None
    comments: list[Comment] = field(default_factory=list)
    comments_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    html_url: str = ""

    @property
    def label_names(self) -> list[str]:
        """Convenience: list of label name strings."""
        return [l.name for l in self.labels]


@dataclass(frozen=True)
class PullRequest:
    """Normalized pull request representation."""
    number: int
    title: str
    body: str = ""
    html_url: str = ""
    state: str = "open"
    head_ref: str = ""
    base_ref: str = ""
    draft: bool = False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class GitProviderError(Exception):
    """Base exception for all git provider errors."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class AuthError(GitProviderError):
    """Authentication failed (401) or forbidden (403)."""
    pass


class NotFoundError(GitProviderError):
    """Resource not found (404)."""
    pass


class RateLimitError(GitProviderError):
    """GitHub API rate limit exceeded (429 or 403 with X-RateLimit-Remaining: 0)."""

    def __init__(self, message: str, reset_at: float = 0):
        super().__init__(message, status_code=429)
        self.reset_at = reset_at


class ValidationError(GitProviderError):
    """Unprocessable entity (422) -- e.g. PR merge conflict, invalid label."""
    pass


class NotApprovedError(GitProviderError):
    """Write operation attempted without prior dashboard approval."""
    pass


# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class IssueProvider(Protocol):
    """Abstract interface for Git hosting issue providers.

    Phase 1: list_issues(), get_issue() -- read-only.
    Phase 2: comment(), add_label(), create_pr() -- write ops via DryRunGate.

    Both GitHubProvider and (future) GiteaProvider must satisfy this Protocol.
    """

    def list_issues(
        self,
        repo: str,
        labels: list[str] | None = None,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
    ) -> list[Issue]: ...

    def get_issue(
        self,
        repo: str,
        number: int,
        include_comments: bool = False,
    ) -> Issue: ...

    def comment(
        self,
        repo: str,
        number: int,
        body: str,
    ) -> Comment: ...

    def add_label(
        self,
        repo: str,
        number: int,
        labels: list[str],
    ) -> None: ...

    def create_pr(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> PullRequest: ...


# ---------------------------------------------------------------------------
# GitHub Provider
# ---------------------------------------------------------------------------

_GITHUB_API = "https://api.github.com"
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0   # seconds
_BACKOFF_FACTOR = 2.0
_MAX_RATE_WAIT = 300   # max 5 min sleep on rate limit
_DEFAULT_TIMEOUT = 15.0


class GitHubProvider:
    """GitHub REST API v3 provider using httpx sync client.

    Auth: Fine-grained Personal Access Token (Bearer).
    Retry: Exponential backoff on 5xx + rate limit (429).
    Rate limit: Parses X-RateLimit-* headers, sleeps until reset.

    Phase 1: Read-only (list_issues, get_issue).
    Phase 2: Write ops unlocked behind DryRunGate.
    """

    def __init__(
        self,
        token: str,
        base_url: str = _GITHUB_API,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        if not token:
            raise ValueError("GitHub token is required (fine-grained PAT)")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "aircp-git-provider/0.1",
            },
            timeout=timeout,
            follow_redirects=True,
        )
        # Rate limit state
        self._rate_remaining: int | None = None
        self._rate_reset: float = 0

    def close(self):
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # -- Internal: rate limit tracking ------------------------------------

    def _update_rate_limit(self, response: httpx.Response):
        """Parse X-RateLimit-* headers from GitHub response."""
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_ts = response.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            try:
                self._rate_remaining = int(remaining)
            except ValueError:
                pass
        if reset_ts is not None:
            try:
                self._rate_reset = float(reset_ts)
            except ValueError:
                pass

    def _sleep_until_rate_reset(self) -> bool:
        """Sleep until rate limit resets. Returns True if sleep happened."""
        if self._rate_reset > 0:
            wait = self._rate_reset - time.time()
            if 0 < wait <= _MAX_RATE_WAIT:
                logger.warning(
                    "Rate limited, sleeping %.1fs until reset", wait
                )
                time.sleep(wait + 0.5)  # small buffer past reset
                return True
        return False

    # -- Internal: HTTP with retry ----------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Execute HTTP request with retry and exponential backoff.

        Retries on: 5xx, 429 rate limit, network timeouts.
        Raises immediately on: 401, 403 (non-rate-limit), 404, 422.
        """
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._client.request(method, path, **kwargs)
                self._update_rate_limit(resp)

                # -- Success --
                if resp.status_code < 400:
                    return resp

                # -- 401: auth failure, no retry --
                if resp.status_code == 401:
                    raise AuthError(
                        f"Authentication failed: {resp.text[:200]}",
                        status_code=401,
                    )

                # -- 403: rate limit or permission --
                if resp.status_code == 403:
                    if self._rate_remaining == 0:
                        if self._sleep_until_rate_reset():
                            continue
                        raise RateLimitError(
                            "Rate limit exceeded (403)",
                            reset_at=self._rate_reset,
                        )
                    raise AuthError(
                        f"Forbidden: {resp.text[:200]}",
                        status_code=403,
                    )

                # -- 404: not found, no retry --
                if resp.status_code == 404:
                    raise NotFoundError(
                        f"Not found: {method} {path}",
                        status_code=404,
                    )

                # -- 422: validation error, no retry --
                if resp.status_code == 422:
                    raise ValidationError(
                        f"Validation failed: {resp.text[:300]}",
                        status_code=422,
                    )

                # -- 429: explicit rate limit --
                if resp.status_code == 429:
                    if self._sleep_until_rate_reset():
                        continue
                    backoff = _BACKOFF_BASE * (_BACKOFF_FACTOR ** attempt)
                    logger.warning("Rate limited (429), backoff %.1fs", backoff)
                    time.sleep(backoff)
                    last_exc = RateLimitError(
                        "Rate limit exceeded (429)",
                        reset_at=self._rate_reset,
                    )
                    continue

                # -- 5xx: server error, retry --
                if resp.status_code >= 500:
                    backoff = _BACKOFF_BASE * (_BACKOFF_FACTOR ** attempt)
                    logger.warning(
                        "Server error %d on %s %s, retry %d/%d in %.1fs",
                        resp.status_code, method, path,
                        attempt + 1, _MAX_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                    last_exc = GitProviderError(
                        f"Server error: {resp.status_code}",
                        status_code=resp.status_code,
                    )
                    continue

                # -- Other 4xx: no retry --
                raise GitProviderError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                backoff = _BACKOFF_BASE * (_BACKOFF_FACTOR ** attempt)
                logger.warning(
                    "Network error on %s %s: %s, retry %d/%d in %.1fs",
                    method, path, exc, attempt + 1, _MAX_RETRIES, backoff,
                )
                time.sleep(backoff)
                last_exc = GitProviderError(f"Network error: {exc}")

            except (AuthError, NotFoundError, ValidationError):
                raise  # Never retry client errors

            except httpx.HTTPError as exc:
                backoff = _BACKOFF_BASE * (_BACKOFF_FACTOR ** attempt)
                logger.warning(
                    "HTTP error on %s %s: %s, retry %d/%d in %.1fs",
                    method, path, exc, attempt + 1, _MAX_RETRIES, backoff,
                )
                time.sleep(backoff)
                last_exc = GitProviderError(f"HTTP error: {exc}")

        # All retries exhausted
        raise last_exc or GitProviderError(
            f"Request failed after {_MAX_RETRIES} retries"
        )

    # -- Parsing helpers ---------------------------------------------------

    @staticmethod
    def _parse_user(data: dict | None) -> User:
        if not data:
            return User(login="unknown")
        return User(
            login=data.get("login", "unknown"),
            avatar_url=data.get("avatar_url", ""),
        )

    @staticmethod
    def _parse_label(data: dict) -> Label:
        return Label(
            name=data.get("name", ""),
            color=data.get("color", ""),
            description=data.get("description", ""),
        )

    @classmethod
    def _parse_comment(cls, data: dict) -> Comment:
        return Comment(
            id=data.get("id", 0),
            body=data.get("body", ""),
            user=cls._parse_user(data.get("user")),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )

    @classmethod
    def _parse_issue(cls, data: dict, comments: list[Comment] | None = None) -> Issue:
        return Issue(
            number=data.get("number", 0),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            state=data.get("state", "open"),
            labels=[cls._parse_label(lb) for lb in data.get("labels", [])],
            assignees=[cls._parse_user(a) for a in data.get("assignees", [])],
            user=cls._parse_user(data.get("user")),
            comments=comments or [],
            comments_count=data.get("comments", 0),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            html_url=data.get("html_url", ""),
        )

    @classmethod
    def _parse_pull_request(cls, data: dict) -> PullRequest:
        head = data.get("head", {})
        base = data.get("base", {})
        return PullRequest(
            number=data.get("number", 0),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            html_url=data.get("html_url", ""),
            state=data.get("state", "open"),
            head_ref=head.get("ref", "") if isinstance(head, dict) else "",
            base_ref=base.get("ref", "") if isinstance(base, dict) else "",
            draft=data.get("draft", False),
        )

    # -- IssueProvider implementation (Phase 1: read-only) -----------------

    def list_issues(
        self,
        repo: str,
        labels: list[str] | None = None,
        state: str = "open",
        page: int = 1,
        per_page: int = 30,
    ) -> list[Issue]:
        """List issues for a repository.

        Args:
            repo: Owner/repo format (e.g. "hdds-team/aircp").
            labels: Filter by label names (AND logic on GitHub).
            state: "open", "closed", or "all".
            page: Page number (1-indexed).
            per_page: Results per page (max 100).

        Returns:
            List of Issue objects (excludes PRs).
        """
        params: dict = {
            "state": state,
            "page": page,
            "per_page": min(per_page, 100),
            "sort": "updated",
            "direction": "desc",
        }
        if labels:
            params["labels"] = ",".join(labels)

        resp = self._request("GET", f"/repos/{repo}/issues", params=params)
        data = resp.json()

        # GitHub issues endpoint includes PRs -- filter them out
        issues = []
        for item in data:
            if "pull_request" not in item:
                issues.append(self._parse_issue(item))
        return issues

    def get_issue(
        self,
        repo: str,
        number: int,
        include_comments: bool = False,
    ) -> Issue:
        """Get a single issue with optional comments.

        Args:
            repo: Owner/repo format.
            number: Issue number.
            include_comments: If True, fetches comments (extra API call).
        """
        resp = self._request("GET", f"/repos/{repo}/issues/{number}")
        data = resp.json()

        comments: list[Comment] = []
        if include_comments and data.get("comments", 0) > 0:
            comments_resp = self._request(
                "GET",
                f"/repos/{repo}/issues/{number}/comments",
                params={"per_page": 100},
            )
            comments = [
                self._parse_comment(c) for c in comments_resp.json()
            ]

        return self._parse_issue(data, comments=comments)

    # -- Phase 2 stubs (write operations) ----------------------------------

    def comment(self, repo: str, number: int, body: str) -> Comment:
        """Post a comment on an issue. Phase 2 -- use DryRunGate."""
        resp = self._request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        return self._parse_comment(resp.json())

    def add_label(self, repo: str, number: int, labels: list[str]) -> None:
        """Add labels to an issue. Phase 2 -- use DryRunGate."""
        self._request(
            "POST",
            f"/repos/{repo}/issues/{number}/labels",
            json={"labels": labels},
        )

    def create_pr(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> PullRequest:
        """Create a pull request. Phase 2 -- use DryRunGate."""
        resp = self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={
                "head": head,
                "base": base,
                "title": title,
                "body": body,
                "draft": draft,
            },
        )
        return self._parse_pull_request(resp.json())


# ---------------------------------------------------------------------------
# DryRunGate -- safety layer for all write operations
# ---------------------------------------------------------------------------

class DryRunGate:
    """All write operations go through this gate.

    dry_run=True (default): logs the action, returns a preview dict.
    dry_run=False (live):   checks dashboard approval, then executes.

    Every action is logged to the audit trail regardless of mode.
    The dashboard can query action_log to show "would have done" previews.
    """

    def __init__(
        self,
        provider: IssueProvider,
        dry_run: bool = True,
        approval_checker=None,
    ):
        """
        Args:
            provider: The IssueProvider to delegate write calls to.
            dry_run: If True, never actually calls write methods.
            approval_checker: Optional callable(action: str, params: dict) -> bool.
                Returns True if the action was approved in the dashboard.
        """
        self.provider = provider
        self.dry_run = dry_run
        self._approval_checker = approval_checker
        self.action_log: list[dict] = []

    def execute(self, action: str, **kwargs) -> dict:
        """Execute a write action through the safety gate.

        Args:
            action: Method name on the provider (e.g. "comment").
            **kwargs: Arguments passed to the provider method.

        Returns:
            Dict with keys: status, action, params, and optionally result.

        Raises:
            NotApprovedError: Live mode and action not approved.
            GitProviderError: Underlying provider error.
        """
        entry = {
            "action": action,
            "params": kwargs,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dry_run": self.dry_run,
        }
        self.action_log.append(entry)

        if self.dry_run:
            logger.info("[DRY-RUN] Would execute: %s(%s)", action, kwargs)
            return {
                "status": "dry_run",
                "would_execute": action,
                **kwargs,
            }

        # Live mode -- approval required
        if self._approval_checker and not self._approval_checker(action, kwargs):
            raise NotApprovedError(
                f"Action '{action}' not approved in dashboard"
            )

        method = getattr(self.provider, action, None)
        if method is None:
            raise GitProviderError(f"Unknown provider action: {action}")

        result = method(**kwargs)
        entry["result"] = repr(result)[:500]
        return {
            "status": "executed",
            "action": action,
            "result": result,
        }

    def get_pending_actions(self) -> list[dict]:
        """Return all dry-run actions (for dashboard queue display)."""
        return [e for e in self.action_log if e.get("dry_run")]
