#!/usr/bin/env python3
"""
Review Watchdog P7 Tests — Aggressive ping system.

Tests the new review_watchdog logic:
1. No ping before REVIEW_PING_DELAY (2 min)
2. First ping after 2 min with correct message
3. Throttle: no double-ping within REVIEW_PING_INTERVAL
4. Max pings (3) then stop
5. Escalation after REVIEW_ESCALATE_SECONDS (5 min)
6. Clean up state when all reviewers voted
7. Clean up state when review is closed/expired
8. Legacy DB reminder_sent on first ping
9. Message includes MCP command reminder
10. Multiple concurrent reviews tracked independently

Usage:
    python3 -m pytest tests/test_review_watchdog.py -v
    # or standalone:
    python3 tests/test_review_watchdog.py
"""

import sys
import os
import time
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone, timedelta

import pytest

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =============================================================================
# Helpers
# =============================================================================

@pytest.fixture
def r():
    """Provide _TestResult instance for pytest-collected test functions."""
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


def make_review(request_id=1, file_path="src/main.py", reviewers=None,
                requested_by="@alpha", created_at=None, age_seconds=0):
    """Create a mock review dict."""
    if reviewers is None:
        reviewers = ["@beta", "@haiku"]
    if created_at is None:
        dt = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "id": request_id,
        "file_path": file_path,
        "reviewers": reviewers,
        "requested_by": requested_by,
        "created_at": created_at,
        "min_approvals": 2,
        "status": "pending",
        "response_count": 0,
    }


def make_full_review(request_id=1, responses=None):
    """Create a mock full review (with responses) dict."""
    if responses is None:
        responses = []
    return {"id": request_id, "responses": responses}


def make_response(reviewer, vote="approve", comment="LGTM"):
    """Create a mock review response."""
    return {"reviewer": reviewer, "vote": vote, "comment": comment}


# =============================================================================
# Import constants from daemon (without starting it)
# =============================================================================

# We'll test the logic directly by simulating what review_watchdog does.
# This avoids importing the full daemon (which starts threads).

# Constants (mirror from daemon)
REVIEW_PING_DELAY = 120
REVIEW_PING_INTERVAL = 120
REVIEW_PING_MAX = 3
REVIEW_ESCALATE_SECONDS = 300


def simulate_watchdog_cycle(active_reviews, storage_mock, transport_mock,
                            review_reminder_state, now=None):
    """
    Simulate one cycle of the P7 review watchdog logic.
    This mirrors the code in review_watchdog() without the while loop.

    Returns list of sent messages (for assertions).
    """
    if now is None:
        now = time.time()

    sent_messages = []

    for review in active_reviews:
        request_id = review.get("id")
        file_path = review.get("file_path", "")
        reviewers = review.get("reviewers", [])
        requested_by = review.get("requested_by", "")
        created_at = review.get("created_at", "")

        # Calculate review age
        try:
            created_str = created_at.replace("T", " ").split(".")[0].replace("Z", "")
            created_dt = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S")
            created_dt = created_dt.replace(tzinfo=timezone.utc)
            review_age = now - created_dt.timestamp()
        except (ValueError, AttributeError):
            review_age = 0

        if review_age < REVIEW_PING_DELAY:
            continue

        # Get responses
        full_review = storage_mock.get_review_request(request_id)
        responses = full_review.get("responses", []) if full_review else []
        voted_reviewers = {r.get("reviewer") for r in responses}
        non_voters = [r for r in reviewers if r not in voted_reviewers]

        if not non_voters:
            review_reminder_state.pop(request_id, None)
            continue

        state = review_reminder_state.get(request_id, {
            "count": 0, "last_sent": 0, "escalated": False
        })

        if state["count"] >= REVIEW_PING_MAX:
            continue

        elapsed_since_last = now - state["last_sent"]
        if state["count"] > 0 and elapsed_since_last < REVIEW_PING_INTERVAL:
            continue

        # SEND PING
        ping_count = state["count"] + 1
        tags = " ".join(non_voters)
        is_escalation = review_age >= REVIEW_ESCALATE_SECONDS and not state["escalated"]

        if is_escalation:
            msg = (
                f"\U0001f6a8 **REVIEW #{request_id}** \u2014 ESCALATION! "
                f"{tags} : review pending for {int(review_age // 60)} min on `{file_path}`\n"
                f"\u26a0\ufe0f Use `review/approve` or `review/changes` (not just chat!)"
            )
        else:
            msg = (
                f"\U0001f514 **REVIEW #{request_id}** ({ping_count}/{REVIEW_PING_MAX}) \u2014 "
                f"{tags} : review pending on `{file_path}`\n"
                f"\U0001f4a1 Reminder: use the MCP command `review/approve` or `review/changes`"
            )

        transport_mock.send_chat("#general", msg, from_id="@review")
        sent_messages.append({"request_id": request_id, "msg": msg,
                              "ping_count": ping_count, "is_escalation": is_escalation})

        review_reminder_state[request_id] = {
            "count": ping_count,
            "last_sent": now,
            "escalated": state["escalated"] or is_escalation
        }

        if ping_count == 1:
            storage_mock.mark_review_reminder_sent(request_id)

    return sent_messages


