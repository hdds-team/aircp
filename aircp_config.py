"""
AIRCP Configuration Parser

Reads and validates aircp-config.toml
Used by: Hub, Runners, CLI tools
"""
import tomllib  # Python 3.11+ use tomli for older versions
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass


@dataclass
class HubConfig:
    """Hub configuration"""
    bind: str = "127.0.0.1:7777"
    log_level: str = "info"
    data_dir: str = "~/.local/share/aircp"
    default_rooms: List[str] = None
    max_history: int = 1000
    storage_path: str = "aircp.db"

    def __post_init__(self):
        if self.default_rooms is None:
            self.default_rooms = ["#general"]


@dataclass
class AuthKey:
    """Authentication key"""
    id: str
    key: str
    roles: List[str] = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["agent"]


@dataclass
class AgentConfig:
    """Agent runner configuration"""
    id: str
    type: str  # "api", "x11_capture", "stdio", etc.
    enabled: bool = True
    workspace: int = 1
    room: str = "#general"
    api_key: str = None
    config: Dict[str, Any] = None

    def __post_init__(self):
        if self.config is None:
            self.config = {}


@dataclass
class RoutingRule:
    """Message routing rule"""
    name: str
    enabled: bool = True
    priority: int = 0
    from_agent: Optional[str] = None
    pattern: Optional[str] = None
    to: List[str] = None
    mode: str = "first"  # first, all, random, round_robin
    aggregate: bool = False
    then: Optional[List[str]] = None

    def __post_init__(self):
        if self.to is None:
            self.to = []


class AIRCPConfigParser:
    """Parse and validate AIRCP configuration"""

    @staticmethod
    def load(config_path: Path) -> Dict[str, Any]:
        """Load and parse TOML config file"""
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, "rb") as f:
            try:
                config = tomllib.load(f)
            except Exception as e:
                raise ValueError(f"Failed to parse TOML: {e}")

        return AIRCPConfigParser._validate_config(config)

    @staticmethod
    def _validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
        """Validate config structure"""
        if not isinstance(config, dict):
            raise ValueError("Config must be a dictionary")

        # Parse hub config
        hub_dict = config.get("hub", {})
        config["hub"] = HubConfig(**{k: v for k, v in hub_dict.items() if k in HubConfig.__dataclass_fields__})

        # Parse auth keys
        auth_dict = config.get("auth", {})
        auth_keys = []
        for key_dict in auth_dict.get("keys", []):
            auth_keys.append(AuthKey(**key_dict))
        config["auth_keys"] = auth_keys

        # Parse agents
        agent_list = []
        for agent_dict in config.get("agents", []):
            agent_id = agent_dict.get("id")
            agent_type = agent_dict.get("type")
            if not agent_id or not agent_type:
                raise ValueError("Each agent must have 'id' and 'type'")

            # Extract agent-specific config
            agent_config = {k: v for k, v in agent_dict.items()
                           if k not in ["id", "type", "enabled", "workspace", "room", "api_key", "capture", "api", "behavior", "inject"]}

            # Store nested configs
            if "capture" in agent_dict:
                agent_config["capture"] = agent_dict["capture"]
            if "api" in agent_dict:
                agent_config["api"] = agent_dict["api"]
            if "behavior" in agent_dict:
                agent_config["behavior"] = agent_dict["behavior"]
            if "inject" in agent_dict:
                agent_config["inject"] = agent_dict["inject"]

            agent = AgentConfig(
                id=agent_id,
                type=agent_type,
                enabled=agent_dict.get("enabled", True),
                workspace=agent_dict.get("workspace", 1),
                room=agent_dict.get("room", "#general"),
                api_key=agent_dict.get("api_key"),
                config=agent_config
            )
            agent_list.append(agent)

        config["agents"] = agent_list

        # Parse routing rules
        routing_dict = config.get("routing", {})
        rules = []
        for rule_dict in routing_dict.get("rules", []):
            rule_name = rule_dict.get("name")
            if not rule_name:
                raise ValueError("Each rule must have a 'name'")

            rule = RoutingRule(
                name=rule_name,
                enabled=rule_dict.get("enabled", True),
                priority=rule_dict.get("priority", 0),
                from_agent=rule_dict.get("from"),
                pattern=rule_dict.get("pattern"),
                to=rule_dict.get("to", []),
                mode=rule_dict.get("mode", "first"),
                aggregate=rule_dict.get("aggregate", False),
                then=rule_dict.get("then")
            )
            rules.append(rule)

        config["routing_rules"] = rules

        # Parse IRC client config
        irc_dict = config.get("irc", {})
        config["irc"] = {
            "server": irc_dict.get("server", "ws://127.0.0.1:7777"),
            "api_key": irc_dict.get("api_key"),
            "nick": irc_dict.get("nick", "operator"),
            "auto_join": irc_dict.get("auto_join", ["#general"]),
            "ui": irc_dict.get("ui", {})
        }

        return config

    @staticmethod
    def from_dict(config_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Parse config from dictionary (for testing)"""
        return AIRCPConfigParser._validate_config(config_dict)

    @staticmethod
    def example() -> str:
        """Return example config"""
        return """
[hub]
bind = "127.0.0.1:7777"
log_level = "info"
data_dir = "~/.local/share/aircp"
default_rooms = ["#general"]
max_history = 1000

[auth]
[[auth.keys]]
id = "irc-client"
key = "changeme-irc"
roles = ["admin"]

[[auth.keys]]
id = "runner-echo"
key = "changeme-echo"
roles = ["agent"]

# Echo runner (testing)
[[agents]]
id = "echo"
type = "stdio"
enabled = true
workspace = 1
room = "#general"
api_key = "runner-echo"

[agents.behavior]
prefix = "ECHO: "
auto_respond = true

# IRC Client
[irc]
server = "ws://127.0.0.1:7777"
api_key = "irc-client"
nick = "operator"
auto_join = ["#general"]

[irc.ui]
theme = "dark"
timestamps = true
"""
