#!/usr/bin/env python3
"""
AIRCP Claude OCR Runner - The Holy Grail 🎯

Integrates Claude Desktop's OCR capabilities into AIRCP.

Future enhancement: This will run within Claude Desktop environment and:
1. Listen for "ocr" messages from other agents
2. Extract text from images using Claude's vision
3. Return OCR results to the hub
4. Participate in multi-agent conversations

Status: Skeleton/documentation for future implementation
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


class ClaudeOCRRunner:
    """
    Claude Desktop OCR Agent

    This agent would run INSIDE Claude Desktop and:
    1. Receive "ocr" command messages
    2. Use Claude's vision to extract text from images
    3. Send results back to the hub

    Example interaction:
    ```
    user → hub:     "Please OCR this image: [base64]"
    hub → claude:   "Extract text from image"
    claude → hub:   "Extracted text: ..."
    hub → user:     "Text extracted successfully"
    ```

    This is the "Zero-API" dream:
    - No REST API server needed
    - Claude is embedded in the conversation
    - Can see images, process them, respond
    - Fully integrated with other agents
    """

    def __init__(self, config: dict, agent_config: dict):
        self.config = config
        self.agent_config = agent_config
        self.agent_id = f"@{agent_config.id}"
        self.room = agent_config.room
        self.api_key = agent_config.api_key
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

    async def run(self):
        """Connect to hub and run"""
        hub_url = self.config["irc"]["server"].replace("ws://", "wss://")
        hub_url = hub_url.replace("wss://", "ws://")  # Use ws for local

        logger.info(f"🎯 Claude OCR runner connecting to {hub_url}")

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

                logger.info(f"✅ Claude OCR Ready! Listening to {self.room}")

                # Listen for messages
                async for message in ws:
                    try:
                        envelope = msgpack.unpackb(message, raw=False)
                        await self.handle_message(envelope)
                    except Exception as e:
                        logger.error(f"Error: {e}")

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            logger.info("""
╔════════════════════════════════════════════════════════════╗
║  Claude OCR Runner - Future Implementation                 ║
╠════════════════════════════════════════════════════════════╣
║                                                            ║
║  This runner is designed to run INSIDE Claude Desktop     ║
║  and provide native OCR capabilities to AIRCP.            ║
║                                                            ║
║  Implementation steps:                                     ║
║  1. Embed AIRCP client in Claude Desktop                  ║
║  2. Add vision capability detection                       ║
║  3. Implement OCR message handler                         ║
║  4. Return extracted text to hub                          ║
║                                                            ║
║  Benefits:                                                 ║
║  ✓ No separate API server needed                          ║
║  ✓ Claude participates in multi-agent chats              ║
║  ✓ Zero-API architecture                                  ║
║  ✓ Image processing in real-time                         ║
║                                                            ║
║  This completes the AIRCP vision!                         ║
║                                                            ║
╚════════════════════════════════════════════════════════════╝
""")
            sys.exit(1)

    async def handle_message(self, envelope: dict):
        """Handle incoming message"""
        kind = envelope.get("kind")
        logger.debug(f"[OCR] Received {kind} message")

        if kind != "chat":
            return

        # Get content
        payload = envelope.get("payload", {})
        content = payload.get("content", "")
        from_id = envelope.get("from", {}).get("id")

        logger.info(f"[OCR] Message from {from_id}: {content[:50]}")

        if not content or from_id == self.agent_id:
            logger.info(f"[OCR] Ignoring (empty or self)")
            return

        # Check if this is an OCR request
        if "ocr" in content.lower() or "extract" in content.lower():
            logger.info(f"[OCR] Processing OCR request")

            # Here's where the magic would happen:
            # 1. Extract base64 image from message
            # 2. Call Claude's vision API (embedded in Desktop)
            # 3. Get extracted text
            # 4. Format response
            # 5. Send back to hub

            # For now, placeholder response
            response_text = f"[OCR would process image here]"
            logger.info(f"[OCR] → Responding: {response_text}")

            response = self.create_envelope("chat", {
                "role": "assistant",
                "content": response_text
            }, to={"room": self.room, "broadcast": True})

            await self.send(response)
            logger.info(f"[OCR] ✓ Response sent")

    async def send_hello(self):
        """Send hello"""
        envelope = self.create_envelope("control", {
            "command": "hello",
            "args": {
                "client_name": "claude-ocr-runner",
                "client_version": "0.1.0",
                "capabilities": ["chat", "ocr", "vision"]
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

    # Find claude-ocr agent config
    # (would need to be added to aircp-config.toml)
    claude_ocr_agent = None
    for agent in config.get("agents", []):
        if agent.id == "claude-ocr":
            claude_ocr_agent = agent
            break

    if not claude_ocr_agent:
        logger.error("Claude OCR agent not configured")
        logger.info("Add to aircp-config.toml:")
        print("""
[[agents]]
id = "claude-ocr"
type = "vision"
room = "#general"
api_key = "changeme-claude"

[agents.vision]
capability = "ocr"
enable_extraction = true
""")
        sys.exit(1)

    runner = ClaudeOCRRunner(config, claude_ocr_agent)
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
