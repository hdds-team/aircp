"""
AIRCP v2.1 - Channels

Active channels: #general, #brainstorm
Reserved channels (v0.2) deprecated — data in SQLite storage + dashboard APIs.

| Channel      | Purpose           | Status     |
|--------------|-------------------|------------|
| #general     | Discussion libre  | ✅ Active  |
| #brainstorm  | Brainstorm/Ideas  | ✅ Active  |
| #claims      | (deprecated)      | ❌ v2.1    |
| #locks       | (deprecated)      | ❌ v2.1    |
| #activity    | (deprecated)      | ❌ v2.1    |
| #presence    | (deprecated)      | ❌ v2.1    |
| #system      | (deprecated)      | ❌ v2.1    |
"""

from dataclasses import dataclass

# Active channels
CHANNEL_GENERAL = "#general"
CHANNEL_BRAINSTORM = "#brainstorm"

# Deprecated channel constants (kept for backward compat, no longer joined)
CHANNEL_CLAIMS = "#claims"
CHANNEL_LOCKS = "#locks"
CHANNEL_ACTIVITY = "#activity"
CHANNEL_PRESENCE = "#presence"
CHANNEL_SYSTEM = "#system"

# v2.1: No reserved channels are auto-joined anymore
RESERVED_CHANNELS = set()

# v2.1: No hub-only write restrictions needed
HUB_ONLY_WRITE = set()

# Active writable channels
AGENT_WRITABLE = {
    CHANNEL_GENERAL,
    CHANNEL_BRAINSTORM,
}


@dataclass
class ChannelPermission:
    """Permission check result."""
    allowed: bool
    reason: str = ""


def can_agent_write(channel: str, agent_id: str) -> ChannelPermission:
    """
    Check if an agent can write to a channel.

    Args:
        channel: Channel name (e.g., "#general")
        agent_id: Agent identifier (e.g., "@alpha")

    Returns:
        ChannelPermission with allowed status and reason
    """
    # All channels are writable now (deprecated ones won't exist)
    return ChannelPermission(allowed=True)


def is_reserved_channel(channel: str) -> bool:
    """Check if a channel is reserved (v2.1: always False)."""
    return False


def get_channel_description(channel: str) -> str:
    """Get human-readable description of a channel."""
    descriptions = {
        CHANNEL_GENERAL: "General discussion",
        CHANNEL_BRAINSTORM: "Brainstorm & Ideas",
    }
    return descriptions.get(channel, "User channel")
