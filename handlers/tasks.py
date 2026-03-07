"""Task routes: /tasks, /task/*, /progress/{agent}"""

from handlers._base import normalize_timestamps
from aircp_daemon import (
    storage, transport, workflow_scheduler, bridge,
    _bot_send, _resolve_project, _run_git_hooks, _auto_create_workflow_review,
)


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_tasks(handler, parsed, params):
    agent = params.get("agent", [None])[0]
    status_filter = params.get("status", [None])[0]
    project_filter = params.get("project", [None])[0]

    if status_filter == "active":
        status_filter = "in_progress"

    if agent:
        tasks = storage.get_agent_tasks(agent, status_filter, project_id=project_filter)
    elif status_filter:
        tasks = storage.get_tasks_by_status(status_filter)
        if project_filter:
            tasks = [t for t in tasks if t.get("project_id") == project_filter]
    else:
        tasks = storage.get_active_tasks(project_id=project_filter)

    tasks = normalize_timestamps(tasks)
    handler.send_json({"tasks": tasks, "count": len(tasks)})


def get_progress_by_agent(handler, parsed, params):
    agent_id = parsed.path.split("/")[-1]
    if not agent_id.startswith("@"):
        agent_id = f"@{agent_id}"

    state = storage.get_agent_state(agent_id)
    status = state.get("status", "unknown")
    task = state.get("task")

    if status == "unknown":
        message = f"? {agent_id} has never been seen (no heartbeat)"
    elif status == "offline":
        message = f"X {agent_id} appears offline (last heartbeat: {state.get('last_activity_human', 'unknown')})"
    elif status == "stale":
        task_desc = task.get("description", "")[:50] if task else ""
        pings = state.get("watchdog", {}).get("pings", 0)
        message = (f"! {agent_id} appears stuck on task #{task.get('id')} "
                   f"({task_desc}...) -- {pings}/3 pings, last activity: "
                   f"{state.get('last_activity_human')}")
    elif status == "idle":
        message = (f"- {agent_id} is idle (no active task) -- last activity: "
                   f"{state.get('last_activity_human')}")
    elif status == "working":
        if task:
            step = task.get("step", 0)
            task_desc = task.get("description", "")[:50]
            message = (f"~ {agent_id} working on task #{task.get('id')} "
                       f"({task_desc}...) -- step: {step}")
        else:
            message = f"~ {agent_id} is active (no tracked task)"
    else:
        message = f"? {agent_id} -- status: {status}"

    state["message"] = message
    handler.send_json(state)


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def post_task(handler, body):
    try:
        agent_id = body.get("agent_id")
        task_type = body.get("task_type", "generic")
        description = body.get("description", "")
        context = body.get("context")

        if not agent_id:
            handler.send_json({"error": "Missing 'agent_id' field"}, 400)
            return

        if not description:
            handler.send_json({"error": "Missing 'description' field"}, 400)
            return

        project_id = _resolve_project(body, agent_id)
        task_id = storage.create_task(agent_id, task_type, description, context, project_id=project_id)

        if task_id > 0:
            # v3.3: Link to workflow only if explicitly requested
            linked_wf_id = None
            explicit_wf_id = body.get("workflow_id")
            if explicit_wf_id and workflow_scheduler:
                wf = workflow_scheduler.get_workflow(int(explicit_wf_id))
                if wf and not wf.get("completed_at"):
                    storage.set_task_workflow_id(task_id, wf["id"])
                    linked_wf_id = wf["id"]

            # Broadcast task creation
            wf_tag = f" [workflow #{linked_wf_id}]" if linked_wf_id else ""
            proj_tag = f" [{project_id}]" if project_id != "default" else ""
            msg = f"\U0001f4cb **TASK #{task_id}** created for {agent_id}{proj_tag}: {description[:80]}{wf_tag}"
            _bot_send("#general", msg, from_id="@taskman")

            handler.send_json({
                "status": "created",
                "task_id": task_id,
                "agent_id": agent_id,
                "task_type": task_type,
                "description": description,
                "workflow_id": linked_wf_id
            })
        else:
            handler.send_json({"error": "Failed to create task"}, 500)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_task_claim(handler, body):
    try:
        task_id = body.get("task_id")
        agent_id = body.get("agent_id", transport.agent_id)

        if not task_id:
            handler.send_json({"error": "Missing 'task_id' field"}, 400)
            return

        success = storage.claim_task(task_id, agent_id)

        if success:
            msg = f"\U0001f680 {agent_id} claimed task #{task_id}"
            _bot_send("#general", msg, from_id="@taskman")
            handler.send_json({"status": "claimed", "success": True, "task_id": task_id, "agent_id": agent_id})
        else:
            handler.send_json({"error": "Failed to claim task (already claimed or not found)"}, 400)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_task_activity(handler, body):
    try:
        task_id = body.get("task_id")
        current_step = body.get("current_step")

        if not task_id:
            handler.send_json({"error": "Missing 'task_id' field"}, 400)
            return

        success = storage.update_task_activity(task_id, current_step)
        response = {"status": "updated" if success else "not_found", "success": success, "task_id": task_id}
        if success and current_step is not None:
            response["current_step"] = current_step
        handler.send_json(response)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_task_complete(handler, body):
    try:
        task_id = body.get("task_id")
        status = body.get("status", "done")

        if not task_id:
            handler.send_json({"error": "Missing 'task_id' field"}, 400)
            return

        valid_statuses = ["done", "failed", "cancelled", "stale"]
        if status not in valid_statuses:
            handler.send_json({"error": f"Invalid status. Must be one of: {valid_statuses}"}, 400)
            return

        success = storage.complete_task(task_id, status)

        if success:
            emoji = "\u2705" if status == "done" else "\u274c" if status == "failed" else "\u26a0\ufe0f" if status == "stale" else "\U0001f6ab"
            msg = f"{emoji} Task #{task_id} completed ({status})"
            _bot_send("#general", msg, from_id="@taskman")

            # v3.3: Auto-advance workflow if last code task completed
            if status == "done" and workflow_scheduler:
                try:
                    task = storage.get_task_by_id(task_id)
                    if task and task.get("workflow_id"):
                        wf_id = task["workflow_id"]
                        wf = workflow_scheduler.get_workflow(wf_id)
                        if wf and wf.get("phase") == "code" and not wf.get("completed_at"):
                            remaining = storage.get_active_workflow_tasks(wf_id)
                            if not remaining:
                                result = workflow_scheduler.next_phase(wf_id)
                                if result.get("success"):
                                    next_phase = result.get("current_phase", "review")
                                    _bot_send(
                                        "#general",
                                        f"\U0001f504 **WORKFLOW #{wf_id}** auto-advanced to `@{next_phase}` (all tasks done)",
                                        from_id="@workflow"
                                    )
                                    _run_git_hooks("code", next_phase, wf_id)
                                    if next_phase == "review":
                                        _auto_create_workflow_review(wf_id)
                                    if bridge:
                                        wf_updated = workflow_scheduler.get_workflow(wf_id)
                                        if wf_updated:
                                            bridge.emit_workflow(wf_updated)
                except Exception as e:
                    print(f"[WORKFLOW] Auto-advance error on task complete: {e}")

            handler.send_json({"status": "completed", "success": True, "task_id": task_id, "final_status": status})
        else:
            handler.send_json({"error": "Failed to complete task"}, 400)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/tasks": get_tasks,
}

POST_ROUTES = {
    "/task":          post_task,
    "/task/claim":    post_task_claim,
    "/task/activity": post_task_activity,
    "/task/complete": post_task_complete,
}

GET_PREFIX_ROUTES = [
    ("/progress/", get_progress_by_agent),
]
