# TaskManager - Specification

> Agent task management with anti-stale watchdog.
> Version: 1.0 | Updated 2026-02-06

## Overview

The TaskManager is a task management system built into the AIRCP daemon. It allows you to:
- Assign tasks to agents
- Track their progress (step by step)
- Detect inactive agents (watchdog)
- Persist state to survive restarts

---

## Usage via MCP (for agents)

### Create a task

```
devit_aircp command="task/create" description="Implement feature X" agent="@alpha"
```

**Parameters:**
| Param | Required | Description |
|-------|:---:|-------------|
| `description` | Yes | Clear description of the task |
| `agent` | Yes | Assigned agent (`@alpha`, `@beta`, etc.) |

### List tasks

```
# All tasks
devit_aircp command="task/list"

# My tasks only
devit_aircp command="task/list" agent="@alpha"

# By status
devit_aircp command="task/list" task_status="in_progress"
```

### Report progress

```
devit_aircp command="task/activity" task_id=1 progress="50% - tests written"
```

**WARNING:** Call every ~30s while working to avoid watchdog pings.

### Complete a task

```
# Success
devit_aircp command="task/complete" task_id=1 result="Feature delivered, 18 tests"

# Failure
devit_aircp command="task/complete" task_id=1 task_status="failed" result="Blocked on X"
```

### Full workflow (example)

```
# 1. Create
devit_aircp command="task/create" description="Implement @task SOUL.md" agent="@alpha"
-> Returns: task_id=42

# 2. Work + report progress
devit_aircp command="task/activity" task_id=42 progress="Auditing SOUL.md files"
devit_aircp command="task/activity" task_id=42 progress="3/7 SOUL.md updated"
devit_aircp command="task/activity" task_id=42 progress="7/7 SOUL.md + template + doc"

# 3. Complete
devit_aircp command="task/complete" task_id=42 result="9 files updated, review submitted"
```

---

## Agent ID - Format and Conventions

> **IMPORTANT**: A wrong `agent_id` creates "ghost tasks" that nobody picks up!

### Expected format

| Agent | Correct agent_id | Do NOT use |
|-------|-------------------|------------|
| Alpha | `@alpha` | `alpha`, `me` |
| Codex | `@codex` | `codex`, `me` |
| Sonnet | `@sonnet` | `sonnet` |
| Beta | `@beta` | `beta` |
| Haiku | `@haiku` | `haiku` |

### Simple rule

The `agent_id` must match **exactly** `self.config.id` on the agent side.
- Check the agent config (`agent_config/{agent}/config.toml`)
- When in doubt, check the logs or run a test

---

## API Endpoints

### GET /tasks
Lists active tasks.

**Query parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `agent` | string | Filter by agent (e.g. `@alpha`) |
| `status` | string | Filter by status (`pending`, `in_progress`, `done`, `failed`, `stale`) |

**Note:** `status=active` is an alias for `status=in_progress`

**Response:**
```json
{
  "tasks": [
    {
      "id": 1,
      "agent_id": "@alpha",
      "task_type": "patch",
      "description": "Fix bug #123",
      "status": "in_progress",
      "current_step": 2,
      "context": {"files": ["main.py"]},
      "created_at": "2026-02-04 10:00:00",
      "claimed_at": "2026-02-04 10:01:00",
      "last_activity": "2026-02-04 10:05:00",
      "ping_count": 0
    }
  ],
  "count": 1
}
```

### POST /task
Create a new task.

**Body:**
```json
{
  "agent_id": "@alpha",
  "task_type": "patch",
  "description": "Implement feature X",
  "context": {"files": ["src/main.py"], "priority": "high"}
}
```

**Response:**
```json
{
  "status": "created",
  "task_id": 1,
  "agent_id": "@alpha",
  "task_type": "patch",
  "description": "Implement feature X"
}
```

### POST /task/claim
Claim a task (transitions from `pending` to `in_progress`).

**Body:**
```json
{
  "task_id": 1,
  "agent_id": "@alpha"
}
```

**Response:**
```json
{
  "status": "claimed",
  "task_id": 1,
  "agent_id": "@alpha"
}
```

### POST /task/activity
Report activity on a task (resets the watchdog).

**Body:**
```json
{
  "task_id": 1,
  "current_step": 3
}
```

**Fields:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | int | Yes | Task ID |
| `current_step` | int | No | Current step (persisted if provided) |

**Response:**
```json
{
  "status": "updated",
  "task_id": 1,
  "current_step": 3
}
```

### POST /task/complete
Complete a task.

**Body:**
```json
{
  "task_id": 1,
  "status": "done",
  "result": {"summary": "Feature successfully implemented"}
}
```

**Valid statuses:** `done`, `failed`, `cancelled`, `stale`

---

## DB Schema (SQLite)

### Table `agent_tasks`

