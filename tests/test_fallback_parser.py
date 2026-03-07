#!/usr/bin/env python3
"""
Test harness for FallbackParser (MCP Offline v1.1 — P6)

Tests:
1. Basic [TOOL:...] pattern extraction
2. Argument parsing (key:value, key=value, quoted values)
3. Multiple tools in one text block
4. Unknown tool rejection
5. Empty / no-pattern text → empty list
6. Type coercion (integer, boolean, string)
7. Edge cases (partial patterns, malformed, Unicode)
8. has_tool_intents() quick check
9. Integration with ToolRouter.execute()

Usage:
    python3 -m pytest tests/test_fallback_parser.py -v
    # or standalone:
    python3 tests/test_fallback_parser.py
"""

import asyncio
import sys
import os
from pathlib import Path

import pytest

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.fallback_parser import FallbackParser, KNOWN_TOOLS


# =============================================================================
# Helpers
# =============================================================================
class _TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def ok(self, name: str):
        self.passed += 1
        print(f"  ✅ {name}")

    def fail(self, name: str, reason: str):
        self.failed += 1
        self.errors.append((name, reason))
        print(f"  ❌ {name}: {reason}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            print("\nFailures:")
            for name, reason in self.errors:
                print(f"  - {name}: {reason}")
        print(f"{'='*60}")
        return self.failed == 0


@pytest.fixture
def results():
    """Provide _TestResult instance for pytest-collected test functions."""
    return _TestResult()


# =============================================================================
# Tests: Basic extraction
# =============================================================================
def test_single_tool_extraction(results: _TestResult):
    """Extract a single tool intent from text."""
    print("\n🔧 Single Tool Extraction:")

    parser = FallbackParser()

    text = """Let me check the chat history.
[TOOL: aircp_history]
room: #general
limit: 10
"""
    intents = parser.extract_tool_intents(text)

    if len(intents) == 1:
        results.ok("Found 1 intent")
    else:
        results.fail("Intent count", f"Expected 1, got {len(intents)}")
        return

    intent = intents[0]
    if intent["name"] == "aircp_history":
        results.ok("Tool name = aircp_history")
    else:
        results.fail("Tool name", f"Expected aircp_history, got {intent['name']}")

    if intent["arguments"].get("room") == "#general":
        results.ok("room = #general")
    else:
        results.fail("room arg", f"Got {intent['arguments']}")

    if intent["arguments"].get("limit") == 10:
        results.ok("limit = 10 (integer coerced)")
    else:
        results.fail("limit arg", f"Got {intent['arguments'].get('limit')}")


def test_multiple_tools(results: _TestResult):
    """Extract multiple tool intents from one text block."""
    print("\n🔧 Multiple Tools:")

    parser = FallbackParser()

    text = """I'll first read the history, then send a message.

[TOOL: aircp_history]
room: #general
limit: 5

Now let me send a reply.

[TOOL: aircp_send]
room: #general
message: Hello from fallback mode!
"""
    intents = parser.extract_tool_intents(text)

    if len(intents) == 2:
        results.ok("Found 2 intents")
    else:
        results.fail("Intent count", f"Expected 2, got {len(intents)}")
        return

    if intents[0]["name"] == "aircp_history":
        results.ok("First tool = aircp_history")
    else:
        results.fail("First tool", f"Got {intents[0]['name']}")

    if intents[1]["name"] == "aircp_send":
        results.ok("Second tool = aircp_send")
    else:
        results.fail("Second tool", f"Got {intents[1]['name']}")

    if intents[1]["arguments"].get("message") == "Hello from fallback mode!":
        results.ok("Message argument preserved")
    else:
        results.fail("Message arg", f"Got {intents[1]['arguments']}")


def test_file_read_intent(results: _TestResult):
    """Extract file_read tool intent."""
    print("\n📖 file_read Intent:")

    parser = FallbackParser()

    text = """Let me read that file.
[TOOL: file_read]
path: /projects/aircp/README.md
limit: 50
"""
    intents = parser.extract_tool_intents(text)

    if len(intents) == 1:
        results.ok("Found 1 intent")
    else:
        results.fail("Intent count", f"Expected 1, got {len(intents)}")
        return

    args = intents[0]["arguments"]
    if args.get("path") == "/projects/aircp/README.md":
        results.ok("path = /projects/aircp/README.md")
    else:
        results.fail("path arg", f"Got {args}")

    if args.get("limit") == 50:
        results.ok("limit = 50 (integer)")
    else:
        results.fail("limit arg", f"Got {args.get('limit')}")


# =============================================================================
# Tests: Argument parsing variants
# =============================================================================
def test_equals_sign_args(results: _TestResult):
    """Arguments with = instead of : separator."""
    print("\n🔧 Equals Sign Arguments:")

    parser = FallbackParser()

    text = """[TOOL: file_list]
path = /projects/aircp/agents
"""
    intents = parser.extract_tool_intents(text)

    if len(intents) == 1 and intents[0]["arguments"].get("path") == "/projects/aircp/agents":
        results.ok("Equals sign argument parsed")
    else:
        results.fail("Equals parsing", f"Got {intents}")


def test_quoted_values(results: _TestResult):
    """Arguments with quoted values."""
    print("\n🔧 Quoted Values:")

    parser = FallbackParser()

    text = '''[TOOL: aircp_send]
room: "#general"
message: "Hello world! This has: colons inside"
'''
    intents = parser.extract_tool_intents(text)

    if len(intents) == 1:
        results.ok("Found 1 intent")
    else:
        results.fail("Intent count", f"Expected 1, got {len(intents)}")
        return

    args = intents[0]["arguments"]
    if args.get("room") == "#general":
        results.ok("Quoted room stripped")
    else:
        results.fail("Quoted room", f"Got {args.get('room')}")

    if args.get("message") == "Hello world! This has: colons inside":
        results.ok("Quoted message with colons preserved")
    else:
        results.fail("Quoted message", f"Got {args.get('message')}")


def test_single_quoted_values(results: _TestResult):
    """Arguments with single-quoted values."""
    print("\n🔧 Single Quoted Values:")

    parser = FallbackParser()

    text = """[TOOL: aircp_send]
room: '#general'
message: 'Test message'
"""
    intents = parser.extract_tool_intents(text)

    if len(intents) == 1:
        results.ok("Found 1 intent")
        args = intents[0]["arguments"]
        if args.get("room") == "#general":
            results.ok("Single-quoted room stripped")
        else:
            results.fail("Single-quoted room", f"Got {args.get('room')}")
    else:
        results.fail("Intent count", f"Expected 1, got {len(intents)}")


# =============================================================================
# Tests: Edge cases
# =============================================================================
def test_empty_text(results: _TestResult):
    """Empty or whitespace-only text → empty list."""
    print("\n⚡ Edge Cases — Empty:")

    parser = FallbackParser()

    for text in ["", "   ", None, "\n\n"]:
        intents = parser.extract_tool_intents(text or "")
        if len(intents) == 0:
            results.ok(f"Empty text ({repr(text)}) → []")
        else:
            results.fail(f"Empty text ({repr(text)})", f"Got {len(intents)} intents")


def test_no_patterns(results: _TestResult):
    """Text without [TOOL:...] patterns → empty list."""
    print("\n⚡ Edge Cases — No Patterns:")

    parser = FallbackParser()

    texts = [
        "Just a normal response without any tools.",
        "I'll use aircp_send to communicate but not via the pattern.",
        "The [BRACKET] thing is not a tool call.",
        "TOOL: aircp_send without brackets",
    ]

    for text in texts:
        intents = parser.extract_tool_intents(text)
        if len(intents) == 0:
            results.ok(f"No patterns in: {text[:50]}...")
        else:
            results.fail(f"False positive", f"Found {len(intents)} in: {text[:50]}")


def test_unknown_tool_skipped(results: _TestResult):
    """Unknown tool names are skipped."""
    print("\n🚫 Unknown Tool Rejection:")

    parser = FallbackParser()

    text = """[TOOL: shell_exec]
command: rm -rf /

[TOOL: aircp_send]
message: This one is valid
"""
    intents = parser.extract_tool_intents(text)

    if len(intents) == 1 and intents[0]["name"] == "aircp_send":
        results.ok("Unknown tool skipped, valid tool kept")
    else:
        results.fail("Unknown tool", f"Got {intents}")


def test_case_insensitive(results: _TestResult):
    """[TOOL:...] pattern is case-insensitive."""
    print("\n⚡ Case Insensitive:")

    parser = FallbackParser()

    for pattern in ["[TOOL: aircp_send]", "[tool: aircp_send]", "[Tool: aircp_send]"]:
        text = f"{pattern}\nmessage: test"
        intents = parser.extract_tool_intents(text)
        if len(intents) == 1:
            results.ok(f"Pattern '{pattern}' recognized")
        else:
            results.fail(f"Pattern '{pattern}'", f"Got {len(intents)} intents")


def test_no_space_after_colon(results: _TestResult):
    """[TOOL:name] without space after colon."""
    print("\n⚡ No Space After Colon:")

    parser = FallbackParser()

    text = "[TOOL:aircp_history]\nroom: #general"
    intents = parser.extract_tool_intents(text)

    if len(intents) == 1 and intents[0]["name"] == "aircp_history":
        results.ok("No-space pattern recognized")
    else:
        results.fail("No-space pattern", f"Got {intents}")


def test_tool_with_no_args(results: _TestResult):
    """Tool with no arguments at all."""
    print("\n⚡ Tool With No Args:")

    parser = FallbackParser()

    text = "Let me check.\n[TOOL: aircp_history]\nThat's all."
    intents = parser.extract_tool_intents(text)

    if len(intents) == 1 and intents[0]["arguments"] == {}:
        results.ok("Tool with no args → empty dict")
    else:
        results.fail("No args", f"Got {intents}")


def test_has_tool_intents_quick_check(results: _TestResult):
    """Quick check with has_tool_intents()."""
    print("\n⚡ has_tool_intents():")

    parser = FallbackParser()

    if parser.has_tool_intents("[TOOL: aircp_send]\nmessage: hi"):
        results.ok("Detected tool pattern")
    else:
        results.fail("Detection", "Should detect [TOOL:...]")

    if not parser.has_tool_intents("No tools here"):
        results.ok("No false positive")
    else:
        results.fail("False positive", "Should not detect")

    if not parser.has_tool_intents(""):
        results.ok("Empty string → False")
    else:
        results.fail("Empty", "Should not detect")

    if not parser.has_tool_intents(None):
        results.ok("None → False")
    else:
        results.fail("None", "Should not detect")


# =============================================================================
# Tests: Type coercion
# =============================================================================
def test_type_coercion(results: _TestResult):
    """Integer and string type coercion from param hints."""
    print("\n🔄 Type Coercion:")

    parser = FallbackParser()

    text = """[TOOL: aircp_history]
room: #general
limit: 42
"""
    intents = parser.extract_tool_intents(text)
    args = intents[0]["arguments"]

    if isinstance(args["limit"], int) and args["limit"] == 42:
        results.ok("limit coerced to int (42)")
    else:
        results.fail("int coercion", f"Got {type(args['limit'])} {args['limit']}")

    if isinstance(args["room"], str) and args["room"] == "#general":
        results.ok("room stays str")
    else:
        results.fail("str coercion", f"Got {type(args['room'])} {args['room']}")


def test_invalid_int_stays_string(results: _TestResult):
    """Invalid integer value stays as string."""
    print("\n🔄 Invalid Int Fallback:")

    parser = FallbackParser()

    text = """[TOOL: aircp_history]
limit: not_a_number
"""
    intents = parser.extract_tool_intents(text)
    args = intents[0]["arguments"]

    if isinstance(args["limit"], str) and args["limit"] == "not_a_number":
        results.ok("Invalid int → kept as string")
    else:
        results.fail("Invalid int fallback", f"Got {type(args['limit'])} {args['limit']}")


# =============================================================================
# Tests: Custom known_tools
# =============================================================================
def test_custom_known_tools(results: _TestResult):
    """Parser with custom known_tools set."""
    print("\n🛡️ Custom Known Tools:")

    parser = FallbackParser(known_tools={"file_read"})

    text = """[TOOL: aircp_send]
message: should be skipped

[TOOL: file_read]
path: /projects/aircp/README.md
"""
    intents = parser.extract_tool_intents(text)

    if len(intents) == 1 and intents[0]["name"] == "file_read":
        results.ok("Only file_read accepted (custom whitelist)")
    else:
        results.fail("Custom whitelist", f"Got {intents}")


# =============================================================================
# Tests: Integration with ToolRouter
# =============================================================================
async def test_fallback_to_router(results: _TestResult):
    """Fallback intents can be passed to ToolRouter.execute()."""
    print("\n🔗 Fallback → Router Integration:")

    from agents.tool_router import ToolRouter

    parser = FallbackParser()
    router = ToolRouter(
        agent_id="@test-fallback",
        daemon_url="http://localhost:5555",
    )

    # Parse a file_read intent from text
    text = """[TOOL: file_read]
path: /projects/aircp/README.md
limit: 10
"""
    intents = parser.extract_tool_intents(text)

    if len(intents) != 1:
        results.fail("Parse", f"Expected 1 intent, got {len(intents)}")
        return

    # Execute through router
    intent = intents[0]
    result = await router.execute(intent["name"], intent["arguments"])

    if result["success"] and result.get("result"):
        results.ok("Fallback intent executed through router successfully")
    else:
        results.fail("Router exec", f"Failed: {result.get('error')}")

    # Parse an aircp_history intent
    text2 = """[TOOL: aircp_history]
room: #general
limit: 3
"""
    intents2 = parser.extract_tool_intents(text2)
    if intents2:
        result2 = await router.execute(intents2[0]["name"], intents2[0]["arguments"])
        if result2["success"]:
            results.ok("aircp_history via fallback works")
        else:
            results.fail("History fallback", f"Failed: {result2.get('error')}")

    # Parse a sandbox escape — should be caught by router
    text3 = """[TOOL: file_read]
path: /etc/passwd
"""
    intents3 = parser.extract_tool_intents(text3)
    if intents3:
        result3 = await router.execute(intents3[0]["name"], intents3[0]["arguments"])
        if not result3["success"] and "sandbox" in result3.get("error", "").lower():
            results.ok("Sandbox escape blocked even via fallback")
        else:
            results.fail("Sandbox via fallback", f"Got {result3}")


# =============================================================================
# Main
# =============================================================================
async def main():
    print("=" * 60)
    print("🧪 Fallback Parser P6 — Test Harness")
    print("=" * 60)

    results = _TestResult()

    # Pure parser tests (no async needed)
    test_single_tool_extraction(results)
    test_multiple_tools(results)
    test_file_read_intent(results)
    test_equals_sign_args(results)
    test_quoted_values(results)
    test_single_quoted_values(results)
    test_empty_text(results)
    test_no_patterns(results)
    test_unknown_tool_skipped(results)
    test_case_insensitive(results)
    test_no_space_after_colon(results)
    test_tool_with_no_args(results)
    test_has_tool_intents_quick_check(results)
    test_type_coercion(results)
    test_invalid_int_stays_string(results)
    test_custom_known_tools(results)

    # Integration tests (async, need daemon)
    await test_fallback_to_router(results)

    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
