#!/usr/bin/env python3
"""
AIRCP Echo Runner - Simplest possible agent for testing

Just echoes everything back with a prefix.
Used to validate the hub works end-to-end.
"""
import asyncio
import websockets
import msgpack
import uuid
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from aircp_validator import AIRCPValidator
from aircp_config import AIRCPConfigParser

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


class EchoRunner:
    """Echo runner - repeats everything"""

    def __init__(self, config: dict, agent_config: dict):
        self.config = config
        self.agent_config = agent_config
        self.agent_id = f"@{agent_config.id}"
        self.prefix = agent_config.config.get("behavior", {}).get("prefix", "ECHO: ")
        self.room = agent_config.room
        self.api_key = agent_config.api_key
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

    async def run(self):
        """Connect to hub and run"""
        hub_url = self.config["irc"]["server"].replace("ws://", "wss://")
        hub_url = hub_url.replace("wss://", "ws://")  # Use ws for local

        logger.info(f"🤖 Echo runner connecting to {hub_url}")

        try:
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

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            sys.exit(1)

    async def handle_message(self, envelope: dict):
        """Handle incoming message"""
        kind = envelope.get("kind")
        logger.debug(f"[ECHO] Received {kind} message")

        if kind != "chat":
            return

        # Get content
        payload = envelope.get("payload", {})
        content = payload.get("content", "")
        from_id = envelope.get("from", {}).get("id")

        logger.info(f"[ECHO] Message from {from_id}: {content[:50]}")

        if not content or from_id == self.agent_id:
            logger.info(f"[ECHO] Ignoring (empty or self)")
            return

        # Don't echo other agents (avoid feedback loops!)
        if from_id and from_id.startswith("@") and from_id != "@test" and from_id != "@operator":
            if from_id in ["@lmstudio", "@echo", "@claude-ocr"]:
                logger.info(f"[ECHO] Ignoring message from other agent {from_id} (avoid loop)")
                return

        # Echo back
        response_text = f"{self.prefix}{content}"
        logger.info(f"[ECHO] → Responding: {response_text[:50]}")

        response = self.create_envelope("chat", {
            "role": "assistant",
            "content": response_text
        }, to={"room": self.room, "broadcast": True})

        await self.send(response)
        logger.info(f"[ECHO] ✓ Response sent")

    async def send_hello(self):
        """Send hello"""
        envelope = self.create_envelope("control", {
            "command": "hello",
            "args": {
                "client_name": "echo-runner",
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

    # Find echo agent config
    echo_agent = None
    for agent in config.get("agents", []):
        if agent.id == "echo":
            echo_agent = agent
            break

    if not echo_agent:
        logger.error("Echo agent not configured")
        sys.exit(1)

    runner = EchoRunner(config, echo_agent)
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
