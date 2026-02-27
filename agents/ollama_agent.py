"""
OllamaAgent - Local LLM agent via Ollama (v1.1)

Uses Ollama API (OpenAI-compatible) for local inference.
Features adaptive timeout, auto-retry with channel notification,
tool calling support for MCP-like capabilities, and fallback parsing
when the model fails to produce proper function calling JSON.

v1.1: Fallback parser (P6) + tool specs from TOML (P5) + binary detection (P3).
"""

import httpx
import json
import logging
import re
import time
from pathlib import Path
from dataclasses import dataclass, field

from .base_agent import PersistentAgent
from .tool_router import ToolRouter, TOOL_DEFINITIONS
from .fallback_parser import FallbackParser

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRY = 3
BACKOFF_MULTIPLIER = 1.5  # Each retry adds 50% more time

# Tool calling configuration
MAX_TOOL_ROUNDS = 8  # Max consecutive tool call rounds (prevent infinite loops)

# Fallback configuration
MAX_FALLBACK_INTENTS = 3  # Max tool intents to execute from fallback parsing


@dataclass
class AdaptiveTimer:
    """
    Adaptive timeout calculator based on model size, context, and history.

    Formula: timeout = base + (msg_count * per_msg) + ema_adjustment
    - base: 60s (small) / 120s (medium) / 180s (large) / 300s (huge)
    - per_msg: 2s (small) / 4s (medium) / 6s (large) / 10s (huge)
    - ema_adjustment: exponential moving average of past response times
    """
    # Historical response times for EMA calculation
    response_times: list[float] = field(default_factory=list)
    ema: float = 0.0
    ema_alpha: float = 0.3  # Smoothing factor (higher = more weight to recent)

    # Model size thresholds (extracted from model name like "24b", "7b")
    SIZE_SMALL = 3      # <= 3B
    SIZE_MEDIUM = 10    # <= 10B
    SIZE_LARGE = 30     # <= 30B
    # > 30B = huge

    def _extract_model_size(self, model_name: str) -> float:
        """Extract parameter count from model name (e.g., 'qwen3:8b' -> 8.0)."""
        # Match patterns like "8b", "24b", "1.5b", "70b", "235b"
        match = re.search(r'(\d+(?:\.\d+)?)\s*b', model_name.lower())
        if match:
            return float(match.group(1))
        # Default to medium if can't parse
        return 8.0

    def _get_base_params(self, model_size: float) -> tuple[float, float]:
        """Get (base_timeout, per_message_cost) based on model size."""
        if model_size <= self.SIZE_SMALL:
            return (60.0, 2.0)
        elif model_size <= self.SIZE_MEDIUM:
            return (120.0, 4.0)
        elif model_size <= self.SIZE_LARGE:
            return (180.0, 6.0)
        else:  # Huge models (70b, 235b, etc.)
            return (300.0, 10.0)

    def calculate_timeout(self, model_name: str, msg_count: int) -> float:
        """
        Calculate adaptive timeout for a request.

        Returns timeout in seconds.
        """
        model_size = self._extract_model_size(model_name)
        base, per_msg = self._get_base_params(model_size)

        # Base calculation
        timeout = base + (msg_count * per_msg)

        # EMA adjustment: if recent responses are slower, add buffer
        if self.ema > 0:
            # Add 50% of EMA as safety margin
            timeout = max(timeout, self.ema * 1.5)

        # Cap at 10 minutes max
        timeout = min(timeout, 600.0)

        logger.debug(
            f"Adaptive timeout: {timeout:.0f}s "
            f"(model={model_size:.1f}B, msgs={msg_count}, ema={self.ema:.1f}s)"
        )

        return timeout

    def record_response_time(self, elapsed: float):
        """Record a successful response time to update EMA."""
        self.response_times.append(elapsed)

        # Keep last 20 measurements
        if len(self.response_times) > 20:
            self.response_times = self.response_times[-20:]

        # Update EMA
        if self.ema == 0:
            self.ema = elapsed
        else:
            self.ema = self.ema_alpha * elapsed + (1 - self.ema_alpha) * self.ema

        logger.debug(f"Response time recorded: {elapsed:.1f}s (new EMA: {self.ema:.1f}s)")


