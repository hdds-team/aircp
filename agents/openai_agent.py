"""
OpenAI-Compatible Agent - Generic agent for OpenAI API format.

Works with:
- OpenAI (api.openai.com)
- LMStudio (localhost:1234)
- Groq (api.groq.com)
- Mistral (api.mistral.ai)
- Together.ai (api.together.xyz)
- DeepSeek (api.deepseek.com)
- Perplexity (api.perplexity.ai)
- vLLM, llama.cpp, text-generation-webui, etc.

v2.0: Tool calling, adaptive timeout, retry with backoff, fallback parser.
      Ported from OllamaAgent for local LLM cluster support.

Config example:
    [llm]
    provider = "openai"
    model = "gpt-4o"
    api_key = "${OPENAI_API_KEY}"
    base_url = "https://api.openai.com/v1"
    max_tokens = 1024

    [timeout]
    base = 120
    per_msg = 4
    max = 600

    [tools]
    enabled = true
    allowed = ["aircp_send", "aircp_history", "file_read", "file_list"]
    fallback = true
"""

import httpx
import json
import logging
import os
import re
import time
from pathlib import Path

from .base_agent import PersistentAgent

logger = logging.getLogger(__name__)

# Known provider base URLs
PROVIDER_URLS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together": "https://api.together.xyz/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "perplexity": "https://api.perplexity.ai",
    "lmstudio": "http://localhost:1234/v1",
    "vllm": "http://localhost:8000/v1",
    "llamacpp": "http://localhost:8080/v1",
    "ollama-openai": "http://localhost:11434/v1",
}

# Retry configuration
MAX_RETRY = 3
BACKOFF_MULTIPLIER = 1.5

# Tool calling configuration
MAX_TOOL_ROUNDS = 5
MAX_FALLBACK_INTENTS = 3

