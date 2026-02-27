#!/usr/bin/env python3
"""
AIRCP LMStudio Runner - AI agent powered by LMStudio local LLM

Connects to hub and sends chat messages to LMStudio API for completions.
Used to validate that AI agents can participate in AIRCP conversations.
"""
import asyncio
import websockets
import msgpack
import uuid
import logging
import sys
import aiohttp
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from aircp_validator import AIRCPValidator
from aircp_config import AIRCPConfigParser

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


class LMStudioRunner:
    """LMStudio runner - uses local LLM for responses"""

    def __init__(self, config: dict, agent_config: dict):
        self.config = config
        self.agent_config = agent_config
        self.agent_id = f"@{agent_config.id}"
        self.room = agent_config.room
        self.api_key = agent_config.api_key

        # LMStudio API config
        self.lm_config = agent_config.config.get("lm", {})
        self.lm_host = self.lm_config.get("host", "localhost")
        self.lm_port = self.lm_config.get("port", 1234)
        self.lm_url = f"http://{self.lm_host}:{self.lm_port}"
        self.system_prompt = self.lm_config.get("system_prompt", "You are a helpful assistant.")
        self.max_tokens = self.lm_config.get("max_tokens", 256)

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.http_session: Optional[aiohttp.ClientSession] = None

    async def run(self):
        """Connect to hub and run"""
        hub_url = self.config["irc"]["server"].replace("ws://", "wss://")
        hub_url = hub_url.replace("wss://", "ws://")  # Use ws for local

        logger.info(f"🤖 LMStudio runner connecting to {hub_url}")
        logger.info(f"   LMStudio API: {self.lm_url}")

        try:
            # Create HTTP session for LMStudio API calls
            self.http_session = aiohttp.ClientSession()

            async with websockets.connect(hub_url) as ws:
                self.ws = ws

                # Handshake
                await self.send_hello()
                await self.receive()

                # Auth
                await self.send_auth()
                await self.receive()

                # Join room
                await self.join_room()
                await self.receive()

                logger.info(f"✅ Ready! Listening to {self.room}")

                # Listen for messages
                async for message in ws:
                    try:
                        envelope = msgpack.unpackb(message, raw=False)
                        await self.handle_message(envelope)
                    except Exception as e:
                        logger.error(f"Error: {e}")

        except aiohttp.ClientConnectorError as e:
            logger.error(f"Cannot connect to LMStudio API at {self.lm_url}: {e}")
            logger.error("Make sure LMStudio is running: lmstudio")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            sys.exit(1)
        finally:
            if self.http_session:
                await self.http_session.close()

    async def handle_message(self, envelope: dict):
        """Handle incoming message"""
        kind = envelope.get("kind")
        logger.debug(f"[LM] Received {kind} message")

        if kind != "chat":
            return

        # Get content
        payload = envelope.get("payload", {})
        content = payload.get("content", "")
        from_id = envelope.get("from", {}).get("id")

        logger.info(f"[LM] Message from {from_id}: {content[:50]}")

        if not content or from_id == self.agent_id:
            logger.info(f"[LM] Ignoring (empty or self)")
            return

        # Call LMStudio API
        try:
            response_text = await self.call_lmstudio(content)
            if not response_text:
                logger.warning(f"[LM] Empty response from LMStudio")
                return

            logger.info(f"[LM] → Responding: {response_text[:50]}")

            response = self.create_envelope("chat", {
                "role": "assistant",
                "content": response_text
            }, to={"room": self.room, "broadcast": True})

            await self.send(response)
            logger.info(f"[LM] ✓ Response sent")

        except Exception as e:
            logger.error(f"[LM] Error calling LMStudio: {e}")

    async def call_lmstudio(self, prompt: str) -> Optional[str]:
        """Call LMStudio API for completion"""
        if not self.http_session:
            return None

        try:
            # LMStudio API endpoint
            url = f"{self.lm_url}/v1/chat/completions"

            payload = {
                "model": "local-model",
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": self.max_tokens,
                "stream": False
            }

            logger.debug(f"[LM] Calling {url}")
            async with self.http_session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.error(f"[LM] LMStudio returned {resp.status}")
                    return None

                data = await resp.json()

                # Extract message from response
                if "choices" in data and len(data["choices"]) > 0:
                    message = data["choices"][0].get("message", {})
                    content = message.get("content", "").strip()
                    return content if content else None

                logger.warning(f"[LM] Unexpected response format: {data}")
                return None

        except asyncio.TimeoutError:
            logger.error(f"[LM] LMStudio API timeout")
            return None
        except Exception as e:
            logger.error(f"[LM] API call failed: {e}")
            return None

    async def send_hello(self):
        """Send hello"""
        envelope = self.create_envelope("control", {
            "command": "hello",
            "args": {
                "client_name": "lmstudio-runner",
                "client_version": "0.1.0",
                "capabilities": ["chat"]
            }
        })
        await self.send(envelope)

    async def send_auth(self):
        """Send auth"""
        envelope = self.create_envelope("control", {
            "command": "auth",
            "args": {"api_key": self.api_key}
        })
        await self.send(envelope)

    async def join_room(self):
        """Join room"""
        envelope = self.create_envelope("control", {
            "command": "join",
            "args": {"room": self.room}
        })
        await self.send(envelope)

    async def send(self, envelope: dict):
        """Send envelope"""
        if not self.ws:
            return

        try:
            await self.ws.send(msgpack.packb(envelope))
        except Exception as e:
            logger.error(f"Send failed: {e}")

    async def receive(self) -> Optional[dict]:
        """Receive and validate"""
        if not self.ws:
            return None

        try:
            message = await asyncio.wait_for(self.ws.recv(), timeout=5)
            envelope = msgpack.unpackb(message, raw=False)

            # Validate
            is_valid, err = AIRCPValidator.validate_envelope(envelope)
            if not is_valid:
                logger.warning(f"Invalid envelope: {err}")
                return None

            kind = envelope.get("kind")
            if kind == "error":
                logger.error(f"Error from hub: {envelope.get('payload', {}).get('message')}")
            else:
                logger.debug(f"← {kind}")

            return envelope

        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for message")
            return None

    def create_envelope(self, kind: str, payload: dict, **kwargs) -> dict:
        """Create envelope"""
        envelope = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "from": {"type": "agent", "id": self.agent_id},
            "to": kwargs.get("to", {}),
            "kind": kind,
            "payload": payload,
            "meta": {"protocol_version": "0.1.0"}
        }
        return envelope


async def main():
    """CLI entry point"""
    config_path = Path(sys.argv[1] if len(sys.argv) > 1 else "aircp-config.toml")

    if not config_path.exists():
        logger.error(f"Config not found: {config_path}")
        sys.exit(1)

    config = AIRCPConfigParser.load(config_path)

    # Find lmstudio agent config
    lmstudio_agent = None
    for agent in config.get("agents", []):
        if agent.id == "lmstudio":
            lmstudio_agent = agent
            break

    if not lmstudio_agent:
        logger.error("LMStudio agent not configured")
        sys.exit(1)

    runner = LMStudioRunner(config, lmstudio_agent)
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
