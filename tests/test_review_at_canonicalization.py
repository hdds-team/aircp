#!/usr/bin/env python3
"""
Task #159 fix tests : review-response @-prefix canonicalization.

Reproduces the parser misfire pattern observed during the 2026-05-09
session (#6 / #7) :

  1. Reviewer @beta is assigned to review #N.
  2. @beta votes via MCP (`review/approve`); storage path inserts row
     with reviewer="beta" (no @ prefix).
  3. @beta posts a follow-up chat message containing "LGTM" / "approved".
  4. chat_triggers `_detect_implicit_review` runs the auto-detect; it
     calls `add_review_response` with from_id="@beta" (with @ prefix
     because the chat layer carries identities with @).
  5. PRE-FIX BUG: ON CONFLICT(request_id, reviewer) UNIQUE key sees
     ("beta") and ("@beta") as DIFFERENT rows -> upsert turns into
     INSERT, two physical rows for the same agent are stored.
  6. CONSEQUENCE: a code-type review (needs 2 approvals) auto-closes
     at "2/2" with a single physical voter.

The fix lives in two places:

  - aircp_storage.add_review_response : canonicalize reviewer at
    insert time by stripping leading "@". New writes never store the
    "@" prefix.

  - chat_triggers._detect_implicit_review (already_voted check) +
    chat_triggers._check_review_consensus (dedupe by canonical
    reviewer): handle the read side, including legacy DB rows where
    pre-fix duplicates already exist.

Tests cover both the storage-layer canonicalization and the
consensus dedupe.

Usage:
    python3 -m pytest tests/test_review_at_canonicalization.py -v
"""

import os
import sys
import tempfile
import unittest

# Add parent to path so `import aircp_storage` resolves the in-tree module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aircp_storage import AIRCPStorage


class TestReviewerCanonicalization(unittest.TestCase):
    """Storage-layer fix : add_review_response strips '@' prefix."""

    def setUp(self):
        # Use a temp file rather than ":memory:" because some daemon
        # paths use thread-local connections that can't share an
        # in-memory DB cleanly across the test's single thread either.
        # Tempfile is simpler and removed in tearDown.
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.storage = AIRCPStorage(db_path=self._tmp.name)
        # Open a review request once per test; tests reuse this id.
        self.req_id = self.storage.create_review_request(
            file_path="src/foo.cpp",
            requested_by="alpha",
            reviewers=["@beta", "@sonnet"],
            review_type="code",
            timeout_seconds=3600,
        )
        self.assertGreater(self.req_id, 0)

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_mcp_then_chat_does_not_create_duplicate(self):
        """Reproduces parser misfire #6/#7 : MCP vote ('beta') then
        chat-detect vote ('@beta') for the same agent must result in
        ONE row, not two."""
        # MCP path: storage stores the reviewer as 'beta' (no @).
        ok1 = self.storage.add_review_response(
            self.req_id, "beta", "approve",
            "MCP path verdict + 4 nits checked"
        )
        self.assertTrue(ok1)

        # Chat auto-detect path: from_id has '@' prefix.
        # Pre-fix, this inserted a SECOND row keyed by ("@beta") and
        # bypassed the (request_id, reviewer) UNIQUE constraint.
        # Post-fix, storage canonicalizes -> upsert -> single row.
        ok2 = self.storage.add_review_response(
            self.req_id, "@beta", "approve",
            "[auto-detected from chat] LGTM, looks good"
        )
        self.assertTrue(ok2)

        rev = self.storage.get_review_request(self.req_id)
        self.assertIsNotNone(rev)
        responses = rev.get("responses", [])
        # Single physical voter -> single row.
        self.assertEqual(
            len(responses), 1,
            f"expected 1 response after MCP+chat for same agent, "
            f"got {len(responses)} : {responses}"
        )
        # Stored reviewer is canonical (no '@').
        self.assertEqual(responses[0]["reviewer"], "beta")
        # Latest comment wins (DO UPDATE).
        self.assertIn("auto-detected", responses[0].get("comment") or "")

    def test_chat_then_mcp_does_not_create_duplicate(self):
        """Reverse order : chat first ('@beta'), MCP second ('beta').
        Same expectation : one canonical row."""
        ok1 = self.storage.add_review_response(
            self.req_id, "@beta", "approve", "[auto-detected from chat] LGTM"
        )
        ok2 = self.storage.add_review_response(
            self.req_id, "beta", "approve", "MCP override"
        )
        self.assertTrue(ok1 and ok2)
        responses = self.storage.get_review_request(self.req_id).get(
            "responses", []
        )
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["reviewer"], "beta")

    def test_distinct_agents_still_distinct(self):
        """Sanity : two different agents get two rows. The fix must
        not collapse @beta and @sonnet."""
        self.storage.add_review_response(
            self.req_id, "beta", "approve", "QA OK"
        )
        self.storage.add_review_response(
            self.req_id, "@sonnet", "approve", "Algo OK"
        )
        responses = self.storage.get_review_request(self.req_id).get(
            "responses", []
        )
        self.assertEqual(len(responses), 2)
        names = sorted(r["reviewer"] for r in responses)
        self.assertEqual(names, ["beta", "sonnet"])

    def test_changes_then_approve_updates_vote(self):
        """Existing semantic : same reviewer changing vote replaces
        the row (DO UPDATE on ON CONFLICT). The @-canonicalization
        must not break this."""
        self.storage.add_review_response(
            self.req_id, "@beta", "changes", "Bug at L120"
        )
        self.storage.add_review_response(
            self.req_id, "beta", "approve", "Fix verified"
        )
        responses = self.storage.get_review_request(self.req_id).get(
            "responses", []
        )
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["vote"], "approve")
        self.assertEqual(responses[0]["reviewer"], "beta")


