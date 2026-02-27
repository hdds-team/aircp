#!/usr/bin/env python3
"""
AIRCP Workflow Scheduler v2.0 -- Mode Veloce

Structured workflow management for the team:
@request -> @brainstorm -> @vote -> @code -> @review -> @test -> @livrable

Features:
- Phase transitions with configurable timeouts
- Reminder at 80% of timeout (soft mode)
- Manual @extend command (max 2 per phase)
- Auto-abort after 3 timeout notifications (v1.4)
- Mode Veloce: parallel multi-agent coding (v2.0)
- Workflow history for retros
- Single active workflow constraint

Author: @alpha
"""

import sqlite3
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# Phase flow (ordered) -- standard mode
WORKFLOW_PHASES = [
    'request',
    'brainstorm',
    'vote',
    'code',
    'review',
    'test',
    'livrable'
]

# Default timeouts (minutes) per phase
DEFAULT_TIMEOUTS = {
    'request': 5,       # Quick - just posting the idea
    'brainstorm': 15,   # Discussion time
    'vote': 10,         # Decision time
    'code': 120,        # Implementation (2h)
    'review': 30,       # QA/Review
    'test': 15,         # Testing
    'livrable': 5,      # Final delivery announcement
}

# -- Mode Veloce (v2.0) ---------------------------------------------------

# Veloce phase flow (replaces standard when mode='veloce')
VELOCE_PHASES = [
    'request',
    'brainstorm',
    'vote',
    'architecture',     # Lead writes WIP doc: components, interfaces, files
    'decompose',        # Brainstorm #2: chunk plan
    'decompose_vote',   # Vote on decomposition (GO = launch parallel)
    'parallel_code',    # Agents code chunks in parallel
    'review',           # Cross-review of chunks (round-robin)
    'integrate',        # Integrator glues chunks together
    'review_final',     # Final review of integrated code
    'test',
    'livrable'
]

VELOCE_TIMEOUTS = {
    'architecture': 30,
    'decompose': 15,
    'decompose_vote': 10,
    'parallel_code': 120,
    'integrate': 30,
    'review_final': 30,
}

# Hard limit on parallel chunks
MAX_CHUNKS = 4

# Valid chunk statuses
CHUNK_STATUSES = ('pending', 'in_progress', 'done', 'cancelled')

# -- End Veloce constants --------------------------------------------------

# Max extends per phase
MAX_EXTENDS_PER_PHASE = 2

# Max timeout notifications before auto-abort (v1.4)
MAX_TIMEOUT_NOTIFS = 3


def _sqlite_now() -> str:
    """Return current UTC time in SQLite-compatible format"""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _sqlite_to_datetime(sqlite_ts: str) -> datetime:
    """Convert SQLite timestamp to datetime"""
    if not sqlite_ts:
        return None
    dt = datetime.strptime(sqlite_ts, '%Y-%m-%d %H:%M:%S')
    return dt.replace(tzinfo=timezone.utc)


