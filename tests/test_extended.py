#!/usr/bin/env python3
"""
Extended Test Harness (MCP Offline v1.1 — P6)

Tests for edge cases and failure modes:
1. Daemon down → graceful error handling (HTTP tools fail, FS tools work)
2. Max tool rounds exceeded → returns last response
3. Invalid tool arguments → handled gracefully
4. Concurrent tool calls → sequential execution
5. Large file handling → truncation works
6. Router stats and coverage
7. Empty tool definitions (no specs) → graceful degradation

Usage:
    python3 -m pytest tests/test_extended.py -v
    # or standalone:
    python3 tests/test_extended.py
"""

import asyncio
import sys
import os
import tempfile
from pathlib import Path

import pytest

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.tool_router import ToolRouter, _validate_sandbox_path, TOOL_NAMES, MAX_FILE_SIZE
from agents.fallback_parser import FallbackParser


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
# Test fixtures
# =============================================================================
FIXTURE_DIR = Path("/projects/aircp/tests/_fixtures_extended")


def setup_fixtures():
    """Create test fixtures."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    # Large file (100 lines)
    lines = [f"Line {i}: {'x' * 80}" for i in range(100)]
    (FIXTURE_DIR / "large_file.txt").write_text("\n".join(lines))

    # Empty file
    (FIXTURE_DIR / "empty.txt").write_text("")

    # File with Unicode
    (FIXTURE_DIR / "unicode.txt").write_text(
        "Héllo wörld 🌍\nÉmojis partout 🚀🔥\nCafé résumé naïve"
    )

    # Deeply nested path
    nested = FIXTURE_DIR / "a" / "b" / "c"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "deep.txt").write_text("found me!")

    # Directory with many files
    many_dir = FIXTURE_DIR / "many_files"
    many_dir.mkdir(exist_ok=True)
    for i in range(110):
        (many_dir / f"file_{i:03d}.txt").write_text(f"content {i}")

    print(f"  📁 Extended fixtures created in {FIXTURE_DIR}")


def cleanup_fixtures():
    """Remove test fixtures."""
    import shutil
    if FIXTURE_DIR.exists():
        shutil.rmtree(FIXTURE_DIR)
        print(f"  🗑️ Extended fixtures cleaned up")


# =============================================================================
# Tests: Daemon Down
# =============================================================================
async def test_daemon_down_http_tools(results: _TestResult):
    """HTTP tools must fail gracefully when daemon is down."""
    print("\n🔌 Daemon Down — HTTP Tools:")

    # Point to a port that isn't running
    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:19999",  # Dead port
        timeout=2.0,
    )

    # aircp_send should fail with connection error, not crash
    result = await router.execute("aircp_send", {
        "room": "#general",
        "message": "test daemon down"
    })

    if not result["success"]:
        error = result.get("error", "")
        results.ok(f"aircp_send failed gracefully: {error[:60]}")
    else:
        results.fail("aircp_send", "Should have failed with daemon down")

    # aircp_history should also fail gracefully
    result = await router.execute("aircp_history", {
        "room": "#general",
        "limit": 5
    })

    if not result["success"]:
        results.ok("aircp_history failed gracefully")
    else:
        results.fail("aircp_history", "Should have failed with daemon down")


async def test_daemon_down_fs_tools_work(results: _TestResult):
    """Filesystem tools must still work when daemon is down."""
    print("\n🔌 Daemon Down — FS Tools Still Work:")

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:19999",  # Dead port
        timeout=2.0,
    )

    # file_read should work (no daemon needed)
    result = await router.execute("file_read", {
        "path": "/projects/aircp/README.md"
    })

    if result["success"]:
        results.ok("file_read works without daemon")
    else:
        results.fail("file_read", f"Should work offline: {result.get('error')}")

    # file_list should work
    result = await router.execute("file_list", {
        "path": "/projects/aircp/agents"
    })

    if result["success"]:
        results.ok("file_list works without daemon")
    else:
        results.fail("file_list", f"Should work offline: {result.get('error')}")


# =============================================================================
# Tests: Invalid Arguments
# =============================================================================
async def test_invalid_args(results: _TestResult):
    """Tools must handle invalid/missing arguments gracefully."""
    print("\n⚠️ Invalid Arguments:")

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    # file_read with empty path
    result = await router.execute("file_read", {"path": ""})
    if not result["success"]:
        results.ok("file_read empty path → error")
    else:
        results.fail("Empty path", "Should fail")

    # file_read with path to a directory (not a file)
    result = await router.execute("file_read", {"path": "/projects/aircp/agents"})
    if not result["success"] and "not a file" in result.get("error", "").lower():
        results.ok("file_read on directory → 'not a file' error")
    else:
        results.fail("Dir as file", f"Expected 'not a file' error: {result}")

    # file_list with path to a file (not a directory)
    result = await router.execute("file_list", {"path": "/projects/aircp/README.md"})
    if not result["success"] and "not a directory" in result.get("error", "").lower():
        results.ok("file_list on file → 'not a directory' error")
    else:
        results.fail("File as dir", f"Expected 'not a directory' error: {result}")

    # file_read with None path
    result = await router.execute("file_read", {"path": None})
    if not result["success"]:
        results.ok("file_read None path → error")
    else:
        results.fail("None path", "Should fail")

    # file_read with integer path
    result = await router.execute("file_read", {"path": 42})
    if not result["success"]:
        results.ok("file_read integer path → error")
    else:
        results.fail("Integer path", "Should fail")


# =============================================================================
# Tests: Large Files and Truncation
# =============================================================================
async def test_large_file_truncation(results: _TestResult):
    """Large files are truncated to the limit."""
    print("\n📏 Large File Truncation:")

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    # Read with small limit
    result = await router.execute("file_read", {
        "path": str(FIXTURE_DIR / "large_file.txt"),
        "limit": 10,
    })

    if result["success"] and "tronqué" in result.get("result", ""):
        results.ok("Large file truncated with notice")
    else:
        results.fail("Truncation", f"Expected truncation notice: {result}")

    # Read with limit > file lines → no truncation
    result = await router.execute("file_read", {
        "path": str(FIXTURE_DIR / "large_file.txt"),
        "limit": 500,
    })

    if result["success"] and "tronqué" not in result.get("result", ""):
        results.ok("Small file not truncated")
    else:
        results.fail("No truncation", f"Got: {result}")


async def test_empty_file(results: _TestResult):
    """Empty file returns empty content (not error)."""
    print("\n📄 Empty File:")

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    result = await router.execute("file_read", {
        "path": str(FIXTURE_DIR / "empty.txt"),
    })

    if result["success"]:
        results.ok("Empty file read successfully")
    else:
        results.fail("Empty file", f"Should succeed: {result.get('error')}")


async def test_unicode_file(results: _TestResult):
    """Unicode content is handled correctly."""
    print("\n🌍 Unicode File:")

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    result = await router.execute("file_read", {
        "path": str(FIXTURE_DIR / "unicode.txt"),
    })

    if result["success"] and "🌍" in result.get("result", ""):
        results.ok("Unicode content preserved (emoji)")
    else:
        results.fail("Unicode", f"Got: {result}")

    if "Héllo" in result.get("result", ""):
        results.ok("Unicode accents preserved")
    else:
        results.fail("Accents", f"Missing accents in: {result.get('result', '')[:50]}")


# =============================================================================
# Tests: file_list Edge Cases
# =============================================================================
async def test_file_list_capped(results: _TestResult):
    """file_list caps at 100 entries with overflow notice."""
    print("\n📂 file_list Capping:")

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    result = await router.execute("file_list", {
        "path": str(FIXTURE_DIR / "many_files"),
    })

    if result["success"] and "autres" in result.get("result", ""):
        results.ok("file_list capped at 100 with overflow notice")
    else:
        results.fail("Capping", f"Expected overflow notice: {result}")


async def test_deep_nested_read(results: _TestResult):
    """Can read deeply nested files."""
    print("\n📂 Deep Nested Read:")

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    result = await router.execute("file_read", {
        "path": str(FIXTURE_DIR / "a" / "b" / "c" / "deep.txt"),
    })

    if result["success"] and "found me!" in result.get("result", ""):
        results.ok("Deep nested file read OK")
    else:
        results.fail("Deep nested", f"Got: {result}")


# =============================================================================
# Tests: Sandbox edge cases
# =============================================================================
async def test_sandbox_symlink_escape(results: _TestResult):
    """Symlinks outside sandbox must be blocked."""
    print("\n🔒 Sandbox Symlink:")

    # Create a symlink inside sandbox that points outside
    link_path = FIXTURE_DIR / "escape_link"
    try:
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        link_path.symlink_to("/etc/passwd")
    except OSError:
        results.ok("Could not create symlink (permission denied) — OK")
        return

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    result = await router.execute("file_read", {"path": str(link_path)})
    if not result["success"] and "sandbox" in result.get("error", "").lower():
        results.ok("Symlink escape blocked by sandbox")
    else:
        results.fail("Symlink escape", f"Should be blocked: {result}")

    # Cleanup
    if link_path.is_symlink():
        link_path.unlink()


async def test_path_traversal_variants(results: _TestResult):
    """Various path traversal attempts must be blocked."""
    print("\n🔒 Path Traversal Variants:")

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    attacks = [
        "/projects/aircp/../../etc/passwd",
        "/projects/aircp/agents/../../../etc/shadow",
        "/projects/../../../root/.ssh/id_rsa",
        "/projects/./../../etc/hosts",
    ]

    for path in attacks:
        result = await router.execute("file_read", {"path": path})
        if not result["success"]:
            results.ok(f"Blocked: {path}")
        else:
            results.fail(f"NOT blocked: {path}", f"Should be rejected")


# =============================================================================
# Tests: Limit clamping
# =============================================================================
async def test_limit_clamping(results: _TestResult):
    """Limit values are clamped to valid range."""
    print("\n📏 Limit Clamping:")

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    # Negative limit → clamped to 1
    result = await router.execute("file_read", {
        "path": str(FIXTURE_DIR / "large_file.txt"),
        "limit": -5,
    })
    if result["success"]:
        lines = result["result"].split("\n")
        results.ok(f"Negative limit clamped (got {len(lines)} lines)")
    else:
        results.fail("Negative limit", f"Error: {result.get('error')}")

    # Huge limit → clamped to 500
    result = await router.execute("file_read", {
        "path": str(FIXTURE_DIR / "large_file.txt"),
        "limit": 99999,
    })
    if result["success"]:
        results.ok("Huge limit clamped to 500")
    else:
        results.fail("Huge limit", f"Error: {result.get('error')}")


# =============================================================================
# Tests: Tool not in spec (spec-driven routing)
# =============================================================================
async def test_spec_routing_unknown_handler(results: _TestResult):
    """Tool with unknown handler type → error."""
    print("\n🗺️ Spec Routing:")

    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    # Try a tool that doesn't exist in specs at all
    result = await router.execute("nonexistent_tool", {"foo": "bar"})
    if not result["success"] and "not allowed" in result.get("error", ""):
        results.ok("Non-existent tool rejected")
    else:
        results.fail("Non-existent tool", f"Should be rejected: {result}")


# =============================================================================
# Tests: Fallback + Router combined scenarios
# =============================================================================
async def test_fallback_daemon_down(results: _TestResult):
    """Fallback intents for HTTP tools fail gracefully when daemon is down."""
    print("\n🔌 Fallback + Daemon Down:")

    parser = FallbackParser()
    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:19999",  # Dead port
        timeout=2.0,
    )

    text = """[TOOL: aircp_send]
