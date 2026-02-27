"""
TaskWorkerMixin - Enables autonomous task execution in heartbeat loop.

P0 Implementation: Working Phase Heartbeat
Transforms agents from reactive (wait for ping) to proactive (work between pings).

Usage:
    class MyAgent(TaskWorkerMixin, PersistentAgent):
        pass

Fixes v2 (per @codex review):
- current_step now persisted via _report_activity()
- I/O calls wrapped in asyncio.to_thread() to avoid blocking
- Proper error handling with status reporting

Fix v3:
- _claim_task() now includes agent_id (required for claim validation)

Fix v4 (Phase 2 - current_step persistence):
- Read current_step from task dict directly (persisted in DB via v0.8 API)
- Fallback to context["current_step"] for backwards compatibility
- See docs/TASKMANAGER.md for full spec
"""

import asyncio
import json
import logging
import os
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Constants
TASKMANAGER_URL = "http://localhost:5555"
CHUNK_TIMEOUT_SECONDS = 60  # Max time per work chunk
MAX_CHUNKS_PER_HEARTBEAT = 1  # Process 1 task step per heartbeat (non-blocking)
HTTP_TIMEOUT = 5  # HTTP request timeout


def _aircp_auth_token() -> str | None:
    token = os.environ.get("AIRCP_AUTH_TOKEN", "").strip()
    if token:
        return token
    tokens = [t.strip() for t in os.environ.get("AIRCP_AUTH_TOKENS", "").split(",") if t.strip()]
    return tokens[0] if tokens else None


def _apply_aircp_auth_header(req: urllib.request.Request) -> None:
    token = _aircp_auth_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")


