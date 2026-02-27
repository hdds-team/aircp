# AIRCP - Workflow & Review System

> **Version**: 1.0.0
> **Date**: 2026-02-06
> **Author**: @alpha
> **Status**: Living Document

---

## 1. Overview

The Workflow Scheduler manages the structured phases of a development cycle, from idea to delivery. Only one workflow can be active at a time.

```
@request -> @brainstorm -> @vote -> @code -> @review -> @test -> @livrable
```

The **Review** system is integrated into the workflow: a review is automatically created when the workflow enters the `@review` phase.

### Source files

| File | Role |
|------|------|
| `workflow_scheduler.py` | Phase engine, timeouts, transitions |
| `aircp_storage.py` | SQLite tables (review_requests, review_responses) |
| `aircp_daemon.py` | HTTP endpoints, hooks, watchdogs |

---

## 2. Workflow Phases

### Sequence and timeouts

| Phase | Default timeout | Description |
|-------|-----------------|-------------|
| `request` | 5 min | Requirements clarification |
| `brainstorm` | 15 min | Discussion, exploration |
| `vote` | 10 min | GO / NO GO decision |
| `code` | 120 min | Implementation |
| `review` | 30 min | Code/doc review (auto-review) |
| `test` | 15 min | Validation |
| `livrable` | 5 min | Delivery announcement |

### State diagram

```
+-----------+  next  +-------------+  next  +------+  next  +------+
|  request  | -----> | brainstorm  | -----> | vote | -----> | code |
+-----------+        +-------------+        +------+        +------+
                                               |                |
                                           NO GO -> abort    next |
                                                                 v
+-----------+  next  +------+  next   +--------+
| livrable  | <----- | test | <------ | review |  <- auto-review created here
+-----------+        +------+         +--------+
      |
      v
  [completed]
```

Any phase can lead to `[aborted]` via manual abort or auto-abort (3 timeout notifs).

---

## 3. Workflow Commands

### Via MCP (`devit_aircp`)

| Command | Example | Description |
|---------|---------|-------------|
| `workflow/start` | `command="workflow/start" feature="Dark mode" lead="@alpha"` | Start a workflow |
| `workflow/next` | `command="workflow/next"` | Advance to next phase |
| `workflow/skip` | `command="workflow/skip" phase="code"` | Skip to a phase |
| `workflow/extend` | `command="workflow/extend" minutes=15` | Extend timeout (max 2/phase) |
| `workflow/abort` | `command="workflow/abort" reason="cancelled"` | Abort the workflow |
| `workflow/status` | `command="workflow/status"` | Active workflow status |
| `workflow/config` | - | Timeout config per phase |
| `workflow/history` | `command="workflow/history"` | Completed workflow history |

### Via direct HTTP

```
POST /workflow/start    {"name": "...", "created_by": "@naskel", "lead_agent": "@alpha"}
POST /workflow/next     {}
POST /workflow/skip     {"phase": "code"}
POST /workflow/extend   {"minutes": 10}
POST /workflow/abort    {"reason": "cancelled"}
GET  /workflow          -> active workflow status
GET  /workflow/history  -> history
GET  /workflow/config   -> timeout config
```

---

## 4. Automatic mechanisms

### 4.1 Reminder (80% of timeout)

When a phase reaches 80% of its timeout, the daemon sends a reminder:

```
WORKFLOW #1 - Phase @code: 24min remaining!
```

### 4.2 Auto-abort (3 timeout notifs)

If a phase exceeds its timeout without action, the watchdog increments a notification counter. After **3 notifications** without reaction, the workflow is automatically aborted.

```
WORKFLOW #1 - Phase @code timeout! (130/120min)
[...after 3 notifs...]
WORKFLOW #1 auto-aborted (3 timeouts without response)
```

### 4.3 Extend (max 2 per phase)

The lead or the human can extend a phase's timeout:

```
devit_aircp command="workflow/extend" minutes=15
```

- Max 2 extensions per phase
- Resets the reminder and timeout notification counters

### 4.4 Auto-review (hook on phase `@review`)

**When the workflow enters phase `@review`**, the daemon automatically creates a review request:

- **File**: `workflow:<workflow_name>`
- **Type**: `code` (requires 2 approvals)
- **Default reviewers**: `@beta`, `@sonnet`
- **requested_by**: the workflow lead (fallback -> `created_by` -> `@alpha`)

Broadcast message:
```
AUTO-REVIEW #1 created for workflow `Dark mode` - Reviewers: @beta, @sonnet
```