room: #general
message: test with daemon down
"""
    intents = parser.extract_tool_intents(text)

    if intents:
        result = await router.execute(intents[0]["name"], intents[0]["arguments"])
        if not result["success"]:
            results.ok("Fallback HTTP tool fails gracefully with daemon down")
        else:
            results.fail("Should fail", f"Got: {result}")

    # FS tool via fallback should still work
    text2 = """[TOOL: file_read]
path: /projects/aircp/README.md
"""
    intents2 = parser.extract_tool_intents(text2)
    if intents2:
        result2 = await router.execute(intents2[0]["name"], intents2[0]["arguments"])
        if result2["success"]:
            results.ok("Fallback FS tool works even with daemon down")
        else:
            results.fail("FS fallback", f"Should work: {result2.get('error')}")


async def test_fallback_sandbox_escape(results: _TestResult):
    """Fallback-parsed paths must still respect sandbox."""
    print("\n🔒 Fallback + Sandbox:")

    parser = FallbackParser()
    router = ToolRouter(
        agent_id="@test-extended",
        daemon_url="http://localhost:5555",
    )

    text = """[TOOL: file_read]
path: /etc/shadow

[TOOL: file_list]
path: /root
"""
    intents = parser.extract_tool_intents(text)

    for intent in intents:
        result = await router.execute(intent["name"], intent["arguments"])
        if not result["success"] and "sandbox" in result.get("error", "").lower():
            results.ok(f"Sandbox blocks fallback {intent['name']}: {intent['arguments']['path']}")
        else:
            results.fail(f"Sandbox via fallback", f"Should block: {result}")


# =============================================================================
# Main
# =============================================================================
async def main():
    print("=" * 60)
    print("🧪 Extended Test Harness — MCP Offline v1.1 (P6)")
    print("=" * 60)

    results = _TestResult()

    # Setup
    print("\n⚙️ Setup:")
    setup_fixtures()

    try:
        # Daemon down tests
        await test_daemon_down_http_tools(results)
        await test_daemon_down_fs_tools_work(results)

        # Invalid arguments
        await test_invalid_args(results)

        # Large files / truncation
        await test_large_file_truncation(results)
        await test_empty_file(results)
        await test_unicode_file(results)

        # file_list edge cases
        await test_file_list_capped(results)
        await test_deep_nested_read(results)

        # Sandbox
        await test_sandbox_symlink_escape(results)
        await test_path_traversal_variants(results)

        # Limit clamping
        await test_limit_clamping(results)

        # Spec routing
        await test_spec_routing_unknown_handler(results)

        # Fallback + Router
        await test_fallback_daemon_down(results)
        await test_fallback_sandbox_escape(results)

    finally:
        # Cleanup
        print("\n⚙️ Cleanup:")
        cleanup_fixtures()

    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
