"""
Fallback Parser — Extract tool intents from raw LLM text (MCP Offline v1.1)

When a local LLM (Ollama/devstral) fails to use proper function calling
(invalid JSON, no tool_calls, model doesn't support tools), this module
parses the raw text response to extract tool intents using regex patterns.

This is the "Option 4: Mode Script" fallback described in IDEAS-MCP-OFFLINE.md.

Supported patterns (case-insensitive, flexible whitespace):
    [TOOL: tool_name] or [TOOL:tool_name]
    arg_name: value
    arg_name = value
    
Example LLM output that gets parsed:
    "Let me check the history first.
     [TOOL: aircp_history]
     room: #general
     limit: 10
     
     Then I'll read the file.
     [TOOL: file_read]
     path: /projects/aircp/README.md"

Falls back gracefully: if no patterns found, returns empty list (no crash).

Usage:
    from .fallback_parser import FallbackParser
    
    parser = FallbackParser()
    intents = parser.extract_tool_intents(raw_text)
    # [{"name": "aircp_history", "arguments": {"room": "#general", "limit": 10}}, ...]
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Regex patterns for tool intent extraction
# Matches: [TOOL: name], [TOOL:name], [tool: name], etc.
TOOL_HEADER_RE = re.compile(
    r'\[TOOL\s*:\s*(\w+)\]',
    re.IGNORECASE,
)

# Matches: key: value  or  key = value (on a single line)
# Value can be quoted or unquoted
ARG_RE = re.compile(
    r'^\s*(\w+)\s*[:=]\s*(.+?)\s*$',
    re.MULTILINE,
)

# Known tool names (for validation)
KNOWN_TOOLS = {"aircp_send", "aircp_history", "file_read", "file_list"}

# Parameter type hints for auto-coercion
PARAM_TYPES: dict[str, dict[str, str]] = {
    "aircp_send": {"room": "string", "message": "string"},
    "aircp_history": {"room": "string", "limit": "integer"},
    "file_read": {"path": "string", "limit": "integer"},
    "file_list": {"path": "string"},
}


class FallbackParser:
    """
    Extracts tool call intents from raw LLM text output.
    
    Used as fallback when the model doesn't produce proper
    function calling JSON (tool_calls format).
    """

    def __init__(self, known_tools: set[str] | None = None):
        """
        Args:
            known_tools: Set of valid tool names. Defaults to KNOWN_TOOLS.
        """
        self.known_tools = known_tools or KNOWN_TOOLS

    def extract_tool_intents(self, text: str) -> list[dict[str, Any]]:
        """
        Parse raw text for tool call patterns.
        
        Args:
            text: Raw LLM response text.
            
        Returns:
            List of tool intent dicts: [{"name": str, "arguments": dict}, ...]
            Empty list if no tools found or text is empty.
        """
        if not text or not text.strip():
            return []

        intents = []
        
        # Split text by tool headers
        # Find all [TOOL: xxx] positions
        matches = list(TOOL_HEADER_RE.finditer(text))
        
        if not matches:
            logger.debug("No [TOOL:...] patterns found in text")
            return []

        for i, match in enumerate(matches):
            tool_name = match.group(1).lower()
            
            # Validate tool name
            if tool_name not in self.known_tools:
                logger.warning(
                    f"Fallback parser: unknown tool '{tool_name}', skipping"
                )
                continue
            
            # Extract the block of text after this header until next header or end
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block = text[start:end]
            
            # Parse arguments from the block
            arguments = self._parse_arguments(block, tool_name)
            
            intents.append({
                "name": tool_name,
                "arguments": arguments,
            })
            
            logger.info(
                f"Fallback parsed: {tool_name}({arguments})"
            )

        return intents

    def _parse_arguments(self, block: str, tool_name: str) -> dict[str, Any]:
        """
        Extract key:value or key=value pairs from a text block.
        
        Applies type coercion based on PARAM_TYPES.
        """
        args = {}
        type_hints = PARAM_TYPES.get(tool_name, {})
        
        for match in ARG_RE.finditer(block):
            key = match.group(1).lower()
            value = match.group(2).strip()
            
            # Strip surrounding quotes
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            
            # Type coercion
            expected_type = type_hints.get(key, "string")
            args[key] = self._coerce_type(value, expected_type)
        
        return args

    @staticmethod
    def _coerce_type(value: str, expected_type: str) -> Any:
        """Coerce a string value to the expected type."""
        if expected_type == "integer":
            try:
                return int(value)
            except ValueError:
                return value  # Keep as string if can't parse
        elif expected_type == "boolean":
            return value.lower() in ("true", "1", "yes", "oui")
        else:
            return value  # string by default

    def has_tool_intents(self, text: str) -> bool:
        """Quick check: does the text contain any [TOOL:...] patterns?"""
        return bool(TOOL_HEADER_RE.search(text or ""))