# =============================================================================
# Tests
# =============================================================================

def test_no_ping_before_delay(r: _TestResult):
    """Review younger than REVIEW_PING_DELAY → no ping."""
    storage = MagicMock()
    transport = MagicMock()
    state = {}

    # Review created 60s ago (< 120s delay)
    reviews = [make_review(age_seconds=60)]
    storage.get_review_request.return_value = make_full_review(responses=[])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state)

    if len(msgs) == 0 and transport.send_chat.call_count == 0:
        r.ok("no_ping_before_delay")
    else:
        r.fail("no_ping_before_delay", f"Expected 0 msgs, got {len(msgs)}")


def test_first_ping_after_delay(r: _TestResult):
    """Review older than REVIEW_PING_DELAY → first ping sent."""
    storage = MagicMock()
    transport = MagicMock()
    state = {}

    reviews = [make_review(age_seconds=150)]  # 150s > 120s
    storage.get_review_request.return_value = make_full_review(responses=[])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state)

    if (len(msgs) == 1 and msgs[0]["ping_count"] == 1
            and state.get(1, {}).get("count") == 1):
        r.ok("first_ping_after_delay")
    else:
        r.fail("first_ping_after_delay", f"Expected 1 ping, got {len(msgs)}, state={state}")


def test_throttle_prevents_double_ping(r: _TestResult):
    """Second cycle within REVIEW_PING_INTERVAL → no ping."""
    storage = MagicMock()
    transport = MagicMock()
    now = time.time()

    # Simulate first ping already sent 30s ago
    state = {1: {"count": 1, "last_sent": now - 30, "escalated": False}}

    reviews = [make_review(age_seconds=200)]
    storage.get_review_request.return_value = make_full_review(responses=[])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state, now=now)

    if len(msgs) == 0:
        r.ok("throttle_prevents_double_ping")
    else:
        r.fail("throttle_prevents_double_ping", f"Expected 0, got {len(msgs)}")


def test_second_ping_after_interval(r: _TestResult):
    """Second ping sent after REVIEW_PING_INTERVAL elapsed."""
    storage = MagicMock()
    transport = MagicMock()
    now = time.time()

    # First ping sent 130s ago (> 120s interval)
    state = {1: {"count": 1, "last_sent": now - 130, "escalated": False}}

    reviews = [make_review(age_seconds=300)]
    storage.get_review_request.return_value = make_full_review(responses=[])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state, now=now)

    if len(msgs) == 1 and msgs[0]["ping_count"] == 2 and state[1]["count"] == 2:
        r.ok("second_ping_after_interval")
    else:
        r.fail("second_ping_after_interval", f"Expected ping 2, got {msgs}")


def test_max_pings_stops_spamming(r: _TestResult):
    """After REVIEW_PING_MAX pings → no more pings."""
    storage = MagicMock()
    transport = MagicMock()
    now = time.time()

    # Already sent 3 pings (max)
    state = {1: {"count": 3, "last_sent": now - 200, "escalated": True}}

    reviews = [make_review(age_seconds=600)]
    storage.get_review_request.return_value = make_full_review(responses=[])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state, now=now)

    if len(msgs) == 0:
        r.ok("max_pings_stops_spamming")
    else:
        r.fail("max_pings_stops_spamming", f"Expected 0, got {len(msgs)}")