class TestConsensusDedupeOnLegacyData(unittest.TestCase):
    """chat_triggers._check_review_consensus dedupes by canonical
    reviewer. Protects against legacy DB rows where the pre-fix bug
    already created duplicate rows for the same agent."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.storage = AIRCPStorage(db_path=self._tmp.name)
        self.req_id = self.storage.create_review_request(
            file_path="src/legacy.cpp",
            requested_by="alpha",
            reviewers=["@beta", "@sonnet"],
            review_type="code",  # needs 2 approvals
            timeout_seconds=3600,
        )

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_consensus_dedupe_handles_legacy_duplicates(self):
        """Simulate a legacy DB state (pre-fix) by inserting two rows
        for the same agent via raw SQL (bypassing the canonicalizing
        add_review_response). The consensus check must still treat
        these as ONE physical voter for the code-type 2/2 gate."""
        # Manually insert two rows for the same agent, bypassing
        # add_review_response canonicalization (simulates legacy data).
        conn = self.storage._get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO review_responses "
            "(request_id, reviewer, vote, comment, responded_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (self.req_id, "beta", "approve", "MCP")
        )
        # Different reviewer key thanks to '@' -- this is the bug we
        # fixed at the canonicalization point, but we want to assert
        # the CONSENSUS check tolerates the historical artifact.
        c.execute(
            "INSERT INTO review_responses "
            "(request_id, reviewer, vote, comment, responded_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (self.req_id, "@beta", "approve", "[chat]")
        )
        conn.commit()

        # Now exercise _check_review_consensus via the chat_triggers
        # module. We need to inject our storage instance so the module
        # global resolves to it.
        import chat_triggers
        chat_triggers.storage = self.storage

        # Sanity : raw responses contain the duplicate.
        rev_raw = self.storage.get_review_request(self.req_id)
        self.assertEqual(len(rev_raw["responses"]), 2)

        # Run consensus. For code-type (needs 2), the dedupe must
        # collapse the two rows to one canonical voter -> NOT close.
        chat_triggers._check_review_consensus(self.req_id)

        rev_after = self.storage.get_review_request(self.req_id)
        # Pre-fix bug behaviour: would have closed at 2/2 with 1 voter.
        # Post-fix: stays pending because dedupe collapses to 1
        # approval, below the code-type threshold of 2.
        self.assertEqual(
            rev_after.get("status"), "pending",
            "consensus check must not auto-close on legacy "
            "duplicate-row data (parser misfire #6/#7 root cause)"
        )


# ============================================================================
# Task #159 PART 2 : read-side leaks discovered by @beta during the
# 2026-05-09 post-restart smoke test on review #80.
#
# The write-path canonicalization (#159 part 1) stores responses sans "@",
# but two read paths still compared raw `reviewers` (with "@") to the
# canonical responses (without "@"), so a reviewer who had voted still
# appeared as "pending" :
#
#   - handlers/reviews.py get_review_by_id  -> summary.reviewers_pending
#   - watchdogs.py review_watchdog          -> non_voters list / ping tags
#
# Fix : strip "@" on both sides of the membership test, mirroring the
# pattern already used by the brainstorm-vote watchdog.
# ============================================================================


class TestReviewersPendingCanonical(unittest.TestCase):
    """handlers/reviews.py get_review_by_id : summary.reviewers_pending
    must canonicalize both sides before comparing, otherwise a voter
    stored as "beta" never matches the assigned "@beta" entry."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.storage = AIRCPStorage(db_path=self._tmp.name)
        self.req_id = self.storage.create_review_request(
            file_path="src/foo.cpp",
            requested_by="alpha",
            reviewers=["@beta", "@sonnet"],
            review_type="code",
            timeout_seconds=3600,
        )

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def _summary_pending(self, review):
        """Reproduce the production logic from handlers/reviews.py
        get_review_by_id (post-fix). Kept inline rather than importing
        the handler to avoid pulling in the HTTP layer for a unit test
        on a set comprehension."""
        responses = review.get("responses", [])
        voted_canonical = {(resp.get("reviewer") or "").lstrip("@")
                           for resp in responses}
        return [r for r in review.get("reviewers", [])
                if (r or "").lstrip("@") not in voted_canonical]

    def test_pending_excludes_voter_after_mcp_vote(self):
        """MCP vote stores reviewer='beta'; the assigned reviewers
        list is ['@beta', '@sonnet']. Pending must collapse to
        just '@sonnet', not still contain '@beta'."""
        self.storage.add_review_response(
            self.req_id, "beta", "approve", "MCP vote"
        )
        rev = self.storage.get_review_request(self.req_id)
        pending = self._summary_pending(rev)
        self.assertEqual(
            pending, ["@sonnet"],
            f"@beta voted but still appears pending: {pending}"
        )

    def test_pending_excludes_voter_after_chat_vote(self):
        """Chat-detect vote (passes from_id='@beta') is canonicalized
        at storage to 'beta'. Pending must still drop '@beta'."""
        self.storage.add_review_response(
            self.req_id, "@beta", "approve", "[chat] LGTM"
        )
        rev = self.storage.get_review_request(self.req_id)
        pending = self._summary_pending(rev)
        self.assertEqual(pending, ["@sonnet"])

    def test_pending_empty_when_all_voted(self):
        """Both reviewers vote -> pending is empty list."""
        self.storage.add_review_response(self.req_id, "beta", "approve", "ok")
        self.storage.add_review_response(self.req_id, "@sonnet", "approve", "ok")
        rev = self.storage.get_review_request(self.req_id)
        pending = self._summary_pending(rev)
        self.assertEqual(pending, [])

    def test_pending_handles_legacy_duplicate_rows(self):
        """Legacy DB with both ('beta') and ('@beta') rows : neither
        should make '@beta' show up as pending."""
        conn = self.storage._get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO review_responses "
            "(request_id, reviewer, vote, comment, responded_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (self.req_id, "beta", "approve", "MCP")
        )
        c.execute(
            "INSERT INTO review_responses "
            "(request_id, reviewer, vote, comment, responded_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (self.req_id, "@beta", "approve", "[chat]")
        )
        conn.commit()
        rev = self.storage.get_review_request(self.req_id)
        pending = self._summary_pending(rev)
        self.assertNotIn("@beta", pending)
        self.assertEqual(pending, ["@sonnet"])

    def test_handler_uses_canonical_logic(self):
        """Smoke check that the production handler module actually
        uses the canonicalizing comparison. Imports the module, votes,
        then invokes the handler with a tiny mock and asserts the
        resulting reviewers_pending."""
        import importlib
        import handlers.reviews as reviews_handler
        # Wire the module's storage global to our temp DB.
        reviews_handler.storage = self.storage

        self.storage.add_review_response(
            self.req_id, "beta", "approve", "MCP"
        )

        captured = {}

        class _MockHandler:
            def send_json(self, data, status=200):
                captured["data"] = data
                captured["status"] = status

        class _MockParsed:
            def __init__(self, req_id):
                self.path = f"/review/status/{req_id}"

        reviews_handler.get_review_by_id(
            _MockHandler(), _MockParsed(self.req_id), {}
        )
        self.assertIn("data", captured, "handler did not call send_json")
        summary = captured["data"].get("summary") or {}
        self.assertEqual(
            summary.get("reviewers_pending"), ["@sonnet"],
            f"production handler still leaks @beta as pending: {summary}"
        )