# Strip <think>...</think> blocks from reasoning models (qwen3, deepseek-r1, etc.)
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class OpenAIAgent(PersistentAgent):
    """
    Generic agent for OpenAI-compatible APIs.

    Supports any service that implements the OpenAI chat completions API.
    v2.0: Tool calling, adaptive timeout, retry with backoff.
    """

    def __init__(self, config_dir: Path):
        super().__init__(config_dir)

        # API config
        self.base_url = self._get_base_url()
        self.model = self.config.llm_model
        self.api_key = self.config.llm_api_key or ""

        # Tool calling (MCP Offline v1.1)
        self.tools_enabled = self._load_tools_config()
        self.fallback_enabled = self._load_fallback_config()

        if self.tools_enabled:
            from .tool_router import ToolRouter
            from .fallback_parser import FallbackParser

            allowed_tools = self._get_allowed_tools()
            self.tool_router = ToolRouter(
                agent_id=self.agent_id,
                daemon_url="http://localhost:5555",
                allowed_tools=allowed_tools,
            )
            self.fallback_parser = FallbackParser(
                known_tools=self.tool_router.allowed_tools
            )
            logger.info(f"Tool calling enabled: {sorted(allowed_tools or [])}")
            if self.fallback_enabled:
                logger.info("Fallback parser enabled (text-based tool extraction)")
        else:
            self.tool_router = None
            self.fallback_parser = None

        # Stats
        self._fallback_count = 0
        self._tool_call_count = 0

        logger.info(f"OpenAIAgent initialized: {self.agent_id}")
        logger.info(f"Model: {self.model} @ {self.base_url}")
        logger.info(
            f"Timeout: base={self.config.timeout_base}s, "
            f"per_msg={self.config.timeout_per_msg}s, "
            f"max={self.config.timeout_max}s"
        )

    def _get_base_url(self) -> str:
        """Determine the API base URL from config."""
        if hasattr(self.config, 'llm_base_url') and self.config.llm_base_url:
            return self.config.llm_base_url.rstrip('/')

        provider = self.config.llm_provider.lower()
        if provider in PROVIDER_URLS:
            return PROVIDER_URLS[provider]

        env_url = os.environ.get("OPENAI_BASE_URL")
        if env_url:
            return env_url.rstrip('/')

        return PROVIDER_URLS["openai"]

    def _load_tools_config(self) -> bool:
        """Load tool calling config from config.toml [tools] section."""
        config_path = self.config_dir / "config.toml"
        try:
            import tomllib
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
            return raw.get("tools", {}).get("enabled", False)
        except Exception:
            return False

    def _load_fallback_config(self) -> bool:
        """Load fallback parser config from config.toml [tools] section."""
        config_path = self.config_dir / "config.toml"
        try:
            import tomllib
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
            return raw.get("tools", {}).get("fallback", True)
        except Exception:
            return True

    def _get_allowed_tools(self) -> list[str] | None:
        """Get tool whitelist from config.toml [tools] section."""
        config_path = self.config_dir / "config.toml"
        try:
            import tomllib
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
            allowed = raw.get("tools", {}).get("allowed", None)
            if isinstance(allowed, list) and len(allowed) > 0:
                return allowed
            return None
        except Exception:
            return None

    def _calculate_timeout(self, msg_count: int) -> float:
        """Calculate timeout from config-based parameters."""
        timeout = self.config.timeout_base + (msg_count * self.config.timeout_per_msg)
        return min(timeout, self.config.timeout_max)

    async def _api_chat(
        self,
        messages: list[dict],
        timeout: float,
        tools: list[dict] | None = None,
    ) -> dict | None:
        """
        Single OpenAI-compatible API call with timeout.

        Returns the full response dict, or None on error.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.config.llm_max_tokens,
            "stream": False,
        }

        # reasoning_budget only supported by OpenAI and DeepSeek
        if any(h in self.base_url for h in ("openai.com", "deepseek.com")):
            body["reasoning_budget"] = 0

        if tools:
            body["tools"] = tools

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=body,
            )

            if response.status_code != 200:
                logger.error(f"API error {response.status_code}: {response.text[:200]}")
                return None

            data = response.json()

            # Strip <think>...</think> blocks from reasoning models
            for choice in data.get("choices", []):
                msg = choice.get("message", {})
                content = msg.get("content")
                if content and "<think>" in content:
                    cleaned = _THINK_RE.sub("", content).strip()
                    logger.info(
                        f"Stripped <think> block ({len(content) - len(cleaned)} chars)"
                    )
                    msg["content"] = cleaned

            # Log usage if available
            usage = data.get("usage", {})
            if usage:
                logger.debug(
                    f"Tokens: {usage.get('prompt_tokens', '?')} in, "
                    f"{usage.get('completion_tokens', '?')} out"
                )

            return data

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
            choices = response_data.get("choices", [])
            if not choices:
                return "[Error: Empty response during tool loop]"

            msg = choices[0].get("message", {})
            tool_calls = msg.get("tool_calls", [])

            if not tool_calls:
                return msg.get("content", "")

            logger.info(f"Tool round {round_num + 1}: {len(tool_calls)} tool call(s)")

            # Append assistant message with tool_calls
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })

            # Execute each tool call
            for tc in tool_calls:
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                raw_args = func.get("arguments", {})

                if isinstance(raw_args, str):
                    try:
                        arguments = json.loads(raw_args)
                    except json.JSONDecodeError:
                        arguments = {}
                else:
                    arguments = raw_args

                logger.info(f"  -> Executing tool: {tool_name}({arguments})")
                result = await self.tool_router.execute(tool_name, arguments)

                result_content = result.get("result", result.get("error", ""))
                if result_content is None:
                    result_content = "(no result)"

                # OpenAI format: tool role with tool_call_id
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": str(result_content),
                })

                logger.info(
                    f"  <- Tool result: success={result['success']}, "
                    f"len={len(str(result_content))}"
                )

            # Call model again with tool results
            response_data = await self._api_chat(messages, timeout, tools)
            if response_data is None:
                return "[Error: API failed during tool call loop]"

        # Exceeded max rounds
        logger.warning(f"Exceeded {MAX_TOOL_ROUNDS} tool rounds, returning last response")
        choices = response_data.get("choices", [])
        return choices[0].get("message", {}).get("content", "") if choices else ""

    async def _handle_fallback(
        self,
        text: str,
        messages: list[dict],
        timeout: float,
    ) -> str:
        """
        Fallback mode: parse raw text for [TOOL:...] patterns and execute them.
        Re-prompt the model with tool results.
        """
        intents = self.fallback_parser.extract_tool_intents(text)

        if not intents:
            return text

        if len(intents) > MAX_FALLBACK_INTENTS:
            logger.warning(f"Fallback: capped {len(intents)} intents to {MAX_FALLBACK_INTENTS}")
            intents = intents[:MAX_FALLBACK_INTENTS]

        self._fallback_count += 1
        logger.info(f"Fallback mode: {len(intents)} intent(s) (total: {self._fallback_count})")

        # Execute intents
        tool_results = []
        for intent in intents:
            tool_name = intent["name"]
            arguments = intent["arguments"]

            logger.info(f"  -> Fallback executing: {tool_name}({arguments})")
            result = await self.tool_router.execute(tool_name, arguments)

            result_content = result.get("result", result.get("error", ""))
            if result_content is None:
                result_content = "(no result)"

            tool_results.append({
                "tool": tool_name,
                "success": result["success"],
                "result": str(result_content),
            })

        # Build results summary
        results_text = "\n\n".join(
            f"[Tool: {r['tool']}] {'OK' if r['success'] else 'ERROR'}\n{r['result']}"
            for r in tool_results
        )

        # Re-prompt model with tool results (no tools param = force text response)
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": (
                f"Here are the tool results:\n\n"
                f"{results_text}\n\n"
                f"Use these results to answer the original question."
            ),
        })

        response_data = await self._api_chat(messages, timeout, tools=None)
        if response_data is None:
            return f"(Tool results)\n\n{results_text}"

        choices = response_data.get("choices", [])
        return choices[0].get("message", {}).get("content", "") if choices else ""

    async def generate_response(
        self,
        context: list[dict],
        new_message: dict
    ) -> str:
        """
        Generate a response using OpenAI-compatible API.

        Flow:
        1. Call API with tool definitions (if enabled)
        2. If model returns tool_calls -> handle tool loop
        3. If model returns text with [TOOL:...] patterns -> fallback parse
        4. If model returns plain text -> return as-is

        Retry with backoff on timeout.
        """
        # Build messages — merge consecutive same-role messages
        # (some models like mistral-nemo require strict user/assistant alternation)
        messages = []
        for msg in context:
            role = msg["role"]
            content = msg["content"]
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n\n" + content
            else:
                messages.append({"role": role, "content": content})

        logger.info(f"Sending {len(messages)} messages to {self.base_url}")

        # Prepare tools if enabled
        tools = None
        if self.tools_enabled and self.tool_router:
            tools = self.tool_router.get_tool_definitions()
            if tools:
                logger.info(f"Tool calling active: {len(tools)} tools available")

        # Calculate adaptive timeout
        base_timeout = self._calculate_timeout(len(messages))

        # Extract room for notifications
        room = new_message.get("room", "#general")

        # Retry loop
        last_error = None
        for attempt in range(1, MAX_RETRY + 1):
            timeout = base_timeout * (BACKOFF_MULTIPLIER ** (attempt - 1))
            logger.info(f"Attempt {attempt}/{MAX_RETRY}: timeout={timeout:.0f}s")

            start_time = time.time()

            try:
                response_data = await self._api_chat(messages, timeout, tools)
                elapsed = time.time() - start_time

                if response_data is None and tools:
                    # Tool calling may have caused a 500 (Jinja template error)
                    # Retry without tools — fallback parser will handle tool intents
                    logger.warning("API error with tools, retrying without tools")
                    tools = None
                    response_data = await self._api_chat(messages, timeout, None)
                    elapsed = time.time() - start_time

                if response_data is None:
                    return "[Error: API returned an error]"

                # Capture token usage
                usage = response_data.get("usage", {})
                self._last_usage = {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "estimated": not bool(usage),
                    "latency_ms": int(elapsed * 1000),
                }

                logger.info(f"Response received in {elapsed:.1f}s (attempt {attempt})")

                # Extract message from OpenAI format
                choices = response_data.get("choices", [])
                if not choices:
                    return "[Error: Empty response from API]"

                msg = choices[0].get("message", {})
                tool_calls = msg.get("tool_calls", [])
                content = msg.get("content", "")

                if tool_calls and self.tool_router:
                    # Path A: Proper function calling
                    self._tool_call_count += 1
                    logger.info(f"Model requested {len(tool_calls)} tool call(s)")
                    return await self._handle_tool_calls(
                        messages, response_data, timeout
                    )

                elif (
                    self.fallback_enabled
                    and self.fallback_parser
                    and self.tool_router
                    and self.fallback_parser.has_tool_intents(content)
                ):
                    # Path B: Fallback text parsing
                    logger.info("No tool_calls but [TOOL:...] found, using fallback")
                    return await self._handle_fallback(content, messages, timeout)

                else:
                    # Path C: Plain text response
                    return content

            except httpx.TimeoutException as e:
                elapsed = time.time() - start_time
                last_error = e
                logger.warning(
                    f"Timeout on attempt {attempt}/{MAX_RETRY} after {elapsed:.1f}s"
                )

                # Notify channel (except on last attempt — final failure handles it)
                if attempt < MAX_RETRY:
                    try:
                        self.transport.send_chat(
                            room,
                            f"[{self.agent_id}] Timeout after {timeout:.0f}s "
                            f"(attempt {attempt}/{MAX_RETRY}). Retrying..."
                        )
                    except Exception:
                        pass

            except httpx.ConnectError as e:
                logger.error(f"Connection error: {e}")
                return f"[Error: Cannot connect to {self.base_url}]"

            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                last_error = e
                break  # Don't retry non-timeout errors

        # All retries exhausted
        try:
            self.transport.send_chat(
                room,
                f"[{self.agent_id}] Failed after {MAX_RETRY} attempts. "
                f"Local model may be overloaded."
            )
        except Exception:
            pass

        logger.error(f"All {MAX_RETRY} attempts failed: {last_error}")
        return ""
