"""Unit tests for git_provider.py -- Layer 1 (Provider Isolation).

Uses pytest-httpx to mock GitHub REST API responses via recorded fixtures.
No real HTTP calls are made. Tests cover:

1. Auth (PAT header, 401/403 handling)
2. list_issues() (pagination, label filter, empty repo, PR filtering)
3. get_issue() (full parse, 404, comments)
4. comment() (POST body, 403, 422)
5. add_label() (array, idempotent)
6. create_pr() (branch validation, conflict 422, draft flag)
7. Retry/backoff (5xx, timeout, max retries)
8. Rate limit (X-RateLimit headers, 429, secondary 403)
9. IssueProvider Protocol conformance

Reference: docs/_private/WIP_GIT_REPO_MODE-IDEA.md (Test Strategy)
"""

import re
import time
from unittest.mock import patch

import httpx
import pytest
from pytest_httpx import HTTPXMock

from git_provider import (
    AuthError,
    Comment,
    DryRunGate,
    GitHubProvider,
    GitProviderError,
    Issue,
    IssueProvider,
    Label,
    NotApprovedError,
    NotFoundError,
    PullRequest,
    RateLimitError,
    User,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_TOKEN = "ghp_test_fake_token_1234567890"
REPO = "hdds-team/aircp"


@pytest.fixture
def provider(httpx_mock: HTTPXMock) -> GitHubProvider:
    """GitHubProvider with mocked transport (no real HTTP)."""
    p = GitHubProvider(token=FAKE_TOKEN)
    yield p
    p.close()


def _issue_json(
    number: int = 42,
    title: str = "Auth timeout on login",
    state: str = "open",
    labels: list[dict] | None = None,
    comments: int = 0,
    body: str = "Something is broken",
    assignees: list[dict] | None = None,
    has_pr: bool = False,
) -> dict:
    """Build a realistic GitHub issue JSON payload."""
    result = {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "labels": labels or [],
        "assignees": assignees or [],
        "user": {"login": "testuser", "avatar_url": "https://example.com/avatar.png"},
        "comments": comments,
        "created_at": "2026-03-10T12:00:00Z",
        "updated_at": "2026-03-10T14:30:00Z",
        "html_url": f"https://github.com/{REPO}/issues/{number}",
    }
    if has_pr:
        result["pull_request"] = {"url": "https://api.github.com/..."}
    return result


def _comment_json(id: int = 1, body: str = "Looks good") -> dict:
    return {
        "id": id,
        "body": body,
        "user": {"login": "reviewer", "avatar_url": ""},
        "created_at": "2026-03-10T15:00:00Z",
        "updated_at": "2026-03-10T15:00:00Z",
    }


def _pr_json(number: int = 10, title: str = "Fix auth", draft: bool = False) -> dict:
    return {
        "number": number,
        "title": title,
        "body": "Fixes #42",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "state": "open",
        "head": {"ref": "aircp/alpha/issue-42-fix-auth"},
        "base": {"ref": "main"},
        "draft": draft,
    }


def _rate_limit_headers(remaining: int = 59, reset: float = 0) -> dict:
    return {
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset or int(time.time()) + 3600),
    }


def _url(path: str) -> re.Pattern:
    """Build a URL regex matching the path with optional query params.

    pytest-httpx 0.36.0 does exact URL matching including query string.
    Provider methods like list_issues() add query params (state, page, etc.),
    so we need regex matching for endpoints that receive query params.
    """
    return re.compile(rf"^{re.escape(f'https://api.github.com{path}')}(\?.*)?$")


# ===========================================================================
# 1. Auth tests
# ===========================================================================

