#!/usr/bin/env python3
"""
AIRCP CLI - Command-line interface for HDDS transport.

Uses the AIRCP daemon (HTTP API) for persistent connections.
Falls back to direct HDDS if daemon not running.

Usage:
    aircp_cli.py join <room>
    aircp_cli.py send <room> <message>
    aircp_cli.py history <room> [--limit N]
    aircp_cli.py status
"""

import sys
import os
import json
import argparse
import time
import urllib.request
import urllib.error

DAEMON_URL = "http://localhost:5555"

def _daemon_auth_token():
    token = os.environ.get("AIRCP_AUTH_TOKEN", "").strip()
    if token:
        return token
    tokens = [t.strip() for t in os.environ.get("AIRCP_AUTH_TOKENS", "").split(",") if t.strip()]
    return tokens[0] if tokens else None


def _daemon_headers(extra=None):
    headers = dict(extra or {})
    token = _daemon_auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def daemon_request(method: str, path: str, data: dict = None) -> dict:
    """Make request to daemon, return None if daemon not running."""
    try:
        url = f"{DAEMON_URL}{path}"
        if method == "GET":
            req = urllib.request.Request(url, headers=_daemon_headers())
        else:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode() if data else None,
                headers=_daemon_headers({"Content-Type": "application/json"}),
                method=method
            )
        from aircp_http import safe_urlopen
        with safe_urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError:
        return None  # Daemon not running

# Setup paths and HDDS env
_AIRCP_HOME = os.environ.get("AIRCP_HOME", os.path.dirname(os.path.abspath(__file__)))
_hdds_lib = os.environ.get("HDDS_LIB_PATH", os.path.join(_AIRCP_HOME, "lib"))
os.environ.setdefault("HDDS_LIB_PATH", _hdds_lib)
os.environ.setdefault("HDDS_REUSEPORT", "1")  # Enable inter-process discovery
_hdds_sdk = os.path.join(_AIRCP_HOME, "lib", "hdds_sdk", "python")
if os.path.isdir(_hdds_sdk) and _hdds_sdk not in sys.path:
    sys.path.insert(0, _hdds_sdk)
if _AIRCP_HOME not in sys.path:
    sys.path.insert(0, _AIRCP_HOME)

if "LD_LIBRARY_PATH" not in os.environ:
    os.environ["LD_LIBRARY_PATH"] = _hdds_lib


def cmd_join(args):
    """Join a room and confirm."""
    # With daemon, joining happens automatically on send/history
    result = daemon_request("GET", "/status")

    if result is not None:
        # Daemon running, just confirm
        result = {
            "success": True,
            "room": args.room,
            "agent_id": result.get("agent_id", args.agent_id),
            "mode": "daemon"
        }
        print(json.dumps(result))
        return

    # Fallback to direct HDDS
    from transport.hdds import AIRCPTransport
    transport = AIRCPTransport(args.agent_id)
    success = transport.join_room(args.room)

    result = {
        "success": success,
        "room": args.room,
        "agent_id": args.agent_id,
        "mode": "direct"
    }

    time.sleep(0.5)
    transport.close()
    print(json.dumps(result))


def cmd_send(args):
    """Send a message to a room."""
    # Try daemon first
    result = daemon_request("POST", "/send", {
        "room": args.room,
        "message": args.message,
        "from": args.agent_id
    })

    if result is not None:
        result["content"] = args.message
        print(json.dumps(result))
        return

    # Fallback to direct HDDS
    from transport.hdds import AIRCPTransport
    transport = AIRCPTransport(args.agent_id)
    transport.join_room(args.room)
    time.sleep(3)

    msg_id = transport.send_chat(args.room, args.message)
    time.sleep(1)

    result = {
        "success": msg_id is not None,
        "message_id": msg_id,
        "room": args.room,
        "content": args.message
    }

    transport.close()
    print(json.dumps(result))


def cmd_history(args):
    """Get message history from a room."""
    # Try daemon first (URL-encode the room name, # becomes %23)
    from urllib.parse import quote
    encoded_room = quote(args.room, safe='')
    result = daemon_request("GET", f"/history?room={encoded_room}&limit={args.limit}")

    if result is not None:
        print(json.dumps(result))
        return

    # Fallback to direct HDDS
    from transport.hdds import AIRCPTransport
    transport = AIRCPTransport(args.agent_id)
    transport.join_room(args.room)
    time.sleep(1)

    messages = transport.get_history(args.room, limit=args.limit)

    result = {
        "room": args.room,
        "count": len(messages),
        "messages": [
            {
                "id": msg.id,
                "from": msg.from_id,
                "content": msg.payload.get("content", ""),
                "timestamp": msg.timestamp_ns
            }
            for msg in messages
        ]
    }

    transport.close()
    print(json.dumps(result))


def cmd_status(args):
    """Check HDDS transport status."""
    # Try daemon first
    result = daemon_request("GET", "/status")

    if result is not None:
        print(json.dumps(result))
        return

    # Fallback to direct HDDS
    try:
        from transport.hdds import AIRCPTransport
        transport = AIRCPTransport(args.agent_id)
        transport.join_room("#status-check")
        time.sleep(0.5)
        transport.close()

        result = {
            "status": "ok",
            "hdds": "connected",
            "agent_id": args.agent_id,
            "mode": "direct"
        }
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e)
        }

    print(json.dumps(result))


def load_identity():
    """Load identity from ~/.aircp/identity.toml"""
    try:
        import tomllib
        from pathlib import Path
        config_path = Path.home() / ".aircp" / "identity.toml"
        if config_path.exists():
            with open(config_path, "rb") as f:
                config = tomllib.load(f)
            return config.get("identity", {}).get("nickname", "@anonymous")
    except Exception:
        pass
    return "@anonymous"


def main():
    default_nick = load_identity()

    parser = argparse.ArgumentParser(description="AIRCP CLI")
    parser.add_argument("--agent-id", default=default_nick,
                        help="Agent ID for this session")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # join
    join_parser = subparsers.add_parser("join", help="Join a room")
    join_parser.add_argument("room", help="Room name (e.g., #general)")

    # send
    send_parser = subparsers.add_parser("send", help="Send a message")
    send_parser.add_argument("room", help="Room name")
    send_parser.add_argument("message", help="Message content")

    # history
    history_parser = subparsers.add_parser("history", help="Get room history")
    history_parser.add_argument("room", help="Room name")
    history_parser.add_argument("--limit", type=int, default=20,
                                help="Max messages to return")

    # status
    subparsers.add_parser("status", help="Check HDDS status")

    args = parser.parse_args()

    commands = {
        "join": cmd_join,
        "send": cmd_send,
        "history": cmd_history,
        "status": cmd_status,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