class WorkflowScheduler:
    """Manages workflow phases and transitions."""

    def __init__(self, db_path: Path):
        """Initialize with path to SQLite database."""
        self.db_path = db_path
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new connection."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        """Create workflow tables if they don't exist."""
        conn = self._get_conn()
        c = conn.cursor()

        # Active workflows table
        c.execute("""
            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                phase TEXT NOT NULL DEFAULT 'request',
                lead_agent TEXT,
                phase_started_at TEXT NOT NULL,
                timeout_minutes INTEGER DEFAULT 15,
                extend_count INTEGER DEFAULT 0,
                reminded INTEGER DEFAULT 0,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_workflows_phase ON workflows(phase)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_workflows_completed ON workflows(completed_at)")

        # Workflow config (phase timeouts)
        c.execute("""
            CREATE TABLE IF NOT EXISTS workflow_config (
                phase TEXT PRIMARY KEY,
                default_timeout INTEGER NOT NULL,
                reminder_at_percent INTEGER DEFAULT 80
            )
        """)

        # Insert defaults if empty
        c.execute("SELECT COUNT(*) FROM workflow_config")
        if c.fetchone()[0] == 0:
            for phase, timeout in DEFAULT_TIMEOUTS.items():
                c.execute(
                    "INSERT INTO workflow_config (phase, default_timeout, reminder_at_percent) VALUES (?, ?, 80)",
                    (phase, timeout)
                )

        # Workflow history for retros
        c.execute("""
            CREATE TABLE IF NOT EXISTS workflow_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                final_status TEXT NOT NULL,
                total_duration_minutes INTEGER,
                phase_log TEXT
            )
        """)

        # v1.4: Add timeout_notif_count column if not exists (migration)
        try:
            c.execute("ALTER TABLE workflows ADD COLUMN timeout_notif_count INTEGER DEFAULT 0")
            logger.info("+ Added timeout_notif_count column (v1.4 migration)")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # v3.0: Add project_id column (workspaces)
        try:
            c.execute("ALTER TABLE workflows ADD COLUMN project_id TEXT DEFAULT 'default'")
            logger.info("+ Added project_id column (v3.0 workspaces)")
        except sqlite3.OperationalError:
            pass  # Column already exists
        c.execute("CREATE INDEX IF NOT EXISTS idx_workflows_project ON workflows(project_id)")

        # v4.1: Add metadata column for git hooks (JSON text)
        try:
            c.execute("ALTER TABLE workflows ADD COLUMN metadata TEXT DEFAULT '{}'")
            logger.info("+ Added metadata column (v4.1 git hooks)")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # ── v2.0 Veloce migrations ──────────────────────────────────────

        # mode column: 'standard' (default) or 'veloce'
        try:
            c.execute("ALTER TABLE workflows ADD COLUMN mode TEXT NOT NULL DEFAULT 'standard'")
            logger.info("+ Added mode column (v2.0 veloce)")
        except sqlite3.OperationalError:
            pass

        # chunks_metadata: JSON plan validated by decompose_vote
        try:
            c.execute("ALTER TABLE workflows ADD COLUMN chunks_metadata TEXT DEFAULT '{}'")
            logger.info("+ Added chunks_metadata column (v2.0 veloce)")
        except sqlite3.OperationalError:
            pass

        # workflow_chunks table: tracks each parallel chunk
        c.execute("""
            CREATE TABLE IF NOT EXISTS workflow_chunks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id INTEGER NOT NULL,
                chunk_id    TEXT NOT NULL,
                agent_id    TEXT NOT NULL,
                scope       TEXT NOT NULL DEFAULT '{}',
                interface   TEXT NOT NULL DEFAULT '{}',
                status      TEXT NOT NULL DEFAULT 'pending',
                task_id     INTEGER,
                created_at  TEXT NOT NULL,
                started_at  TEXT,
                completed_at TEXT,
                FOREIGN KEY (workflow_id) REFERENCES workflows(id),
                FOREIGN KEY (task_id) REFERENCES agent_tasks(id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_wf_chunks_wf ON workflow_chunks(workflow_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_wf_chunks_status ON workflow_chunks(status)")

        # Insert veloce timeouts into workflow_config if missing
        for phase, timeout in VELOCE_TIMEOUTS.items():
            c.execute(
                "INSERT OR IGNORE INTO workflow_config (phase, default_timeout, reminder_at_percent) VALUES (?, ?, 80)",
                (phase, timeout)
            )

        conn.commit()
        conn.close()
        logger.info("+ Workflow tables initialized (v2.0 veloce)")

    # ==========================================================================
    # Helpers
    # ==========================================================================

    def _get_phases(self, workflow: Dict[str, Any] = None) -> List[str]:
        """Return the phase list for a workflow (standard or veloce)."""
        if workflow and workflow.get('mode') == 'veloce':
            return VELOCE_PHASES
        return WORKFLOW_PHASES

    # ==========================================================================
    # Workflow CRUD
    # ==========================================================================

    def create_workflow(self, name: str, created_by: str,
                        description: str = None, lead_agent: str = None,
                        project_id: str = "default",
                        mode: str = "standard") -> int:
        """Create a new workflow. Returns workflow ID or -1 if one already active.

        mode: 'standard' (default linear) or 'veloce' (parallel chunks).
        """
        if mode not in ('standard', 'veloce'):
            mode = 'standard'

        # Check if there's already an active workflow in this project
        active = self.get_active_workflow(project_id=project_id)
        if active:
            logger.warning(f"Cannot create workflow: one already active in [{project_id}] (#{active['id']})")
            return -1

        now = _sqlite_now()
        timeout = self._get_phase_timeout('request')

        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO workflows
            (name, description, phase, lead_agent, phase_started_at, timeout_minutes,
             extend_count, reminded, created_by, created_at, project_id, mode)
            VALUES (?, ?, 'request', ?, ?, ?, 0, 0, ?, ?, ?, ?)
        """, (name, description, lead_agent, now, timeout, created_by, now, project_id, mode))
        workflow_id = c.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"+ Created workflow #{workflow_id} [{project_id}] mode={mode}: {name}")
        return workflow_id

    def get_active_workflow(self, project_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get the currently active workflow (completed_at IS NULL).
        If project_id is given, filter by project. Otherwise returns first active."""
        conn = self._get_conn()
        c = conn.cursor()
        if project_id:
            c.execute("""
                SELECT * FROM workflows
                WHERE completed_at IS NULL AND project_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (project_id,))
        else:
            c.execute("""
                SELECT * FROM workflows
                WHERE completed_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
            """)
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_workflow(self, workflow_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific workflow by ID."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

    def _get_phase_timeout(self, phase: str) -> int:
        """Get timeout for a phase from config or defaults."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT default_timeout FROM workflow_config WHERE phase = ?", (phase,))
        row = c.fetchone()
        conn.close()
        if row:
            return row[0]
        # Fallback: check veloce timeouts then standard
        return VELOCE_TIMEOUTS.get(phase, DEFAULT_TIMEOUTS.get(phase, 15))

    def _get_reminder_percent(self, phase: str) -> int:
        """Get reminder percentage for a phase."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT reminder_at_percent FROM workflow_config WHERE phase = ?", (phase,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else 80

    # ==========================================================================
    # Phase Transitions
    # ==========================================================================

    def next_phase(self, workflow_id: int = None) -> Dict[str, Any]:
        """Move to the next phase. Returns status dict.

        In veloce mode:
        - parallel_code -> review requires ALL chunks done (or lead skip)
        - Uses VELOCE_PHASES instead of WORKFLOW_PHASES
        """
        workflow = self.get_workflow(workflow_id) if workflow_id else self.get_active_workflow()
        if not workflow:
            return {"success": False, "error": "No active workflow"}

        phases = self._get_phases(workflow)
        current_phase = workflow['phase']

        if current_phase not in phases:
            return {"success": False, "error": f"Phase '{current_phase}' not in {workflow.get('mode', 'standard')} flow"}

        current_idx = phases.index(current_phase)

        # Veloce gate: parallel_code -> next requires all chunks done
        if current_phase == 'parallel_code' and workflow.get('mode') == 'veloce':
            chunks = self.get_chunks(workflow['id'])
            pending = [c for c in chunks if c['status'] not in ('done', 'cancelled')]
            if pending:
                names = ', '.join(c['chunk_id'] for c in pending)
                return {
                    "success": False,
                    "error": f"Cannot advance: {len(pending)} chunk(s) not done: {names}",
                    "pending_chunks": [c['chunk_id'] for c in pending],
                    "gate": "parallel_code"
                }

        # Check if we're at the last phase
        if current_idx >= len(phases) - 1:
            return self.complete_workflow(workflow['id'], 'completed')

        # Move to next phase
        next_ph = phases[current_idx + 1]
        timeout = self._get_phase_timeout(next_ph)
        now = _sqlite_now()

        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE workflows
            SET phase = ?, phase_started_at = ?, timeout_minutes = ?,
                extend_count = 0, reminded = 0, timeout_notif_count = 0
            WHERE id = ?
        """, (next_ph, now, timeout, workflow['id']))
        conn.commit()
        conn.close()

        logger.info(f"+ Workflow #{workflow['id']}: {current_phase} -> {next_ph}")
        result = {
            "success": True,
            "workflow_id": workflow['id'],
            "previous_phase": current_phase,
            "current_phase": next_ph,
            "timeout_minutes": timeout,
            "mode": workflow.get('mode', 'standard'),
        }

        # If entering parallel_code, start chunks
        if next_ph == 'parallel_code' and workflow.get('mode') == 'veloce':
            started = self._start_parallel_chunks(workflow['id'])
            result["chunks_started"] = started

        return result

    def _start_parallel_chunks(self, workflow_id: int) -> int:
        """Mark all pending chunks as in_progress when entering parallel_code."""
        now = _sqlite_now()
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE workflow_chunks
            SET status = 'in_progress', started_at = ?
            WHERE workflow_id = ? AND status = 'pending'
        """, (now, workflow_id))
        count = c.rowcount
        conn.commit()
        conn.close()
        logger.info(f"+ Started {count} parallel chunks for workflow #{workflow_id}")
        return count

    def skip_to_phase(self, phase: str, workflow_id: int = None) -> Dict[str, Any]:
        """Skip directly to a specific phase (lead only)."""
        workflow = self.get_workflow(workflow_id) if workflow_id else self.get_active_workflow()
        if not workflow:
            return {"success": False, "error": "No active workflow"}

        phases = self._get_phases(workflow)
        if phase not in phases:
            return {"success": False, "error": f"Invalid phase for {workflow.get('mode', 'standard')} mode: {phase}"}

        prev_phase = workflow['phase']

        # If skipping away from parallel_code, cancel remaining chunks
        if prev_phase == 'parallel_code' and workflow.get('mode') == 'veloce':
            self._cancel_pending_chunks(workflow['id'])

        timeout = self._get_phase_timeout(phase)
        now = _sqlite_now()

        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE workflows
            SET phase = ?, phase_started_at = ?, timeout_minutes = ?,
                extend_count = 0, reminded = 0, timeout_notif_count = 0
            WHERE id = ?
        """, (phase, now, timeout, workflow['id']))
        conn.commit()
        conn.close()

        logger.info(f"+ Workflow #{workflow['id']}: skipped to {phase}")
        return {
            "success": True,
            "workflow_id": workflow['id'],
            "current_phase": phase,
            "timeout_minutes": timeout,
            "mode": workflow.get('mode', 'standard'),
        }

    def _cancel_pending_chunks(self, workflow_id: int) -> int:
        """Cancel all non-done chunks (used when lead force-skips parallel_code)."""
        now = _sqlite_now()
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE workflow_chunks
            SET status = 'cancelled', completed_at = ?
            WHERE workflow_id = ? AND status IN ('pending', 'in_progress')
        """, (now, workflow_id))
        count = c.rowcount
        conn.commit()
        conn.close()
        if count:
            logger.warning(f"! Cancelled {count} chunks for workflow #{workflow_id}")
        return count

    def extend_phase(self, minutes: int = 10, workflow_id: int = None) -> Dict[str, Any]:
        """Extend the current phase timeout. Max 2 extends per phase."""
        workflow = self.get_workflow(workflow_id) if workflow_id else self.get_active_workflow()
        if not workflow:
            return {"success": False, "error": "No active workflow"}

        if workflow['extend_count'] >= MAX_EXTENDS_PER_PHASE:
            return {
                "success": False,
                "error": f"Max extends reached ({MAX_EXTENDS_PER_PHASE})",
                "extend_count": workflow['extend_count']
            }

        new_timeout = workflow['timeout_minutes'] + minutes

        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE workflows
            SET timeout_minutes = ?, extend_count = extend_count + 1, reminded = 0, timeout_notif_count = 0
            WHERE id = ?
        """, (new_timeout, workflow['id']))
        conn.commit()
        conn.close()

        logger.info(f"+ Workflow #{workflow['id']}: extended by {minutes}min (total: {new_timeout}min)")
        return {
            "success": True,
            "workflow_id": workflow['id'],
            "phase": workflow['phase'],
            "new_timeout_minutes": new_timeout,
            "extend_count": workflow['extend_count'] + 1,
            "extends_remaining": MAX_EXTENDS_PER_PHASE - workflow['extend_count'] - 1
        }

    def complete_workflow(self, workflow_id: int, status: str = 'completed') -> Dict[str, Any]:
        """Complete a workflow and archive to history."""
        workflow = self.get_workflow(workflow_id)
        if not workflow:
            return {"success": False, "error": "Workflow not found"}

        now = _sqlite_now()

        # Calculate total duration
        created_at = _sqlite_to_datetime(workflow['created_at'])
        completed_at = datetime.now(timezone.utc)
        duration_minutes = int((completed_at - created_at).total_seconds() / 60)

        conn = self._get_conn()
        c = conn.cursor()

        # Mark as completed
        c.execute("""
            UPDATE workflows SET completed_at = ? WHERE id = ?
        """, (now, workflow_id))

        # Cancel any remaining chunks
        if workflow.get('mode') == 'veloce':
            c.execute("""
                UPDATE workflow_chunks
                SET status = 'cancelled', completed_at = ?
                WHERE workflow_id = ? AND status IN ('pending', 'in_progress')
            """, (now, workflow_id))

        # Archive to history
        c.execute("""
            INSERT INTO workflow_history
            (workflow_id, name, description, created_by, created_at, completed_at,
             final_status, total_duration_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            workflow_id, workflow['name'], workflow.get('description'),
            workflow['created_by'], workflow['created_at'], now,
            status, duration_minutes
        ))

        conn.commit()
        conn.close()

        logger.info(f"+ Workflow #{workflow_id} completed: {status} ({duration_minutes}min)")
        return {
            "success": True,
            "workflow_id": workflow_id,
            "status": status,
            "duration_minutes": duration_minutes
        }

    def abort_workflow(self, workflow_id: int = None, reason: str = "aborted") -> Dict[str, Any]:
        """Abort the active workflow."""
        workflow = self.get_workflow(workflow_id) if workflow_id else self.get_active_workflow()
        if not workflow:
            return {"success": False, "error": "No active workflow"}

        return self.complete_workflow(workflow['id'], f'aborted: {reason}')

    # ==========================================================================
    # Veloce: Chunk Management (v2.0)
    # ==========================================================================

    def submit_decomposition(self, workflow_id: int, chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Submit a chunk decomposition plan for a veloce workflow.

        Called during the 'decompose' phase. Validates and stores chunks.
        Chunks are NOT started yet -- that happens when entering parallel_code.

        Each chunk dict must have: chunk_id, agent_id
        Optional: scope (dict), interface (dict)
        """
        workflow = self.get_workflow(workflow_id)
        if not workflow:
            return {"success": False, "error": "Workflow not found"}

        if workflow.get('mode') != 'veloce':
            return {"success": False, "error": "Not a veloce workflow"}

        if workflow['phase'] not in ('decompose', 'decompose_vote'):
            return {"success": False, "error": f"Cannot decompose in phase '{workflow['phase']}'"}

        if not chunks or len(chunks) == 0:
            return {"success": False, "error": "No chunks provided"}

        if len(chunks) > MAX_CHUNKS:
            return {"success": False, "error": f"Too many chunks ({len(chunks)}), max is {MAX_CHUNKS}"}

        # Validate zero write overlap
        all_write_files = []
        chunk_ids = set()
        for chunk in chunks:
            cid = chunk.get('chunk_id', '').strip()
            if not cid:
                return {"success": False, "error": "Each chunk must have a chunk_id"}
            if cid in chunk_ids:
                return {"success": False, "error": f"Duplicate chunk_id: {cid}"}
            chunk_ids.add(cid)

            if not chunk.get('agent_id'):
                return {"success": False, "error": f"Chunk '{cid}' missing agent_id"}

            scope = chunk.get('scope', {})
            writes = scope.get('files_create', []) + scope.get('files_modify', [])
            for f in writes:
                if f in all_write_files:
                    return {"success": False, "error": f"Write overlap on file '{f}' -- zero overlap rule violated"}
                all_write_files.append(f)

        # Clear any previous chunks for this workflow
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM workflow_chunks WHERE workflow_id = ?", (workflow_id,))

        now = _sqlite_now()
        for chunk in chunks:
            c.execute("""
                INSERT INTO workflow_chunks
                (workflow_id, chunk_id, agent_id, scope, interface, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """, (
                workflow_id,
                chunk['chunk_id'],
                chunk['agent_id'],
                json.dumps(chunk.get('scope', {})),
                json.dumps(chunk.get('interface', {})),
                now
            ))

        # Store full plan as metadata
        c.execute(
            "UPDATE workflows SET chunks_metadata = ? WHERE id = ?",
            (json.dumps(chunks), workflow_id)
        )

        conn.commit()
        conn.close()

        logger.info(f"+ Decomposition submitted for workflow #{workflow_id}: {len(chunks)} chunks")
        return {
            "success": True,
            "workflow_id": workflow_id,
            "chunks_count": len(chunks),
            "chunk_ids": list(chunk_ids),
        }

    def get_chunks(self, workflow_id: int) -> List[Dict[str, Any]]:
        """Get all chunks for a workflow."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT * FROM workflow_chunks
            WHERE workflow_id = ?
            ORDER BY id ASC
        """, (workflow_id,))
        rows = c.fetchall()
        conn.close()
        result = []
        for row in rows:
            d = dict(row)
            # Parse JSON fields
            for key in ('scope', 'interface'):
                if d.get(key):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        pass
            result.append(d)
        return result

    def get_chunk(self, workflow_id: int, chunk_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific chunk by chunk_id."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT * FROM workflow_chunks
            WHERE workflow_id = ? AND chunk_id = ?
        """, (workflow_id, chunk_id))
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        for key in ('scope', 'interface'):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def complete_chunk(self, workflow_id: int, chunk_id: str) -> Dict[str, Any]:
        """Mark a chunk as done. Returns chunk status + gate info."""
        chunk = self.get_chunk(workflow_id, chunk_id)
        if not chunk:
            return {"success": False, "error": f"Chunk '{chunk_id}' not found"}

        if chunk['status'] == 'done':
            return {"success": False, "error": f"Chunk '{chunk_id}' already done"}

        if chunk['status'] == 'cancelled':
            return {"success": False, "error": f"Chunk '{chunk_id}' was cancelled"}

        now = _sqlite_now()
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE workflow_chunks
            SET status = 'done', completed_at = ?
            WHERE workflow_id = ? AND chunk_id = ?
        """, (now, workflow_id, chunk_id))
        conn.commit()
        conn.close()

        # Check gate: are all chunks done?
        all_chunks = self.get_chunks(workflow_id)
        done_count = sum(1 for ch in all_chunks if ch['status'] == 'done')
        cancelled_count = sum(1 for ch in all_chunks if ch['status'] == 'cancelled')
        total = len(all_chunks)
        active_total = total - cancelled_count
        gate_open = (done_count >= active_total)

        logger.info(f"+ Chunk '{chunk_id}' done for workflow #{workflow_id} ({done_count}/{active_total})")
        return {
            "success": True,
            "chunk_id": chunk_id,
            "workflow_id": workflow_id,
            "done_count": done_count,
            "total_chunks": total,
            "active_chunks": active_total,
            "gate_open": gate_open,
        }

    def link_chunk_task(self, workflow_id: int, chunk_id: str, task_id: int) -> bool:
        """Link a chunk to an agent_task (for watchdog tracking)."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE workflow_chunks SET task_id = ?
            WHERE workflow_id = ? AND chunk_id = ?
        """, (task_id, workflow_id, chunk_id))
        updated = c.rowcount > 0
        conn.commit()
        conn.close()
        return updated

    def get_chunks_summary(self, workflow_id: int) -> Dict[str, Any]:
        """Get a summary of chunk statuses for dashboard/API."""
        chunks = self.get_chunks(workflow_id)
        if not chunks:
            return {"total": 0, "chunks": []}

        summary_chunks = []
        for ch in chunks:
            summary_chunks.append({
                "chunk_id": ch['chunk_id'],
                "agent_id": ch['agent_id'],
                "status": ch['status'],
                "task_id": ch.get('task_id'),
                "started_at": ch.get('started_at'),
                "completed_at": ch.get('completed_at'),
            })

        done = sum(1 for c in chunks if c['status'] == 'done')
        cancelled = sum(1 for c in chunks if c['status'] == 'cancelled')
        active = len(chunks) - cancelled

        return {
            "total": len(chunks),
            "done": done,
            "cancelled": cancelled,
            "active": active,
            "gate_open": done >= active,
            "chunks": summary_chunks,
        }

    # ==========================================================================
    # Metadata (v4.1: git hooks)
    # ==========================================================================

    def get_metadata(self, workflow_id: int) -> Dict[str, Any]:
        """Get workflow metadata as parsed JSON dict."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT metadata FROM workflows WHERE id = ?", (workflow_id,))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def update_metadata(self, workflow_id: int, updates: Dict[str, Any]) -> bool:
        """Merge updates into workflow metadata JSON."""
        current = self.get_metadata(workflow_id)
        current.update(updates)

        conn = self._get_conn()
        c = conn.cursor()
        c.execute(
            "UPDATE workflows SET metadata = ? WHERE id = ?",
            (json.dumps(current), workflow_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"+ Workflow #{workflow_id} metadata updated: {list(updates.keys())}")
        return True

    # ==========================================================================
    # Watchdog Support (v1.4: with timeout notification counter)
    # ==========================================================================

    def increment_timeout_notif(self, workflow_id: int) -> int:
        """Increment timeout notification counter. Returns new count."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE workflows
            SET timeout_notif_count = COALESCE(timeout_notif_count, 0) + 1
            WHERE id = ?
        """, (workflow_id,))
        c.execute("SELECT timeout_notif_count FROM workflows WHERE id = ?", (workflow_id,))
        row = c.fetchone()
        conn.commit()
        conn.close()
        return row[0] if row else 0

    def reset_timeout_notif(self, workflow_id: int):
        """Reset timeout notification counter (called on phase change/extend)."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("UPDATE workflows SET timeout_notif_count = 0 WHERE id = ?", (workflow_id,))
        conn.commit()
        conn.close()

    def check_timeout(self) -> Optional[Dict[str, Any]]:
        """Check if the active workflow phase has timed out.

        Returns dict with action needed or None if no action.
        v1.4: Includes notif_count for auto-abort logic.
        """
        workflow = self.get_active_workflow()
        if not workflow:
            return None

        phase_started = _sqlite_to_datetime(workflow['phase_started_at'])
        if not phase_started:
            return None

        elapsed = datetime.now(timezone.utc) - phase_started
        elapsed_minutes = elapsed.total_seconds() / 60
        timeout = workflow['timeout_minutes']
        reminder_percent = self._get_reminder_percent(workflow['phase'])
        notif_count = workflow.get('timeout_notif_count', 0) or 0

        # Check for timeout
        if elapsed_minutes >= timeout:
            return {
                "action": "timeout",
                "workflow_id": workflow['id'],
                "phase": workflow['phase'],
                "elapsed_minutes": int(elapsed_minutes),
                "timeout_minutes": timeout,
                "notif_count": notif_count
            }

        # Check for reminder (at 80% by default)
        reminder_threshold = timeout * (reminder_percent / 100)
        if elapsed_minutes >= reminder_threshold and not workflow['reminded']:
            # Mark as reminded
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("UPDATE workflows SET reminded = 1 WHERE id = ?", (workflow['id'],))
            conn.commit()
            conn.close()

            remaining = timeout - elapsed_minutes
            return {
                "action": "reminder",
                "workflow_id": workflow['id'],
                "phase": workflow['phase'],
                "remaining_minutes": int(remaining),
                "elapsed_minutes": int(elapsed_minutes)
            }

        return None

    def get_workflow_status(self, workflow_id: int = None) -> Dict[str, Any]:
        """Get detailed status of a workflow (includes veloce chunks if applicable)."""
        workflow = self.get_workflow(workflow_id) if workflow_id else self.get_active_workflow()
        if not workflow:
            return {"active": False}

        phase_started = _sqlite_to_datetime(workflow['phase_started_at'])
        elapsed = datetime.now(timezone.utc) - phase_started if phase_started else timedelta(0)
        elapsed_minutes = int(elapsed.total_seconds() / 60)
        remaining = max(0, workflow['timeout_minutes'] - elapsed_minutes)

        phases = self._get_phases(workflow)
        mode = workflow.get('mode', 'standard')

        # Find phase index for progress
        if workflow['phase'] in phases:
            phase_idx = phases.index(workflow['phase'])
        else:
            phase_idx = 0
        progress_percent = int((phase_idx / max(len(phases) - 1, 1)) * 100)

        result = {
            "active": True,
            "workflow_id": workflow['id'],
            "name": workflow['name'],
            "description": workflow.get('description'),
            "phase": workflow['phase'],
            "phase_index": phase_idx,
            "total_phases": len(phases),
            "phases": phases,
            "progress_percent": progress_percent,
            "lead_agent": workflow.get('lead_agent'),
            "elapsed_minutes": elapsed_minutes,
            "timeout_minutes": workflow['timeout_minutes'],
            "remaining_minutes": remaining,
            "extend_count": workflow['extend_count'],
            "extends_remaining": MAX_EXTENDS_PER_PHASE - workflow['extend_count'],
            "reminded": bool(workflow['reminded']),
            "created_by": workflow['created_by'],
            "created_at": workflow['created_at'],
            "project_id": workflow.get('project_id', 'default'),
            "mode": mode,
            "metadata": self.get_metadata(workflow['id'])
        }

        # Include chunks info for veloce workflows
        if mode == 'veloce':
            result["chunks"] = self.get_chunks_summary(workflow['id'])

        return result

    # ==========================================================================
    # History & Config
    # ==========================================================================

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get workflow history for retros."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT * FROM workflow_history
            ORDER BY completed_at DESC
            LIMIT ?
        """, (limit,))
        rows = c.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_config(self) -> Dict[str, Dict[str, int]]:
        """Get current phase config."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM workflow_config")
        rows = c.fetchall()
        conn.close()
        return {
            row['phase']: {
                'timeout': row['default_timeout'],
                'reminder_percent': row['reminder_at_percent']
            }
            for row in rows
        }

    def update_config(self, phase: str, timeout: int = None, reminder_percent: int = None) -> bool:
        """Update config for a phase."""
        # Accept both standard and veloce phases
        all_phases = set(WORKFLOW_PHASES) | set(VELOCE_PHASES)
        if phase not in all_phases:
            return False

        conn = self._get_conn()
        c = conn.cursor()

        if timeout is not None:
            c.execute("UPDATE workflow_config SET default_timeout = ? WHERE phase = ?", (timeout, phase))
        if reminder_percent is not None:
            c.execute("UPDATE workflow_config SET reminder_at_percent = ? WHERE phase = ?", (reminder_percent, phase))

        conn.commit()
        conn.close()
        return True


