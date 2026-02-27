"""
Tool Loader — Auto-generate Ollama tool definitions from TOML specs (P5 v1.1)

Reads tool_specs.toml and produces:
1. Ollama-compatible function definitions (for LLM tool calling)
2. Tool metadata (handler type, HTTP config) for ToolRouter dispatch

Usage:
    from .tool_loader import load_tool_specs, generate_ollama_definitions
    
    specs = load_tool_specs()                   # List of ToolSpec
    defs = generate_ollama_definitions(specs)    # Ollama JSON format
"""

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default specs file location (next to this module)
DEFAULT_SPECS_PATH = Path(__file__).parent / "tool_specs.toml"


@dataclass
class ToolParam:
    """A single tool parameter."""
    name: str
    type: str  # JSON Schema type: string, integer, boolean
    description: str
    required: bool = False
    default: Any = None  # sentinel: None means no default


@dataclass
class ToolSpec:
    """A complete tool specification."""
    name: str
    description: str
    handler: str  # "http" or "filesystem"
    params: list[ToolParam] = field(default_factory=list)
    # HTTP handler fields (only when handler == "http")
    http_method: str | None = None  # GET, POST
    http_path: str | None = None    # e.g., "/send"


def load_tool_specs(path: Path | None = None) -> list[ToolSpec]:
    """
    Load tool specifications from TOML file.
    
    Args:
        path: Path to tool_specs.toml. Defaults to file next to this module.
        
    Returns:
        List of ToolSpec objects.
        
    Raises:
        FileNotFoundError: If specs file doesn't exist.
        ValueError: If specs file has invalid format.
    """
    specs_path = path or DEFAULT_SPECS_PATH
    
    if not specs_path.exists():
        raise FileNotFoundError(f"Tool specs not found: {specs_path}")
    
    with open(specs_path, "rb") as f:
        raw = tomllib.load(f)
    
    tools_raw = raw.get("tools", [])
    if not isinstance(tools_raw, list):
        raise ValueError("tool_specs.toml must contain [[tools]] array")
    
    specs = []
    for i, t in enumerate(tools_raw):
        # Validate required fields
        name = t.get("name")
        if not name:
            raise ValueError(f"Tool #{i}: missing 'name'")
        
        description = t.get("description", "")
        handler = t.get("handler", "")
        if handler not in ("http", "filesystem"):
            raise ValueError(
                f"Tool '{name}': handler must be 'http' or 'filesystem', "
                f"got '{handler}'"
            )
        
        # Parse params
        params = []
        for p in t.get("params", []):
            param = ToolParam(
                name=p["name"],
                type=p.get("type", "string"),
                description=p.get("description", ""),
                required=p.get("required", False),
                default=p.get("default"),
            )
            params.append(param)
        
        spec = ToolSpec(
            name=name,
            description=description,
            handler=handler,
            params=params,
            http_method=t.get("http_method"),
            http_path=t.get("http_path"),
        )
        specs.append(spec)
    
    logger.info(f"Loaded {len(specs)} tool specs from {specs_path}")
    return specs


def generate_ollama_definitions(specs: list[ToolSpec]) -> list[dict]:
    """
    Generate Ollama-compatible tool definitions from specs.
    
    Produces the JSON format expected by Ollama's function calling:
    {
        "type": "function",
        "function": {
            "name": "...",
            "description": "...",
            "parameters": { JSON Schema }
        }
    }
    """
    definitions = []
    
    for spec in specs:
        # Build JSON Schema for parameters
        properties = {}
        required = []
        
        for param in spec.params:
            prop: dict[str, Any] = {
                "type": param.type,
                "description": param.description,
            }
            if param.default is not None:
                prop["default"] = param.default
            
            properties[param.name] = prop
            
            if param.required:
                required.append(param.name)
        
        definition = {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
            }
        }
        
        definitions.append(definition)
    
    return definitions


def get_tool_names(specs: list[ToolSpec]) -> set[str]:
    """Get set of all tool names from specs."""
    return {s.name for s in specs}


def get_handler_map(specs: list[ToolSpec]) -> dict[str, ToolSpec]:
    """Get mapping of tool_name → ToolSpec for routing."""
    return {s.name: s for s in specs}