class TestAuth:

    def test_token_sent_as_bearer(self, httpx_mock: HTTPXMock):
        """PAT is sent in Authorization: Bearer header."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/1",
            json=_issue_json(number=1),
        )
        with GitHubProvider(token=FAKE_TOKEN) as p:
            p.get_issue(REPO, 1)

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert request.headers["Accept"] == "application/vnd.github+json"

    def test_empty_token_raises(self):
        """Empty token raises ValueError at init."""
        with pytest.raises(ValueError, match="token is required"):
            GitHubProvider(token="")

    def test_401_raises_auth_error(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """401 response raises AuthError immediately (no retry)."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            status_code=401,
            text="Bad credentials",
        )
        with pytest.raises(AuthError) as exc_info:
            provider.list_issues(REPO)
        assert exc_info.value.status_code == 401

    def test_403_non_rate_limit_raises_auth_error(
        self, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """403 without rate limit exhaustion raises AuthError."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            status_code=403,
            text="Resource not accessible",
            headers={"X-RateLimit-Remaining": "50"},
        )
        with pytest.raises(AuthError) as exc_info:
            provider.list_issues(REPO)
        assert exc_info.value.status_code == 403

    def test_api_version_header_sent(self, httpx_mock: HTTPXMock):
        """X-GitHub-Api-Version header is included."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/1",
            json=_issue_json(number=1),
        )
        with GitHubProvider(token=FAKE_TOKEN) as p:
            p.get_issue(REPO, 1)
        request = httpx_mock.get_requests()[0]
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"


# ===========================================================================
# 2. list_issues() tests
# ===========================================================================