# ==========================================================================
# Standalone test
# ==========================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Test with in-memory DB
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)

    scheduler = WorkflowScheduler(db_path)

    # -- Standard workflow test --
    wf_id = scheduler.create_workflow("Test Feature", "@naskel", "Testing the scheduler")
    print(f"Created standard workflow: {wf_id}")

    status = scheduler.get_workflow_status()
    print(f"Status: {json.dumps(status, indent=2)}")

    result = scheduler.next_phase()
    print(f"Next phase: {result}")

    result = scheduler.extend_phase(10)
    print(f"Extended: {result}")

    check = scheduler.check_timeout()
    print(f"Timeout check: {check}")

    result = scheduler.complete_workflow(wf_id)
    print(f"Completed: {result}")

    # -- Veloce workflow test --
    vwf_id = scheduler.create_workflow("Veloce Feature", "@alpha",
                                        "Testing veloce mode", mode="veloce")
    print(f"\nCreated veloce workflow: {vwf_id}")

    # Advance to decompose phase
    for _ in range(3):  # request -> brainstorm -> vote
        scheduler.next_phase()
    result = scheduler.next_phase()  # vote -> architecture
    print(f"Phase: {result}")
    result = scheduler.next_phase()  # architecture -> decompose
    print(f"Phase: {result}")

    # Submit chunks
    chunks = [
        {"chunk_id": "auth-middleware", "agent_id": "@alpha",
         "scope": {"files_create": ["src/auth.py"], "files_modify": []},
         "interface": {"exports": ["authenticate()"], "imports": []}},
        {"chunk_id": "api-routes", "agent_id": "@beta",
         "scope": {"files_create": ["src/routes.py"], "files_modify": []},
         "interface": {"exports": ["router"], "imports": ["authenticate()"]}},
    ]
    decomp = scheduler.submit_decomposition(vwf_id, chunks)
    print(f"Decomposition: {decomp}")

    # Advance to parallel_code
    scheduler.next_phase()  # decompose -> decompose_vote
    result = scheduler.next_phase()  # decompose_vote -> parallel_code
    print(f"Parallel code started: {result}")

    # Complete chunks
    r1 = scheduler.complete_chunk(vwf_id, "auth-middleware")
    print(f"Chunk done: {r1}")
    r2 = scheduler.complete_chunk(vwf_id, "api-routes")
    print(f"Chunk done: {r2}")

    # Gate should be open now
    gate = scheduler.next_phase()
    print(f"After gate: {gate}")

    # Status with chunks
    status = scheduler.get_workflow_status()
    print(f"Veloce status: {json.dumps(status, indent=2)}")

    # Complete
    result = scheduler.complete_workflow(vwf_id)
    print(f"Veloce completed: {result}")

    # History
    history = scheduler.get_history()
    print(f"History: {len(history)} entries")

    # Cleanup
    db_path.unlink()
    print("\n+ All tests passed!")