class TaskWorkerMixin(ABC):
    """
    Mixin that adds autonomous task execution to agents.
    
    Must be mixed with PersistentAgent (or subclass).
    Requires: self.config.id
    """
    
    # Track current work state
    _current_task: Optional[Dict[str, Any]] = None
    _work_start_time: float = 0
    
    # --- Sync HTTP helpers (run in thread) ---
    
    def _sync_http_get(self, url: str) -> Dict[str, Any]:
        """Synchronous HTTP GET - to be called via asyncio.to_thread()."""
        from aircp_http import safe_urlopen
        req = urllib.request.Request(url, method="GET")
        req.add_header("Content-Type", "application/json")
        _apply_aircp_auth_header(req)
        with safe_urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())

    def _sync_http_post(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Synchronous HTTP POST - to be called via asyncio.to_thread()."""
        from aircp_http import safe_urlopen
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        _apply_aircp_auth_header(req)
        with safe_urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    
    # --- Async TaskManager API ---
    
    async def _fetch_my_tasks(self, status: str = "in_progress") -> List[Dict[str, Any]]:
        """Fetch tasks assigned to this agent from TaskManager (non-blocking)."""
        try:
            url = f"{TASKMANAGER_URL}/tasks?agent={self.config.id}&status={status}"
            data = await asyncio.to_thread(self._sync_http_get, url)
            return data.get("tasks", [])
        except urllib.error.URLError as e:
            logger.debug(f"TaskManager unreachable: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to fetch tasks: {e}")
            return []
    
    async def _report_activity(
        self, 
        task_id: int, 
        step: Optional[int] = None, 
        progress: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
        error: Optional[str] = None
    ) -> bool:
        """
        Send activity heartbeat to TaskManager with optional progress info.
        
        Args:
            task_id: Task identifier
            step: Current step number (persisted as current_step in TaskManager)
            progress: Additional progress metadata
            status: Optional status update (e.g., "error", "blocked")
            error: Error message if something went wrong
        """
        try:
            payload: Dict[str, Any] = {"task_id": task_id}
            
            # Include step for persistence (this is the source of truth)
            if step is not None:
                payload["current_step"] = step
            
            # Include progress metadata
            if progress:
                payload["progress"] = progress
            
            # Include status if provided (for error reporting)
            if status:
                payload["status"] = status
            
            # Include error message
            if error:
                payload["error"] = error
            
            url = f"{TASKMANAGER_URL}/task/activity"
            result = await asyncio.to_thread(self._sync_http_post, url, payload)
            return result.get("success", False)
        except Exception as e:
            logger.error(f"Failed to report activity: {e}")
            return False
    
    async def _complete_task(self, task_id: int, status: str = "done", result: Any = None) -> bool:
        """Mark task as complete in TaskManager (non-blocking)."""
        try:
            payload: Dict[str, Any] = {"task_id": task_id, "status": status}
            if result is not None:
                payload["result"] = result
            
            url = f"{TASKMANAGER_URL}/task/complete"
            resp = await asyncio.to_thread(self._sync_http_post, url, payload)
            return resp.get("success", False)
        except Exception as e:
            logger.error(f"Failed to complete task: {e}")
            return False
    
    async def _claim_task(self, task_id: int) -> bool:
        """Claim a pending task (non-blocking)."""
        try:
            url = f"{TASKMANAGER_URL}/task/claim"
            # Include agent_id - required for claim validation in TaskManager
            payload = {
                "task_id": task_id,
                "agent_id": self.config.id
            }
            result = await asyncio.to_thread(self._sync_http_post, url, payload)
            return result.get("success", False)
        except Exception as e:
            logger.error(f"Failed to claim task {task_id}: {e}")
            return False
    
    def _should_work_on(self, task: Dict[str, Any]) -> bool:
        """
        Determine if we should work on this task.
        
        Checks:
        - Task is assigned to us
        - Task is in workable status (pending or in_progress)
        """
        # Check assignment
        task_agent = task.get("agent_id", "").lstrip("@").lower()
        my_id = self.config.id.lower()
        
        if task_agent != my_id:
            logger.debug(f"Task {task.get('id')} not assigned to us ({task_agent} != {my_id})")
            return False
        
        # Check status
        status = task.get("status", "")
        if status not in ("pending", "in_progress"):
            logger.debug(f"Task {task.get('id')} not workable (status={status})")
            return False
        
        return True
    
    @abstractmethod
    async def _execute_task_step(self, task: Dict[str, Any], step: int) -> Dict[str, Any]:
        """
        Execute one step of a task. MUST be implemented by subclass.
        
        Args:
            task: Task dict from TaskManager
            step: Current step number (0-indexed)
        
        Returns:
            Dict with:
                - "done": bool - True if task is complete
                - "next_step": int | None - Next step to execute (default: step + 1)
                - "error": str | None - Error message if failed
                - "result": Any - Step result (for logging/context)
        
        IMPORTANT: 
        - This should be a quick operation (< CHUNK_TIMEOUT_SECONDS).
        - For long tasks, break into multiple steps.
        - The mixin handles persistence of next_step via _report_activity().
        """
        pass
    
    async def _work_on_task(self, task: Dict[str, Any]) -> bool:
        """
        Execute one work chunk on a task.
        
        Returns True if task completed (success or failure), False if more work needed.
        """
        task_id = task.get("id")
        description = task.get("description", "unknown task")
        
        # Get current step - prefer direct field (v0.8), fallback to context for compat
        current_step = task.get("current_step")
        if current_step is None:
            # Backwards compatibility: check context JSON
            context = task.get("context")
            if isinstance(context, str):
                try:
                    context = json.loads(context)
                except:
                    context = {}
            context = context or {}
            current_step = context.get("current_step", 0)
        
        logger.info(f"Working on task {task_id}: {description[:50]}... (step {current_step})")
        
        # Record start time for timeout
        self._work_start_time = time.time()
        self._current_task = task
        
        try:
            # Execute one step with timeout
            step_result = await asyncio.wait_for(
                self._execute_task_step(task, current_step),
                timeout=CHUNK_TIMEOUT_SECONDS
            )
            
            # Check if done
            if step_result.get("done"):
                logger.info(f"Task {task_id} completed!")
                await self._complete_task(task_id, "done", step_result.get("result"))
                return True
            
            # Check for error
            if step_result.get("error"):
                error_msg = step_result["error"]
                logger.error(f"Task {task_id} failed: {error_msg}")
                # Report error with context before completing
                await self._report_activity(task_id, step=current_step, status="error", error=error_msg)
                await self._complete_task(task_id, "failed", {"error": error_msg})
                return True  # Done (failed)
            
            # More work to do - persist next_step
            next_step = step_result.get("next_step")
            if next_step is None:
                next_step = current_step + 1
            
            # Report activity with updated step (SOURCE OF TRUTH for current_step)
            await self._report_activity(
                task_id, 
                step=next_step,
                progress={"last_result": str(step_result.get("result", ""))[:200]}
            )
            
            logger.debug(f"Task {task_id} step {current_step} done, next: {next_step}")
            return False
            
        except asyncio.TimeoutError:
            logger.warning(f"Task {task_id} step {current_step} timed out after {CHUNK_TIMEOUT_SECONDS}s")
            # Report timeout but don't fail - let it retry from same step
            await self._report_activity(
                task_id, 
                step=current_step,  # Keep same step for retry
                status="timeout",
                error=f"Step {current_step} timed out"
            )
            return False
            
        except Exception as e:
            logger.error(f"Task {task_id} step {current_step} error: {e}")
            # Report error but don't mark as failed immediately - allow retry
            await self._report_activity(
                task_id,
                step=current_step,  # Keep same step for retry
                status="error",
                error=str(e)
            )
            return False
            
        finally:
            self._current_task = None
    
    async def process_tasks(self):
        """
        Process pending/active tasks. Called from heartbeat().
        
        Priority order:
        1. Tasks already in_progress (finish what we started)
        2. Pending tasks assigned to us (claim and start)
        
        Non-blocking: processes at most MAX_CHUNKS_PER_HEARTBEAT task steps.
        """
        # First: finish in-progress tasks
        active_tasks = await self._fetch_my_tasks(status="in_progress")
        
        for task in active_tasks[:MAX_CHUNKS_PER_HEARTBEAT]:
            if self._should_work_on(task):
                await self._work_on_task(task)
                return  # One task per heartbeat (non-blocking)
        
        # Then: check for new pending tasks
        pending_tasks = await self._fetch_my_tasks(status="pending")
        
        for task in pending_tasks[:1]:  # Only pick up one new task at a time
            if self._should_work_on(task):
                task_id = task.get("id")
                
                # Claim it first (atomic operation)
                if await self._claim_task(task_id):
                    logger.info(f"Claimed task {task_id}")
                    task["status"] = "in_progress"
                    await self._work_on_task(task)
                    return
                else:
                    logger.debug(f"Could not claim task {task_id} (already claimed?)")
