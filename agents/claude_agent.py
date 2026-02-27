"""
ClaudeAgent - Anthropic Claude-powered agent.

Uses the Anthropic API to generate responses.
"""

import anthropic
import logging
import time
from pathlib import Path

from .base_agent import PersistentAgent

logger = logging.getLogger(__name__)

MAX_RETRY = 3
BACKOFF_MULTIPLIER = 1.5


class ClaudeAgent(PersistentAgent):
    """Agent powered by Anthropic Claude API."""

    def __init__(self, config_dir: Path):
        super().__init__(config_dir)

        # Initialize Anthropic client
        # If api_key is None, don't pass it - let the library use ANTHROPIC_API_KEY env var
        if self.config.llm_api_key:
            self.client = anthropic.Anthropic(api_key=self.config.llm_api_key)
        else:
            self.client = anthropic.Anthropic()  # Uses ANTHROPIC_API_KEY from env

        logger.info(f"ClaudeAgent initialized: {self.agent_id}")
        logger.info(f"Model: {self.config.llm_model}")

    async def generate_response(
        self,
        context: list[dict],
        new_message: dict
    ) -> str:
        """
        Generate a response using Claude API.

        Args:
            context: Conversation history (system + messages)
            new_message: The new message payload

        Returns:
            Response text from Claude
        """
        # Extract system prompt (first message)
        system_prompt = ""
        messages = []

        for msg in context:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            else:
                messages.append(msg)

        # Add the new message
        content = new_message.get("content", "")
        from_id = new_message.get("from_id", "user")
        messages.append({
            "role": "user",
            "content": f"[{from_id}]: {content}"
        })

        logger.debug(f"Sending {len(messages)} messages to Claude")

        # Extract room for error notifications
        room = new_message.get("room", "#general")

        # Retry loop with exponential backoff
        last_error = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                response = self.client.messages.create(
                    model=self.config.llm_model,
                    max_tokens=self.config.llm_max_tokens,
                    system=system_prompt,
                    messages=messages
                )

                # Extract text response
                result = response.content[0].text
                logger.debug(f"Claude response: {result[:100]}...")
                return result

            except anthropic.AuthenticationError as e:
                # 401 - fatal, no retry
                logger.error(f"Authentication error (fatal): {e}")
                return f"[Error: API authentication failed - check API key]"

            except anthropic.BadRequestError as e:
                # 400 - fatal, no retry
                logger.error(f"Bad request (fatal): {e}")
                return f"[Error: Bad request - {e}]"

            except anthropic.RateLimitError as e:
                # 429 - retry with longer backoff
                last_error = e
                wait = 5.0 * (BACKOFF_MULTIPLIER ** (attempt - 1))
                logger.warning(
                    f"Rate limited on attempt {attempt}/{MAX_RETRY}, "
                    f"waiting {wait:.1f}s"
                )
                if attempt < MAX_RETRY:
                    try:
                        self.transport.send_chat(
                            room,
                            f"[{self.agent_id}] Rate limited (429), "
                            f"attempt {attempt}/{MAX_RETRY}. "
                            f"Retry in {wait:.0f}s..."
                        )
                    except Exception:
                        pass
                    time.sleep(wait)

            except anthropic.APIStatusError as e:
                # 500, 529 (overloaded), etc. - retry
                last_error = e
                wait = 2.0 * (BACKOFF_MULTIPLIER ** (attempt - 1))
                logger.warning(
                    f"API error {e.status_code} on attempt "
                    f"{attempt}/{MAX_RETRY}: {e}"
                )
                if attempt < MAX_RETRY:
                    try:
                        self.transport.send_chat(
                            room,
                            f"[{self.agent_id}] API error {e.status_code} "
                            f"(attempt {attempt}/{MAX_RETRY}). "
                            f"Retry in {wait:.0f}s..."
                        )
                    except Exception:
                        pass
                    time.sleep(wait)

            except anthropic.APIConnectionError as e:
                # Network error - retry
                last_error = e
                wait = 2.0 * (BACKOFF_MULTIPLIER ** (attempt - 1))
                logger.warning(
                    f"Connection error on attempt "
                    f"{attempt}/{MAX_RETRY}: {e}"
                )
                if attempt < MAX_RETRY:
                    try:
                        self.transport.send_chat(
                            room,
                            f"[{self.agent_id}] Network error "
                            f"(attempt {attempt}/{MAX_RETRY}). "
                            f"Retry in {wait:.0f}s..."
                        )
                    except Exception:
                        pass
                    time.sleep(wait)

        # All retries exhausted
        logger.error(f"All {MAX_RETRY} attempts failed: {last_error}")
        try:
            self.transport.send_chat(
                room,
                f"[{self.agent_id}] Failed after {MAX_RETRY} attempts: "
                f"{type(last_error).__name__}"
            )
        except Exception:
            pass
        return ""
