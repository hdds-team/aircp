"""Workflow routes: /workflow/*"""

import aircp_user_config as _ucfg
from workflow_scheduler import WORKFLOW_PHASES
from aircp_daemon import (
    workflow_scheduler, transport, storage, bridge,
    _bot_send, ensure_room, _resolve_project, telegram_notify,
    _run_git_hooks, _auto_create_workflow_review,
)


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_workflow(handler, parsed, params):
    if workflow_scheduler:
        status = workflow_scheduler.get_workflow_status()
        handler.send_json(status)
    else:
        handler.send_json({"error": "Workflow scheduler not initialized"}, 500)


def get_workflow_history(handler, parsed, params):
    limit = int(params.get("limit", [20])[0])
    if workflow_scheduler:
        history = workflow_scheduler.get_history(limit)
        handler.send_json({"history": history, "count": len(history)})
    else:
        handler.send_json({"error": "Workflow scheduler not initialized"}, 500)


def get_workflow_config(handler, parsed, params):
    if workflow_scheduler:
        config = workflow_scheduler.get_config()
        handler.send_json({"phases": WORKFLOW_PHASES, "config": config})
    else:
        handler.send_json({"error": "Workflow scheduler not initialized"}, 500)


def get_workflow_chunks(handler, parsed, params):
    """List chunks for active workflow."""
    if not workflow_scheduler:
        handler.send_json({"error": "Workflow scheduler not initialized"}, 500)
        return

    wf = workflow_scheduler.get_active_workflow()
    if not wf:
        handler.send_json({"chunks": [], "total": 0})
        return

    summary = workflow_scheduler.get_chunks_summary(wf['id'])
    summary["workflow_id"] = wf['id']
    summary["mode"] = wf.get('mode', 'standard')
    handler.send_json(summary)


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def post_workflow_start(handler, body):
    try:
        name = body.get("name", "")
        description = body.get("description", "")
        created_by = body.get("created_by", _ucfg.user())
        lead_agent = body.get("lead_agent") or body.get("lead") or created_by
        mode = body.get("mode", "standard")

        if not name:
            handler.send_json({"error": "Missing 'name' field"}, 400)
            return

        if mode not in ("standard", "veloce"):
            handler.send_json({"error": f"Invalid mode: {mode}. Use 'standard' or 'veloce'"}, 400)
            return

        if not workflow_scheduler:
            handler.send_json({"error": "Workflow scheduler not initialized"}, 500)
            return

        project_id = _resolve_project(body, created_by)

        workflow_id = workflow_scheduler.create_workflow(
            name=name,
            created_by=created_by,
            description=description,
            lead_agent=lead_agent,
            project_id=project_id,
            mode=mode
        )

        if workflow_id > 0:
            mode_tag = " **[VELOCE]**" if mode == "veloce" else ""
            msg = f"\U0001f680 **WORKFLOW #{workflow_id}**{mode_tag} started: {name}\n"
            msg += f"Created by: {created_by}\n"
            if mode == "veloce":
                msg += f"Phase: `@request` -- Mode Veloce (parallel coding)"
            else:
                msg += f"Phase: `@request` -- Waiting for brainstorm..."
            if transport:
                ensure_room("#general")
                _bot_send("#general", msg, from_id="@workflow")
            if bridge:
                wf = workflow_scheduler.get_workflow(workflow_id)
                if wf:
                    bridge.emit_workflow(wf)

            handler.send_json({
                "status": "created",
                "workflow_id": workflow_id,
                "name": name,
                "phase": "request",
                "created_by": created_by,
                "mode": mode
            })
        else:
            handler.send_json({
                "error": "Cannot create workflow - one already active",
                "active_workflow": workflow_scheduler.get_active_workflow()
            }, 409)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_workflow_next(handler, body):
    try:
        if not workflow_scheduler:
            handler.send_json({"error": "Workflow scheduler not initialized"}, 500)
            return

        result = workflow_scheduler.next_phase()

        if result.get("success"):
            if result.get("status"):
                # Workflow completed
                status = result.get("status")
                duration = result.get("duration_minutes", 0)
                wf_id = result.get("workflow_id")

                msg = f"\u2705 **WORKFLOW #{wf_id}** completed ({status}) \u2014 Duration: {duration}min"
                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@workflow")

                reminder = """\U0001f4cb **POST-DELIVERY CHECKLIST:**
1. Update `docs/*.md` (IDEAS, TASKMANAGER, etc.)
2. Check if agent `SOUL.md` files need updates
3. Verify `dashboard.html` reflects the changes
4. Nothing gets forgotten!"""
                if transport:
                    _bot_send("#general", reminder, from_id="@workflow")
                if bridge:
                    bridge.emit_workflow(None)

                telegram_notify("workflow/complete", {
                    "workflow_id": wf_id,
                    "duration_minutes": duration,
                    "status": status,
                })

                _run_git_hooks("livrable", "done", wf_id)
            else:
                # Normal phase transition
                prev = result.get("previous_phase")
                curr = result.get("current_phase")
                timeout = result.get("timeout_minutes")
                wf_id = result.get("workflow_id")

                mode = result.get("mode", "standard")
                mode_tag = " [VELOCE]" if mode == "veloce" else ""
                msg = f"\u27a1\ufe0f **WORKFLOW**{mode_tag} - Phase `@{prev}` -> `@{curr}` (timeout: {timeout}min)"
                chunks_started = result.get("chunks_started")
                if chunks_started:
                    msg += f"\n\U0001f4e6 {chunks_started} parallel chunk(s) started!"
                if transport:
                    ensure_room("#general")
                    _bot_send("#general", msg, from_id="@workflow")

                telegram_notify("workflow/phase", {
                    "previous_phase": prev,
                    "current_phase": curr,
                    "timeout_minutes": timeout,
                    "workflow_id": wf_id,
                })

                _run_git_hooks(prev, curr, wf_id)

                if curr == "review":
                    wf = workflow_scheduler.get_active_workflow()
                    if wf:
                        _auto_create_workflow_review(wf["id"])

                if bridge:
                    wf = workflow_scheduler.get_active_workflow()
                    bridge.emit_workflow(wf)

        handler.send_json(result)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_workflow_extend(handler, body):
    try:
        minutes = body.get("minutes", 10)

        if not workflow_scheduler:
            handler.send_json({"error": "Workflow scheduler not initialized"}, 500)
            return

        result = workflow_scheduler.extend_phase(minutes)

        if result.get("success"):
            phase = result.get("phase")
            new_timeout = result.get("new_timeout_minutes")
            remaining = result.get("extends_remaining")

            msg = f"\u23f0 **WORKFLOW** \u2014 Phase `@{phase}` extended by {minutes}min (total: {new_timeout}min, {remaining} extend(s) remaining)"
            if transport:
                ensure_room("#general")
                _bot_send("#general", msg, from_id="@workflow")
            if bridge:
                wf = workflow_scheduler.get_active_workflow()
                bridge.emit_workflow(wf)

        handler.send_json(result)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_workflow_skip(handler, body):
    try:
        phase = body.get("phase")

        if not phase:
            handler.send_json({"error": "Missing 'phase' field"}, 400)
            return

        if not workflow_scheduler:
            handler.send_json({"error": "Workflow scheduler not initialized"}, 500)
            return

        wf_before = workflow_scheduler.get_active_workflow()
        prev_phase = wf_before["phase"] if wf_before else None

        result = workflow_scheduler.skip_to_phase(phase)

        if result.get("success"):
            msg = f"\u23ed\ufe0f **WORKFLOW** \u2014 Skipped to `@{phase}` (lead override)"
            if transport:
                ensure_room("#general")
                _bot_send("#general", msg, from_id="@workflow")

            wf_id = result.get("workflow_id")
            if wf_id:
                _run_git_hooks(prev_phase, phase, wf_id)

            if bridge:
                wf = workflow_scheduler.get_active_workflow()
                bridge.emit_workflow(wf)

        handler.send_json(result)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_workflow_abort(handler, body):
    try:
        reason = body.get("reason", "aborted")

        if not workflow_scheduler:
            handler.send_json({"error": "Workflow scheduler not initialized"}, 500)
            return

        result = workflow_scheduler.abort_workflow(reason=reason)

        if result.get("success"):
            msg = f"\U0001f6d1 **WORKFLOW** aborted: {reason}"
            if transport:
                ensure_room("#general")
                _bot_send("#general", msg, from_id="@workflow")
            if bridge:
                bridge.emit_workflow(None)
                _bot_send("#general",
                          "\U0001f4cb If work was done, remember to document the current state in `docs/`.",
                          from_id="@workflow")

        handler.send_json(result)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_workflow_decompose(handler, body):
    """Submit chunk decomposition plan."""
    try:
        if not workflow_scheduler:
            handler.send_json({"error": "Workflow scheduler not initialized"}, 500)
            return

        wf = workflow_scheduler.get_active_workflow()
        if not wf:
            handler.send_json({"error": "No active workflow"}, 404)
            return

        if wf.get('mode') != 'veloce':
            handler.send_json({"error": "Not a veloce workflow"}, 400)
            return

        chunks = body.get("chunks", [])
        if not chunks:
            handler.send_json({"error": "Missing 'chunks' array"}, 400)
            return

        result = workflow_scheduler.submit_decomposition(wf['id'], chunks)

        if result.get("success"):
            chunk_ids = result.get("chunk_ids", [])
            count = result.get("chunks_count", 0)
            msg = f"\U0001f4e6 **WORKFLOW** \u2014 Decomposition submitted: {count} chunks\n"
            for c in chunks:
                msg += f"  - `{c['chunk_id']}` -> {c.get('agent_id', '?')}\n"
            if transport:
                ensure_room("#general")
                _bot_send("#general", msg, from_id="@workflow")
            if bridge:
                wf = workflow_scheduler.get_active_workflow()
                bridge.emit_workflow(wf)

        handler.send_json(result)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