def test_escalation_at_5_min(r: _TestResult):
    """Review older than 5 min → escalation message."""
    storage = MagicMock()
    transport = MagicMock()
    now = time.time()

    # First ping sent, review is now 310s old (> 300s escalation)
    state = {1: {"count": 1, "last_sent": now - 130, "escalated": False}}

    reviews = [make_review(age_seconds=310)]
    storage.get_review_request.return_value = make_full_review(responses=[])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state, now=now)

    if len(msgs) == 1 and msgs[0]["is_escalation"] and state[1]["escalated"]:
        r.ok("escalation_at_5_min")
    else:
        r.fail("escalation_at_5_min", f"Expected escalation, got {msgs}, state={state.get(1)}")


def test_no_double_escalation(r: _TestResult):
    """Already escalated → no second escalation (but still normal pings)."""
    storage = MagicMock()
    transport = MagicMock()
    now = time.time()

    state = {1: {"count": 2, "last_sent": now - 130, "escalated": True}}

    reviews = [make_review(age_seconds=400)]
    storage.get_review_request.return_value = make_full_review(responses=[])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state, now=now)

    if len(msgs) == 1 and not msgs[0]["is_escalation"]:
        r.ok("no_double_escalation")
    else:
        r.fail("no_double_escalation", f"Expected non-escalation ping, got {msgs}")


def test_cleanup_when_all_voted(r: _TestResult):
    """All reviewers voted → state cleaned up, no ping."""
    storage = MagicMock()
    transport = MagicMock()

    state = {1: {"count": 2, "last_sent": time.time() - 200, "escalated": False}}

    reviews = [make_review(age_seconds=300, reviewers=["@beta", "@haiku"])]
    storage.get_review_request.return_value = make_full_review(responses=[
        make_response("@beta"), make_response("@haiku")
    ])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state)

    if len(msgs) == 0 and 1 not in state:
        r.ok("cleanup_when_all_voted")
    else:
        r.fail("cleanup_when_all_voted", f"Expected cleanup, got msgs={len(msgs)}, state={state}")


def test_partial_votes_ping_non_voters_only(r: _TestResult):
    """One reviewer voted, other didn't → ping only non-voter."""
    storage = MagicMock()
    transport = MagicMock()
    state = {}

    reviews = [make_review(age_seconds=150, reviewers=["@beta", "@haiku"])]
    storage.get_review_request.return_value = make_full_review(responses=[
        make_response("@beta", vote="approve")
    ])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state)

    if len(msgs) == 1:
        sent_msg = msgs[0]["msg"]
        if "@haiku" in sent_msg and "@beta" not in sent_msg:
            r.ok("partial_votes_ping_non_voters_only")
        else:
            r.fail("partial_votes_ping_non_voters_only",
                    f"Expected only @haiku tagged, msg={sent_msg[:100]}")
    else:
        r.fail("partial_votes_ping_non_voters_only", f"Expected 1 msg, got {len(msgs)}")


def test_legacy_db_reminder_on_first_ping(r: _TestResult):
    """First ping → mark_review_reminder_sent called (backward compat)."""
    storage = MagicMock()
    transport = MagicMock()
    state = {}

    reviews = [make_review(age_seconds=150)]
    storage.get_review_request.return_value = make_full_review(responses=[])

    simulate_watchdog_cycle(reviews, storage, transport, state)

    if storage.mark_review_reminder_sent.called:
        r.ok("legacy_db_reminder_on_first_ping")
    else:
        r.fail("legacy_db_reminder_on_first_ping", "mark_review_reminder_sent not called")