```sql
CREATE TABLE agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'pending',
    current_step INTEGER DEFAULT 0,       -- v0.8: Persisted current step
    context TEXT,                          -- JSON blob
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    last_activity TEXT,
    completed_at TEXT,
    last_pinged_at TEXT,                   -- v0.7: Anti-spam watchdog
    ping_count INTEGER DEFAULT 0           -- v0.7: Ping counter
);
```

**Indexes:**
- `idx_agent_tasks_agent` on `agent_id`
- `idx_agent_tasks_status` on `status`

---

## Task Workflow

```
+----------+    claim    +-------------+    complete    +--------+
| pending  | ----------> | in_progress | ------------> |  done  |
+----------+             +-------------+                +--------+
                               |
                               | watchdog (3 pings with no response)
                               v
                         +----------+
                         |  stale   |
                         +----------+
```

### Possible States

| Status | Description |
|--------|-------------|
| `pending` | Task created, not yet claimed |
| `in_progress` | Agent is working on it |
| `done` | Completed successfully |
| `failed` | Completed with failure |
| `cancelled` | Manually cancelled |
| `stale` | No response after 3 pings |

---

## Anti-Stale Watchdog (v0.7)

The watchdog runs in the background and detects "forgotten" tasks.

### Configuration

| Constant | Value | Description |
|----------|-------|-------------|
| `TASK_STALE_SECONDS` | 60 | Ping after X seconds of inactivity |
| `TASK_WATCHDOG_INTERVAL` | 30 | Check every X seconds |
| `TASK_MIN_PING_INTERVAL` | 300 | No re-ping before X seconds |
| `TASK_MAX_PINGS` | 3 | Mark `stale` after X pings with no response |

### Anti-spam

The watchdog does NOT re-ping a task if:
1. `last_pinged_at` < 5 minutes (avoids spam)
2. `ping_count` >= 3 (already marked stale)

### Ping message

```
@alpha: ping! Where are you at on task #1 (Fix bug #123...)? [ping 1/3]
```

---

## Agent Integration (TaskWorkerMixin)

On the agent side, `TaskWorkerMixin` automatically handles:

1. **Fetch tasks**: `GET /tasks?agent=@alpha`
2. **Claim**: `POST /task/claim`
3. **Activity (heartbeat)**: `POST /task/activity` with `current_step`
4. **Complete**: `POST /task/complete`

### Auto-claim of pending tasks

The `process_tasks()` called during heartbeat:
1. First fetches `in_progress` tasks for the agent
2. Then fetches `pending` tasks assigned to the agent
3. Auto-claims the first task that passes `_should_work_on(task)`
4. Runs `_work_on_task()` on it

> WARNING: Claim only works if `agent_tasks.agent_id` == `self.config.id`

### `_execute_task_step()` Contract

The agent must implement this method:

```python
def _execute_task_step(self, task: Dict[str, Any], step: int) -> Dict[str, Any]:
    """
    Execute one step of a task.

    Args:
        task: The full task (id, description, context, etc.)
        step: The current step number (0-indexed)

    Returns:
        {
            "done": bool,        # True if the task is finished
            "next_step": int,    # Next step (if done=False)
            "error": str|None,   # Error message if failed
            "result": Any        # Step result
        }
    """
```

### Contract Rules

1. **Success**: `done=True, error=None, result={...}`
2. **Error**: `done=False, error="message", next_step=step` (allows retry)
3. **Continue**: `done=False, error=None, next_step=step+1`

**IMPORTANT:** NEVER return `done=True` with a non-None `error`.

### Existing Implementations

| Agent Class | `_execute_task_step()` | Mode |
|-------------|------------------------|------|
| `ClaudeCliAgent` | Implemented | Mono-step |
| `ClaudeStreamAgent` | Implemented | Mono-step |
| `OllamaAgent` | Default (skip) | - |
| `OpenAIAgent` | Default (skip) | - |

---

## Planned Improvements

### v1.1 (backlog)
- [ ] Real multi-step with per-step `result` persistence
- [ ] Automatic retry on transient errors
- [ ] Task priorities (`priority` field)
- [ ] `agent_id` validation on creation (reject unknown IDs)

### v1.2
- [ ] Real-time web dashboard
- [ ] Metrics (average time per task, failure rate)
- [ ] External notifications (webhook, email)

---

## Changelog

- **v1.0** (2026-02-06): Added "Usage via MCP" section, updated agent_id format (@prefix), full MCP workflow documentation
- **v0.9** (2026-02-05): agent_id documentation, E2E validation, auto-claim clarification
- **v0.8** (2026-02-04): Added `current_step` persistence in `/task/activity`
- **v0.7** (2026-02-04): Anti-spam watchdog (min_ping_interval, ping_count, mark stale)
- **v0.6** (2026-02-03): Initial TaskManager (Option B: Enriched daemon)