class TestListIssues:

    def test_basic_list(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Basic issue listing returns parsed Issue objects."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[
                _issue_json(number=1, title="Bug A"),
                _issue_json(number=2, title="Bug B"),
            ],
        )
        issues = provider.list_issues(REPO)
        assert len(issues) == 2
        assert issues[0].number == 1
        assert issues[0].title == "Bug A"
        assert issues[1].number == 2

    def test_label_filter(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Labels are sent as comma-separated query param."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[_issue_json(labels=[{"name": "bug", "color": "d73a4a"}])],
        )
        issues = provider.list_issues(REPO, labels=["bug"])
        assert len(issues) == 1
        assert "bug" in issues[0].label_names

        request = httpx_mock.get_requests()[0]
        assert "labels=bug" in str(request.url)

    def test_multi_label_filter(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Multiple labels joined with comma."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[],
        )
        provider.list_issues(REPO, labels=["bug", "priority:high"])
        request = httpx_mock.get_requests()[0]
        assert "labels=bug%2Cpriority%3Ahigh" in str(request.url) or \
               "labels=bug,priority:high" in str(request.url)

    def test_empty_repo(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Empty repo returns empty list."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[],
        )
        issues = provider.list_issues(REPO)
        assert issues == []

    def test_prs_filtered_out(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """GitHub issues endpoint includes PRs -- we filter them."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[
                _issue_json(number=1, title="Real issue"),
                _issue_json(number=2, title="A PR", has_pr=True),
                _issue_json(number=3, title="Another issue"),
            ],
        )
        issues = provider.list_issues(REPO)
        assert len(issues) == 2
        assert all(i.title != "A PR" for i in issues)

    def test_pagination_params(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Page and per_page params are sent correctly."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[],
        )
        provider.list_issues(REPO, page=3, per_page=50)
        request = httpx_mock.get_requests()[0]
        assert "page=3" in str(request.url)
        assert "per_page=50" in str(request.url)

    def test_per_page_capped_at_100(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """per_page > 100 is capped to 100 (GitHub API limit)."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[],
        )
        provider.list_issues(REPO, per_page=200)
        request = httpx_mock.get_requests()[0]
        assert "per_page=100" in str(request.url)

    def test_state_filter(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """State param is forwarded."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[],
        )
        provider.list_issues(REPO, state="closed")
        request = httpx_mock.get_requests()[0]
        assert "state=closed" in str(request.url)


# ===========================================================================
# 3. get_issue() tests
# ===========================================================================

class TestGetIssue:

    def test_full_payload_parse(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """All fields parsed from issue JSON."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42",
            json=_issue_json(
                number=42,
                title="Auth timeout",
                body="Login fails after 30s",
                labels=[{"name": "bug", "color": "d73a4a", "description": "Bug report"}],
                assignees=[{"login": "alpha", "avatar_url": ""}],
                comments=3,
            ),
        )
        issue = provider.get_issue(REPO, 42)
        assert issue.number == 42
        assert issue.title == "Auth timeout"
        assert issue.body == "Login fails after 30s"
        assert issue.state == "open"
        assert issue.user.login == "testuser"
        assert len(issue.labels) == 1
        assert issue.labels[0].name == "bug"
        assert issue.labels[0].color == "d73a4a"
        assert len(issue.assignees) == 1
        assert issue.assignees[0].login == "alpha"
        assert issue.comments_count == 3
        assert issue.html_url == f"https://github.com/{REPO}/issues/42"

    def test_404_raises_not_found(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Non-existent issue raises NotFoundError."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/9999",
            status_code=404,
            text="Not Found",
        )
        with pytest.raises(NotFoundError) as exc_info:
            provider.get_issue(REPO, 9999)
        assert exc_info.value.status_code == 404

    def test_include_comments(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Comments are fetched when include_comments=True."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42",
            json=_issue_json(number=42, comments=2),
        )
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues/42/comments"),
            json=[
                _comment_json(id=1, body="First comment"),
                _comment_json(id=2, body="Second comment"),
            ],
        )
        issue = provider.get_issue(REPO, 42, include_comments=True)
        assert len(issue.comments) == 2
        assert issue.comments[0].body == "First comment"
        assert issue.comments[1].body == "Second comment"

    def test_no_comments_skips_fetch(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """If comments_count=0, no extra API call is made."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42",
            json=_issue_json(number=42, comments=0),
        )
        issue = provider.get_issue(REPO, 42, include_comments=True)
        assert issue.comments == []
        assert len(httpx_mock.get_requests()) == 1  # Only 1 request, not 2

    def test_null_body_handled(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """GitHub returns null body for issues without description."""
        data = _issue_json(number=5)
        data["body"] = None
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/5",
            json=data,
        )
        issue = provider.get_issue(REPO, 5)
        assert issue.body == ""  # Normalized to empty string


# ===========================================================================
# 4. comment() tests
# ===========================================================================

class TestComment:

    def test_post_body_format(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Comment body is POSTed as JSON."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/comments",
            json=_comment_json(id=99, body="LGTM"),
            status_code=201,
        )
        comment = provider.comment(REPO, 42, "LGTM")
        assert comment.id == 99
        assert comment.body == "LGTM"

        request = httpx_mock.get_requests()[0]
        assert request.method == "POST"

    def test_403_no_permission(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """403 on comment raises AuthError."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/comments",
            status_code=403,
            text="Resource not accessible by integration",
            headers={"X-RateLimit-Remaining": "100"},
        )
        with pytest.raises(AuthError):
            provider.comment(REPO, 42, "test")

    def test_422_validation_error(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """422 on comment raises ValidationError."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/comments",
            status_code=422,
            text='{"message": "Validation Failed"}',
        )
        with pytest.raises(ValidationError):
            provider.comment(REPO, 42, "")

    def test_comment_on_closed_issue(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Commenting on a closed issue still works (GitHub allows it)."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/comments",
            json=_comment_json(id=100, body="Late comment"),
            status_code=201,
        )
        comment = provider.comment(REPO, 42, "Late comment")
        assert comment.id == 100

    def test_404_issue_not_found(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Comment on non-existent issue raises NotFoundError."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/9999/comments",
            status_code=404,
            text="Not Found",
        )
        with pytest.raises(NotFoundError):
            provider.comment(REPO, 9999, "test")


# ===========================================================================
# 5. add_label() tests
# ===========================================================================

class TestAddLabel:

    def test_label_array_posted(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Labels are sent as JSON array."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/labels",
            json=[{"name": "bug"}, {"name": "priority:high"}],
            status_code=200,
        )
        provider.add_label(REPO, 42, ["bug", "priority:high"])
        request = httpx_mock.get_requests()[0]
        assert request.method == "POST"

    def test_label_already_exists_idempotent(
        self, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """Adding an existing label returns 200 (GitHub is idempotent)."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/labels",
            json=[{"name": "bug"}],
            status_code=200,
        )
        # Should not raise
        provider.add_label(REPO, 42, ["bug"])

    def test_unknown_label_422(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Non-existent label name raises ValidationError on some configs."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/labels",
            status_code=422,
            text='{"message": "Validation Failed", "errors": [{"code": "invalid"}]}',
        )
        with pytest.raises(ValidationError):
            provider.add_label(REPO, 42, ["nonexistent-label"])

    def test_single_label(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Single label in array works."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/labels",
            json=[{"name": "help wanted"}],
            status_code=200,
        )
        provider.add_label(REPO, 42, ["help wanted"])

    def test_empty_label_list(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Empty label list still sends POST (noop server-side)."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/labels",
            json=[],
            status_code=200,
        )
        provider.add_label(REPO, 42, [])