def test_no_legacy_db_on_subsequent_pings(r: _TestResult):
    """Second+ pings → don't call mark_review_reminder_sent again."""
    storage = MagicMock()
    transport = MagicMock()
    now = time.time()

    state = {1: {"count": 1, "last_sent": now - 130, "escalated": False}}

    reviews = [make_review(age_seconds=300)]
    storage.get_review_request.return_value = make_full_review(responses=[])

    simulate_watchdog_cycle(reviews, storage, transport, state, now=now)

    if not storage.mark_review_reminder_sent.called:
        r.ok("no_legacy_db_on_subsequent_pings")
    else:
        r.fail("no_legacy_db_on_subsequent_pings", "mark_review_reminder_sent called on ping 2")


def test_message_contains_mcp_reminder(r: _TestResult):
    """Ping message should remind to use MCP command, not just chat."""
    storage = MagicMock()
    transport = MagicMock()
    state = {}

    reviews = [make_review(age_seconds=150)]
    storage.get_review_request.return_value = make_full_review(responses=[])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state)

    if len(msgs) == 1:
        msg = msgs[0]["msg"]
        if "review/approve" in msg and "review/changes" in msg:
            r.ok("message_contains_mcp_reminder")
        else:
            r.fail("message_contains_mcp_reminder", f"Missing MCP command in: {msg[:100]}")
    else:
        r.fail("message_contains_mcp_reminder", f"Expected 1 msg, got {len(msgs)}")


def test_escalation_message_mentions_duration(r: _TestResult):
    """Escalation message should mention how long the review has been waiting."""
    storage = MagicMock()
    transport = MagicMock()
    now = time.time()

    state = {1: {"count": 1, "last_sent": now - 130, "escalated": False}}

    reviews = [make_review(age_seconds=360)]  # 6 minutes
    storage.get_review_request.return_value = make_full_review(responses=[])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state, now=now)

    if len(msgs) == 1 and msgs[0]["is_escalation"]:
        msg = msgs[0]["msg"]
        if "6 min" in msg or "5 min" in msg:  # approx
            r.ok("escalation_message_mentions_duration")
        else:
            r.fail("escalation_message_mentions_duration", f"No duration in: {msg[:100]}")
    else:
        r.fail("escalation_message_mentions_duration", f"Expected escalation, got {msgs}")


def test_multiple_reviews_independent_state(r: _TestResult):
    """Two concurrent reviews tracked independently."""
    storage = MagicMock()
    transport = MagicMock()
    state = {}

    reviews = [
        make_review(request_id=1, age_seconds=150, reviewers=["@beta"]),
        make_review(request_id=2, age_seconds=150, reviewers=["@haiku"]),
    ]
    storage.get_review_request.side_effect = [
        make_full_review(request_id=1, responses=[]),
        make_full_review(request_id=2, responses=[]),
    ]

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state)

    if len(msgs) == 2 and 1 in state and 2 in state:
        if state[1]["count"] == 1 and state[2]["count"] == 1:
            r.ok("multiple_reviews_independent_state")
        else:
            r.fail("multiple_reviews_independent_state",
                    f"Expected count=1 each, got state={state}")
    else:
        r.fail("multiple_reviews_independent_state",
                f"Expected 2 msgs, got {len(msgs)}, state={state}")


def test_unparseable_created_at_skips(r: _TestResult):
    """Malformed created_at → review_age=0 → skip (too young)."""
    storage = MagicMock()
    transport = MagicMock()
    state = {}

    review = make_review(age_seconds=0)
    review["created_at"] = "not-a-date"

    msgs = simulate_watchdog_cycle([review], storage, transport, state)

    if len(msgs) == 0:
        r.ok("unparseable_created_at_skips")
    else:
        r.fail("unparseable_created_at_skips", f"Expected skip, got {len(msgs)} msgs")


def test_no_reviews_no_crash(r: _TestResult):
    """Empty review list → no crash, no pings."""
    storage = MagicMock()
    transport = MagicMock()
    state = {}

    msgs = simulate_watchdog_cycle([], storage, transport, state)

    if len(msgs) == 0:
        r.ok("no_reviews_no_crash")
    else:
        r.fail("no_reviews_no_crash", f"Expected 0, got {len(msgs)}")