class TestWatchdogNonVotersCanonical(unittest.TestCase):
    """watchdogs.py review_watchdog : non_voters must canonicalize
    both the assigned reviewers list and the voted set before doing
    the difference, otherwise reviewers who already voted via MCP
    keep getting pinged."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.storage = AIRCPStorage(db_path=self._tmp.name)
        self.req_id = self.storage.create_review_request(
            file_path="src/bar.cpp",
            requested_by="alpha",
            reviewers=["@beta", "@sonnet"],
            review_type="code",
            timeout_seconds=3600,
        )

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def _watchdog_non_voters(self, reviewers, responses):
        """Mirror the production logic from watchdogs.py review_watchdog
        (post-fix). Same inline rationale as above : we test the set
        comparison directly to avoid pulling in the full ping loop."""
        voted_reviewers = {(r.get("reviewer") or "").lstrip("@")
                           for r in responses}
        return [r for r in reviewers
                if (r or "").lstrip("@") not in voted_reviewers]

    def test_non_voters_excludes_mcp_voter(self):
        """@beta votes via MCP (stored as 'beta'). Watchdog must NOT
        re-ping @beta."""
        self.storage.add_review_response(
            self.req_id, "beta", "approve", "MCP"
        )
        rev = self.storage.get_review_request(self.req_id)
        non_voters = self._watchdog_non_voters(
            ["@beta", "@sonnet"], rev.get("responses", [])
        )
        self.assertEqual(non_voters, ["@sonnet"])

    def test_non_voters_excludes_chat_voter(self):
        """Chat-detect vote (canonicalizes to 'beta') -> still drop
        '@beta' from the ping list."""
        self.storage.add_review_response(
            self.req_id, "@beta", "approve", "[chat]"
        )
        rev = self.storage.get_review_request(self.req_id)
        non_voters = self._watchdog_non_voters(
            ["@beta", "@sonnet"], rev.get("responses", [])
        )
        self.assertEqual(non_voters, ["@sonnet"])

    def test_non_voters_empty_when_all_voted(self):
        self.storage.add_review_response(self.req_id, "beta", "approve", "ok")
        self.storage.add_review_response(self.req_id, "@sonnet", "approve", "ok")
        rev = self.storage.get_review_request(self.req_id)
        non_voters = self._watchdog_non_voters(
            ["@beta", "@sonnet"], rev.get("responses", [])
        )
        self.assertEqual(non_voters, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