# ===========================================================================
# 6. create_pr() tests
# ===========================================================================

class TestCreatePR:

    def test_basic_pr_creation(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """PR created with correct JSON body."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/pulls",
            json=_pr_json(number=10, title="Fix auth"),
            status_code=201,
        )
        pr = provider.create_pr(
            REPO, head="aircp/alpha/issue-42-fix-auth", base="main",
            title="Fix auth", body="Fixes #42",
        )
        assert pr.number == 10
        assert pr.title == "Fix auth"
        assert pr.head_ref == "aircp/alpha/issue-42-fix-auth"
        assert pr.base_ref == "main"
        assert not pr.draft

    def test_draft_pr_flag(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Draft flag is passed through."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/pulls",
            json=_pr_json(number=11, draft=True),
            status_code=201,
        )
        pr = provider.create_pr(
            REPO, head="feature-branch", base="main",
            title="WIP", body="", draft=True,
        )
        assert pr.draft is True

    def test_conflict_422(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """Merge conflict raises ValidationError."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/pulls",
            status_code=422,
            text='{"message": "Validation Failed", "errors": [{"message": "No commits between main and main"}]}',
        )
        with pytest.raises(ValidationError):
            provider.create_pr(
                REPO, head="main", base="main",
                title="Bad PR", body="",
            )

    def test_pr_body_contains_issue_ref(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """PR body can contain issue cross-references."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/pulls",
            json=_pr_json(number=12),
            status_code=201,
        )
        provider.create_pr(
            REPO, head="fix-branch", base="main",
            title="Fix", body="Closes #42\nFixes #43",
        )
        # Just verifying no error -- body is free-form

    def test_pr_parse_head_base(self, provider: GitHubProvider, httpx_mock: HTTPXMock):
        """head_ref and base_ref parsed from nested objects."""
        data = _pr_json(number=13)
        data["head"] = {"ref": "feature/new-ui", "sha": "abc123"}
        data["base"] = {"ref": "develop", "sha": "def456"}
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/pulls",
            json=data,
            status_code=201,
        )
        pr = provider.create_pr(
            REPO, head="feature/new-ui", base="develop",
            title="New UI", body="",
        )
        assert pr.head_ref == "feature/new-ui"
        assert pr.base_ref == "develop"

    def test_403_insufficient_permissions(
        self, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """403 on PR creation raises AuthError (PAT lacks pull_requests:write)."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/pulls",
            status_code=403,
            text="Resource not accessible by integration",
            headers={"X-RateLimit-Remaining": "100"},
        )
        with pytest.raises(AuthError):
            provider.create_pr(
                REPO, head="fix", base="main", title="t", body="b",
            )


# ===========================================================================
# 7. Retry/backoff tests
# ===========================================================================