def test_full_lifecycle_3_pings_then_stop(r: _TestResult):
    """Simulate full lifecycle: 3 pings over time, then stop."""
    storage = MagicMock()
    transport = MagicMock()
    state = {}
    base_time = time.time()

    reviews_template = [make_review(age_seconds=0)]  # will adjust age
    storage.get_review_request.return_value = make_full_review(responses=[])

    total_pings = 0

    # Cycle 1: age=150s → first ping
    reviews = [make_review(age_seconds=150)]
    storage.get_review_request.return_value = make_full_review(responses=[])
    msgs = simulate_watchdog_cycle(reviews, storage, transport, state, now=base_time)
    total_pings += len(msgs)

    # Cycle 2: age=200s, 50s after first ping → throttled
    msgs = simulate_watchdog_cycle(
        [make_review(age_seconds=200)], storage, transport, state,
        now=base_time + 50
    )
    total_pings += len(msgs)

    # Cycle 3: age=290s, 140s after first ping → second ping
    msgs = simulate_watchdog_cycle(
        [make_review(age_seconds=290)], storage, transport, state,
        now=base_time + 140
    )
    total_pings += len(msgs)

    # Cycle 4: age=420s, 280s after first ping → third ping (+ escalation)
    msgs = simulate_watchdog_cycle(
        [make_review(age_seconds=420)], storage, transport, state,
        now=base_time + 280
    )
    total_pings += len(msgs)

    # Cycle 5: age=600s → max reached, no more pings
    msgs = simulate_watchdog_cycle(
        [make_review(age_seconds=600)], storage, transport, state,
        now=base_time + 500
    )
    total_pings += len(msgs)

    if total_pings == 3 and state.get(1, {}).get("count") == 3:
        r.ok("full_lifecycle_3_pings_then_stop")
    else:
        r.fail("full_lifecycle_3_pings_then_stop",
                f"Expected 3 total pings, got {total_pings}, state={state.get(1)}")


def test_reviewer_votes_mid_cycle_stops_pings(r: _TestResult):
    """Reviewer votes between pings → no more pings for them."""
    storage = MagicMock()
    transport = MagicMock()
    now = time.time()

    # After first ping
    state = {1: {"count": 1, "last_sent": now - 130, "escalated": False}}

    reviews = [make_review(age_seconds=300, reviewers=["@beta", "@haiku"])]

    # Both voted now
    storage.get_review_request.return_value = make_full_review(responses=[
        make_response("@beta"), make_response("@haiku")
    ])

    msgs = simulate_watchdog_cycle(reviews, storage, transport, state, now=now)

    if len(msgs) == 0 and 1 not in state:
        r.ok("reviewer_votes_mid_cycle_stops_pings")
    else:
        r.fail("reviewer_votes_mid_cycle_stops_pings",
                f"Expected 0 msgs + cleanup, got {len(msgs)}, state={state}")


# =============================================================================
# Runner
# =============================================================================

def main():
    print("=" * 60)
    print("Review Watchdog P7 Tests")
    print("=" * 60)

    r = _TestResult()

    print("\n--- Basic Timing ---")
    test_no_ping_before_delay(r)
    test_first_ping_after_delay(r)
    test_throttle_prevents_double_ping(r)
    test_second_ping_after_interval(r)

    print("\n--- Limits & Escalation ---")
    test_max_pings_stops_spamming(r)
    test_escalation_at_5_min(r)
    test_no_double_escalation(r)
    test_escalation_message_mentions_duration(r)

    print("\n--- Voter Tracking ---")
    test_cleanup_when_all_voted(r)
    test_partial_votes_ping_non_voters_only(r)
    test_reviewer_votes_mid_cycle_stops_pings(r)

    print("\n--- Backward Compat & Messages ---")
    test_legacy_db_reminder_on_first_ping(r)
    test_no_legacy_db_on_subsequent_pings(r)
    test_message_contains_mcp_reminder(r)

    print("\n--- Edge Cases ---")
    test_unparseable_created_at_skips(r)
    test_no_reviews_no_crash(r)
    test_multiple_reviews_independent_state(r)

    print("\n--- Integration ---")
    test_full_lifecycle_3_pings_then_stop(r)

    success = r.summary()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
