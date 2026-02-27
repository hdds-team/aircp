"""
GeminiCliAgent - Agent powered by Gemini CLI.

Uses `gemini` CLI (v0.26+) to generate responses via Google auth.
No API key needed - uses OAuth. Features retry with backoff.
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
MAX_RETRY = 2
FATAL_ERRORS = ["auth", "not found", "permission denied", "invalid model"]
BASE_TIMEOUT = 90
PER_MESSAGE_COST = 3
CODE_TASK_BONUS = 120
MAX_TIMEOUT = 600


class GeminiCliAgent(PersistentAgent):
    """Agent powered by Gemini CLI (uses Google auth)."""

    def __init__(self, config_dir: Path):
        super().__init__(config_dir)
        self.cli_model = self.config.llm_model or "gemini-2.5-flash"
        logger.info(f"GeminiCliAgent initialized: {self.agent_id}")
        logger.info(f"CLI model: {self.cli_model}")

    def _format_prompt(self, context: list[dict], new_message: dict) -> str:
        """Format context and new message for CLI input."""
        lines = []

        for msg in context:
            if msg["role"] == "system":
                continue
            if msg["role"] == "assistant":
                lines.append(f"[You]: {msg['content']}")
            else:
                lines.append(msg["content"])

        lines.append("")
        lines.append("Respond naturally to the latest message. Keep it concise.")
        return "\n".join(lines)

    def _calculate_timeout(self, prompt: str, msg_count: int) -> float:
        """Calculate adaptive timeout based on context and task complexity."""
        timeout = BASE_TIMEOUT + (msg_count * PER_MESSAGE_COST)

        code_keywords = ['implement', 'coder', 'code', 'patch',
                         'ajouter', 'modifier', 'create', 'write']
        if any(kw in prompt.lower() for kw in code_keywords):
            timeout += CODE_TASK_BONUS

        return min(timeout, MAX_TIMEOUT)

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
        """Generate a response using Gemini CLI."""
        prompt = self._format_prompt(context, new_message)
        room = new_message.get("room", "#general")

        logger.info(f"Calling gemini CLI with {len(prompt)} chars")

        # Build command: prompt goes via stdin, soul via --prompt
        cmd = [
            "gemini",
            "--prompt", self.soul,
            "--output-format", "text",
            "--yolo",
            "--model", self.cli_model,
        ]

        # Calculate adaptive timeout
        base_timeout = self._calculate_timeout(prompt, len(context))

        # Retry loop
        for attempt in range(1, MAX_RETRY + 1):
            timeout = base_timeout * (1.5 ** (attempt - 1))
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
                    return ""

                if proc.returncode != 0:
                    error_detail = stderr.strip() or stdout.strip()
                    logger.error(
                        f"Gemini CLI error (rc={proc.returncode}, "
                        f"attempt {attempt}/{MAX_RETRY}):\n"
                        f"  stderr: {stderr[:200]}\n"
                        f"  stdout: {stdout[:200]}"
                    )

                    error_lower = error_detail.lower()
                    if any(fe in error_lower for fe in FATAL_ERRORS):
                        logger.error(f"Fatal CLI error, not retrying: {error_detail[:100]}")
                        return f"[Error: Gemini CLI failed - {error_detail[:100]}]"

                    if attempt < MAX_RETRY:
                        backoff = 2 * attempt
                        logger.info(f"Transient CLI error, retrying in {backoff}s...")
                        await asyncio.sleep(backoff)
                        continue

                    return f"[Error: Gemini CLI failed - {error_detail[:100]}]"

                response = stdout.strip()
                elapsed = time.time() - start_time
                logger.info(f"Gemini response in {elapsed:.1f}s (attempt {attempt})")

                self._last_usage = {
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "estimated": True,
                    "latency_ms": int(elapsed * 1000),
                }

                return response

            except FileNotFoundError:
                logger.error("Gemini CLI not found - is it installed?")
                return "[Error: gemini command not found]"

        return ""

    # === TaskWorkerMixin Implementation ===

    async def _execute_task_step(
        self,
        task: Dict[str, Any],
        step: int
    ) -> Dict[str, Any]:
        """Execute one step of a task using Gemini CLI."""
        task_id = task.get("id")
        description = task.get("description", "")
        task_type = task.get("task_type", "general")

        if not description:
            return {
                "done": False,
                "next_step": step,
                "error": "Task has no description",
                "result": None
            }

        logger.info(f"Executing task {task_id} (step {step}): {description[:80]}...")

        virtual_message = {
            "role": "user",
            "content": f"[TaskManager] Task #{task_id} ({task_type}): {description}",
            "room": "#tasks",
        }

        context = [{"role": "system", "content": self.soul}]

        try:
            response = await self.generate_response(context, virtual_message)

            if not response or response.startswith("[Error"):
                return {
                    "done": False,
                    "next_step": step,
                    "error": response or "Empty response from Gemini CLI",
                    "result": None
                }

            logger.info(f"Task {task_id} completed: {response[:100]}...")
            return {
                "done": True,
                "next_step": None,
                "error": None,
                "result": response[:500]
            }

        except Exception as e:
            logger.error(f"Task {task_id} execution error: {e}")
            return {
                "done": False,
                "next_step": step,
                "error": str(e),
                "result": None
            }