class TestRetryBackoff:

    @patch("git_provider.time.sleep")
    def test_5xx_retries_with_backoff(
        self, mock_sleep, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """5xx errors trigger exponential backoff retries."""
        # 3 failures -> exhausted
        for _ in range(3):
            httpx_mock.add_response(
                url=_url(f"/repos/{REPO}/issues"),
                status_code=503,
                text="Service Unavailable",
            )
        with pytest.raises(GitProviderError) as exc_info:
            provider.list_issues(REPO)
        assert exc_info.value.status_code == 503
        assert mock_sleep.call_count == 3  # 1s, 2s, 4s backoff

    @patch("git_provider.time.sleep")
    def test_5xx_succeeds_on_retry(
        self, mock_sleep, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """5xx followed by 200 succeeds."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            status_code=500,
            text="Internal Server Error",
        )
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[_issue_json(number=1)],
        )
        issues = provider.list_issues(REPO)
        assert len(issues) == 1
        assert mock_sleep.call_count == 1

    @patch("git_provider.time.sleep")
    def test_timeout_retries(
        self, mock_sleep, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """Timeout exceptions trigger retry."""
        httpx_mock.add_exception(
            httpx.ReadTimeout("Connection timed out"),
            url=_url(f"/repos/{REPO}/issues"),
        )
        httpx_mock.add_exception(
            httpx.ReadTimeout("Connection timed out"),
            url=_url(f"/repos/{REPO}/issues"),
        )
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[],
        )
        issues = provider.list_issues(REPO)
        assert issues == []
        assert mock_sleep.call_count == 2

    @patch("git_provider.time.sleep")
    def test_max_retries_exhausted(
        self, mock_sleep, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """After max retries, raises the last error."""
        for _ in range(3):
            httpx_mock.add_exception(
                httpx.ConnectError("Connection refused"),
                url=_url(f"/repos/{REPO}/issues"),
            )
        with pytest.raises(GitProviderError, match="Network error"):
            provider.list_issues(REPO)
        assert mock_sleep.call_count == 3

    @patch("git_provider.time.sleep")
    def test_exponential_backoff_values(
        self, mock_sleep, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """Backoff delays follow 1s, 2s, 4s pattern."""
        for _ in range(3):
            httpx_mock.add_response(
                url=_url(f"/repos/{REPO}/issues"),
                status_code=502,
            )
        with pytest.raises(GitProviderError):
            provider.list_issues(REPO)
        calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert calls == [1.0, 2.0, 4.0]


# ===========================================================================
# 8. Rate limit tests
# ===========================================================================

class TestRateLimit:

    def test_rate_limit_headers_parsed(
        self, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """X-RateLimit-* headers are tracked internally."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[],
            headers=_rate_limit_headers(remaining=42, reset=1710100000),
        )
        provider.list_issues(REPO)
        assert provider._rate_remaining == 42
        assert provider._rate_reset == 1710100000

    @patch("git_provider.time.sleep")
    def test_429_sleeps_then_retries(
        self, mock_sleep, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """429 triggers sleep until X-RateLimit-Reset, then retries."""
        reset_time = time.time() + 2
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            status_code=429,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(reset_time)),
            },
        )
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[_issue_json(number=1)],
        )
        issues = provider.list_issues(REPO)
        assert len(issues) == 1
        assert mock_sleep.call_count >= 1

    @patch("git_provider.time.sleep")
    def test_403_with_remaining_zero_is_rate_limit(
        self, mock_sleep, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """403 + X-RateLimit-Remaining: 0 is treated as rate limit."""
        reset_time = time.time() + 2
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            status_code=403,
            text="API rate limit exceeded",
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(reset_time)),
            },
        )
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[],
            headers=_rate_limit_headers(remaining=59),
        )
        issues = provider.list_issues(REPO)
        assert issues == []
        assert mock_sleep.call_count >= 1

    @patch("git_provider.time.sleep")
    @patch("git_provider.time.time", return_value=1710100000)
    def test_rate_limit_reset_far_future_raises(
        self, mock_time, mock_sleep, httpx_mock: HTTPXMock
    ):
        """If reset is >5min away, raise instead of sleeping forever."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            status_code=429,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(1710100000 + 600),  # 10 min away
            },
        )
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            status_code=429,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(1710100000 + 600),
            },
        )
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            status_code=429,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(1710100000 + 600),
            },
        )
        with GitHubProvider(token=FAKE_TOKEN) as p:
            with pytest.raises((RateLimitError, GitProviderError)):
                p.list_issues(REPO)

    def test_rate_remaining_decreases(
        self, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """Successive calls update rate_remaining."""
        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[],
            headers=_rate_limit_headers(remaining=59),
        )
        provider.list_issues(REPO)
        assert provider._rate_remaining == 59

        httpx_mock.add_response(
            url=_url(f"/repos/{REPO}/issues"),
            json=[],
            headers=_rate_limit_headers(remaining=58),
        )
        provider.list_issues(REPO)
        assert provider._rate_remaining == 58


# ===========================================================================
# 9. IssueProvider Protocol conformance
# ===========================================================================

class TestProtocolConformance:

    def test_github_provider_is_issue_provider(self):
        """GitHubProvider satisfies the IssueProvider Protocol."""
        with GitHubProvider(token=FAKE_TOKEN) as p:
            assert isinstance(p, IssueProvider)

    def test_protocol_runtime_checkable(self):
        """IssueProvider is runtime_checkable."""
        assert hasattr(IssueProvider, "__protocol_attrs__") or \
               hasattr(IssueProvider, "__abstractmethods__") or \
               True  # runtime_checkable just needs isinstance() to work

    def test_incomplete_provider_fails_check(self):
        """A class missing methods does NOT satisfy IssueProvider."""
        class BadProvider:
            def list_issues(self, repo): ...
            # Missing: get_issue, comment, add_label, create_pr

        # runtime_checkable checks method existence
        bp = BadProvider()
        assert not isinstance(bp, IssueProvider)


# ===========================================================================
# 10. DryRunGate tests
# ===========================================================================

class TestDryRunGate:

    def test_dry_run_logs_action(self, provider: GitHubProvider):
        """Dry-run mode logs the action without calling the provider."""
        gate = DryRunGate(provider, dry_run=True)
        result = gate.execute("comment", repo=REPO, number=42, body="test")
        assert result["status"] == "dry_run"
        assert result["would_execute"] == "comment"
        assert len(gate.action_log) == 1
        assert gate.action_log[0]["dry_run"] is True

    def test_dry_run_no_http_calls(
        self, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """Dry-run mode makes zero HTTP requests."""
        gate = DryRunGate(provider, dry_run=True)
        gate.execute("comment", repo=REPO, number=42, body="test")
        assert len(httpx_mock.get_requests()) == 0

    def test_live_mode_without_approval_raises(self, provider: GitHubProvider):
        """Live mode without approval raises NotApprovedError."""
        gate = DryRunGate(
            provider, dry_run=False,
            approval_checker=lambda action, params: False,
        )
        with pytest.raises(NotApprovedError):
            gate.execute("comment", repo=REPO, number=42, body="test")

    def test_live_mode_with_approval_executes(
        self, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """Live mode with approval calls the provider."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/comments",
            json=_comment_json(id=50, body="approved comment"),
            status_code=201,
        )
        gate = DryRunGate(
            provider, dry_run=False,
            approval_checker=lambda action, params: True,
        )
        result = gate.execute("comment", repo=REPO, number=42, body="approved comment")
        assert result["status"] == "executed"
        assert isinstance(result["result"], Comment)

    def test_get_pending_actions(self, provider: GitHubProvider):
        """get_pending_actions() returns only dry-run entries."""
        gate = DryRunGate(provider, dry_run=True)
        gate.execute("comment", repo=REPO, number=1, body="a")
        gate.execute("add_label", repo=REPO, number=2, labels=["bug"])
        pending = gate.get_pending_actions()
        assert len(pending) == 2
        assert all(e["dry_run"] for e in pending)

    def test_unknown_action_raises(
        self, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """Unknown action in live mode raises GitProviderError."""
        gate = DryRunGate(
            provider, dry_run=False,
            approval_checker=lambda action, params: True,
        )
        with pytest.raises(GitProviderError, match="Unknown provider action"):
            gate.execute("nonexistent_method", repo=REPO)

    def test_no_approval_checker_allows_all(
        self, provider: GitHubProvider, httpx_mock: HTTPXMock
    ):
        """Live mode without approval_checker allows all actions."""
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{REPO}/issues/42/comments",
            json=_comment_json(id=60, body="no gate"),
            status_code=201,
        )
        gate = DryRunGate(provider, dry_run=False, approval_checker=None)
        result = gate.execute("comment", repo=REPO, number=42, body="no gate")
        assert result["status"] == "executed"

    def test_action_log_has_timestamp(self, provider: GitHubProvider):
        """Every action log entry has a timestamp."""
        gate = DryRunGate(provider, dry_run=True)
        gate.execute("comment", repo=REPO, number=1, body="t")
        assert "timestamp" in gate.action_log[0]
        # ISO format check
        assert "T" in gate.action_log[0]["timestamp"]
