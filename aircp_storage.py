#!/usr/bin/env python3
"""
AIRCP Message Storage - SQLite-based message persistence

Stores messages for:
- History retrieval
- Audit logging
- Analytics
- Message replay
- Mode state (MODES.md v0.3)
- Agent tasks (TaskManager Option B v0.8 - with current_step persistence)
- Agent presence (Heartbeat v0.9)
- Brainstorm sessions (v1.0)

Storage strategy:
- Direct SQLite on disk with WAL mode (v5.0 - replaced /dev/shm model)
- WAL provides concurrent reads + fast writes without RAM copy overhead
- No shm/disk sync needed, no shutil.copy2 fragility
"""
import sqlite3
import json
import logging
import tomllib
import threading as _threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# Database path (disk-based with WAL mode)
DISK_DB_PATH = Path("aircp.db")


def _sqlite_now() -> str:
    """Return current UTC time in SQLite-compatible format"""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _sqlite_to_iso8601(sqlite_ts: str) -> str:
    """Convert SQLite timestamp (YYYY-MM-DD HH:MM:SS) to ISO8601 (YYYY-MM-DDTHH:MM:SSZ)"""
    if not sqlite_ts:
        return None
    return sqlite_ts.replace(' ', 'T') + 'Z'


class AIRCPStorage:
    """SQLite-based message storage with WAL mode (disk-direct, no shm copy)"""

    def __init__(self, db_path: str = None, use_ram: bool = True):
        """
        Initialize storage.

        Args:
            db_path: Override path (for tests). If None, uses DISK_DB_PATH.
            use_ram: Deprecated, ignored. Kept for API compat.
        """
        if db_path is not None:
            self.db_path = Path(db_path)
        else:
            self.db_path = DISK_DB_PATH

        # Back-compat aliases (used by some callers)
        self.disk_path = self.db_path

        # Single shared connection + lock (replaces leaky per-thread pattern)
        self._conn_lock = _threading.Lock()
        self._conn: sqlite3.Connection | None = None

        self.init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get the shared SQLite connection (thread-safe via lock).

        Single connection with WAL mode. All callers share one connection;
        the _conn_lock in each method serializes writes. SQLite WAL handles
        concurrent reads internally.
        """
        if self._conn is None:
            with self._conn_lock:
                if self._conn is None:
                    self._conn = sqlite3.connect(
                        self.db_path,
                        check_same_thread=False,
                        timeout=10,
                    )
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def persist_to_disk(self):
        """Legacy no-op. DB is already on disk with WAL mode.

        Kept for API compat -- callers that still call this are harmless.
        Does a WAL checkpoint to merge WAL into main DB file.
        """
        try:
            conn = self._get_conn()
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as e:
            logger.warning(f"WAL checkpoint failed: {e}")

    def _persist_critical(self, reason: str = ""):
        """Legacy no-op. DB writes go to disk immediately via WAL.

        Kept for API compat -- callers (task complete, review close, etc.)
        still call this but it does nothing harmful.
        """
        pass

    def close(self):
        """Checkpoint WAL and close connection on shutdown."""
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
            except Exception as e:
                logger.warning(f"Error during close: {e}")
            finally:
                self._conn = None

    def init_db(self):
        """Initialize database schema"""
        conn = self._get_conn()
        c = conn.cursor()

        # Messages table
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                from_id TEXT NOT NULL,
                room TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT,
                payload TEXT,
                envelope TEXT,
                room_seq INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Index for quick lookups
        c.execute("CREATE INDEX IF NOT EXISTS idx_room_ts ON messages(room, ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_room_seq ON messages(room, room_seq)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_from_id ON messages(from_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kind ON messages(kind)")

        # ========== FTS5 Full-Text Search (Memory API v3.0) ==========

        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(content, from_id, room, content='messages', content_rowid='rowid')
        """)

        # Auto-sync triggers for FTS5
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content, from_id, room)
                VALUES (new.rowid, new.content, new.from_id, new.room);
            END
        """)

        c.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, from_id, room)
                VALUES ('delete', old.rowid, old.content, old.from_id, old.room);
            END
        """)

        # Sessions table
        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                room TEXT,
                authenticated BOOLEAN,
                connected_at TIMESTAMP,
                disconnected_at TIMESTAMP
            )
        """)

        # Rooms table
        c.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                name TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                member_count INTEGER DEFAULT 0
            )
        """)

        # Analytics table
        c.execute("""
            CREATE TABLE IF NOT EXISTS analytics (
                date TEXT,
                room TEXT,
                message_count INTEGER,
                agent_count INTEGER,
                PRIMARY KEY (date, room)
            )
        """)

        # ========== MODES.md v0.3 Tables ==========

        # Mode state table (singleton - stores current mode)
        c.execute("""
            CREATE TABLE IF NOT EXISTS mode_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                mode TEXT NOT NULL DEFAULT 'neutral',
                lead TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                timeout_at TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        # Mode history table (tracks transitions for @mode history)
        c.execute("""
            CREATE TABLE IF NOT EXISTS mode_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                lead TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                reason TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mode_history_ended ON mode_history(ended_at)")

        # Pending asks table (cleared on mode change per MODES.md v0.3)
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_asks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                question TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # ========== TaskManager Tables (Option B: Daemon enrichi v0.8) ==========

        # Agent tasks table - tracks active tasks per agent
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'pending',
                current_step INTEGER DEFAULT 0,
                context TEXT,
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                last_activity TEXT,
                completed_at TEXT,
                last_pinged_at TEXT,
                ping_count INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent ON agent_tasks(agent_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status)")

        # Migration: add columns if they don't exist (for existing DBs)
        try:
            c.execute("ALTER TABLE agent_tasks ADD COLUMN last_pinged_at TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            c.execute("ALTER TABLE agent_tasks ADD COLUMN ping_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            c.execute("ALTER TABLE agent_tasks ADD COLUMN current_step INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        # v3.3: workflow_id FK for auto-linking tasks to workflows
        try:
            c.execute("ALTER TABLE agent_tasks ADD COLUMN workflow_id INTEGER")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # ========== Agent Presence Table (Heartbeat v0.9) ==========

        # Agent presence table - tracks agent heartbeats
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_presence (
                agent_id TEXT PRIMARY KEY,
                status TEXT DEFAULT 'idle',
                current_task TEXT,
                last_seen TEXT NOT NULL,
                created_at TEXT NOT NULL,
                capacity INTEGER DEFAULT 1
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_agent_presence_status ON agent_presence(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_agent_presence_last_seen ON agent_presence(last_seen)")

        # E3: Agent capacity column (safe migration)
        try:
            c.execute("ALTER TABLE agent_presence ADD COLUMN capacity INTEGER DEFAULT 1")
        except Exception:
            pass  # Column already exists

        # ========== Brainstorm System Tables (v1.0) ==========

        # Brainstorm sessions table
        c.execute("""
            CREATE TABLE IF NOT EXISTS brainstorm_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                topic TEXT NOT NULL,
                created_by TEXT NOT NULL,
                participants TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                consensus TEXT,
                created_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                closed_at TEXT,
                auto_workflow INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_brainstorm_status ON brainstorm_sessions(status)")

        # Migration: add auto_workflow column if missing (for existing DBs)
        try:
            c.execute("ALTER TABLE brainstorm_sessions ADD COLUMN auto_workflow INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        # v3.3: workflow_id FK for auto-linking brainstorms to workflows
        try:
            c.execute("ALTER TABLE brainstorm_sessions ADD COLUMN workflow_id INTEGER")
        except sqlite3.OperationalError:
            pass  # Column already exists
        # v4.2: workflow_mode for idea → workflow auto-trigger
        try:
            c.execute("ALTER TABLE brainstorm_sessions ADD COLUMN workflow_mode TEXT DEFAULT 'standard'")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Brainstorm votes table
        c.execute("""
            CREATE TABLE IF NOT EXISTS brainstorm_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                agent_id TEXT NOT NULL,
                vote TEXT NOT NULL,
                comment TEXT,
                voted_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES brainstorm_sessions(id),
                UNIQUE(session_id, agent_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_brainstorm_votes_session ON brainstorm_votes(session_id)")

        # ========== Review System Tables (v1.5) ==========

        # Review requests table
        c.execute("""
            CREATE TABLE IF NOT EXISTS review_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                reviewers TEXT NOT NULL,
                review_type TEXT DEFAULT 'doc',
                min_approvals INTEGER DEFAULT 1,
                status TEXT DEFAULT 'pending',
                consensus TEXT,
                created_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                reminder_sent INTEGER DEFAULT 0,
                closed_at TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_review_status ON review_requests(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_review_deadline ON review_requests(deadline_at)")

        # Review responses table
        c.execute("""
            CREATE TABLE IF NOT EXISTS review_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                reviewer TEXT NOT NULL,
                vote TEXT NOT NULL,
                comment TEXT,
                responded_at TEXT NOT NULL,
                FOREIGN KEY (request_id) REFERENCES review_requests(id),
                UNIQUE(request_id, reviewer)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_review_responses_request ON review_responses(request_id)")
        # v3.3: workflow_id FK for auto-linking reviews to workflows
        try:
            c.execute("ALTER TABLE review_requests ADD COLUMN workflow_id INTEGER")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # ========== Agent Activity Table (Passive Observability v2.0) ==========

        # Inferred agent activity from API calls (no manual heartbeat needed)
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_activity (
                agent_id TEXT PRIMARY KEY,
                activity TEXT NOT NULL DEFAULT 'idle',
                context TEXT,
                started_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_agent_activity_activity ON agent_activity(activity)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_agent_activity_last_seen ON agent_activity(last_seen)")

        # ========== Project Workspaces Tables (v3.0) ==========

        c.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                owner TEXT NOT NULL DEFAULT '@operator',
                brief_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Auto-insert "default" project if absent
        now = _sqlite_now()
        c.execute("""
            INSERT OR IGNORE INTO projects (id, name, description, owner, created_at, updated_at)
            VALUES ('default', 'Default', 'Default workspace', '@operator', ?, ?)
        """, (now, now))

        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_active_project (
                agent_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL DEFAULT 'default',
                switched_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
        """)

        # Migration: add project_id to coordination tables + messages
        for table in ["agent_tasks", "brainstorm_sessions", "review_requests", "messages"]:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT DEFAULT 'default'")
            except sqlite3.OperationalError:
                pass
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_project ON {table}(project_id)")

        # ========== Compactor v3 Migration (Option C: soft-delete + GC) ==========
        # Add compacted_at and is_summary columns for soft-delete compaction
        for col, typedef in [
            ("compacted_at", "TEXT DEFAULT NULL"),
            ("is_summary", "INTEGER DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE messages ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_compacted ON messages(compacted_at)")

        # Compaction audit log
        c.execute("""
            CREATE TABLE IF NOT EXISTS compaction_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                triggered_at TEXT NOT NULL,
                triggered_by TEXT NOT NULL,
                total_before INTEGER,
                total_after INTEGER,
                deleted_count INTEGER,
                compacted_count INTEGER,
                compression_ratio TEXT,
                summary TEXT
            )
        """)

        # LLM token usage tracking per agent
        c.execute("""
            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                estimated INTEGER DEFAULT 0,
                latency_ms INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_usage_agent_created "
            "ON llm_usage(agent_id, created_at)"
        )

        # ========== Git Agent Mode Tables (IDEA #5 Phase 1) ==========

        # Git repos -- repository configuration (multi-repo ready)
        c.execute("""
            CREATE TABLE IF NOT EXISTS git_repos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                owner TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'github',
                api_url TEXT NOT NULL DEFAULT 'https://api.github.com',
                html_url TEXT NOT NULL DEFAULT '',
                default_branch TEXT NOT NULL DEFAULT 'main',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_git_repos_source ON git_repos(source)")

        # Git events -- audit trail for ALL git agent actions
        c.execute("""
            CREATE TABLE IF NOT EXISTS git_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER,
                event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL DEFAULT '',
                issue_number INTEGER,
                details TEXT,
                project_id TEXT DEFAULT 'default',
                created_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES git_repos(id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_git_events_type ON git_events(event_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_git_events_repo ON git_events(repo_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_git_events_created ON git_events(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_git_events_actor ON git_events(actor_id)")

        # Git issue cache -- cached issues from GitHub for dashboard display
        c.execute("""
            CREATE TABLE IF NOT EXISTS git_issue_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL,
                issue_number INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT DEFAULT '',
                state TEXT NOT NULL DEFAULT 'open',
                labels TEXT DEFAULT '[]',
                assignees TEXT DEFAULT '[]',
                author_login TEXT DEFAULT '',
                comments_count INTEGER DEFAULT 0,
                html_url TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                cached_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES git_repos(id),
                UNIQUE(repo_id, issue_number)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_git_issue_cache_repo_state "
            "ON git_issue_cache(repo_id, state)"
        )

        # Git actions queue -- pending write actions awaiting dashboard approval
        c.execute("""
            CREATE TABLE IF NOT EXISTS git_actions_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL,
                issue_number INTEGER,
                action_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                params TEXT NOT NULL DEFAULT '{}',
                preview TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                approved_by TEXT,
                rejected_by TEXT,
                result TEXT,
                queued_at TEXT NOT NULL,
                decided_at TEXT,
                executed_at TEXT,
                FOREIGN KEY (repo_id) REFERENCES git_repos(id)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_git_actions_queue_status "
            "ON git_actions_queue(status)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_git_actions_queue_repo "
            "ON git_actions_queue(repo_id)"
        )

        # Git issue assignments -- agent assignments to issues (dashboard bridge)
        c.execute("""
            CREATE TABLE IF NOT EXISTS git_issue_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL,
                issue_number INTEGER NOT NULL,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'investigate',
                task_id INTEGER,
                assigned_by TEXT NOT NULL DEFAULT '@naskel',
                assigned_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES git_repos(id),
                FOREIGN KEY (task_id) REFERENCES agent_tasks(id),
                UNIQUE(repo_id, issue_number, agent_id)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_git_issue_assignments_issue "
            "ON git_issue_assignments(repo_id, issue_number)"
        )

        conn.commit()

        logger.info(f"Storage initialized at {self.db_path}")

    # ========== Mode State Methods (MODES.md v0.3) ==========

    def get_mode_state(self) -> Optional[Dict[str, Any]]:
        """Get current mode state (singleton)"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM mode_state WHERE id = 1")
            row = c.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to get mode state: {e}")
            return None

    def set_mode_state(self, mode: str, lead: str, timeout_at: Optional[str] = None) -> bool:
        """Set current mode state (upsert singleton)"""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()

            # Archive current state to history if exists
            c.execute("SELECT mode, lead, started_at FROM mode_state WHERE id = 1")
            current = c.fetchone()
            if current:
                c.execute("""
                    INSERT INTO mode_history (mode, lead, started_at, ended_at, reason)
                    VALUES (?, ?, ?, ?, 'manual')
                """, (current[0], current[1], current[2], now))

            # Upsert new state
            c.execute("""
                INSERT OR REPLACE INTO mode_state (id, mode, lead, started_at, timeout_at, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
            """, (mode, lead, now, timeout_at, now))

            conn.commit()
            logger.info(f"✓ Mode set to {mode} (lead: {lead})")
            return True
        except Exception as e:
            logger.error(f"Failed to set mode state: {e}")
            return False

    def get_mode_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get mode transition history"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM mode_history
                ORDER BY ended_at DESC
                LIMIT ?
            """, (limit,))
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get mode history: {e}")
            return []

    def clear_mode_history(self, keep_last: int = 50):
        """Trim mode history to keep only last N entries"""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                DELETE FROM mode_history
                WHERE id NOT IN (
                    SELECT id FROM mode_history
                    ORDER BY ended_at DESC
                    LIMIT ?
                )
            """, (keep_last,))
            deleted = c.rowcount
            conn.commit()
            if deleted > 0:
                logger.info(f"✓ Trimmed {deleted} old mode history entries")
            return deleted
        except Exception as e:
            logger.error(f"Failed to clear mode history: {e}")
            return 0

    # ========== Pending Asks Methods (MODES.md v0.3) ==========

    def add_pending_ask(self, from_agent: str, to_agent: str, question: Optional[str] = None) -> int:
        """Register a pending @ask"""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO pending_asks (from_agent, to_agent, question, created_at)
                VALUES (?, ?, ?, ?)
            """, (from_agent, to_agent, question, now))
            ask_id = c.lastrowid
            conn.commit()
            logger.debug(f"✓ Registered @ask from {from_agent} to {to_agent}")
            return ask_id
        except Exception as e:
            logger.error(f"Failed to add pending ask: {e}")
            return -1

    def get_pending_asks(self, to_agent: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get pending asks, optionally filtered by target agent"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if to_agent:
                c.execute("""
                    SELECT * FROM pending_asks
                    WHERE to_agent = ? OR to_agent = '@all'
                    ORDER BY created_at ASC
                """, (to_agent,))
            else:
                c.execute("SELECT * FROM pending_asks ORDER BY created_at ASC")
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get pending asks: {e}")
            return []

    def clear_pending_asks(self) -> int:
        """Clear all pending asks (called on mode change per MODES.md v0.3)"""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("DELETE FROM pending_asks")
            deleted = c.rowcount
            conn.commit()
            if deleted > 0:
                logger.info(f"✓ Cleared {deleted} pending asks (mode change)")
            return deleted
        except Exception as e:
            logger.error(f"Failed to clear pending asks: {e}")
            return 0

    def remove_pending_ask(self, ask_id: int) -> bool:
        """Remove a specific pending ask (when answered)"""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("DELETE FROM pending_asks WHERE id = ?", (ask_id,))
            deleted = c.rowcount > 0
            conn.commit()
            return deleted
        except Exception as e:
            logger.error(f"Failed to remove pending ask: {e}")
            return False

    # ========== TaskManager Methods (Option B: Daemon enrichi v0.8) ==========

    def create_task(self, agent_id: str, task_type: str, description: str,
                    context: Optional[Dict] = None,
                    project_id: str = "default") -> int:
        """Create a new task for an agent"""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO agent_tasks
                (agent_id, task_type, description, status, context, created_at, last_activity, ping_count, current_step, project_id)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, 0, 0, ?)
            """, (agent_id, task_type, description,
                  json.dumps(context) if context else None, now, now, project_id))
            task_id = c.lastrowid
            conn.commit()
            logger.info(f"✓ Created task {task_id} for {agent_id} [{project_id}]: {description[:50]}")
            return task_id
        except Exception as e:
            logger.error(f"Failed to create task: {e}")
            return -1

    def claim_task(self, task_id: int, agent_id: str) -> bool:
        """Claim a task (set status to in_progress)"""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                UPDATE agent_tasks 
                SET status = 'in_progress', claimed_at = ?, last_activity = ?
                WHERE id = ? AND agent_id = ? AND status = 'pending'
            """, (now, now, task_id, agent_id))
            updated = c.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"Failed to claim task: {e}")
            return False

    def update_task_activity(self, task_id: int, current_step: Optional[int] = None) -> bool:
        """Update last_activity timestamp for a task (heartbeat), optionally with current_step.
        Auto-switches pending → in_progress when activity is reported."""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            # Auto-switch pending → in_progress when activity is reported
            c.execute("""
                UPDATE agent_tasks SET status = 'in_progress'
                WHERE id = ? AND status = 'pending'
            """, (task_id,))
            if c.rowcount > 0:
                logger.info(f"Task {task_id} auto-switched to in_progress")
            if current_step is not None:
                c.execute("""
                    UPDATE agent_tasks SET last_activity = ?, current_step = ?,
                    ping_count = 0, last_pinged_at = NULL WHERE id = ?
                """, (now, current_step, task_id))
                logger.debug(f"Task {task_id} activity updated with step {current_step}")
            else:
                c.execute("""
                    UPDATE agent_tasks SET last_activity = ?,
                    ping_count = 0, last_pinged_at = NULL WHERE id = ?
                """, (now, task_id))
            updated = c.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"Failed to update task activity: {e}")
            return False

    def update_task_pinged(self, task_id: int) -> bool:
        """Mark task as pinged (for anti-spam watchdog)"""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                UPDATE agent_tasks 
                SET last_pinged_at = ?, ping_count = COALESCE(ping_count, 0) + 1
                WHERE id = ?
            """, (now, task_id))
            updated = c.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"Failed to update task pinged: {e}")
            return False

    def complete_task(self, task_id: int, status: str = 'done') -> bool:
        """Complete a task (status: done, failed, cancelled, stale)"""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                UPDATE agent_tasks 
                SET status = ?, completed_at = ?, last_activity = ?
                WHERE id = ?
            """, (status, now, now, task_id))
            updated = c.rowcount > 0
            conn.commit()
            if updated:
                self._persist_critical(f"task #{task_id} -> {status}")
            return updated
        except Exception as e:
            logger.error(f"Failed to complete task: {e}")
            return False

    def get_agent_tasks(self, agent_id: str, status: Optional[str] = None,
                        project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get tasks for an agent, optionally filtered by status and/or project"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # Map 'active' to 'in_progress' for API convenience
            if status == 'active':
                status = 'in_progress'

            sql = "SELECT * FROM agent_tasks WHERE agent_id = ?"
            params = [agent_id]
            if status:
                sql += " AND status = ?"
                params.append(status)
            if project_id:
                sql += " AND project_id = ?"
                params.append(project_id)
            sql += " ORDER BY created_at DESC"

            c.execute(sql, params)
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get agent tasks: {e}")
            return []

    def get_active_tasks(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all tasks with status 'pending' or 'in_progress'.
        If project_id is given, filter by project."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if project_id:
                c.execute("""
                    SELECT * FROM agent_tasks
                    WHERE status IN ('pending', 'in_progress') AND project_id = ?
                    ORDER BY last_activity ASC
                """, (project_id,))
            else:
                c.execute("""
                    SELECT * FROM agent_tasks
                    WHERE status IN ('pending', 'in_progress')
                    ORDER BY last_activity ASC
                """)
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get active tasks: {e}")
            return []

    def get_tasks_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get all tasks with a specific status (pending, in_progress, done, failed, stale)"""
        try:
            # Map 'active' to 'in_progress' for API convenience
            if status == 'active':
                status = 'in_progress'

            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM agent_tasks
                WHERE status = ?
                ORDER BY created_at DESC
            """, (status,))
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get tasks by status {status}: {e}")
            return []

    def get_stale_tasks(self, stale_seconds: int = 60, min_ping_interval: int = 300) -> List[Dict[str, Any]]:
        """
        Get tasks that haven't had activity in stale_seconds AND
        haven't been pinged in the last min_ping_interval seconds.
        This prevents spam-pinging the same task.
        """
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM agent_tasks 
                WHERE status = 'in_progress'
                AND datetime(last_activity) < datetime('now', '-' || ? || ' seconds')
                AND (
                    last_pinged_at IS NULL 
                    OR datetime(last_pinged_at) < datetime('now', '-' || ? || ' seconds')
                )
                ORDER BY last_activity ASC
            """, (stale_seconds, min_ping_interval))
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get stale tasks: {e}")
            return []

    def get_stale_pending_tasks(self, pending_seconds: int = 600, min_ping_interval: int = 600) -> List[Dict[str, Any]]:
        """v4.3: Get pending tasks that haven't been claimed after pending_seconds.

        Unlike get_stale_tasks() which only monitors in_progress tasks,
        this catches tasks that were created but never started.
        Reuses the same ping_count / last_pinged_at columns.
        """
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM agent_tasks
                WHERE status = 'pending'
                AND datetime(created_at) < datetime('now', '-' || ? || ' seconds')
                AND (
                    last_pinged_at IS NULL
                    OR datetime(last_pinged_at) < datetime('now', '-' || ? || ' seconds')
                )
                ORDER BY created_at ASC
            """, (pending_seconds, min_ping_interval))
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get stale pending tasks: {e}")
            return []

    def mark_stale_tasks_as_stale(self, max_pings: int = 3) -> int:
        """Mark tasks that have been pinged max_pings times without response as 'stale'.
        Safety: also checks last_activity is old enough to avoid race with ping_count reset."""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                UPDATE agent_tasks 
                SET status = 'stale', completed_at = ?
                WHERE status = 'in_progress'
                AND datetime(last_activity) < datetime('now', '-60 seconds')
                AND ping_count >= ?
            """, (now, max_pings))
            updated = c.rowcount
            conn.commit()
            if updated > 0:
                logger.info(f"✓ Marked {updated} tasks as stale (exceeded {max_pings} pings)")
            return updated
        except Exception as e:
            logger.error(f"Failed to mark stale tasks: {e}")
            return 0

    # ========== Workflow Auto-Link Methods (v3.3) ==========

    def get_task_by_id(self, task_id: int) -> Optional[Dict[str, Any]]:
        """Get a single task by ID."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM agent_tasks WHERE id = ?", (task_id,))
            row = c.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to get task {task_id}: {e}")
            return None

    def set_task_workflow_id(self, task_id: int, workflow_id: int) -> bool:
        """Link a task to a workflow."""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("UPDATE agent_tasks SET workflow_id = ? WHERE id = ?",
                       (workflow_id, task_id))
            updated = c.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"Failed to set workflow_id on task {task_id}: {e}")
            return False

    def get_active_workflow_tasks(self, workflow_id: int) -> List[Dict[str, Any]]:
        """Get in_progress/pending tasks for a workflow (code→review gate)."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM agent_tasks
                WHERE workflow_id = ? AND status IN ('pending', 'in_progress')
                ORDER BY created_at ASC
            """, (workflow_id,))
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get workflow tasks for {workflow_id}: {e}")
            return []

    def update_review_workflow_id(self, request_id: int, workflow_id: int) -> bool:
        """Link a review request to a workflow."""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("UPDATE review_requests SET workflow_id = ? WHERE id = ?",
                       (workflow_id, request_id))
            updated = c.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"Failed to set workflow_id on review {request_id}: {e}")
            return False

    # ========== Agent Presence Methods (Heartbeat v0.9) ==========

    def update_agent_presence(self, agent_id: str, status: str = "idle",
                               current_task: Optional[str] = None,
                               capacity: int = 1) -> bool:
        """Update agent presence (heartbeat). Creates entry if not exists."""
        if agent_id and not agent_id.startswith("@"):
            agent_id = f"@{agent_id}"
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO agent_presence (agent_id, status, current_task, last_seen, created_at, capacity)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    status = excluded.status,
                    current_task = excluded.current_task,
                    last_seen = excluded.last_seen,
                    capacity = excluded.capacity
            """, (agent_id, status, current_task, now, now, capacity))
            conn.commit()
            logger.debug(f"Agent {agent_id} heartbeat: {status} (capacity={capacity})")
            return True
        except Exception as e:
            logger.error(f"Failed to update agent presence: {e}")
            return False

    def get_agent_presence(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get presence info for a specific agent."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM agent_presence WHERE agent_id = ?", (agent_id,))
            row = c.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to get agent presence: {e}")
            return None

    def get_all_agent_presence(self) -> List[Dict[str, Any]]:
        """Get presence info for all known agents."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM agent_presence ORDER BY last_seen DESC")
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get all agent presence: {e}")
            return []

    def get_available_agents(self, online_seconds: int = 120) -> List[Dict[str, Any]]:
        """E3: Get agents with spare capacity (active_tasks < capacity)."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=online_seconds)).strftime('%Y-%m-%d %H:%M:%S')
            c.execute("""
                SELECT p.agent_id, p.status, p.capacity,
                       COALESCE(t.active_count, 0) as active_tasks
                FROM agent_presence p
                LEFT JOIN (
                    SELECT agent_id, COUNT(*) as active_count
                    FROM tasks WHERE status = 'in_progress'
                    GROUP BY agent_id
                ) t ON p.agent_id = t.agent_id
                WHERE p.last_seen > ?
                  AND COALESCE(t.active_count, 0) < COALESCE(p.capacity, 1)
                ORDER BY COALESCE(t.active_count, 0) ASC
            """, (cutoff,))
            return [dict(r) for r in c.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get available agents: {e}")
            return []

    def get_stale_agents(self, away_seconds: int = 120, dead_seconds: int = 300) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get agents that haven't sent a heartbeat recently.
        Returns dict with 'away' (>away_seconds) and 'dead' (>dead_seconds) lists.
        """
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # Get all agents that are stale (older than away_seconds)
            c.execute("""
                SELECT * FROM agent_presence
                WHERE datetime(last_seen) < datetime('now', '-' || ? || ' seconds')
                ORDER BY last_seen ASC
            """, (away_seconds,))
            all_stale = [dict(row) for row in c.fetchall()]

            # Split into away vs dead
            away = [a for a in all_stale if self._seconds_since(a['last_seen']) < dead_seconds]
            dead = [a for a in all_stale if self._seconds_since(a['last_seen']) >= dead_seconds]

            return {"away": away, "dead": dead}
        except Exception as e:
            logger.error(f"Failed to get stale agents: {e}")
            return {"away": [], "dead": []}

    def _seconds_since(self, timestamp_str: str) -> float:
        """Calculate seconds elapsed since a SQLite timestamp string."""
        try:
            dt = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
            dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            return float('inf')  # Treat parse errors as very old

    def _format_time_ago(self, seconds: float) -> str:
        """Format seconds elapsed as human-readable string."""
        if seconds < 60:
            return f"{int(seconds)}s ago"
        elif seconds < 3600:
            return f"{int(seconds // 60)}min ago"
        elif seconds < 86400:
            return f"{int(seconds // 3600)}h ago"
        else:
            return f"{int(seconds // 86400)}d ago"

    # ========== @progress Command: Agent State Helper (v1.2) ==========

    # Thresholds (defaults for cloud agents — can be overridden per-agent via offline_threshold_override)
    AGENT_STATE_WORKING_THRESHOLD = 60      # seconds - heartbeat < 60s = working
    AGENT_STATE_OFFLINE_THRESHOLD = 300     # seconds - heartbeat > 5min = offline

    def get_agent_state(self, agent_id: str, offline_threshold_override: float = None) -> Dict[str, Any]:
        """
        Get comprehensive agent state for @progress command.

        Returns a standardized payload with:
        - agent: str
        - status: "working" | "idle" | "stale" | "offline" | "unknown"
        - task: {id, description, step} | null  (step is an integer, NOT a percentage)
        - last_activity: ISO8601 timestamp (YYYY-MM-DDTHH:MM:SSZ)
        - last_activity_human: "2min ago" style string (for display only)
        - watchdog: {pings, max}
        - source: "daemon" (always daemon for this method)

        State calculation (centralized):
        - working: Task in_progress + heartbeat < 60s
        - idle: No task + heartbeat < 5min
        - stale: Task in_progress + heartbeat > 60s
        - offline: Heartbeat > 5min (regardless of task), or offline_threshold_override for local LLMs
        - unknown: No heartbeat ever recorded
        """
        result = {
            "agent": agent_id,
            "status": "unknown",
            "task": None,
            "last_activity": None,
            "last_activity_human": "never",
            "watchdog": {"pings": 0, "max": 3},
            "source": "daemon"
        }

        # Get presence info (heartbeat)
        presence = self.get_agent_presence(agent_id)
        if not presence:
            # Agent never seen
            result["status"] = "unknown"
            return result

        # Calculate time since last heartbeat
        last_seen = presence.get("last_seen", "")
        seconds_since_heartbeat = self._seconds_since(last_seen) if last_seen else float('inf')
        result["last_activity"] = _sqlite_to_iso8601(last_seen)  # Convert to proper ISO8601
        result["last_activity_human"] = self._format_time_ago(seconds_since_heartbeat)

        # Check for active task
        active_tasks = self.get_agent_tasks(agent_id, status="in_progress")
        active_task = active_tasks[0] if active_tasks else None

        if active_task:
            result["task"] = {
                "id": active_task.get("id"),
                "description": active_task.get("description", "")[:100],
                "step": active_task.get("current_step", 0)  # int (not a percentage!)
            }
            result["watchdog"] = {
                "pings": active_task.get("ping_count", 0),
                "max": 3
            }
            # Update last_activity from task if more recent
            task_activity = active_task.get("last_activity", "")
            if task_activity:
                task_seconds = self._seconds_since(task_activity)
                if task_seconds < seconds_since_heartbeat:
                    seconds_since_heartbeat = task_seconds
                    result["last_activity"] = _sqlite_to_iso8601(task_activity)
                    result["last_activity_human"] = self._format_time_ago(task_seconds)

        # Determine status based on thresholds (centralized logic)
        # v4.1: Use per-agent offline threshold for local LLMs (longer generation = no heartbeat)
        offline_threshold = offline_threshold_override or self.AGENT_STATE_OFFLINE_THRESHOLD
        if seconds_since_heartbeat > offline_threshold:
            result["status"] = "offline"
        elif active_task:
            if seconds_since_heartbeat <= self.AGENT_STATE_WORKING_THRESHOLD:
                result["status"] = "working"
            else:
                result["status"] = "stale"
        else:
            # No active task
            if seconds_since_heartbeat <= offline_threshold:
                result["status"] = "idle"
            else:
                result["status"] = "offline"

        return result

    # ========== Brainstorm System Methods (v1.0) ==========

    def create_brainstorm_session(self, topic: str, created_by: str,
                                   participants: List[str], timeout_seconds: int,
                                   task_id: Optional[int] = None,
                                   auto_workflow: bool = False,
                                   project_id: str = "default",
                                   workflow_mode: str = "standard") -> int:
        """Create a new brainstorm session.

        Args:
            topic: The brainstorm topic/idea
            created_by: Who created it (@naskel, etc.)
            participants: List of agents to vote
            timeout_seconds: Time before auto-resolution
            task_id: Optional link to existing task
            auto_workflow: If True, auto-trigger workflow/start on GO consensus
            project_id: Project scope
            workflow_mode: Workflow mode to use on auto-trigger ('standard' or 'veloce')
        """
        try:
            now = _sqlite_now()
            # Calculate deadline
            deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
            deadline_str = deadline.strftime('%Y-%m-%d %H:%M:%S')

            if workflow_mode not in ('standard', 'veloce'):
                workflow_mode = 'standard'

            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO brainstorm_sessions
                (task_id, topic, created_by, participants, status, created_at, deadline_at, auto_workflow, project_id, workflow_mode)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """, (task_id, topic, created_by, json.dumps(participants), now, deadline_str,
                  1 if auto_workflow else 0, project_id, workflow_mode))
            session_id = c.lastrowid
            conn.commit()
            logger.info(f"✓ Created brainstorm session #{session_id} [{project_id}] mode={workflow_mode}: {topic[:50]}")
            return session_id
        except Exception as e:
            logger.error(f"Failed to create brainstorm session: {e}")
            return -1

    def add_brainstorm_vote(self, session_id: int, agent_id: str,
                            vote: str, comment: Optional[str] = None) -> bool:
        """Add a vote to a brainstorm session. Updates if already voted."""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO brainstorm_votes (session_id, agent_id, vote, comment, voted_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id, agent_id) DO UPDATE SET
                    vote = excluded.vote,
                    comment = excluded.comment,
                    voted_at = excluded.voted_at
            """, (session_id, agent_id, vote, comment, now))
            conn.commit()
            logger.debug(f"✓ Vote recorded: session #{session_id}, {agent_id} = {vote}")
            return True
        except Exception as e:
            logger.error(f"Failed to add brainstorm vote: {e}")
            return False

    def get_brainstorm_session(self, session_id: int) -> Optional[Dict[str, Any]]:
        """Get a brainstorm session with its votes."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # Get session
            c.execute("SELECT * FROM brainstorm_sessions WHERE id = ?", (session_id,))
            session = c.fetchone()
            if not session:
                return None

            session_dict = dict(session)
            session_dict['participants'] = json.loads(session_dict.get('participants', '[]'))

            # Get votes
            c.execute("""
                SELECT agent_id, vote, comment, voted_at
                FROM brainstorm_votes WHERE session_id = ?
            """, (session_id,))
            votes = [dict(row) for row in c.fetchall()]
            session_dict['votes'] = votes

            return session_dict
        except Exception as e:
            logger.error(f"Failed to get brainstorm session: {e}")
            return None

    def get_active_brainstorm_sessions(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all brainstorm sessions that are still pending.
        If project_id is given, filter by project."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if project_id:
                c.execute("""
                    SELECT * FROM brainstorm_sessions
                    WHERE status = 'pending' AND project_id = ?
                    ORDER BY created_at DESC
                """, (project_id,))
            else:
                c.execute("""
                    SELECT * FROM brainstorm_sessions
                    WHERE status = 'pending'
                    ORDER BY created_at DESC
                """)
            sessions = []
            for row in c.fetchall():
                s = dict(row)
                s['participants'] = json.loads(s.get('participants', '[]'))
                c.execute("SELECT COUNT(*) FROM brainstorm_votes WHERE session_id = ?", (s['id'],))
                s['vote_count'] = c.fetchone()[0]
                sessions.append(s)
            return sessions
        except Exception as e:
            logger.error(f"Failed to get active brainstorm sessions: {e}")
            return []

    def get_expired_brainstorm_sessions(self) -> List[Dict[str, Any]]:
        """Get brainstorm sessions that have passed their deadline."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM brainstorm_sessions
                WHERE status = 'pending'
                AND datetime(deadline_at) < datetime('now')
            """)
            sessions = [dict(row) for row in c.fetchall()]
            for s in sessions:
                s['participants'] = json.loads(s.get('participants', '[]'))
            return sessions
        except Exception as e:
            logger.error(f"Failed to get expired brainstorm sessions: {e}")
            return []

    def close_brainstorm_session(self, session_id: int, consensus: str, status: str = 'completed') -> bool:
        """Close a brainstorm session with final consensus."""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                UPDATE brainstorm_sessions
                SET status = ?, consensus = ?, closed_at = ?
                WHERE id = ?
            """, (status, consensus, now, session_id))
            updated = c.rowcount > 0
            conn.commit()
            logger.info(f"✓ Brainstorm #{session_id} closed: {consensus} ({status})")
            if updated:
                self._persist_critical(f"brainstorm #{session_id} closed")
            return updated
        except Exception as e:
            logger.error(f"Failed to close brainstorm session: {e}")
            return False

    def get_brainstorm_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent brainstorm sessions (completed/timeout)."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM brainstorm_sessions
                WHERE status != 'pending'
                ORDER BY closed_at DESC
                LIMIT ?
            """, (limit,))
            sessions = [dict(row) for row in c.fetchall()]
            return sessions
        except Exception as e:
            logger.error(f"Failed to get brainstorm history: {e}")
            return []

    # ========== Review System Methods (v1.5) ==========

    def create_review_request(self, file_path: str, requested_by: str,
                               reviewers: List[str], review_type: str = "doc",
                               timeout_seconds: int = 3600,
                               project_id: str = "default") -> int:
        """Create a new review request.

        Args:
            file_path: Path to the file being reviewed
            requested_by: Who requested the review
            reviewers: List of agents to review
            review_type: 'doc' (1 approval) or 'code' (2 approvals)
            timeout_seconds: Time before auto-close (default 1h)
            project_id: Project scope
        """
        try:
            now = _sqlite_now()
            deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
            deadline_str = deadline.strftime('%Y-%m-%d %H:%M:%S')

            # Min approvals: 1 for doc, 2 for code
            min_approvals = 2 if review_type == "code" else 1

            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO review_requests
                (file_path, requested_by, reviewers, review_type, min_approvals,
                 status, created_at, deadline_at, project_id)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """, (file_path, requested_by, json.dumps(reviewers), review_type,
                  min_approvals, now, deadline_str, project_id))
            request_id = c.lastrowid
            conn.commit()
            logger.info(f"✓ Created review request #{request_id} [{project_id}]: {file_path} ({review_type})")
            return request_id
        except Exception as e:
            logger.error(f"Failed to create review request: {e}", exc_info=True)
            return -1

    def add_review_response(self, request_id: int, reviewer: str,
                            vote: str, comment: Optional[str] = None) -> bool:
        """Add a response to a review request.

        Args:
            request_id: The review request ID
            reviewer: Who is responding
            vote: 'approve', 'comment', or 'changes'
            comment: Optional comment
        """
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO review_responses (request_id, reviewer, vote, comment, responded_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(request_id, reviewer) DO UPDATE SET
                    vote = excluded.vote,
                    comment = excluded.comment,
                    responded_at = excluded.responded_at
            """, (request_id, reviewer, vote, comment, now))
            conn.commit()
            logger.debug(f"✓ Review response recorded: #{request_id}, {reviewer} = {vote}")
            self._persist_critical(f"review #{request_id} response by {reviewer}")
            return True
        except Exception as e:
            logger.error(f"Failed to add review response: {e}")
            return False

    def get_review_request(self, request_id: int) -> Optional[Dict[str, Any]]:
        """Get a review request with its responses."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            c.execute("SELECT * FROM review_requests WHERE id = ?", (request_id,))
            request = c.fetchone()
            if not request:
                return None

            request_dict = dict(request)
            request_dict['reviewers'] = json.loads(request_dict.get('reviewers', '[]'))

            # Get responses
            c.execute("""
                SELECT reviewer, vote, comment, responded_at
                FROM review_responses WHERE request_id = ?
            """, (request_id,))
            responses = [dict(row) for row in c.fetchall()]
            request_dict['responses'] = responses

            return request_dict
        except Exception as e:
            logger.error(f"Failed to get review request: {e}")
            return None

    def get_active_review_requests(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all pending review requests.
        If project_id is given, filter by project."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if project_id:
                c.execute("""
                    SELECT * FROM review_requests
                    WHERE status = 'pending' AND project_id = ?
                    ORDER BY created_at DESC
                """, (project_id,))
            else:
                c.execute("""
                    SELECT * FROM review_requests
                    WHERE status = 'pending'
                    ORDER BY created_at DESC
                """)
            requests = []
            for row in c.fetchall():
                r = dict(row)
                r['reviewers'] = json.loads(r.get('reviewers', '[]'))
                c.execute("SELECT COUNT(*) FROM review_responses WHERE request_id = ?", (r['id'],))
                r['response_count'] = c.fetchone()[0]
                requests.append(r)
            return requests
        except Exception as e:
            logger.error(f"Failed to get active review requests: {e}")
            return []

    def get_reviews_needing_reminder(self, reminder_after_seconds: int = 1800) -> List[Dict[str, Any]]:
        """Get pending reviews that need a reminder (30min default)."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM review_requests
                WHERE status = 'pending'
                AND reminder_sent = 0
                AND datetime(created_at, '+' || ? || ' seconds') < datetime('now')
            """, (reminder_after_seconds,))
            requests = [dict(row) for row in c.fetchall()]
            for r in requests:
                r['reviewers'] = json.loads(r.get('reviewers', '[]'))
            return requests
        except Exception as e:
            logger.error(f"Failed to get reviews needing reminder: {e}")
            return []

    def mark_review_reminder_sent(self, request_id: int) -> bool:
        """Mark that a reminder has been sent for a review."""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("UPDATE review_requests SET reminder_sent = 1 WHERE id = ?", (request_id,))
            updated = c.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"Failed to mark review reminder: {e}")
            return False

    def get_expired_review_requests(self) -> List[Dict[str, Any]]:
        """Get review requests that have passed their deadline."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM review_requests
                WHERE status = 'pending'
                AND datetime(deadline_at) < datetime('now')
            """)
            requests = [dict(row) for row in c.fetchall()]
            for r in requests:
                r['reviewers'] = json.loads(r.get('reviewers', '[]'))
            return requests
        except Exception as e:
            logger.error(f"Failed to get expired review requests: {e}")
            return []

    def close_review_request(self, request_id: int, consensus: str, status: str = 'completed') -> bool:
        """Close a review request with final consensus."""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                UPDATE review_requests
                SET status = ?, consensus = ?, closed_at = ?
                WHERE id = ?
            """, (status, consensus, now, request_id))
            updated = c.rowcount > 0
            conn.commit()
            logger.info(f"✓ Review #{request_id} closed: {consensus} ({status})")
            if updated:
                self._persist_critical(f"review #{request_id} closed")
            return updated
        except Exception as e:
            logger.error(f"Failed to close review request: {e}")
            return False

    def get_review_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent closed review requests."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM review_requests
                WHERE status != 'pending'
                ORDER BY closed_at DESC
                LIMIT ?
            """, (limit,))
            requests = [dict(row) for row in c.fetchall()]
            return requests
        except Exception as e:
            logger.error(f"Failed to get review history: {e}")
            return []

    # ========== Project Workspace Methods (v3.0) ==========

    def create_project(self, project_id: str, name: str, description: str = "",
                       owner: str = "@operator", brief_path: str = None) -> bool:
        """Create a new project workspace."""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO projects (id, name, description, owner, brief_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (project_id, name, description, owner, brief_path, now, now))
            conn.commit()
            logger.info(f"✓ Created project: {project_id} ({name})")
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Project already exists: {project_id}")
            return False
        except Exception as e:
            logger.error(f"Failed to create project: {e}")
            return False

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Get a single project by ID."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
            row = c.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to get project: {e}")
            return None

    def get_all_projects(self) -> List[Dict[str, Any]]:
        """List all projects."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM projects ORDER BY created_at ASC")
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get projects: {e}")
            return []

    def update_project(self, project_id: str, **kwargs) -> bool:
        """Update project fields (name, description, owner, brief_path)."""
        allowed = {"name", "description", "owner", "brief_path"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        try:
            updates["updated_at"] = _sqlite_now()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [project_id]
            conn = self._get_conn()
            c = conn.cursor()
            c.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", values)
            updated = c.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"Failed to update project: {e}")
            return False

    def delete_project(self, project_id: str) -> bool:
        """Delete a project. Cannot delete 'default'."""
        if project_id == "default":
            logger.warning("Cannot delete the default project")
            return False
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            deleted = c.rowcount > 0
            conn.commit()
            if deleted:
                logger.info(f"✓ Deleted project: {project_id}")
            return deleted
        except Exception as e:
            logger.error(f"Failed to delete project: {e}")
            return False

    def get_agent_active_project(self, agent_id: str) -> str:
        """Get agent's active project. Returns 'default' if not set."""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("SELECT project_id FROM agent_active_project WHERE agent_id = ?",
                       (agent_id,))
            row = c.fetchone()
            return row[0] if row else "default"
        except Exception as e:
            logger.error(f"Failed to get agent project: {e}")
            return "default"

    def set_agent_active_project(self, agent_id: str, project_id: str) -> bool:
        """Set agent's active project."""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO agent_active_project (agent_id, project_id, switched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    switched_at = excluded.switched_at
            """, (agent_id, project_id, now))
            conn.commit()
            logger.info(f"✓ {agent_id} switched to project: {project_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to set agent project: {e}")
            return False

    def get_agents_in_project(self, project_id: str) -> List[str]:
        """Get all agents currently active in a project."""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("SELECT agent_id FROM agent_active_project WHERE project_id = ?",
                       (project_id,))
            agents = [row[0] for row in c.fetchall()]
            return agents
        except Exception as e:
            logger.error(f"Failed to get agents in project: {e}")
            return []

    # ========== Original Methods ==========

    def get_next_room_seq(self, room: str) -> int:
        """Get the next room_seq for a room"""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                SELECT COALESCE(MAX(room_seq), 0) + 1
                FROM messages WHERE room = ?
            """, (room,))
            next_seq = c.fetchone()[0]
            return next_seq
        except Exception as e:
            logger.error(f"Failed to get room_seq: {e}")
            return 1

    def store_message(self, envelope: Dict[str, Any], room_seq: int = None, project_id: str = "default") -> bool:
        """Store a message"""
        try:
            conn = self._get_conn()
            c = conn.cursor()

            message_id = envelope.get("id")
            ts = envelope.get("ts")
            from_id = envelope.get("from", {}).get("id")
            kind = envelope.get("kind")
            room = envelope.get("to", {}).get("room", "direct")

            # Extract content
            payload = envelope.get("payload", {})
            content = payload.get("content", "")

            c.execute("""
                INSERT OR IGNORE INTO messages
                (id, ts, from_id, room, kind, content, payload, envelope, room_seq, project_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id,
                ts,
                from_id,
                room,
                kind,
                content,
                json.dumps(payload),
                json.dumps(envelope),
                room_seq,
                project_id or "default",
            ))

            conn.commit()

            return True
        except Exception as e:
            logger.error(f"Failed to store message: {e}")
            return False

    def get_room_history(self, room: str, limit: int = 100, since_seq: int = None,
                         project_id: str = None, include_compacted: bool = False) -> Dict:
        """Get message history for a room with room_seq support.
        If project_id is provided, only return messages from that project.
        Compacted messages are excluded by default (Compactor v3)."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            extra_clauses = ""
            extra_params = ()
            if project_id:
                extra_clauses += " AND project_id = ?"
                extra_params += (project_id,)
            if not include_compacted:
                extra_clauses += " AND compacted_at IS NULL"

            if since_seq is not None:
                c.execute(f"""
                    SELECT * FROM messages
                    WHERE room = ? AND kind = 'chat' AND room_seq > ?{extra_clauses}
                    ORDER BY room_seq ASC
                    LIMIT ?
                """, (room, since_seq) + extra_params + (limit,))
            else:
                c.execute(f"""
                    SELECT * FROM messages
                    WHERE room = ? AND kind = 'chat'{extra_clauses}
                    ORDER BY room_seq DESC
                    LIMIT ?
                """, (room,) + extra_params + (limit,))

            rows = c.fetchall()

            # Get total room_seq
            c.execute("SELECT COALESCE(MAX(room_seq), 0) FROM messages WHERE room = ?", (room,))
            total = c.fetchone()[0]


            # Build messages with room_seq in meta (as tests expect)
            messages = []
            for row in (rows if since_seq is not None else reversed(rows)):
                row_dict = dict(row)
                # Parse envelope and add room_seq to meta
                try:
                    envelope = json.loads(row_dict.get('envelope', '{}'))
                    if 'meta' not in envelope:
                        envelope['meta'] = {}
                    envelope['meta']['room_seq'] = row_dict.get('room_seq')
                    messages.append(envelope)
                except json.JSONDecodeError:
                    messages.append(row_dict)

            return {
                'messages': messages,
                'since_seq': since_seq,
                'total': total
            }

        except Exception as e:
            logger.error(f"Failed to get history: {e}")
            return {'messages': [], 'since_seq': since_seq, 'total': 0}

    def get_user_history(self, agent_id: str, limit: int = 50) -> List[Dict]:
        """Get message history for an agent"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            c.execute("""
                SELECT * FROM messages
                WHERE from_id = ?
                ORDER BY ts DESC
                LIMIT ?
            """, (agent_id, limit))

            rows = c.fetchall()

            return [dict(row) for row in reversed(rows)]

        except Exception as e:
            logger.error(f"Failed to get user history: {e}")
            return []

    def rebuild_fts(self):
        """Rebuild FTS5 index from existing messages. Call once at startup."""
        try:
            conn = self._get_conn()
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            conn.commit()
            logger.info("✓ FTS5 index rebuilt")
        except Exception as e:
            logger.error(f"FTS5 rebuild failed: {e}")
            try:
                self._get_conn().rollback()
            except Exception:
                pass

    def search_messages(self, query: str, room: Optional[str] = None,
                        agent: Optional[str] = None, day: Optional[str] = None,
                        limit: int = 50) -> List[Dict]:
        """Search messages using FTS5 full-text search"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            sql = """
                SELECT m.* FROM messages m
                JOIN messages_fts f ON m.rowid = f.rowid
                WHERE messages_fts MATCH ?
            """
            params = [query]

            if room:
                sql += " AND m.room = ?"
                params.append(room)
            if agent:
                sql += " AND m.from_id = ?"
                params.append(agent)
            if day:
                sql += " AND date(m.ts) = ?"
                params.append(day)

            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)

            c.execute(sql, params)
            rows = c.fetchall()

            return [dict(row) for row in rows]

        except Exception as e:
            logger.error(f"FTS5 search failed: {e}")
            return []

    def get_message_by_id(self, message_id: str) -> Optional[Dict]:
        """Get a single message by ID"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
            row = c.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to get message by ID: {e}")
            return None

    def get_messages_by_date(self, day: Optional[str] = None, hour: Optional[int] = None,
                             room: Optional[str] = None, agent: Optional[str] = None,
                             limit: int = 100) -> List[Dict]:
        """Get messages by date/hour range"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            sql = "SELECT * FROM messages WHERE kind = 'chat'"
            params = []

            if day:
                sql += " AND date(ts) = ?"
                params.append(day)
            if hour is not None:
                sql += " AND CAST(strftime('%H', ts) AS INTEGER) = ?"
                params.append(hour)
            if room:
                sql += " AND room = ?"
                params.append(room)
            if agent:
                sql += " AND from_id = ?"
                params.append(agent)

            sql += " ORDER BY ts ASC LIMIT ?"
            params.append(limit)

            c.execute(sql, params)
            rows = c.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get messages by date: {e}")
            return []

    def record_session(self, session_id: str, agent_id: str, room: str):
        """Record a session"""
        try:
            conn = self._get_conn()
            c = conn.cursor()

            c.execute("""
                INSERT OR REPLACE INTO sessions
                (session_id, agent_id, room, authenticated, connected_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (session_id, agent_id, room, True))

            conn.commit()
        except Exception as e:
            logger.error(f"Failed to record session: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get system statistics"""
        try:
            conn = self._get_conn()
            c = conn.cursor()

            # Total messages
            c.execute("SELECT COUNT(*) FROM messages WHERE kind = 'chat'")
            total_messages = c.fetchone()[0]

            # Unique agents
            c.execute("SELECT COUNT(DISTINCT from_id) FROM messages")
            unique_agents = c.fetchone()[0]

            # Unique rooms
            c.execute("SELECT COUNT(DISTINCT room) FROM messages")
            unique_rooms = c.fetchone()[0]

            # Messages by kind
            c.execute("""
                SELECT kind, COUNT(*) as count
                FROM messages
                GROUP BY kind
            """)
            kind_stats = {row[0]: row[1] for row in c.fetchall()}

            # Top rooms
            c.execute("""
                SELECT room, COUNT(*) as count
                FROM messages
                WHERE kind = 'chat'
                GROUP BY room
                ORDER BY count DESC
                LIMIT 10
            """)
            top_rooms = {row[0]: row[1] for row in c.fetchall()}

            # Top agents
            c.execute("""
                SELECT from_id, COUNT(*) as count
                FROM messages
                WHERE kind = 'chat'
                GROUP BY from_id
                ORDER BY count DESC
                LIMIT 10
            """)
            top_agents = {row[0]: row[1] for row in c.fetchall()}


            return {
                "total_messages": total_messages,
                "unique_agents": unique_agents,
                "unique_rooms": unique_rooms,
                "messages_by_kind": kind_stats,
                "top_rooms": top_rooms,
                "top_agents": top_agents,
                "database_size": self.db_path.stat().st_size if self.db_path.exists() else 0
            }

        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return {}

    def delete_messages_by_ids(self, message_ids: list) -> int:
        """Delete specific messages by their IDs. Used by compactor."""
        if not message_ids:
            return 0
        try:
            conn = self._get_conn()
            c = conn.cursor()
            placeholders = ",".join("?" for _ in message_ids)
            c.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", message_ids)
            deleted = c.rowcount
            conn.commit()
            logger.info(f"Compactor deleted {deleted} messages by ID")
            return deleted
        except Exception as e:
            logger.error(f"delete_messages_by_ids failed: {e}")
            return 0

    def cleanup_old_messages(self, days: int = 30):
        """Remove messages older than specified days"""
        try:
            conn = self._get_conn()
            c = conn.cursor()

            # Calculate cutoff date
            cutoff = _sqlite_now()
            # In real use, calculate actual date difference

            c.execute("""
                DELETE FROM messages
                WHERE ts < datetime('now', '-' || ? || ' days')
            """, (days,))

            deleted = c.rowcount
            conn.commit()

            logger.info(f"Deleted {deleted} old messages")
            return deleted

        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
            return 0

    # ========== Compactor v3 Methods (Option C: soft-delete + GC) ==========

    def soft_delete_messages(self, message_ids: list) -> int:
        """Mark messages as compacted (soft-delete). They become invisible
        in get_room_history() but remain in DB for audit/rollback.
        Hard-deleted later by gc_compacted()."""
        if not message_ids:
            return 0
        try:
            conn = self._get_conn()
            c = conn.cursor()
            now = _sqlite_now()
            placeholders = ",".join("?" for _ in message_ids)
            c.execute(
                f"UPDATE messages SET compacted_at = ? WHERE id IN ({placeholders})",
                [now] + list(message_ids)
            )
            updated = c.rowcount
            conn.commit()
            logger.info(f"[COMPACTv3] Soft-deleted {updated} messages")
            return updated
        except Exception as e:
            logger.error(f"soft_delete_messages failed: {e}")
            return 0

    def insert_summary_message(self, room: str, summary: str,
                               project_id: str = "default") -> Optional[str]:
        """Insert a compaction summary as a special message.
        Marked with is_summary=1 so it can be identified later."""
        try:
            import uuid
            msg_id = f"compact-{uuid.uuid4().hex[:12]}"
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()

            # Get next room_seq
            c.execute(
                "SELECT COALESCE(MAX(room_seq), 0) + 1 FROM messages WHERE room = ?",
                (room,)
            )
            next_seq = c.fetchone()[0]

            envelope = json.dumps({
                "id": msg_id,
                "ts": now,
                "from": {"id": "@compactor"},
                "room": room,
                "kind": "chat",
                "payload": {"role": "system", "content": summary},
                "meta": {"room_seq": next_seq, "is_summary": True},
            })

            c.execute("""
                INSERT INTO messages (id, ts, from_id, room, kind, content,
                                      payload, envelope, room_seq, is_summary, project_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """, (
                msg_id, now, "@compactor", room, "chat",
                summary, json.dumps({"role": "system", "content": summary}),
                envelope, next_seq, project_id
            ))
            conn.commit()
            logger.info(f"[COMPACTv3] Summary inserted: {msg_id} in {room}")
            return msg_id
        except Exception as e:
            logger.error(f"insert_summary_message failed: {e}")
            return None

    def log_compaction(self, room: str, triggered_by: str,
                       total_before: int, total_after: int,
                       deleted_count: int, compacted_count: int,
                       compression_ratio: str, summary: str,
                       project_id: str = "default"):
        """Record compaction event in audit log."""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO compaction_log
                    (room, project_id, triggered_at, triggered_by,
                     total_before, total_after, deleted_count,
                     compacted_count, compression_ratio, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                room, project_id, _sqlite_now(), triggered_by,
                total_before, total_after, deleted_count,
                compacted_count, compression_ratio, summary
            ))
            conn.commit()
        except Exception as e:
            logger.error(f"log_compaction failed: {e}")

    def gc_compacted(self, retention_days: int = 7) -> int:
        """Hard-delete messages that were soft-deleted more than
        retention_days ago. This is the GC phase of Compactor v3."""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                DELETE FROM messages
                WHERE compacted_at IS NOT NULL
                  AND compacted_at < datetime('now', '-' || ? || ' days')
            """, (retention_days,))
            purged = c.rowcount
            conn.commit()
            if purged > 0:
                logger.info(f"[GC] Purged {purged} compacted messages (>{retention_days}d)")
            return purged
        except Exception as e:
            logger.error(f"gc_compacted failed: {e}")
            return 0

    def get_compaction_stats(self, room: str = None) -> Dict:
        """Get compaction statistics for dashboard/status endpoint."""
        try:
            conn = self._get_conn()
            c = conn.cursor()

            room_clause = ""
            room_params = ()
            if room:
                room_clause = " WHERE room = ?"
                room_params = (room,)

            # Active (visible) messages
            c.execute(
                f"SELECT COUNT(*) FROM messages{room_clause}"
                + (" AND" if room else " WHERE") + " compacted_at IS NULL",
                room_params
            )
            active = c.fetchone()[0]

            # Soft-deleted (pending GC)
            c.execute(
                f"SELECT COUNT(*) FROM messages{room_clause}"
                + (" AND" if room else " WHERE") + " compacted_at IS NOT NULL",
                room_params
            )
            pending_gc = c.fetchone()[0]

            # Summaries
            c.execute(
                f"SELECT COUNT(*) FROM messages{room_clause}"
                + (" AND" if room else " WHERE") + " is_summary = 1",
                room_params
            )
            summaries = c.fetchone()[0]

            # Recent compaction log
            c.execute("""
                SELECT * FROM compaction_log
                ORDER BY id DESC LIMIT 5
            """)
            recent = []
            for row in c.fetchall():
                recent.append({
                    "id": row[0], "room": row[1], "triggered_at": row[3],
                    "triggered_by": row[4], "deleted": row[7],
                    "compacted": row[8], "ratio": row[9],
                })

            return {
                "active_messages": active,
                "pending_gc": pending_gc,
                "summaries": summaries,
                "total": active + pending_gc,
                "recent_compactions": recent,
            }
        except Exception as e:
            logger.error(f"get_compaction_stats failed: {e}")
            return {}

    def export_json(self, room: str, filename: str):
        """Export room history as JSON"""
        try:
            history = self.get_room_history(room, limit=1000)

            with open(filename, "w") as f:
                json.dump(history, f, indent=2, default=str)

            logger.info(f"✓ Exported {len(history)} messages to {filename}")
            return True

        except Exception as e:
            logger.error(f"Export failed: {e}")
            return False

    # ========== Passive Observability Methods (v2.0) ==========

    # Throttle: minimum seconds between activity updates for same agent
    _ACTIVITY_THROTTLE_SECONDS = 10

    def update_inferred_activity(self, agent_id: str, activity: str,
                                  context: str = "") -> bool:
        """Update inferred agent activity from API calls.
        Throttled to max 1 update per 10s per agent to avoid SQLite spam.

        Args:
            agent_id: The agent (e.g. "@alpha")
            activity: Inferred activity ("coding", "reviewing", "brainstorming",
                      "chatting", "reading", "idle")
            context: What they're doing (e.g. "task #3", "review #6")
        """
        if agent_id and not agent_id.startswith("@"):
            agent_id = f"@{agent_id}"
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()

            # Throttle check: skip if last update was < 10s ago for same activity
            c.execute("""
                SELECT activity, last_seen FROM agent_activity
                WHERE agent_id = ?
            """, (agent_id,))
            existing = c.fetchone()
            if existing:
                prev_activity, prev_seen = existing
                if prev_activity == activity:
                    seconds_since = self._seconds_since(prev_seen)
                    if seconds_since < self._ACTIVITY_THROTTLE_SECONDS:
                        return False  # Throttled, skip

            # Upsert activity
            c.execute("""
                INSERT INTO agent_activity (agent_id, activity, context, started_at, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    activity = CASE
                        WHEN excluded.activity != agent_activity.activity
                        THEN excluded.activity
                        ELSE agent_activity.activity
                    END,
                    context = excluded.context,
                    started_at = CASE
                        WHEN excluded.activity != agent_activity.activity
                        THEN excluded.started_at
                        ELSE agent_activity.started_at
                    END,
                    last_seen = excluded.last_seen
            """, (agent_id, activity, context, now, now))
            conn.commit()
            logger.debug(f"✓ Inferred activity: {agent_id} → {activity} ({context})")
            return True
        except Exception as e:
            logger.error(f"Failed to update inferred activity: {e}")
            return False

    def get_all_agent_activity(self) -> List[Dict[str, Any]]:
        """Get inferred activity for all agents with computed 'since' field."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM agent_activity ORDER BY last_seen DESC")
            rows = c.fetchall()

            results = []
            for row in rows:
                d = dict(row)
                seconds = self._seconds_since(d.get("last_seen", ""))
                d["since"] = self._format_time_ago(seconds)
                d["seconds_since"] = int(seconds)
                # Auto-downgrade to idle/away based on inactivity
                if seconds > 300:
                    d["activity"] = "away"
                elif seconds > 120:
                    d["activity"] = "idle"
                results.append(d)
            return results
        except Exception as e:
            logger.error(f"Failed to get agent activity: {e}")
            return []

    # ========== LLM Usage Tracking ==========

    def record_llm_usage(self, agent_id: str, provider: str, model: str,
                         prompt_tokens: int = None, completion_tokens: int = None,
                         estimated: bool = False, latency_ms: int = None) -> bool:
        """Record one LLM call's token usage."""
        try:
            total = None
            if prompt_tokens is not None and completion_tokens is not None:
                total = prompt_tokens + completion_tokens
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO llm_usage
                    (agent_id, provider, model, prompt_tokens, completion_tokens,
                     total_tokens, estimated, latency_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (agent_id, provider, model, prompt_tokens, completion_tokens,
                  total, 1 if estimated else 0, latency_ms, _sqlite_now()))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"record_llm_usage failed: {e}")
            return False

    def get_llm_usage_stats(self, agent_id: str = None, minutes: int = None,
                            group_by: str = "agent") -> List[Dict[str, Any]]:
        """Aggregated usage stats, optionally filtered by agent and time window."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            conditions = []
            params = []
            if agent_id:
                conditions.append("agent_id = ?")
                params.append(agent_id)
            if minutes:
                conditions.append("created_at >= datetime('now', '-' || ? || ' minutes')")
                params.append(minutes)

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            if group_by == "model":
                group_col = "model"
            else:
                group_col = "agent_id"

            c.execute(f"""
                SELECT {group_col} AS group_key,
                       COUNT(*) AS call_count,
                       COALESCE(SUM(prompt_tokens), 0) AS total_prompt,
                       COALESCE(SUM(completion_tokens), 0) AS total_completion,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       ROUND(AVG(latency_ms)) AS avg_latency_ms,
                       MAX(created_at) AS last_call
                FROM llm_usage {where}
                GROUP BY {group_col}
                ORDER BY total_tokens DESC
            """, params)
            return [dict(r) for r in c.fetchall()]
        except Exception as e:
            logger.error(f"get_llm_usage_stats failed: {e}")
            return []

    def get_llm_usage_timeline(self, agent_id: str = None, minutes: int = 60,
                               bucket_minutes: int = 1) -> List[Dict[str, Any]]:
        """Token usage bucketed by time interval for timeline/charts."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            conditions = ["created_at >= datetime('now', '-' || ? || ' minutes')"]
            params: list = [minutes]
            if agent_id:
                conditions.append("agent_id = ?")
                params.append(agent_id)

            where = "WHERE " + " AND ".join(conditions)

            c.execute(f"""
                SELECT strftime('%Y-%m-%d %H:', created_at)
                       || (CAST(strftime('%M', created_at) AS INTEGER) / ? * ?)
                       AS bucket,
                       COUNT(*) AS call_count,
                       COALESCE(SUM(prompt_tokens), 0) AS total_prompt,
                       COALESCE(SUM(completion_tokens), 0) AS total_completion,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM llm_usage {where}
                GROUP BY bucket
                ORDER BY bucket ASC
            """, [bucket_minutes, bucket_minutes] + params)
            return [dict(r) for r in c.fetchall()]
        except Exception as e:
            logger.error(f"get_llm_usage_timeline failed: {e}")
            return []

    def cleanup_old_usage(self, days: int = 7) -> int:
        """Remove llm_usage entries older than N days."""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                DELETE FROM llm_usage
                WHERE created_at < datetime('now', '-' || ? || ' days')
            """, (days,))
            purged = c.rowcount
            conn.commit()
            if purged > 0:
                logger.info(f"[GC] Purged {purged} old llm_usage rows (>{days}d)")
            return purged
        except Exception as e:
            logger.error(f"cleanup_old_usage failed: {e}")
            return 0

    def get_agents_active_since(self, seconds: int = 120) -> List[str]:
        """Get agent IDs that had activity within the last N seconds."""
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                SELECT agent_id FROM agent_activity
                WHERE datetime(last_seen) > datetime('now', '-' || ? || ' seconds')
            """, (seconds,))
            agents = [row[0] for row in c.fetchall()]
            return agents
        except Exception as e:
            logger.error(f"Failed to get recently active agents: {e}")
            return []

    def can_safely_restart(self) -> dict:
        """Check if daemon can be safely restarted.

        Returns dict with:
            safe: bool - True if safe to restart
            reason: str - Human-readable reason
            blockers: list - What's blocking the restart
        """
        blockers = []

        try:
            # 1. Check active tasks (in_progress)
            active_tasks = self.get_active_tasks()
            if active_tasks:
                agents = list(set(t["agent_id"] for t in active_tasks))
                blockers.append({
                    "type": "active_tasks",
                    "count": len(active_tasks),
                    "agents": agents,
                    "details": [
                        {"id": t["id"], "agent": t["agent_id"],
                         "desc": (t.get("description") or "")[:60]}
                        for t in active_tasks
                    ]
                })

            # 2. Check pending reviews
            pending_reviews = self.get_active_review_requests()
            if pending_reviews:
                blockers.append({
                    "type": "pending_reviews",
                    "count": len(pending_reviews),
                    "details": [
                        {"id": r["id"], "file": r.get("file_path", ""),
                         "by": r.get("requested_by", "")}
                        for r in pending_reviews
                    ]
                })

            # 3. Check recent activity (< 2 min)
            recent_agents = self.get_agents_active_since(seconds=120)
            if recent_agents:
                blockers.append({
                    "type": "recent_activity",
                    "agents": recent_agents,
                    "message": f"Activity in last 2min from: {', '.join(recent_agents)}"
                })

            # 4. Check active brainstorm sessions
            active_brainstorms = self.get_active_brainstorm_sessions()
            if active_brainstorms:
                blockers.append({
                    "type": "active_brainstorms",
                    "count": len(active_brainstorms)
                })

        except Exception as e:
            logger.error(f"Error checking restart safety: {e}")
            return {
                "safe": False,
                "reason": f"Error checking: {e}",
                "blockers": []
            }

        if blockers:
            reasons = []
            for b in blockers:
                if b["type"] == "active_tasks":
                    reasons.append(f"{b['count']} task(s) in progress ({', '.join(b['agents'])})")
                elif b["type"] == "pending_reviews":
                    reasons.append(f"{b['count']} review(s) pending")
                elif b["type"] == "recent_activity":
                    reasons.append(f"Recent activity from {', '.join(b['agents'])}")
                elif b["type"] == "active_brainstorms":
                    reasons.append(f"{b['count']} brainstorm(s) active")
            return {
                "safe": False,
                "reason": "; ".join(reasons),
                "blockers": blockers
            }

        return {
            "safe": True,
            "reason": "No active tasks, reviews, or recent activity. Safe to restart.",
            "blockers": []
        }

    # NOTE: close() is defined at top and checkpoints WAL + closes connection.

    # ========== Git Agent Mode Methods (IDEA #5 Phase 1) ==========

    def add_git_repo(self, name: str, owner: str, source: str = "github",
                     api_url: str = "https://api.github.com",
                     html_url: str = "",
                     default_branch: str = "main") -> int:
        """Register a git repository for agent monitoring.

        Args:
            name: Repo name (e.g. "aircp").
            owner: Repo owner (e.g. "hdds-team").
            source: Provider type ("github" or "gitea").
            api_url: API base URL.
            html_url: Web URL for the repo.
            default_branch: Default branch name.

        Returns:
            Repo ID, or -1 on error.
        """
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT OR IGNORE INTO git_repos
                (name, owner, source, api_url, html_url, default_branch, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, owner, source, api_url, html_url, default_branch, now, now))
            repo_id = c.lastrowid
            conn.commit()
            if repo_id:
                logger.info(f"[git] Registered repo {owner}/{name} (id={repo_id}, source={source})")
            return repo_id or -1
        except Exception as e:
            logger.error(f"Failed to add git repo: {e}")
            return -1

    def get_git_repo(self, name: str = None, repo_id: int = None) -> Optional[Dict[str, Any]]:
        """Get a git repo by name or ID."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if repo_id is not None:
                c.execute("SELECT * FROM git_repos WHERE id = ?", (repo_id,))
            elif name is not None:
                c.execute("SELECT * FROM git_repos WHERE name = ?", (name,))
            else:
                return None
            row = c.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to get git repo: {e}")
            return None

    def get_all_git_repos(self, enabled_only: bool = True) -> List[Dict[str, Any]]:
        """List all registered git repos."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if enabled_only:
                c.execute("SELECT * FROM git_repos WHERE enabled = 1 ORDER BY name")
            else:
                c.execute("SELECT * FROM git_repos ORDER BY name")
            return [dict(row) for row in c.fetchall()]
        except Exception as e:
            logger.error(f"Failed to list git repos: {e}")
            return []

    def log_git_event(self, event_type: str, actor_id: str = "",
                      repo_id: int = None, issue_number: int = None,
                      details: Dict = None,
                      project_id: str = "default") -> int:
        """Log a git agent event to the audit trail.

        Args:
            event_type: Event type (e.g. "list_issues", "assign", "comment",
                        "approve", "reject", "create_pr", "close_issue").
            actor_id: Agent or user who triggered the event.
            repo_id: Associated repo ID (optional).
            issue_number: Associated issue number (optional).
            details: JSON-serializable details dict.
            project_id: Project scope.

        Returns:
            Event ID, or -1 on error.
        """
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO git_events
                (repo_id, event_type, actor_id, issue_number, details, project_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (repo_id, event_type, actor_id, issue_number,
                  json.dumps(details) if details else None,
                  project_id, now))
            event_id = c.lastrowid
            conn.commit()
            logger.debug(f"[git] Event logged: {event_type} by {actor_id} (id={event_id})")
            return event_id
        except Exception as e:
            logger.error(f"Failed to log git event: {e}")
            return -1

    def get_git_events(self, repo_id: int = None, event_type: str = None,
                       actor_id: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Query git events with optional filters."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            where = []
            params = []
            if repo_id is not None:
                where.append("repo_id = ?")
                params.append(repo_id)
            if event_type:
                where.append("event_type = ?")
                params.append(event_type)
            if actor_id:
                where.append("actor_id = ?")
                params.append(actor_id)
            clause = f"WHERE {' AND '.join(where)}" if where else ""
            params.append(limit)
            c.execute(f"""
                SELECT * FROM git_events {clause}
                ORDER BY created_at DESC LIMIT ?
            """, params)
            rows = c.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                if d.get("details"):
                    try:
                        d["details"] = json.loads(d["details"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(d)
            return result
        except Exception as e:
            logger.error(f"Failed to get git events: {e}")
            return []

    def cache_issues(self, repo_id: int, issues: List[Dict[str, Any]]) -> int:
        """Bulk upsert cached issues from GitHub API.

        Args:
            repo_id: Repo ID from git_repos table.
            issues: List of issue dicts with keys matching git_issue_cache columns.

        Returns:
            Number of issues cached.
        """
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            count = 0
            for issue in issues:
                c.execute("""
                    INSERT INTO git_issue_cache
                    (repo_id, issue_number, title, body, state, labels, assignees,
                     author_login, comments_count, html_url, created_at, updated_at, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo_id, issue_number) DO UPDATE SET
                        title = excluded.title,
                        body = excluded.body,
                        state = excluded.state,
                        labels = excluded.labels,
                        assignees = excluded.assignees,
                        comments_count = excluded.comments_count,
                        html_url = excluded.html_url,
                        updated_at = excluded.updated_at,
                        cached_at = excluded.cached_at
                """, (
                    repo_id,
                    issue.get("number", 0),
                    issue.get("title", ""),
                    issue.get("body", ""),
                    issue.get("state", "open"),
                    json.dumps(issue.get("labels", [])),
                    json.dumps(issue.get("assignees", [])),
                    issue.get("author_login", ""),
                    issue.get("comments_count", 0),
                    issue.get("html_url", ""),
                    issue.get("created_at", now),
                    issue.get("updated_at", now),
                    now,
                ))
                count += 1
            conn.commit()
            logger.debug(f"[git] Cached {count} issues for repo_id={repo_id}")
            return count
        except Exception as e:
            logger.error(f"Failed to cache issues: {e}")
            return 0

    def get_cached_issues(self, repo_id: int, state: str = "open") -> List[Dict[str, Any]]:
        """Get cached issues for a repo, optionally filtered by state."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if state == "all":
                c.execute("""
                    SELECT * FROM git_issue_cache
                    WHERE repo_id = ?
                    ORDER BY updated_at DESC
                """, (repo_id,))
            else:
                c.execute("""
                    SELECT * FROM git_issue_cache
                    WHERE repo_id = ? AND state = ?
                    ORDER BY updated_at DESC
                """, (repo_id, state))
            rows = c.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                for field in ("labels", "assignees"):
                    if d.get(field):
                        try:
                            d[field] = json.loads(d[field])
                        except (json.JSONDecodeError, TypeError):
                            pass
                result.append(d)
            return result
        except Exception as e:
            logger.error(f"Failed to get cached issues: {e}")
            return []

    def get_cached_issue(self, repo_id: int, issue_number: int) -> Optional[Dict[str, Any]]:
        """Get a single cached issue."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM git_issue_cache
                WHERE repo_id = ? AND issue_number = ?
            """, (repo_id, issue_number))
            row = c.fetchone()
            if not row:
                return None
            d = dict(row)
            for field in ("labels", "assignees"):
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            return d
        except Exception as e:
            logger.error(f"Failed to get cached issue: {e}")
            return None

    def queue_git_action(self, repo_id: int, action_type: str, actor_id: str,
                         params: Dict = None, issue_number: int = None,
                         preview: str = "") -> int:
        """Queue a write action for dashboard approval.

        Args:
            repo_id: Target repo ID.
            action_type: Action type ("comment", "add_label", "create_pr").
            actor_id: Agent requesting the action.
            params: Action parameters (JSON-serializable).
            issue_number: Associated issue number.
            preview: Human-readable preview of what the action will do.

        Returns:
            Action ID, or -1 on error.
        """
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO git_actions_queue
                (repo_id, issue_number, action_type, actor_id, params, preview, status, queued_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (repo_id, issue_number, action_type, actor_id,
                  json.dumps(params) if params else "{}",
                  preview, now))
            action_id = c.lastrowid
            conn.commit()
            logger.info(
                f"[git] Queued action {action_type} by {actor_id} "
                f"(id={action_id}, issue=#{issue_number})"
            )
            return action_id
        except Exception as e:
            logger.error(f"Failed to queue git action: {e}")
            return -1

    def get_pending_git_actions(self, repo_id: int = None) -> List[Dict[str, Any]]:
        """Get all pending (unapproved) actions in the queue."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if repo_id is not None:
                c.execute("""
                    SELECT * FROM git_actions_queue
                    WHERE status = 'pending' AND repo_id = ?
                    ORDER BY queued_at ASC
                """, (repo_id,))
            else:
                c.execute("""
                    SELECT * FROM git_actions_queue
                    WHERE status = 'pending'
                    ORDER BY queued_at ASC
                """)
            rows = c.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                if d.get("params"):
                    try:
                        d["params"] = json.loads(d["params"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(d)
            return result
        except Exception as e:
            logger.error(f"Failed to get pending git actions: {e}")
            return []

    def approve_git_action(self, action_id: int, approved_by: str) -> bool:
        """Approve a pending action (marks it ready for execution)."""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                UPDATE git_actions_queue
                SET status = 'approved', approved_by = ?, decided_at = ?
                WHERE id = ? AND status = 'pending'
            """, (approved_by, now, action_id))
            updated = c.rowcount > 0
            conn.commit()
            if updated:
                logger.info(f"[git] Action {action_id} approved by {approved_by}")
            return updated
        except Exception as e:
            logger.error(f"Failed to approve git action: {e}")
            return False

    def reject_git_action(self, action_id: int, rejected_by: str) -> bool:
        """Reject a pending action."""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                UPDATE git_actions_queue
                SET status = 'rejected', rejected_by = ?, decided_at = ?
                WHERE id = ? AND status = 'pending'
            """, (rejected_by, now, action_id))
            updated = c.rowcount > 0
            conn.commit()
            if updated:
                logger.info(f"[git] Action {action_id} rejected by {rejected_by}")
            return updated
        except Exception as e:
            logger.error(f"Failed to reject git action: {e}")
            return False

    def mark_git_action_executed(self, action_id: int, result: str = "") -> bool:
        """Mark an approved action as executed."""
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                UPDATE git_actions_queue
                SET status = 'executed', result = ?, executed_at = ?
                WHERE id = ? AND status = 'approved'
            """, (result, now, action_id))
            updated = c.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"Failed to mark git action executed: {e}")
            return False

    def get_git_action(self, action_id: int) -> Optional[Dict[str, Any]]:
        """Get a single action from the queue by ID."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM git_actions_queue WHERE id = ?", (action_id,))
            row = c.fetchone()
            if not row:
                return None
            d = dict(row)
            if d.get("params"):
                try:
                    d["params"] = json.loads(d["params"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return d
        except Exception as e:
            logger.error(f"Failed to get git action: {e}")
            return None

    def assign_agent_to_issue(self, repo_id: int, issue_number: int,
                              agent_id: str, role: str = "investigate",
                              task_id: int = None,
                              assigned_by: str = "@naskel") -> int:
        """Assign an agent to a GitHub issue.

        Args:
            repo_id: Repo ID.
            issue_number: Issue number.
            agent_id: Agent to assign.
            role: Agent role ("triage", "investigate", "code", "review").
            task_id: Linked AIRCP task ID (if auto-created).
            assigned_by: Who assigned (usually @naskel via dashboard).

        Returns:
            Assignment ID, or -1 on error.
        """
        try:
            now = _sqlite_now()
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO git_issue_assignments
                (repo_id, issue_number, agent_id, role, task_id, assigned_by, assigned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, issue_number, agent_id) DO UPDATE SET
                    role = excluded.role,
                    task_id = excluded.task_id,
                    assigned_at = excluded.assigned_at
            """, (repo_id, issue_number, agent_id, role, task_id, assigned_by, now))
            assignment_id = c.lastrowid
            conn.commit()
            logger.info(
                f"[git] Assigned {agent_id} ({role}) to issue #{issue_number} "
                f"(repo_id={repo_id})"
            )
            return assignment_id
        except Exception as e:
            logger.error(f"Failed to assign agent to issue: {e}")
            return -1

    def get_issue_assignments(self, repo_id: int,
                              issue_number: int) -> List[Dict[str, Any]]:
        """Get all agent assignments for an issue."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT * FROM git_issue_assignments
                WHERE repo_id = ? AND issue_number = ?
                ORDER BY assigned_at ASC
            """, (repo_id, issue_number))
            return [dict(row) for row in c.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get issue assignments: {e}")
            return []

    def is_git_action_approved(self, action_type: str, params: Dict) -> bool:
        """Check if a specific action has been approved in the queue.

        Used by DryRunGate's approval_checker callback.
        Matches on action_type and params JSON equality.
        """
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute("""
                SELECT id FROM git_actions_queue
                WHERE action_type = ? AND params = ? AND status = 'approved'
                ORDER BY decided_at DESC LIMIT 1
            """, (action_type, json.dumps(params)))
            return c.fetchone() is not None
        except Exception as e:
            logger.error(f"Failed to check action approval: {e}")
            return False


if __name__ == "__main__":
    # Test storage
    logging.basicConfig(level=logging.INFO)

    storage = AIRCPStorage("test_aircp.db")

    # Sample message
    sample_envelope = {
        "id": "test-123",
        "ts": _sqlite_now(),
        "from": {"type": "agent", "id": "@test"},
        "to": {"room": "#general", "broadcast": True},
        "kind": "chat",
        "payload": {"content": "Test message"},
        "meta": {"protocol_version": "0.1.0"}
    }

    # Store
    storage.store_message(sample_envelope)

    # Get stats
    stats = storage.get_stats()
    print(f"\nStats: {json.dumps(stats, indent=2)}")

    # Get history
    history = storage.get_room_history("#general", limit=10)
    print(f"\nHistory ({len(history)} messages):")
    for msg in history:
        print(f"  {msg.get('from_id')}: {msg.get('content')}")

    # ========== Test MODES.md v0.3 features ==========
    print("\n--- Testing MODES.md v0.3 features ---")

    # Test mode state
    storage.set_mode_state("@dev", "@alpha", None)
    state = storage.get_mode_state()
    print(f"\nMode state: {state}")

    # Change mode (triggers history)
    storage.set_mode_state("@brainstorm", "@sonnet", None)
    state = storage.get_mode_state()
    print(f"New mode state: {state}")

    # Check history
    history = storage.get_mode_history(limit=5)
    print(f"\nMode history: {history}")

    # Test pending asks
    ask_id = storage.add_pending_ask("@alpha", "@codex", "Review this?")
    print(f"\nAdded pending ask: {ask_id}")

    asks = storage.get_pending_asks()
    print(f"Pending asks: {asks}")

    # Clear asks (mode change simulation)
    cleared = storage.clear_pending_asks()
    print(f"Cleared {cleared} asks")

    # ========== Test TaskManager features ==========
    print("\n--- Testing TaskManager features (v0.8) ---")

    # Create task
    task_id = storage.create_task("@alpha", "patch", "Implement task_watchdog()",
                                   {"files": ["aircp_daemon.py"]})
    print(f"\nCreated task: {task_id}")

    # Claim task
    claimed = storage.claim_task(task_id, "@alpha")
    print(f"Claimed: {claimed}")

    # Get active tasks
    active = storage.get_active_tasks()
    print(f"Active tasks: {active}")

    # v0.8: Test current_step persistence
    updated = storage.update_task_activity(task_id, current_step=3)
    print(f"Updated with step 3: {updated}")

    # Verify step was persisted
    tasks = storage.get_agent_tasks("@alpha", status="in_progress")
    if tasks:
        print(f"Task current_step: {tasks[0].get('current_step')}")

    # Test anti-spam: ping the task
    pinged = storage.update_task_pinged(task_id)
    print(f"Pinged task: {pinged}")

    # Get stale tasks (should NOT return our task yet - just pinged)
    stale = storage.get_stale_tasks(stale_seconds=1, min_ping_interval=300)
    print(f"Stale tasks (should be empty): {stale}")

    # Complete task
    completed = storage.complete_task(task_id, "done")
    print(f"Completed: {completed}")
