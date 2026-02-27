# AIRCP Agents
#
# Keep package import lightweight for tooling/tests that only need a subset
# (e.g., tool_router) without cloud provider SDKs installed.
from .base_agent import PersistentAgent

__all__ = ["PersistentAgent"]

try:
    from .claude_agent import ClaudeAgent
    __all__.append("ClaudeAgent")
except Exception:
    pass

try:
    from .claude_cli_agent import ClaudeCliAgent
    __all__.append("ClaudeCliAgent")
except Exception:
    pass

try:
    from .claude_stream_agent import ClaudeStreamAgent
    __all__.append("ClaudeStreamAgent")
except Exception:
    pass

try:
    from .codex_cli_agent import CodexCliAgent
    __all__.append("CodexCliAgent")
except Exception:
    pass

try:
    from .gemini_cli_agent import GeminiCliAgent
    __all__.append("GeminiCliAgent")
except Exception:
    pass

try:
    from .ollama_agent import OllamaAgent
    __all__.append("OllamaAgent")
except Exception:
    pass

try:
    from .openai_agent import OpenAIAgent
    __all__.append("OpenAIAgent")
except Exception:
    pass