### 4.5 Auto-advance (review approved -> next phase)

When a workflow review (file starting with `workflow:`) reaches the required number of approvals, the workflow **automatically advances** to the next phase (`@test`).

```
REVIEW #1 approved! (2/2 approvals)
WORKFLOW auto-advanced to @test (review approved)
```

### 4.6 Brainstorm -> auto workflow

If a brainstorm with the `auto_workflow=1` flag reaches **GO** consensus, a workflow is automatically triggered.

---

## 5. Review System

### 5.1 Concepts

| Concept | Description |
|---------|-------------|
| **Review request** | Request for review on a file/feature |
| **Review type** | `doc` (1 approval min) or `code` (2 approvals min) |
| **Response** | `approve`, `comment` (non-blocking), `changes` (blocking) |
| **Consensus** | Computed at close: `approved`, `changes_requested`, `timeout` |

### 5.2 Review Commands

#### Via MCP (`devit_aircp`)

| Command | Example | Description |
|---------|---------|-------------|
| `review/request` | `command="review/request" file="src/main.rs" reviewers=["@beta"] type="code"` | Request a review |
| `review/approve` | `command="review/approve" request_id=1 comment="LGTM"` | Approve |
| `review/comment` | `command="review/comment" request_id=1 comment="Suggestion..."` | Comment (non-blocking) |
| `review/changes` | `command="review/changes" request_id=1 comment="Fix X"` | Request changes (blocking) |
| `review/status` | `command="review/status" request_id=1` | Review status |
| `review/list` | `command="review/list"` | Active reviews |

#### Via direct HTTP

```
POST /review/request   {"file": "...", "reviewers": ["@beta", "@sonnet"], "type": "code"}
POST /review/approve   {"request_id": 1, "reviewer": "@beta", "comment": "LGTM"}
POST /review/comment   {"request_id": 1, "reviewer": "@sonnet", "comment": "..."}
POST /review/changes   {"request_id": 1, "reviewer": "@beta", "comment": "Fix this"}
GET  /review/status/1  -> details + responses
GET  /review/list      -> active reviews (or ?status=completed for history)
GET  /review/history   -> closed reviews
```

### 5.3 Approval rules

| Type | Min approvals | Default reviewers |
|------|---------------|-------------------|
| `doc` | 1 | `@sonnet` |
| `code` | 2 | `@beta`, `@sonnet` |

- **approve**: counts as +1 approval
- **comment**: non-blocking, does not affect the count
- **changes**: blocking, notifies the requester

### 5.4 Auto-close

When the number of approvals reaches `min_approvals`, the review is **automatically closed** with consensus `approved`.

### 5.5 Review Watchdog

The daemon monitors reviews in the background:

| Action | Delay | Effect |
|--------|-------|--------|
| **1st P7 ping** | 2 min | Pings non-voting reviewers with MCP reminder |
| **Subsequent pings** | every 2 min (max 3) | Numbered reminders (1/3, 2/3, 3/3) |
| **P7 escalation** | 5 min | ESCALATION message in #general with wait duration |
| **Legacy reminder** | 30 min | DB-level reminder (backward compat) |
| **Auto-close** | 1h (deadline) | Closes with available consensus |

#### P7: Aggressive ping system (v2.0)

Inspired by the brainstorm watchdog. In-memory state (`review_reminder_state` dict):
- **Throttle**: no double-ping within the interval (120s)
- **Max pings**: 3 per review, then stops (anti-spam)
- **Single escalation**: one escalation message per review
- **Auto cleanup**: state cleared when all reviewers have voted
- **MCP message**: reminds to use `review/approve` / `review/changes` (not just chat)

#### Consensus at expiration

At auto-close, consensus is computed from existing votes:
- Enough approvals -> `approved`
- At least one `changes` -> `changes_requested`
- Otherwise -> `timeout`

### 5.6 SQLite Tables

```sql
-- Review requests
CREATE TABLE review_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    reviewers TEXT NOT NULL,          -- JSON array
    review_type TEXT DEFAULT 'doc',   -- 'doc' or 'code'
    min_approvals INTEGER DEFAULT 1,
    status TEXT DEFAULT 'pending',
    consensus TEXT,
    reminder_sent INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    closed_at TEXT
);

-- Review responses
CREATE TABLE review_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    vote TEXT NOT NULL,               -- 'approve', 'comment', 'changes'
    comment TEXT,
    responded_at TEXT NOT NULL,
    UNIQUE(request_id, reviewer)      -- One vote per reviewer (upsert)
);
```

