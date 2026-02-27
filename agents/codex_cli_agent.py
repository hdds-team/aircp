"""
CodexCliAgent - Agent powered by OpenAI Codex CLI.

Uses `codex exec -` to generate responses via user's Codex CLI auth.
No API key required if the user is already logged in.
Features adaptive timeout for complex tasks.
"""

import subprocess
import logging
import time
from pathlib import Path

from .base_agent import PersistentAgent

logger = logging.getLogger(__name__)

# Fastest generally-available Codex CLI model (smallest tier).
DEFAULT_CODEX_MODEL = "gpt-5.1"

# Timeout configuration
MAX_RETRY = 2
BASE_TIMEOUT = 120  # 2 min base
PER_MESSAGE_COST = 3  # 3s per message
CODE_TASK_BONUS = 180  # Extra 3 min for code tasks
MAX_TIMEOUT = 600  # 10 min cap


class CodexCliAgent(PersistentAgent):
    """Agent powered by the OpenAI Codex CLI."""

    def __init__(self, config_dir: Path):
        super().__init__(config_dir)

        self.cli_model = self._get_cli_model()
        self.repo_root = Path(__file__).resolve().parent.parent

        logger.info(f"CodexCliAgent initialized: {self.agent_id}")
        logger.info(f"CLI model: {self.cli_model}")

    def _get_cli_model(self) -> str:
        """Pick the CLI model, falling back to a fast Codex default."""
        model = (self.config.llm_model or "").strip()

        # Base config default is Claude; override for Codex if unset or default.
        if not model or model == "claude-sonnet-4-20250514":
            return DEFAULT_CODEX_MODEL

        return model

    def _format_prompt(self, context: list[dict], new_message: dict) -> str:
        """
        Format context and new message for Codex CLI input.

        Returns a single prompt string with conversation history.
        """
        lines = []

        # System prompt (SOUL.md) first.
        lines.append("System instructions:")
        lines.append(self.soul)
        lines.append("")
        lines.append("Conversation:")

        # Add recent conversation (skip system, already included above)
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

        # Final instruction
        lines.append("")
        lines.append("Respond naturally to the latest message. Keep it concise.")
        lines.append("You may use MCP tools (devit_file_read, devit_git_*, devit_search_web, etc.) to gather information.")
        lines.append("Do not modify files unless explicitly asked.")

        return "\n".join(lines)

    def _calculate_timeout(self, prompt: str, msg_count: int) -> float:
        """Calculate adaptive timeout based on context and task complexity."""
        timeout = BASE_TIMEOUT + (msg_count * PER_MESSAGE_COST)

        # Detect code/implementation tasks
        code_keywords = ['implement', 'coder', 'code', 'patch',
                         'review', 'analyze', 'read', 'file']
        prompt_lower = prompt.lower()
        if any(kw in prompt_lower for kw in code_keywords):
            timeout += CODE_TASK_BONUS

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
        Generate a response using Codex CLI with adaptive timeout.

        Args:
            context: Conversation history (system + messages)
            new_message: The new message payload

        Returns:
            Response text from Codex
        """
        prompt = self._format_prompt(context, new_message)
        room = new_message.get("room", "#general")

        logger.info(f"Calling codex CLI with {len(prompt)} chars")

        cmd = [
            "codex",
            "exec",
            "--model", self.cli_model,
            "--cd", str(self.repo_root),
            "--full-auto",
            "-",
        ]

        base_timeout = self._calculate_timeout(prompt, len(context))

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
                    logger.error(f"Codex CLI error: {stderr}")
                    return f"[Error: Codex CLI failed - {stderr[:100]}]"

                response = stdout.strip()
                elapsed = time.time() - start_time
                logger.info(f"Codex response in {elapsed:.1f}s (attempt {attempt})")

                # No token counts available from CLI subprocess
                self._last_usage = {
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "estimated": True,
                    "latency_ms": int(elapsed * 1000),
                }

                return response

            except FileNotFoundError:
                logger.error("Codex CLI not found - is it installed?")
                return "[Error: codex command not found]"

        return ""