class OllamaAgent(PersistentAgent):
    """Agent powered by local Ollama models with adaptive timeout and tool calling."""

    def __init__(self, config_dir: Path):
        super().__init__(config_dir)

        self.base_url = getattr(self.config, 'llm_base_url', 'http://localhost:11434')
        self.model = self.config.llm_model
        self.timer = AdaptiveTimer()

        # =====================================================================
        # v1.1 MCP Offline: Tool Router + Fallback Parser initialization
        # =====================================================================
        self.tools_enabled = self._load_tools_config()
        self.fallback_enabled = self._load_fallback_config()

        if self.tools_enabled:
            allowed_tools = self._get_allowed_tools()
            self.tool_router = ToolRouter(
                agent_id=self.agent_id,
                daemon_url="http://localhost:5555",
                allowed_tools=allowed_tools,
            )
            # Fallback parser uses the same tool whitelist
            self.fallback_parser = FallbackParser(
                known_tools=self.tool_router.allowed_tools
            )
            logger.info(
                f"Tool calling enabled: {sorted(allowed_tools or [])}"
            )
            if self.fallback_enabled:
                logger.info("Fallback parser enabled (text-based tool extraction)")
        else:
            self.tool_router = None
            self.fallback_parser = None
            logger.info("Tool calling disabled (config.toml [tools] not set)")

        # Track fallback usage stats
        self._fallback_count = 0
        self._tool_call_count = 0

        logger.info(f"OllamaAgent initialized: {self.agent_id}")
        logger.info(f"Model: {self.model} @ {self.base_url}")

    def _load_tools_config(self) -> bool:
        """Load tool calling config from config.toml [tools] section."""
        config_path = self.config_dir / "config.toml"
        try:
            import tomllib
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
            tools_config = raw.get("tools", {})
            return tools_config.get("enabled", False)
        except Exception as e:
            logger.warning(f"Could not load tools config: {e}")
            return False

    def _load_fallback_config(self) -> bool:
        """Load fallback parser config from config.toml [tools] section."""
        config_path = self.config_dir / "config.toml"
        try:
            import tomllib
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
            tools_config = raw.get("tools", {})
            # Default: enabled if tools are enabled (opt-out with fallback = false)
            return tools_config.get("fallback", True)
        except Exception:
            return True  # Default: on

    def _get_allowed_tools(self) -> list[str] | None:
        """Get tool whitelist from config.toml [tools] section."""
        config_path = self.config_dir / "config.toml"
        try:
            import tomllib
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
            tools_config = raw.get("tools", {})
            allowed = tools_config.get("allowed", None)
            if isinstance(allowed, list) and len(allowed) > 0:
                return allowed
            return None  # None = all tools
        except Exception:
            return None

    async def _notify_timeout(self, room: str, attempt: int, timeout: float):
        """Send timeout notification to channel."""
        try:
            # Use HDDS to send notification
            if hasattr(self, 'hdds') and self.hdds:
                msg = (
                    f"[{self.agent_id}] Timeout after {timeout:.0f}s "
                    f"(attempt {attempt}/{MAX_RETRY}). Retrying..."
                )
                await self.hdds.send_message(room, msg, self.agent_id)
                logger.info(f"Timeout notification sent to {room}")
        except Exception as e:
            logger.warning(f"Failed to send timeout notification: {e}")

    async def _notify_final_failure(self, room: str):
        """Send final failure notification after all retries exhausted."""
        try:
            if hasattr(self, 'hdds') and self.hdds:
                msg = (
                    f"[{self.agent_id}] Failed after {MAX_RETRY} attempts. "
                    f"Local model may be overloaded. Try again later."
                )
                await self.hdds.send_message(room, msg, self.agent_id)
                logger.info(f"Final failure notification sent to {room}")
        except Exception as e:
            logger.warning(f"Failed to send failure notification: {e}")

    async def _ollama_chat(
        self,
        messages: list[dict],
        timeout: float,
        tools: list[dict] | None = None,
    ) -> dict | None:
        """
        Single Ollama API call with timeout.

        Returns the full response dict, or None on error.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": self.config.llm_max_tokens
            }
        }

        # Add tools if provided and model supports them
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json=payload,
            )

            if response.status_code != 200:
                logger.error(f"Ollama error: {response.text}")
                return None

            return response.json()

    async def _handle_tool_calls(
        self,
        messages: list[dict],
        response_data: dict,
        timeout: float,
    ) -> str:
        """
        Handle tool call loop: execute tools and feed results back to model.

        Returns the final text response after all tool calls are resolved.
        """
        tools = self.tool_router.get_tool_definitions() if self.tool_router else []

        for round_num in range(MAX_TOOL_ROUNDS):
            msg = response_data.get("message", {})
            tool_calls = msg.get("tool_calls", [])

            if not tool_calls:
                # No more tool calls — return the text content
                return msg.get("content", "")

            logger.info(
                f"Tool round {round_num + 1}: "
                f"{len(tool_calls)} tool call(s)"
            )

            # Append the assistant's tool-call message to the conversation
            messages.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": tool_calls,
            })

            # Execute each tool call and collect results
            for tc in tool_calls:
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                raw_args = func.get("arguments", {})

                # Arguments can be a string (JSON) or dict
                if isinstance(raw_args, str):
                    try:
                        arguments = json.loads(raw_args)
                    except json.JSONDecodeError:
                        arguments = {}
                else:
                    arguments = raw_args

                logger.info(f"  → Executing tool: {tool_name}({arguments})")

                result = await self.tool_router.execute(tool_name, arguments)

                # Format result for Ollama tool response
                result_content = result.get("result", result.get("error", ""))
                if result_content is None:
                    result_content = "(no result)"

                messages.append({
                    "role": "tool",
                    "content": str(result_content),
                })

                logger.info(
                    f"  ← Tool result: success={result['success']}, "
                    f"len={len(str(result_content))}"
                )

            # Call model again with tool results
            response_data = await self._ollama_chat(messages, timeout, tools)
            if response_data is None:
                return "[Error: Ollama failed during tool call loop]"

        # Exceeded max rounds — return whatever we have
        logger.warning(f"Exceeded {MAX_TOOL_ROUNDS} tool rounds, returning last response")
        return response_data.get("message", {}).get("content", "")

    async def _handle_fallback(
        self,
        text: str,
        messages: list[dict],
        timeout: float,
    ) -> str:
        """
        Fallback mode: parse raw text for [TOOL:...] patterns and execute them.

        When the LLM produces text with tool intent markers instead of proper
        function calling JSON, this method extracts and executes those intents,
        then re-prompts the model with the results.

        Args:
            text: Raw text response from the model (containing [TOOL:...] patterns)
            messages: Current conversation messages
            timeout: Request timeout

        Returns:
            Final text response after tool results are injected.
        """
        intents = self.fallback_parser.extract_tool_intents(text)

        if not intents:
            return text  # No intents found — return text as-is

        # Cap the number of intents to prevent abuse
        if len(intents) > MAX_FALLBACK_INTENTS:
            logger.warning(
                f"Fallback: capped {len(intents)} intents to {MAX_FALLBACK_INTENTS}"
            )
            intents = intents[:MAX_FALLBACK_INTENTS]

        self._fallback_count += 1
        logger.info(
            f"Fallback mode activated: {len(intents)} intent(s) "
            f"(total fallbacks: {self._fallback_count})"
        )

        # Execute each intent and collect results
        tool_results = []
        for intent in intents:
            tool_name = intent["name"]
            arguments = intent["arguments"]

            logger.info(f"  → Fallback executing: {tool_name}({arguments})")
            result = await self.tool_router.execute(tool_name, arguments)

            result_content = result.get("result", result.get("error", ""))
            if result_content is None:
                result_content = "(no result)"

            tool_results.append({
                "tool": tool_name,
                "success": result["success"],
                "result": str(result_content),
            })

            logger.info(
                f"  ← Fallback result: success={result['success']}, "
                f"len={len(str(result_content))}"
            )

        # Build a summary of tool results to inject back into the conversation
        results_text = "\n\n".join(
            f"[Tool: {r['tool']}] {'✅' if r['success'] else '❌'}\n{r['result']}"
            for r in tool_results
        )

        # Re-prompt the model with the results (no tools this time — just text)
        messages.append({
            "role": "assistant",
            "content": text,
        })
        messages.append({
            "role": "user",
            "content": (
                f"Here are the tool results you requested:\n\n"
                f"{results_text}\n\n"
                f"Use these results to answer the original question."
            ),
        })

        # Call model again WITHOUT tools (force text response)
        response_data = await self._ollama_chat(messages, timeout, tools=None)
        if response_data is None:
            # If re-prompt fails, return the raw tool results as response
            return f"(Tool results)\n\n{results_text}"

        return response_data.get("message", {}).get("content", "")

    async def generate_response(
        self,
        context: list[dict],
        new_message: dict
    ) -> str:
        """
        Generate a response using Ollama API with adaptive timeout, retry,
        tool calling support, and fallback parsing.

        Flow:
        1. Call Ollama with tool definitions
        2. If model returns tool_calls → handle normally (_handle_tool_calls)
        3. If model returns text with [TOOL:...] patterns → fallback parse + execute
        4. If model returns plain text → return as-is

        Args:
            context: Conversation history (system + messages)
            new_message: The new message payload

        Returns:
            Response text from model
        """
        # Build messages for Ollama (OpenAI format)
        messages = []

        for msg in context:
            messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

        # Note: new_message is already in context (added by _append_memory)
        # So we don't add it again here to avoid duplicates

        logger.info(f"Sending {len(messages)} messages to Ollama")

        # Prepare tools if enabled
        tools = None
        if self.tools_enabled and self.tool_router:
            tools = self.tool_router.get_tool_definitions()
            if tools:
                logger.info(f"Tool calling active: {len(tools)} tools available")

        # Calculate adaptive timeout
        base_timeout = self.timer.calculate_timeout(self.model, len(messages))

        # Extract room from new_message for notifications
        room = new_message.get("room", "#general")

        # Retry loop
        # Keep original messages for clean retry (no accumulated tool results)
        original_messages = [m.copy() for m in messages]
        last_error = None
        for attempt in range(1, MAX_RETRY + 1):
            # Increase timeout with each retry (backoff)
            timeout = base_timeout * (BACKOFF_MULTIPLIER ** (attempt - 1))

            # Reset messages to original on each attempt (clean context)
            messages = [m.copy() for m in original_messages]

            logger.info(
                f"Attempt {attempt}/{MAX_RETRY}: timeout={timeout:.0f}s"
            )

            start_time = time.time()

            try:
                response_data = await self._ollama_chat(
                    messages, timeout, tools
                )

                elapsed = time.time() - start_time

                if response_data is None:
                    return "[Error: Ollama returned an error]"

                # Record successful response time for future adaptation
                self.timer.record_response_time(elapsed)

                # Capture token usage
                self._last_usage = {
                    "prompt_tokens": response_data.get("prompt_eval_count"),
                    "completion_tokens": response_data.get("eval_count"),
                    "estimated": False,
                    "latency_ms": int(elapsed * 1000),
                }

                logger.info(
                    f"Ollama response received in {elapsed:.1f}s "
                    f"(attempt {attempt})"
                )

                # Check if model wants to use tools
                msg = response_data.get("message", {})
                tool_calls = msg.get("tool_calls", [])
                content = msg.get("content", "")

                if tool_calls and self.tool_router:
                    # Path A: Proper function calling → handle normally
                    self._tool_call_count += 1
                    logger.info(
                        f"Model requested {len(tool_calls)} tool call(s), "
                        f"entering tool loop"
                    )
                    return await self._handle_tool_calls(
                        messages, response_data, timeout
                    )

                elif (
                    self.fallback_enabled
                    and self.fallback_parser
                    and self.tool_router
                    and self.fallback_parser.has_tool_intents(content)
                ):
                    # Path B: Fallback — model wrote [TOOL:...] in text
                    logger.info(
                        "No tool_calls but [TOOL:...] found in text, "
                        "activating fallback parser"
                    )
                    return await self._handle_fallback(
                        content, messages, timeout
                    )

                else:
                    # Path C: Simple text response (no tools)
                    return content

            except httpx.TimeoutException as e:
                elapsed = time.time() - start_time
                last_error = e
                logger.warning(
                    f"Timeout on attempt {attempt}/{MAX_RETRY} "
                    f"after {elapsed:.1f}s"
                )

                # Notify channel (except on last attempt)
                if attempt < MAX_RETRY:
                    await self._notify_timeout(room, attempt, timeout)

            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                last_error = e
                break  # Don't retry on non-timeout errors

        # All retries exhausted
        await self._notify_final_failure(room)
        logger.error(f"All {MAX_RETRY} attempts failed: {last_error}")
        return ""  # Empty = no response sent