def post_workflow_chunk_done(handler, body):
    """Mark a chunk as completed."""
    try:
        if not workflow_scheduler:
            handler.send_json({"error": "Workflow scheduler not initialized"}, 500)
            return

        chunk_id = body.get("chunk_id", "").strip()
        if not chunk_id:
            handler.send_json({"error": "Missing 'chunk_id' field"}, 400)
            return

        wf = workflow_scheduler.get_active_workflow()
        if not wf:
            handler.send_json({"error": "No active workflow"}, 404)
            return

        result = workflow_scheduler.complete_chunk(wf['id'], chunk_id)

        if result.get("success"):
            done = result.get("done_count", 0)
            total = result.get("active_chunks", 0)
            gate = result.get("gate_open", False)
            msg = f"\u2705 **CHUNK** `{chunk_id}` done ({done}/{total})"
            if gate:
                msg += " -- \U0001f6aa Gate OPEN, ready for next phase!"
            if transport:
                ensure_room("#general")
                _bot_send("#general", msg, from_id="@workflow")
            if bridge:
                wf = workflow_scheduler.get_active_workflow()
                bridge.emit_workflow(wf)

        handler.send_json(result)

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/workflow":         get_workflow,
    "/workflow/history": get_workflow_history,
    "/workflow/config":  get_workflow_config,
    "/workflow/chunks":  get_workflow_chunks,
}

POST_ROUTES = {
    "/workflow/start":      post_workflow_start,
    "/workflow/next":       post_workflow_next,
    "/workflow/extend":     post_workflow_extend,
    "/workflow/skip":       post_workflow_skip,
    "/workflow/abort":      post_workflow_abort,
    "/workflow/decompose":  post_workflow_decompose,
    "/workflow/chunk/done": post_workflow_chunk_done,
}
