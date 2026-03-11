"""Review routes: /review/*"""

import logging

from handlers._base import normalize_timestamps
from handlers.tasks import _auto_lock_files, _auto_release_locks
from aircp_daemon import (
    storage, transport, workflow_scheduler, bridge,
    _bot_send, ensure_room, _resolve_project, telegram_notify,
    _run_git_hooks, review_reminder_state,
    REVIEW_TIMEOUT_SECONDS,
)

logger = logging.getLogger("handlers.reviews")


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_review_list(handler, parsed, params):
    status_filter = params.get("status", ["pending"])[0]
    if status_filter == "pending":
        reviews = storage.get_active_review_requests()
    else:
        reviews = storage.get_review_history(limit=50)
    reviews = normalize_timestamps(reviews)
    handler.send_json({"reviews": reviews, "count": len(reviews)})


def get_review_history(handler, parsed, params):
    limit = int(params.get("limit", [20])[0])
    reviews = storage.get_review_history(limit)
    reviews = normalize_timestamps(reviews)
    handler.send_json({"reviews": reviews, "count": len(reviews)})


def get_review_by_id(handler, parsed, params):
    try:
        request_id = int(parsed.path.split("/")[-1])
        review = storage.get_review_request(request_id)
        if review:
            responses = review.get("responses", [])
            approvals = sum(1 for r in responses if r.get("vote") == "approve")
            changes = sum(1 for r in responses if r.get("vote") == "changes")
            comments = sum(1 for r in responses if r.get("vote") == "comment")
            review["summary"] = {
                "approvals": approvals,
                "changes_requested": changes,
                "comments": comments,
                "total_responses": len(responses),
                "reviewers_pending": [r for r in review.get("reviewers", [])
                                       if r not in {resp.get("reviewer") for resp in responses}]
            }
            handler.send_json(review)
        else:
            handler.send_json({"error": "Review not found"}, 404)
    except ValueError:
        handler.send_json({"error": "Invalid review ID"}, 400)


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def post_review_request(handler, body):
    try:
        file_path = body.get("file", body.get("file_path", ""))
        reviewers = body.get("reviewers", [])
        review_type = body.get("type", "doc")
        requested_by = body.get("requested_by", transport.agent_id)

        if not file_path:
            handler.send_json({"error": "Missing 'file' field"}, 400)
            return

        if not reviewers:
            if review_type == "code":
                reviewers = ["@beta", "@sonnet"]
            else:
                reviewers = ["@sonnet"]

        project_id = _resolve_project(body, requested_by)

        request_id = storage.create_review_request(
            file_path=file_path,
            requested_by=requested_by,
            reviewers=reviewers,
            review_type=review_type,
            timeout_seconds=REVIEW_TIMEOUT_SECONDS,
            project_id=project_id
        )

        if request_id > 0:
            reviewer_tags = " ".join(reviewers)
            msg = f"**REVIEW #{request_id}** requested by {requested_by}\n"
            msg += f"**File:** `{file_path}`\n"
            msg += f"**Type:** {review_type} (min {2 if review_type == 'code' else 1} approval(s))\n"
            msg += f"**Reviewers:** {reviewer_tags}\n"
            msg += f"**Timeout:** 1h (reminder at 30min)"

            if transport:
                ensure_room("#general")
                _bot_send("#general", msg, from_id="@review")

            # Auto-lock the reviewed file (Brainstorm #7)
            _auto_lock_files([file_path], requested_by, request_id)

            handler.send_json({
                "status": "created",
                "request_id": request_id,
                "file": file_path,
                "reviewers": reviewers,
                "review_type": review_type,
                "timeout_seconds": REVIEW_TIMEOUT_SECONDS
            })
        else:
            handler.send_json({"error": "Failed to create review request"}, 500)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_review_approve(handler, body):
    try:
        request_id = body.get("request_id", body.get("id"))
        reviewer = body.get("reviewer", transport.agent_id)
        comment = body.get("comment")

        if not request_id:
            handler.send_json({"error": "Missing 'request_id' field"}, 400)
            return

        review = storage.get_review_request(request_id)
        if not review:
            handler.send_json({"error": "Review not found"}, 404)
            return

        if review.get("status") != "pending":
            handler.send_json({"error": "Review already closed"}, 400)
            return

        success = storage.add_review_response(request_id, reviewer, "approve", comment)

        if success:
            file_path = review.get("file_path", "")
            comment_str = f" - {comment}" if comment else ""
            msg = f"\u2705 {reviewer} approuve review #{request_id} (`{file_path}`){comment_str}"

            if transport:
                ensure_room("#general")
                _bot_send("#general", msg, from_id="@review")

            updated_review = storage.get_review_request(request_id)
            responses = updated_review.get("responses", []) if updated_review else []
            approvals = sum(1 for r in responses if r.get("vote") == "approve")
            min_approvals = review.get("min_approvals", 1)

            if approvals >= min_approvals:
                storage.close_review_request(request_id, "approved", "completed")
                # Auto-release lock on reviewed file (Brainstorm #7)
                requested_by = review.get("requested_by", "")
                _auto_release_locks(requested_by, request_id)
                msg = f"\U0001f389 **REVIEW #{request_id}** approved! ({approvals}/{min_approvals} approvals)"
                if transport:
                    _bot_send("#general", msg, from_id="@review")

                telegram_notify("review/approved", {
                    "request_id": request_id,
                    "approvals": approvals,
                    "min_approvals": min_approvals,
                    "file_path": file_path,
                })

                # HOOK v3.3: Auto-advance workflow on review approval
                if workflow_scheduler:
                    review_wf_id = updated_review.get("workflow_id")
                    file_path = updated_review.get("file_path", "")
                    if not review_wf_id and file_path.startswith("workflow:"):
                        wf = workflow_scheduler.get_active_workflow()
                        review_wf_id = wf["id"] if wf else None
                    if review_wf_id:
                        wf = workflow_scheduler.get_workflow(review_wf_id)
                        if wf and wf.get("phase") == "review" and not wf.get("completed_at"):
                            next_result = workflow_scheduler.next_phase(review_wf_id)
                            if next_result.get("success") and transport:
                                next_phase = next_result.get("current_phase", "test")
                                _bot_send("#general",
                                    f"\U0001f504 **WORKFLOW #{review_wf_id}** auto-advanced to `@{next_phase}` (review approved)",
                                    from_id="@workflow")
                                _run_git_hooks("review", next_phase, review_wf_id)
                                if bridge:
                                    wf_updated = workflow_scheduler.get_workflow(review_wf_id)
                                    if wf_updated:
                                        bridge.emit_workflow(wf_updated)

            handler.send_json({
                "status": "approved",
                "request_id": request_id,
                "reviewer": reviewer,
                "approvals": approvals,
                "min_approvals": min_approvals
            })
        else:
            handler.send_json({"error": "Failed to record approval"}, 500)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_review_comment(handler, body):
    try:
        request_id = body.get("request_id", body.get("id"))
        reviewer = body.get("reviewer", transport.agent_id)
        comment = body.get("comment", "")

        if not request_id:
            handler.send_json({"error": "Missing 'request_id' field"}, 400)
            return

        if not comment:
            handler.send_json({"error": "Missing 'comment' field"}, 400)
            return

        review = storage.get_review_request(request_id)
        if not review:
            handler.send_json({"error": "Review not found"}, 404)
            return

        if review.get("status") != "pending":
            handler.send_json({"error": "Review already closed"}, 400)
            return

        success = storage.add_review_response(request_id, reviewer, "comment", comment)

        if success:
            file_path = review.get("file_path", "")
            msg = f"\U0001f4ac {reviewer} commente review #{request_id}: {comment[:100]}"

            if transport:
                ensure_room("#general")
                _bot_send("#general", msg, from_id="@review")

            handler.send_json({
                "status": "commented",
                "request_id": request_id,
                "reviewer": reviewer
            })
        else:
            handler.send_json({"error": "Failed to record comment"}, 500)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_review_changes(handler, body):
    try:
        request_id = body.get("request_id", body.get("id"))
        reviewer = body.get("reviewer", transport.agent_id)
        comment = body.get("comment", "")

        if not request_id:
            handler.send_json({"error": "Missing 'request_id' field"}, 400)
            return

        if not comment:
            handler.send_json({"error": "Missing 'comment' field (explain what needs to change)"}, 400)
            return

        review = storage.get_review_request(request_id)
        if not review:
            handler.send_json({"error": "Review not found"}, 404)
            return

        if review.get("status") != "pending":
            handler.send_json({"error": "Review already closed"}, 400)
            return

        success = storage.add_review_response(request_id, reviewer, "changes", comment)

        if success:
            file_path = review.get("file_path", "")
            requested_by = review.get("requested_by", "")
            msg = f"{reviewer} requests changes on review #{request_id}: {comment[:100]}"

            if transport:
                ensure_room("#general")
                _bot_send("#general", msg, from_id="@review")

                if requested_by:
                    notify_msg = f"{requested_by} - {reviewer} requests changes on your review #{request_id}"
                    _bot_send("#general", notify_msg, from_id="@review")

            telegram_notify("review/changes", {
                "request_id": request_id,
                "reviewer": reviewer,
                "comment": comment,
            })

            # P1 FIX: changes_requested is blocking - close the review
            storage.close_review_request(request_id, "changes_requested", "completed")
            review_reminder_state.pop(request_id, None)
            if transport:
                _bot_send(
                    "#general",
                    f"\U0001f4cb **REVIEW #{request_id}** closed (changes requested by {reviewer})",
                    from_id="@review"
                )

            handler.send_json({
                "status": "changes_requested",
                "request_id": request_id,
                "reviewer": reviewer,
                "comment": comment
            })
        else:
            handler.send_json({"error": "Failed to record changes request"}, 500)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_review_close(handler, body):
    try:
        request_id = body.get("request_id", body.get("id"))
        reason = body.get("reason", "manually closed")
        closed_by = body.get("closed_by", body.get("reviewer", "unknown"))

        if not request_id:
            handler.send_json({"error": "Missing 'request_id' field"}, 400)
            return

        review = storage.get_review_request(request_id)
        if not review:
            handler.send_json({"error": "Review not found"}, 404)
            return

        if review.get("status") != "pending":
            handler.send_json({
                "status": review.get("status"),
                "message": "Review already closed",
                "request_id": request_id
            })
            return

        success = storage.close_review_request(request_id, reason, "closed")
        if success:
            file_path = review.get("file_path", "")
            if transport:
                ensure_room("#general")
                _bot_send(
                    "#general",
                    f"\U0001f512 **REVIEW #{request_id}** closed by {closed_by}: {reason}",
                    from_id="@review"
                )
            review_reminder_state.pop(request_id, None)

            handler.send_json({
                "status": "closed",
                "request_id": request_id,
                "reason": reason,
                "closed_by": closed_by
            })
        else:
            handler.send_json({"error": "Failed to close review"}, 500)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/review/list":    get_review_list,
    "/review/history": get_review_history,
}

POST_ROUTES = {
    "/review/request": post_review_request,
    "/review/approve": post_review_approve,
    "/review/comment": post_review_comment,
    "/review/changes": post_review_changes,
    "/review/close":   post_review_close,
}

GET_PREFIX_ROUTES = [
    ("/review/status/", get_review_by_id),
]
