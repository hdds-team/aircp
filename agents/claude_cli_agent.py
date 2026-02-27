"""
ClaudeCliAgent - Agent powered by Claude Code CLI.

Uses `claude -p` to generate responses via user's Claude account.
No API key needed - uses OAuth/subscription.
Features adaptive timeout for complex tasks.
"""

import subprocess
import logging
import time
import asyncio
from pathlib import Path
from typing import Dict, Any

from .base_agent import PersistentAgent

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRY = 2  # Claude is cloud-based, fewer retries needed
# v4.5: Errors that should NOT be retried (fatal / config issues)
FATAL_ERRORS = ["cannot be launched inside", "not found", "permission denied",
                "invalid model", "no such file"]
BASE_TIMEOUT = 120  # 2 min base
PER_MESSAGE_COST = 3  # 3s per message in context
CODE_TASK_BONUS = 180  # Extra 3 min for code/implementation tasks
MAX_TIMEOUT = 600  # 10 min cap


class ClaudeCliAgent(PersistentAgent):
    """Agent powered by Claude Code CLI (uses user's Claude account)."""

    def __init__(self, config_dir: Path):
        super().__init__(config_dir)

        # Map config model to CLI model alias
        self.cli_model = self._get_cli_model()

        logger.info(f"ClaudeCliAgent initialized: {self.agent_id}")
        logger.info(f"CLI model: {self.cli_model}")

    def _get_cli_model(self) -> str:
        """Map config model to CLI alias."""
        model = self.config.llm_model.lower()

        if "opus" in model:
            return "opus"
        elif "haiku" in model:
            return "haiku"
        else:
            return "sonnet"  # Default

    def _format_prompt(self, context: list[dict], new_message: dict) -> str:
        """
        Format context and new message for CLI input.

        Returns a single prompt string with conversation history.
        """
        lines = []

        # Add recent conversation (skip system, it goes via --system-prompt)
        for msg in context:
            if msg["role"] == "system":
                continue

            role = msg["role"]
            content = msg["content"]

            if role == "assistant":
                lines.append(f"[You]: {content}")
            else:
                lines.append(content)  # Already formatted as [user]: content

        # Note: new_message is already in context (added by _append_memory)
        # So we don't add it again here to avoid duplicates

        # Add instruction
        lines.append("")
        lines.append("Respond naturally to the latest message. Keep it concise.")

        return "\n".join(lines)

    def _calculate_timeout(self, prompt: str, msg_count: int) -> float:
        """Calculate adaptive timeout based on context and task complexity."""
        timeout = BASE_TIMEOUT + (msg_count * PER_MESSAGE_COST)

        # Detect code/implementation tasks (need more time)
        code_keywords = ['implement', 'coder', 'code', 'patch',
                         'add', 'modify', 'create', 'write']
        prompt_lower = prompt.lower()
        if any(kw in prompt_lower for kw in code_keywords):
            timeout += CODE_TASK_BONUS
            logger.debug(f"Code task detected, adding {CODE_TASK_BONUS}s bonus")

        timeout = min(timeout, MAX_TIMEOUT)
        logger.info(f"Adaptive timeout: {timeout:.0f}s (msgs={msg_count})")
        return timeout

    async def _notify_timeout(self, room: str, attempt: int, timeout: float):
        """Send timeout notification to channel."""
        try:
            if hasattr(self, 'hdds') and self.hdds:
                msg = (
                    f"[@{self.agent_id}] Timeout after {timeout:.0f}s "
                    f"(attempt {attempt}/{MAX_RETRY}). Retrying..."
                )
                await self.hdds.send_message(room, msg, f"@{self.agent_id}")
        except Exception as e:
            logger.warning(f"Failed to send timeout notification: {e}")

    async def generate_response(
        self,
        context: list[dict],
        new_message: dict
    ) -> str:
        """
        Generate a response using Claude CLI with adaptive timeout.

        Args:
            context: Conversation history (system + messages)
            new_message: The new message payload

        Returns:
            Response text from Claude
        """
        prompt = self._format_prompt(context, new_message)
        room = new_message.get("room", "#general")

        logger.info(f"Calling claude CLI with {len(prompt)} chars")

        # Build command
        cmd = [
            "claude", "-p",
            "--system-prompt", self.soul,
            "--model", self.cli_model,
            "--no-session-persistence",
            "--add-dir", "/projects",
            "--setting-sources", "user,project",  # Load MCP from ~/.claude.json project config
        ]

        # Skip permissions for trusted agents (e.g., Alpha with Opus)
        if self.config.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
            logger.info("Skip permissions enabled (trusted agent)")

        # Fallback: Add local MCP config if exists
        mcp_config = self.config_dir / "mcp_servers.json"
        if mcp_config.exists():
            cmd.extend(["--mcp-config", str(mcp_config)])
            logger.info(f"MCP tools enabled from {mcp_config}")

        # Calculate adaptive timeout
        base_timeout = self._calculate_timeout(prompt, len(context))

        # Retry loop
        for attempt in range(1, MAX_RETRY + 1):
            timeout = base_timeout * (1.5 ** (attempt - 1))  # Backoff
            timeout = min(timeout, MAX_TIMEOUT)

            logger.info(f"Attempt {attempt}/{MAX_RETRY}: timeout={timeout:.0f}s")
            start_time = time.time()

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                try:
                    stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
                except subprocess.TimeoutExpired:
                    elapsed = time.time() - start_time
                    logger.warning(f"Timeout attempt {attempt}/{MAX_RETRY} after {elapsed:.1f}s")
                    proc.kill()
                    proc.wait()

                    if attempt < MAX_RETRY:
                        await self._notify_timeout(room, attempt, timeout)
                        continue
                    return ""  # Empty = no response

                if proc.returncode != 0:
                    # v4.5: Log BOTH stdout and stderr for diagnosis
                    error_detail = stderr.strip() or stdout.strip()
                    logger.error(
                        f"Claude CLI error (rc={proc.returncode}, "
                        f"attempt {attempt}/{MAX_RETRY}):\n"
                        f"  stderr: {stderr[:200]}\n"
                        f"  stdout: {stdout[:200]}"
                    )

                    # Check if error is fatal (no point retrying)
                    error_lower = error_detail.lower()
                    if any(fe in error_lower for fe in FATAL_ERRORS):
                        logger.error(f"Fatal CLI error, not retrying: {error_detail[:100]}")
                        return f"[Error: Claude CLI failed - {error_detail[:100]}]"

                    # Transient error (rate limit, overload) -> retry with backoff
                    if attempt < MAX_RETRY:
                        backoff = 2 * attempt  # 2s, 4s
                        logger.info(f"Transient CLI error, retrying in {backoff}s (attempt {attempt}/{MAX_RETRY})...")
                        await asyncio.sleep(backoff)
                        continue

                    return f"[Error: Claude CLI failed - {error_detail[:100]}]"

                response = stdout.strip()
                elapsed = time.time() - start_time
                logger.info(f"Claude response in {elapsed:.1f}s (attempt {attempt})")

                # No token counts available from CLI subprocess
                self._last_usage = {
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "estimated": True,
                    "latency_ms": int(elapsed * 1000),
                }

                return response

            except FileNotFoundError:
                logger.error("Claude CLI not found - is it installed?")
                return "[Error: claude command not found]"

        return ""  # All retries failed

    # === TaskWorkerMixin Implementation ===
    
    async def _execute_task_step(
        self,
        task: Dict[str, Any],
        step: int
    ) -> Dict[str, Any]:
        """
        Execute one step of a task using Claude CLI.
        
        For ClaudeCliAgent, we use the Claude CLI itself to work on tasks.
        The task description becomes the prompt, and Claude generates the work.
        
        This is a mono-step implementation: each task completes in a single
        Claude CLI call. Multi-step workflows can be added later if needed.
        
        Args:
            task: Task dict from TaskManager
            step: Current step number (always 0 for mono-step)
            
        Returns:
            Dict with done, next_step, error, result
        """
        task_id = task.get("id")
        description = task.get("description", "")
        task_type = task.get("task_type", "general")
        
        # Error case: no description -> don't mark done, let mixin handle error
        if not description:
            return {
                "done": False,
                "next_step": step,
                "error": "Task has no description",
                "result": None
            }
        
        logger.info(f"Executing task {task_id} (step {step}): {description[:80]}...")
        
        # Build a virtual message for generate_response()
        # Format it like a TaskManager assignment
        virtual_message = {
            "role": "user",
            "content": f"[TaskManager] Task #{task_id} ({task_type}): {description}",
            "room": "#tasks",  # Virtual room for task context
        }
        
        # Build minimal context (we don't want full chat history for tasks)
        context = [{"role": "system", "content": self.soul}]
        
        try:
            # Use existing generate_response() - it handles Claude CLI call
            response = await self.generate_response(context, virtual_message)
            
            # Error case: empty or error response -> don't mark done
            if not response or response.startswith("[Error"):
                return {
                    "done": False,
                    "next_step": step,
                    "error": response or "Empty response from Claude CLI",
                    "result": None
                }
            
            # Task completed successfully
            logger.info(f"Task {task_id} completed: {response[:100]}...")
            return {
                "done": True,
                "next_step": None,
                "error": None,
                "result": response[:500]  # Truncate for storage
            }
            
        except Exception as e:
            logger.error(f"Task {task_id} execution error: {e}")
            return {
                "done": False,  # Allow retry
                "next_step": step,  # Stay at same step
                "error": str(e),
                "result": None
            }
