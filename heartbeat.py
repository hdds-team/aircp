#!/usr/bin/env python3
"""
AIRCP Agent Heartbeat - Entry point for agent execution.

Usage:
    python heartbeat.py --agent alpha
    python heartbeat.py --agent alpha --once

Designed to be run by cron or systemd timer.
"""

import sys
import os
import asyncio
import argparse
import logging
from pathlib import Path

# Setup paths — use AIRCP_HOME (set by installer) or fallback to script dir
_AIRCP_HOME = os.environ.get("AIRCP_HOME", str(Path(__file__).resolve().parent))
_installed_lib = os.path.join(_AIRCP_HOME, "lib")
if os.path.isdir(_installed_lib):
    os.environ.setdefault("HDDS_LIB_PATH", _installed_lib)
else:
    os.environ.setdefault("HDDS_LIB_PATH", os.environ.get("HDDS_LIB_PATH", _installed_lib))
_hdds_sdk = os.path.join(_AIRCP_HOME, "lib", "hdds_sdk", "python")
if _hdds_sdk not in sys.path:
    sys.path.insert(0, _hdds_sdk)
if _AIRCP_HOME not in sys.path:
    sys.path.insert(0, _AIRCP_HOME)

def load_agent(config_dir: Path):
    """Load the appropriate agent based on provider config."""
    import tomllib

    config_file = config_dir / "config.toml"
    with open(config_file, "rb") as f:
        config = tomllib.load(f)

    provider = config.get("llm", {}).get("provider", "anthropic").lower()

    # Ollama (native API)
    if provider == "ollama":
        from agents.ollama_agent import OllamaAgent
        return OllamaAgent(config_dir)

    # Claude CLI (uses user's subscription)
    elif provider == "claude-cli":
        from agents.claude_cli_agent import ClaudeCliAgent
        return ClaudeCliAgent(config_dir)

    # Codex CLI (uses user's Codex CLI login)
    elif provider in ("codex-cli", "codex"):
        from agents.codex_cli_agent import CodexCliAgent
        return CodexCliAgent(config_dir)

    # Gemini CLI (uses Google auth)
    elif provider in ("gemini-cli", "gemini"):
        from agents.gemini_cli_agent import GeminiCliAgent
        return GeminiCliAgent(config_dir)

    # Claude API (direct Anthropic API)
    elif provider == "anthropic":
        from agents.claude_agent import ClaudeAgent
        return ClaudeAgent(config_dir)

    # OpenAI-compatible APIs (OpenAI, Groq, Mistral, Together, DeepSeek, LMStudio, vLLM, etc.)
    elif provider in ("openai", "groq", "mistral", "together", "deepseek",
                      "perplexity", "lmstudio", "vllm", "llamacpp", "ollama-openai"):
        from agents.openai_agent import OpenAIAgent
        return OpenAIAgent(config_dir)

    else:
        # Default to Claude CLI
        from agents.claude_cli_agent import ClaudeCliAgent
        return ClaudeCliAgent(config_dir)


async def run_heartbeat(agent_id: str, once: bool = True):
    """Run a single heartbeat for the specified agent."""
    config_dir = Path(f"agent_config/{agent_id}")

    if not config_dir.exists():
        print(f"Error: Config not found for agent '{agent_id}'")
        print(f"Expected: {config_dir}")
        sys.exit(1)

    agent = load_agent(config_dir)

    try:
        if once:
            await agent.heartbeat()
        else:
            # Continuous mode (for testing)
            print(f"Agent {agent.agent_id} running continuously...")
            while True:
                await agent.heartbeat()
                await asyncio.sleep(2)
    finally:
        agent.close()


def main():
    parser = argparse.ArgumentParser(description="AIRCP Agent Heartbeat")
    parser.add_argument("--agent", required=True, help="Agent ID (e.g., 'alpha')")
    parser.add_argument("--once", action="store_true", default=True,
                        help="Run once and exit (default)")
    parser.add_argument("--continuous", action="store_true",
                        help="Run continuously (for testing)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose logging")

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )

    # Run
    once = not args.continuous
    asyncio.run(run_heartbeat(args.agent, once=once))


if __name__ == "__main__":
    main()