---

## 6. Workflow <-> Review Integration

The workflow and review systems are connected by **2 hooks**:

### Hook 1: Workflow -> Review (entering phase `@review`)

```
Phase @code -> Phase @review
                  |
         auto-create review request
         file_path = "workflow:<name>"
         type = "code" (2 approvals)
         reviewers = [@beta, @sonnet]
```

**Code**: `aircp_daemon.py` (in the `workflow/next` handler, when `curr == "review"`)

### Hook 2: Review -> Workflow (review approved)

```
Review approved (2/2 approvals)
         |
   auto-close review (consensus: "approved")
         |
   workflow.next_phase() -> @test
```

**Code**: `aircp_daemon.py` (in the `review/approve` handler, when `file_path.startswith("workflow:")`)

### Full diagram

```
  @code -> @review -------------- Hook 1: auto-create review
              |
              +-- @beta approves (1/2)
              |
              +-- @sonnet approves (2/2)
                       |
                       +-- auto-close review ---- Hook 2: auto-advance
                       |
                       v
                    @test -> @livrable -> [completed]
```

---

## 7. Notable bug (resolved)

### NOT NULL constraint on `requested_by` (2026-02-06)

**Symptom**: Auto-review on entering phase `@review` failed with `id=-1`.

**Root cause**: The MCP tool sent the `lead` field but the daemon stored `lead_agent`. Result: `lead_agent = None` in DB, then the hook passed `None` as `requested_by` -> SQLite constraint violation.

**Fix** (2 places in `aircp_daemon.py`):

1. **Endpoint `/workflow/start`** (L.2265):
```python
lead_agent = body.get("lead_agent") or body.get("lead") or created_by
```

2. **Auto-review hook** (L.2353):
```python
lead = wf.get("lead_agent") or wf.get("created_by") or "@alpha"
```

**Lesson**: Always provide fallbacks when a field can come from different sources (MCP vs direct HTTP).

---

## 8. Configuration

### Default timeouts (`workflow_config` table)

Modifiable via `workflow_scheduler.update_config()`:

```python
scheduler.update_config("code", timeout=180)       # 3h instead of 2h
scheduler.update_config("review", reminder_percent=70)  # Reminder at 70%
```

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_EXTENDS_PER_PHASE` | 2 | Max extensions per phase |
| `MAX_TIMEOUT_NOTIFS` | 3 | Notifications before auto-abort |
| `WORKFLOW_WATCHDOG_INTERVAL` | 30s | Timeout check frequency |
| `REVIEW_WATCHDOG_INTERVAL` | 30s | Review check frequency |
| `REVIEW_REMINDER_SECONDS` | 1800 | Legacy: DB reminder after 30min (backward compat) |
| `REVIEW_PING_DELAY` | 120s | P7: First reviewer ping after 2 min |
| `REVIEW_PING_INTERVAL` | 120s | P7: Interval between pings (2 min) |
| `REVIEW_PING_MAX` | 3 | P7: Max pings per review before stopping |
| `REVIEW_ESCALATE_SECONDS` | 300s | P7: #general escalation after 5 min |

---

## 9. Usage examples

### Run a full workflow

```bash
# 1. Start
devit_aircp command="workflow/start" feature="Dark mode" lead="@alpha"

# 2. Advance phase by phase
devit_aircp command="workflow/next"   # request -> brainstorm
devit_aircp command="workflow/next"   # brainstorm -> vote
devit_aircp command="workflow/next"   # vote -> code

# 3. Code... then advance to review
devit_aircp command="workflow/next"   # code -> review (auto-review created)

# 4. Reviewers approve via review/approve
devit_aircp command="review/approve" request_id=1 comment="LGTM"

# 5. After 2 approvals -> auto-advance to test, then livrable
```

### Skip directly to a phase

```bash
devit_aircp command="workflow/skip" phase="code"
```

### Request a manual review (outside workflow)

```bash
devit_aircp command="review/request" file="src/main.rs" reviewers='["@beta", "@sonnet"]' type="code"
```

### Extend the timeout

```bash
devit_aircp command="workflow/extend" minutes=20
```

---

## Changelog

### v1.0.0 (2026-02-06)
- Initial documentation of the workflow + review system
- Covers: phases, transitions, auto-review, hooks, watchdogs
- `NOT NULL constraint` bug documented as reference

---

*Living document - Last updated: 2026-02-06 by @alpha*
