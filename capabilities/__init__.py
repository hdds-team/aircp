"""
AIRCP Capabilities - Tenuo integration for honest agents.

Agents know their REAL capabilities and don't hallucinate.
"""

from tenuo import configure, SigningKey, mint_sync, guard, Capability, Pattern, Exact
import logging

logger = logging.getLogger(__name__)

# Global signing key (in production, load from secure storage)
_issuer_key = None


def init_capabilities(dev_mode: bool = True):
    """Initialize Tenuo with a signing key."""
    global _issuer_key
    _issuer_key = SigningKey.generate()
    configure(issuer_key=_issuer_key, dev_mode=dev_mode)
    logger.info("Tenuo capabilities initialized")


def create_agent_warrant(agent_id: str, capabilities: list[dict]) -> list[Capability]:
    """
    Create capabilities for an agent based on config.

    Args:
        agent_id: Agent identifier (e.g., "@alpha")
        capabilities: List of capability dicts from config
            [{"tool": "file_read", "path": "/projects/*"}, ...]

    Returns:
        List of Tenuo Capability objects
    """
    caps = []

    for cap_config in capabilities:
        tool = cap_config.get("tool")
        if not tool:
            continue

        # Build constraints from config
        constraints = {}
        for key, value in cap_config.items():
            if key == "tool":
                continue
            if value.endswith("*"):
                constraints[key] = Pattern(value)
            else:
                constraints[key] = Exact(value)

        caps.append(Capability(tool, **constraints))
        logger.debug(f"[{agent_id}] Capability: {tool} with {constraints}")

    return caps


def format_capabilities_for_prompt(capabilities: list[dict]) -> str:
    """
    Format capabilities as human-readable text for the agent's prompt.

    This is the KEY part - the agent KNOWS what it can do.
    """
    if not capabilities:
        return """## My Capabilities (IMPORTANT)
I have NO action capabilities. I can ONLY chat.
- I can NOT read files
- I can NOT write files
- I can NOT execute commands
- I can NOT search the web

If asked to perform an action, I answer honestly:
"I don't have capability X. @naskel can ask CC to do it."
"""

    lines = ["## My Capabilities (cryptographically verified)"]
    lines.append("I can ONLY perform the following actions:")

    for cap in capabilities:
        tool = cap.get("tool", "unknown")
        constraints = [f"{k}={v}" for k, v in cap.items() if k != "tool"]
        constraint_str = ", ".join(constraints) if constraints else "unrestricted"
        lines.append(f"- **{tool}** : {constraint_str}")

    lines.append("")
    lines.append("For any action NOT listed above, I reply:")
    lines.append('"I don\'t have this capability. @naskel can ask CC."')

    return "\n".join(lines)


# Initialize on import in dev mode
init_capabilities(dev_mode=True)
