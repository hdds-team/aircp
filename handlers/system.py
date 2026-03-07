"""System routes: /status, /health, /rooms, /, /dashboard.html, /daemon/*"""

import os
import time
import threading

from aircp_daemon import (
    transport, joined_rooms, storage, _watchdog_threads, _daemon_start_time,
    workflow_scheduler, _bot_send, _storage,
)
from channels import RESERVED_CHANNELS

# Dashboard path: resolve relative to project root (parent of handlers/)
_DASHBOARD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dashboard", "index.html"
)


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_status(handler, parsed, params):
    handler.send_json({
        "status": "ok",
        "agent_id": transport.agent_id,
        "rooms": list(joined_rooms),
        "version": "3.1.0"
    })


def get_health(handler, parsed, params):
    """Health check endpoint for monitoring/load balancers.
    Returns 200 if healthy, 503 if any critical check fails.
    Public (no auth), read-only checks only."""
    t0 = time.time()

    # --- Storage check (read-only SELECT) ---
    storage_ok = False
    storage_latency = 0.0
    try:
        st = time.time()
        with storage._conn_lock:
            storage._get_conn().execute("SELECT 1").fetchone()
        storage_latency = round((time.time() - st) * 1000, 2)
        storage_ok = True
    except Exception:
        pass

    # --- Transport check (+ latency probe) ---
    transport_ok = False
    transport_rooms = 0
    transport_latency = 0.0
    try:
        tt = time.time()
        transport_ok = (
            transport is not None
            and transport.participant is not None
        )
        if transport_ok and hasattr(transport, 'ping'):
            transport.ping()
        transport_latency = round((time.time() - tt) * 1000, 2)
        transport_rooms = len(joined_rooms)
    except Exception:
        pass

    # --- Watchdog threads (individual flags) ---
    watchdogs = {}
    for name, thread in _watchdog_threads.items():
        watchdogs[name] = thread.is_alive() if thread else False

    # --- Agents online (from presence, read-only) ---
    agents_online = 0
    try:
        presence = storage.get_all_agent_presence()
        agents_online = sum(
            1 for p in presence
            if p.get("status") == "online"
        )
    except Exception:
        pass

    # --- Uptime ---
    uptime_secs = 0
    if _daemon_start_time:
        uptime_secs = round(time.time() - _daemon_start_time)

    # --- Overall health: storage + transport must be OK ---
    healthy = storage_ok and transport_ok

    response_time_ms = round((time.time() - t0) * 1000, 2)
    from datetime import datetime as _dt, timezone as _tz
    ts = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    result = {
        "healthy": healthy,
        "uptime_secs": uptime_secs,
        "version": "3.1.0",
        "pid": os.getpid(),
        "response_time_ms": response_time_ms,
        "checks": {
            "storage": {
                "ok": storage_ok,
                "latency_ms": storage_latency,
                "checked_at": ts,
            },
            "transport": {
                "ok": transport_ok,
                "rooms": transport_rooms,
                "latency_ms": transport_latency,
                "checked_at": ts,
            },
            "watchdogs": watchdogs,
            "agents_online": agents_online,
        },
        "timestamp": ts,
    }

    status_code = 200 if healthy else 503
    handler.send_json(result, status_code)


def get_rooms(handler, parsed, params):
    rooms_list = [
        {"name": room, "type": "reserved" if room in RESERVED_CHANNELS else "user"}
        for room in sorted(joined_rooms)
    ]
    handler.send_json({"rooms": rooms_list, "count": len(rooms_list)})


def get_dashboard(handler, parsed, params):
    try:
        with open(_DASHBOARD_PATH, "r") as f:
            content = f.read()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler._send_cors_headers()
        handler.end_headers()
        handler.wfile.write(content.encode())
    except FileNotFoundError:
        handler.send_json({"error": "dashboard not found"}, 404)


def get_daemon_can_restart(handler, parsed, params):
    check = storage.can_safely_restart()
    if workflow_scheduler:
        wf = workflow_scheduler.get_active_workflow()
        if wf:
            check["safe"] = False
            check["blockers"].append({
                "type": "active_workflow",
                "phase": wf.get("current_phase", "unknown"),
                "feature": wf.get("feature", "")
            })
            check["reason"] = (check.get("reason", "") +
                f"; Workflow active (phase: {wf.get('current_phase', '?')})")
    status_code = 200 if check["safe"] else 409
    handler.send_json(check, status_code)


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def post_daemon_restart(handler, body):
    try:
        force = body.get("force", False)
        grace_seconds = body.get("grace_seconds", 60)

        check = storage.can_safely_restart()
        # Also check workflow
        if workflow_scheduler:
            wf = workflow_scheduler.get_active_workflow()
            if wf:
                check["safe"] = False
                check["blockers"].append({
                    "type": "active_workflow",
                    "phase": wf.get("current_phase", "unknown"),
                    "feature": wf.get("feature", "")
                })

        if not check["safe"] and not force:
            handler.send_json({
                "restarted": False,
                "reason": check["reason"],
                "blockers": check["blockers"],
                "hint": "Use force=true to override (dangerous)"
            }, 409)
            return

        if not check["safe"] and force:
            _bot_send(
                "#general",
                f"**FORCED RESTART** requested! Blocker reason: {check['reason']}. "
                f"Save your work, shutdown in {grace_seconds}s.",
                from_id="@system"
            )

        _bot_send(
            "#general",
            "\U0001f504 **Daemon restart** in progress. Persisting DB...",
            from_id="@system"
        )

        # Persist DB before restart
        if _storage is not None:
            _storage.persist_to_disk()

        handler.send_json({
            "restarted": True,
            "was_forced": force,
            "blockers": check["blockers"],
            "message": "DB persisted. Daemon shutting down. External process must restart."
        })

        # Schedule shutdown after response is sent
        def _delayed_shutdown():
            time.sleep(2)
            os._exit(0)

        threading.Thread(target=_delayed_shutdown, daemon=True).start()

    except Exception as e:
        handler.send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/status":             get_status,
    "/health":             get_health,
    "/rooms":              get_rooms,
    "/":                   get_dashboard,
    "/dashboard.html":     get_dashboard,
    "/daemon/can-restart": get_daemon_can_restart,
}

POST_ROUTES = {
    "/daemon/restart": post_daemon_restart,
}
