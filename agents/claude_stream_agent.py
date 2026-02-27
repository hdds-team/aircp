"""
ClaudeStreamAgent - Persistent Claude CLI with JSON streaming.

Uses `claude -p --input-format stream-json --output-format stream-json` for:
- Single persistent process (no 100Mo respawn per request)
- Preserved context between messages
- Clean JSON parsing (no TTY/ANSI hell)
- Reliable end-of-response detection via {"type":"result"}

Drop-in replacement for ClaudeCliAgent.
"""

import asyncio
import subprocess
import logging
import time
import json
from pathlib import Path
from typing import Dict, Any, Optional

from .base_agent import PersistentAgent

logger = logging.getLogger(__name__)

# Process management
STARTUP_TIMEOUT = 30  # Wait for Claude to initialize
READ_TIMEOUT = 0.1    # Non-blocking read timeout
RESPONSE_TIMEOUT = 300  # Max wait for a response (5 min)
MAX_RESPAWN_ATTEMPTS = 3


class ClaudeStreamAgent(PersistentAgent):
    """
    Agent powered by persistent Claude CLI with JSON streaming.
    
    Maintains a single Claude process that stays warm between requests,
    preserving context and avoiding expensive respawns.
    """

    def __init__(self, config_dir: Path):
        super().__init__(config_dir)

        # Map config model to CLI model alias
        self.cli_model = self._get_cli_model()

        # Persistent process state
        self._process: Optional[subprocess.Popen] = None
        self._process_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()  # Serialize requests to prevent race conditions
        self._respawn_count = 0

        logger.info(f"ClaudeStreamAgent initialized: {self.agent_id}")
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

    def _build_command(self) -> list[str]:
        """Build the Claude CLI command with stream-json mode."""
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            # Note: --verbose disabled to avoid stderr buffer filling up
            "--model", self.cli_model,
            "--add-dir", "/projects",
            "--setting-sources", "user,project",
        ]

        # Skip permissions for trusted agents
        if self.config.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
            logger.info("Skip permissions enabled (trusted agent)")

        # Add MCP config if exists
        mcp_config = self.config_dir / "mcp_servers.json"
        if mcp_config.exists():
            cmd.extend(["--mcp-config", str(mcp_config)])
            logger.info(f"MCP tools enabled from {mcp_config}")

        # Inject soul/system-prompt for agent personality
        if hasattr(self, 'soul') and self.soul:
            cmd.extend(["--system-prompt", self.soul])
            logger.info("System prompt (soul) injected")

        return cmd

    async def _ensure_process(self) -> subprocess.Popen:
        """
        Ensure the Claude process is running, spawning if needed.
        
        Returns the active process or raises if unable to start.
        """
        async with self._process_lock:
            # Check if process is alive
            if self._process is not None:
                poll = self._process.poll()
                if poll is None:
                    # Process is alive
                    return self._process
                else:
                    logger.warning(f"Claude process died (returncode={poll}), respawning...")
                    self._process = None

            # Need to spawn
            if self._respawn_count >= MAX_RESPAWN_ATTEMPTS:
                raise RuntimeError(f"Claude process failed {MAX_RESPAWN_ATTEMPTS} times, giving up")

            self._respawn_count += 1
            logger.info(f"Spawning Claude process (attempt {self._respawn_count})...")

            cmd = self._build_command()
            logger.debug(f"Command: {' '.join(cmd)}")

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,  # Don't buffer stderr (can block on long sessions)
                    text=True,
                    bufsize=1,  # Line buffered
                )

                # Wait for init message
                init_received = await self._wait_for_init()
                if not init_received:
                    logger.error("Claude process did not send init message")
                    self._process.kill()
                    self._process = None
                    raise RuntimeError("Claude init timeout")

                logger.info("Claude process ready (init received)")
                self._respawn_count = 0  # Reset on success
                return self._process

            except FileNotFoundError:
                logger.error("Claude CLI not found - is it installed?")
                raise

    async def _wait_for_init(self) -> bool:
        """Wait for the init message from Claude."""
        if not self._process or not self._process.stdout:
            return False

        start = time.time()
        while time.time() - start < STARTUP_TIMEOUT:
            try:
                # Non-blocking read with asyncio
                line = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, self._process.stdout.readline
                    ),
                    timeout=1.0
                )

                if not line:
                    await asyncio.sleep(0.1)
                    continue

                line = line.strip()
                if not line:
                    continue

                logger.debug(f"Init line: {line[:100]}")

                try:
                    data = json.loads(line)
                    if data.get("type") == "system" and data.get("subtype") == "init":
                        logger.info(f"Session ID: {data.get('session_id', 'unknown')}")
                        return True
                except json.JSONDecodeError:
                    # Not JSON, might be stderr or debug output
                    continue

            except asyncio.TimeoutError:
                continue

        return False

    def _format_input_message(self, content: str) -> str:
        """Format a message for stream-json input."""
        msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": content
            }
        }
        return json.dumps(msg, ensure_ascii=False)

    async def _send_message(self, content: str) -> str:
        """
        Send a message and wait for the complete response.

        Returns the assistant's response text.
        """
        # Serialize all requests to prevent stdin/stdout race conditions
        async with self._request_lock:
            proc = await self._ensure_process()

            if not proc.stdin or not proc.stdout:
                raise RuntimeError("Process streams not available")

            # Send message
            input_json = self._format_input_message(content)
            logger.debug(f"Sending: {input_json[:200]}")

            proc.stdin.write(input_json + "\n")
            proc.stdin.flush()

            # Collect response
            response_parts = []
            start = time.time()

            while time.time() - start < RESPONSE_TIMEOUT:
                try:
                    line = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, proc.stdout.readline
                        ),
                        timeout=READ_TIMEOUT
                    )

                    if not line:
                        # Check if process died
                        if proc.poll() is not None:
                            logger.error("Claude process died during response")
                            self._process = None
                            break
                        continue

                    line = line.strip()
                    if not line:
                        continue

                    logger.debug(f"Output: {line[:200]}")

                    try:
                        data = json.loads(line)
                        msg_type = data.get("type")

                        if msg_type == "assistant":
                            # Extract text content
                            message = data.get("message", {})
                            content_blocks = message.get("content", [])
                            for block in content_blocks:
                                if block.get("type") == "text":
                                    response_parts.append(block.get("text", ""))

                        elif msg_type == "result":
                            # End of response
                            subtype = data.get("subtype")
                            if subtype == "success":
                                # Use the result field if present
                                result = data.get("result", "")
                                if result and not response_parts:
                                    response_parts.append(result)
                            else:
                                logger.warning(f"Result subtype: {subtype}")

                            # Extract token usage from result (if available)
                            usage = data.get("usage", {})
                            elapsed_ms = int((time.time() - start) * 1000)
                            self._last_usage = {
                                "prompt_tokens": usage.get("input_tokens"),
                                "completion_tokens": usage.get("output_tokens"),
                                "estimated": not bool(usage),
                                "latency_ms": elapsed_ms,
                            }

                            logger.info(f"Response complete in {time.time() - start:.1f}s")
                            break

                        elif msg_type == "error":
                            error_msg = data.get("message", "Unknown error")
                            logger.error(f"Claude error: {error_msg}")
                            return f"[Error: {error_msg}]"

                    except json.JSONDecodeError:
                        # Not JSON, log and continue
                        logger.debug(f"Non-JSON line: {line[:100]}")
                        continue

                except asyncio.TimeoutError:
                    continue

            return "".join(response_parts)

    def _format_prompt(self, context: list[dict], new_message: dict) -> str:
        """
        Format context and new message for the persistent session.
        
        Since the session maintains its own context, we primarily send
        the new message. However, for the first message after spawn,
        we include recent history.
        """
        lines = []

        # Add recent conversation context (skip system, it's in the agent)
        for msg in context:
            if msg["role"] == "system":
                continue

            role = msg["role"]
            content = msg["content"]

            if role == "assistant":
                lines.append(f"[You]: {content}")
            else:
                lines.append(content)

        # Add instruction
        lines.append("")
        lines.append("Respond naturally to the latest message. Keep it concise.")

        return "\n".join(lines)

    async def generate_response(
        self,
        context: list[dict],
        new_message: dict
    ) -> str:
        """
        Generate a response using the persistent Claude process.

        Args:
            context: Conversation history (system + messages)
            new_message: The new message payload

        Returns:
            Response text from Claude
        """
        prompt = self._format_prompt(context, new_message)
        logger.info(f"Generating response ({len(prompt)} chars)")

        try:
            response = await self._send_message(prompt)

            if not response:
                logger.warning("Empty response from Claude")
                return ""

            logger.info(f"Response: {response[:100]}...")
            return response

        except Exception as e:
            logger.error(f"Error generating response: {e}")
            # Kill the process to force respawn on next call
            if self._process:
                self._process.kill()
                self._process = None
            return f"[Error: {str(e)}]"

    async def _execute_task_step(
        self,
        task: Dict[str, Any],
        step: int
    ) -> Dict[str, Any]:
        """
        Execute one step of a task using the persistent Claude process.
        
        Same logic as ClaudeCliAgent but uses our persistent process.
        """
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
                    "error": response or "Empty response",
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

    def close(self):
        """Clean up resources including the persistent process."""
        if self._process:
            logger.info("Terminating Claude process...")
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

        super().close()
