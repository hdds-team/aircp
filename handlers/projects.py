"""Project routes: /projects, /projects/{id}, /agent/project"""

import aircp_user_config as _ucfg
from aircp_daemon import storage, transport, _bot_send


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def get_projects(handler, parsed, params):
    projects = storage.get_all_projects()
    handler.send_json({"projects": projects})


def get_project_by_id(handler, parsed, params):
    parts = parsed.path.split("/")
    if len(parts) != 3:
        handler.send_json({"error": "Not found"}, 404)
        return
    project_id = parts[2]
    project = storage.get_project(project_id)
    if project:
        agents = storage.get_agents_in_project(project_id)
        project["agents"] = agents
        handler.send_json({"project": project})
    else:
        handler.send_json({"error": f"Project not found: {project_id}"}, 404)


def get_agent_project(handler, parsed, params):
    agent_id = params.get("agent_id", [None])[0]
    if not agent_id:
        handler.send_json({"error": "Missing agent_id"}, 400)
    else:
        project_id = storage.get_agent_active_project(agent_id)
        handler.send_json({"agent_id": agent_id, "project_id": project_id})


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def post_projects(handler, body):
    project_id = body.get("id")
    name = body.get("name", project_id or "")
    description = body.get("description", "")
    owner = body.get("owner", _ucfg.user())
    if not project_id:
        handler.send_json({"error": "Missing project id"}, 400)
        return
    ok = storage.create_project(project_id, name, description, owner)
    if ok:
        handler.send_json({"project": storage.get_project(project_id)}, 201)
    else:
        handler.send_json({"error": f"Project already exists: {project_id}"}, 409)


def post_projects_delete(handler, body):
    project_id = body.get("id") or body.get("project_id")
    if not project_id:
        handler.send_json({"error": "Missing project id"}, 400)
        return
    if project_id == "default":
        handler.send_json({"error": "Cannot delete the default project"}, 400)
        return
    ok = storage.delete_project(project_id)
    if ok:
        handler.send_json({"ok": True, "deleted": project_id})
    else:
        handler.send_json({"error": f"Project not found: {project_id}"}, 404)


def post_agent_project(handler, body):
    agent_id = body.get("agent_id")
    project_id = body.get("project_id")
    if not agent_id or not project_id:
        handler.send_json({"error": "Missing agent_id or project_id"}, 400)
        return
    if not storage.get_project(project_id):
        handler.send_json({"error": f"Project not found: {project_id}"}, 404)
        return
    storage.set_agent_active_project(agent_id, project_id)
    if transport:
        msg = f"[project] {agent_id} switched to project [{project_id}]"
        _bot_send("#general", msg, from_id="@system")
    handler.send_json({"ok": True, "agent_id": agent_id, "project_id": project_id})


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/projects":      get_projects,
    "/agent/project": get_agent_project,
}

POST_ROUTES = {
    "/projects":        post_projects,
    "/projects/delete": post_projects_delete,
    "/agent/project":   post_agent_project,
}

GET_PREFIX_ROUTES = [
    ("/projects/", get_project_by_id),
]
