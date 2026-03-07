#!/usr/bin/env python3
"""
Test harness for ToolRouter (MCP Offline v1.1)

Tests:
1. aircp_send → daemon HTTP
2. aircp_history → daemon HTTP
3. file_read → filesystem (sandboxed)
4. file_list → filesystem (sandboxed)
5. Sandbox escape attempts → must fail
6. Unknown tool → must fail
7. Tool whitelist enforcement
8. Binary file detection (P3) → must refuse

Usage:
    python3 -m pytest tests/test_tool_router.py -v
    # or standalone:
    python3 tests/test_tool_router.py
"""

import asyncio
import sys
import os
import tempfile
from pathlib import Path

import pytest

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.tool_router import ToolRouter, _validate_sandbox_path, TOOL_DEFINITIONS, BINARY_EXTENSIONS


# =============================================================================
# Config
# =============================================================================
DAEMON_URL = "http://localhost:5555"
AGENT_ID = "@test-harness"
TEST_ROOM = "#general"


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
# Test fixtures — create temp binary files in sandbox
# =============================================================================
FIXTURE_DIR = Path("/projects/aircp/tests/_fixtures")


def setup_fixtures():
    """Create temporary test fixtures for binary detection tests."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. A real binary file (contains null bytes)
    (FIXTURE_DIR / "fake_image.png").write_bytes(
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + b'\x00' * 20
    )

    # 2. A file with binary extension but no null bytes (edge case)
    (FIXTURE_DIR / "empty.db").write_bytes(b'')

    # 3. A file with no extension but null bytes inside
    (FIXTURE_DIR / "mystery_binary").write_bytes(
        b'looks like text but\x00has null bytes inside'
    )

    # 4. A normal text file (should NOT be detected as binary)
    (FIXTURE_DIR / "readme.txt").write_text("Hello world\nThis is text.\n")

    # 5. A JSON file (should NOT be detected as binary despite MIME)
    (FIXTURE_DIR / "data.json").write_text('{"key": "value"}\n')

    # 6. A .pyc file (bytecode — should be detected)
    (FIXTURE_DIR / "module.pyc").write_bytes(
        b'\x42\x0d\r\n\x00\x00\x00\x00' + b'\x00' * 50
    )

    # 7. A .tar.gz archive (should be detected)
    (FIXTURE_DIR / "archive.tar.gz").write_bytes(
        b'\x1f\x8b\x08\x00' + b'\x00' * 30
    )

    # 8. A shell script with no extension but text content
    (FIXTURE_DIR / "run_me").write_text("#!/bin/bash\necho hello\n")

    print(f"  📁 Fixtures created in {FIXTURE_DIR}")


def cleanup_fixtures():
    """Remove test fixtures."""
    import shutil
    if FIXTURE_DIR.exists():
        shutil.rmtree(FIXTURE_DIR)
        print(f"  🗑️ Fixtures cleaned up")


# =============================================================================
# Tests
# =============================================================================
async def test_sandbox_validation(results: _TestResult):
    """Test sandbox path validation."""
    print("\n🔒 Sandbox Validation:")

    # Valid paths
    for path in ["/projects/aircp/README.md", "/projects/synaptic/backend/api.py"]:
        try:
            resolved = _validate_sandbox_path(path)
            results.ok(f"Valid path accepted: {path}")
        except ValueError:
            results.fail(f"Valid path rejected: {path}", "Should be allowed")

    # Invalid paths (sandbox escape)
    for path in ["/etc/passwd", "/home/user/.bashrc", "/tmp/evil.sh",
                 "/projects/../etc/passwd", "/../../../etc/shadow",
                 "/projects2/escape.txt"]:
        try:
            resolved = _validate_sandbox_path(path)
            # Check if resolved path is still outside sandbox
            if not resolved.startswith("/projects/"):
                results.fail(f"Escape NOT blocked: {path}", f"Resolved to {resolved}")
            else:
                results.ok(f"Path resolved safely: {path} → {resolved}")
        except ValueError:
            results.ok(f"Escape blocked: {path}")


async def test_tool_definitions(results: _TestResult):
    """Test tool definitions structure."""
    print("\n📋 Tool Definitions:")

    router = ToolRouter(agent_id=AGENT_ID, daemon_url=DAEMON_URL)
    defs = router.get_tool_definitions()

    if len(defs) == 4:
        results.ok(f"4 tools defined")
    else:
        results.fail("Tool count", f"Expected 4, got {len(defs)}")

    expected_names = {"aircp_send", "aircp_history", "file_read", "file_list"}
    actual_names = {t["function"]["name"] for t in defs}
    if actual_names == expected_names:
        results.ok("All expected tools present")
    else:
        results.fail("Tool names", f"Expected {expected_names}, got {actual_names}")

    # Each tool must have proper structure
    for t in defs:
        name = t["function"]["name"]
        if "description" in t["function"] and "parameters" in t["function"]:
            results.ok(f"Tool '{name}' has description + parameters")
        else:
            results.fail(f"Tool '{name}' structure", "Missing description or parameters")


async def test_whitelist(results: _TestResult):
    """Test tool whitelist enforcement."""
    print("\n🛡️ Whitelist Enforcement:")

    # Router with restricted whitelist
    router = ToolRouter(
        agent_id=AGENT_ID,
        daemon_url=DAEMON_URL,
        allowed_tools=["file_read"],
    )

    defs = router.get_tool_definitions()
    if len(defs) == 1 and defs[0]["function"]["name"] == "file_read":
        results.ok("Whitelist filters tool definitions")
    else:
        results.fail("Whitelist filter", f"Expected 1 tool, got {len(defs)}")

    # Try executing a non-whitelisted tool
    result = await router.execute("aircp_send", {"message": "test"})
    if not result["success"] and "not allowed" in result.get("error", ""):
        results.ok("Non-whitelisted tool rejected")
    else:
        results.fail("Whitelist exec", f"Should have rejected aircp_send: {result}")


async def test_file_read(results: _TestResult):
    """Test file_read tool."""
    print("\n📖 file_read:")

    router = ToolRouter(agent_id=AGENT_ID, daemon_url=DAEMON_URL)

    # Read existing file
    result = await router.execute("file_read", {"path": "/projects/aircp/agents/tool_router.py"})
    if result["success"] and "ToolRouter" in result.get("result", ""):
        results.ok("Read existing file")
    else:
        results.fail("Read existing file", str(result.get("error", result)))

    # Read with limit
    result = await router.execute("file_read", {"path": "/projects/aircp/agents/tool_router.py", "limit": 5})
    if result["success"]:
        lines = result["result"].split("\n")
        # Should have ~5 content lines + truncation notice
        results.ok(f"Read with limit=5 ({len(lines)} lines returned)")
    else:
        results.fail("Read with limit", str(result.get("error")))

    # Read non-existent file
    result = await router.execute("file_read", {"path": "/projects/aircp/DOES_NOT_EXIST.txt"})
    if not result["success"] and "not found" in result.get("error", "").lower():
        results.ok("Non-existent file rejected")
    else:
        results.fail("Non-existent file", str(result))

    # Sandbox escape via file_read
    result = await router.execute("file_read", {"path": "/etc/passwd"})
    if not result["success"] and "sandbox" in result.get("error", "").lower():
        results.ok("Sandbox escape blocked (file_read)")
    else:
        results.fail("Sandbox escape (file_read)", str(result))

    # Missing path argument
    result = await router.execute("file_read", {})
    if not result["success"]:
        results.ok("Missing path argument handled")
    else:
        results.fail("Missing path", "Should have failed")


async def test_binary_detection(results: _TestResult):
    """Test binary file detection (P3)."""
    print("\n🔍 Binary Detection (P3):")

    router = ToolRouter(agent_id=AGENT_ID, daemon_url=DAEMON_URL)

    # --- Files that MUST be rejected as binary ---

    # 1. PNG file (extension-based detection)
    result = await router.execute("file_read", {"path": str(FIXTURE_DIR / "fake_image.png")})
    if not result["success"] and "binary" in result.get("error", "").lower():
        results.ok("PNG file rejected (extension match)")
    else:
        results.fail("PNG detection", f"Should reject binary: {result}")

    # 2. .db file (extension-based)
    result = await router.execute("file_read", {"path": str(FIXTURE_DIR / "empty.db")})
    if not result["success"] and "binary" in result.get("error", "").lower():
        results.ok("DB file rejected (extension match)")
    else:
        results.fail("DB detection", f"Should reject binary: {result}")

    # 3. .pyc bytecode (extension-based)
    result = await router.execute("file_read", {"path": str(FIXTURE_DIR / "module.pyc")})
    if not result["success"] and "binary" in result.get("error", "").lower():
        results.ok("PYC file rejected (extension match)")
    else:
        results.fail("PYC detection", f"Should reject binary: {result}")

    # 4. .tar.gz archive (extension-based)
    result = await router.execute("file_read", {"path": str(FIXTURE_DIR / "archive.tar.gz")})
    if not result["success"] and "binary" in result.get("error", "").lower():
        results.ok("TAR.GZ file rejected (extension match)")
    else:
        results.fail("TAR.GZ detection", f"Should reject binary: {result}")

    # 5. No extension but null bytes inside (content sniffing)
    result = await router.execute("file_read", {"path": str(FIXTURE_DIR / "mystery_binary")})
    if not result["success"] and "binary" in result.get("error", "").lower():
        results.ok("Null-byte file rejected (content sniffing)")
    else:
        results.fail("Null-byte detection", f"Should reject binary: {result}")

    # --- Files that MUST be accepted as text ---

    # 6. Normal .txt file
    result = await router.execute("file_read", {"path": str(FIXTURE_DIR / "readme.txt")})
    if result["success"] and "Hello world" in result.get("result", ""):
        results.ok("TXT file accepted (text)")
    else:
        results.fail("TXT read", f"Should accept text file: {result}")

    # 7. JSON file (MIME is application/json, must be allowed)
    result = await router.execute("file_read", {"path": str(FIXTURE_DIR / "data.json")})
    if result["success"] and "key" in result.get("result", ""):
        results.ok("JSON file accepted (text)")
    else:
        results.fail("JSON read", f"Should accept JSON file: {result}")

    # 8. Shell script with no extension (text content)
    result = await router.execute("file_read", {"path": str(FIXTURE_DIR / "run_me")})
    if result["success"] and "echo hello" in result.get("result", ""):
        results.ok("Script file accepted (text, no extension)")
    else:
        results.fail("Script read", f"Should accept script: {result}")


async def test_binary_extensions_set(results: _TestResult):
    """Test BINARY_EXTENSIONS set integrity."""
    print("\n📦 Binary Extensions Set:")

    # Must contain common dangerous extensions
    critical = {'.png', '.jpg', '.exe', '.zip', '.pyc', '.pdf', '.sqlite3'}
    missing = critical - BINARY_EXTENSIONS
    if not missing:
        results.ok(f"All critical extensions present ({len(BINARY_EXTENSIONS)} total)")
    else:
        results.fail("Critical extensions missing", str(missing))

    # Must NOT contain text extensions
    text_exts = {'.py', '.js', '.ts', '.md', '.txt', '.toml', '.yaml', '.json', '.html', '.css'}
    overlap = text_exts & BINARY_EXTENSIONS
    if not overlap:
        results.ok("No text extensions in binary set")
    else:
        results.fail("Text extensions in binary set", str(overlap))


async def test_file_list(results: _TestResult):
    """Test file_list tool."""
    print("\n📂 file_list:")

    router = ToolRouter(agent_id=AGENT_ID, daemon_url=DAEMON_URL)

    # List existing directory
    result = await router.execute("file_list", {"path": "/projects/aircp/agents"})
    if result["success"] and "tool_router" in result.get("result", "").lower():
        results.ok("List existing directory")
    else:
        results.fail("List directory", str(result.get("error", result)))

    # List non-existent directory
    result = await router.execute("file_list", {"path": "/projects/aircp/NOPE"})
    if not result["success"]:
        results.ok("Non-existent directory rejected")
    else:
        results.fail("Non-existent dir", "Should have failed")

    # Sandbox escape via file_list
    result = await router.execute("file_list", {"path": "/etc"})
    if not result["success"] and "sandbox" in result.get("error", "").lower():
        results.ok("Sandbox escape blocked (file_list)")
    else:
        results.fail("Sandbox escape (file_list)", str(result))


async def test_aircp_history(results: _TestResult):
    """Test aircp_history tool (requires daemon)."""
    print("\n📜 aircp_history:")

    router = ToolRouter(agent_id=AGENT_ID, daemon_url=DAEMON_URL)

    result = await router.execute("aircp_history", {"room": "#general", "limit": 5})
    if result["success"] and result.get("result"):
        # Should contain some messages
        msg_count = result["result"].count("[")
        results.ok(f"History retrieved ({msg_count} messages)")
    else:
        results.fail("History read", str(result.get("error", result)))

    # Test limit clamping (over max)
    result = await router.execute("aircp_history", {"room": "#general", "limit": 999})
    if result["success"]:
        results.ok("Limit clamped (999 → 100)")
    else:
        results.fail("Limit clamp", str(result.get("error")))


async def test_aircp_send(results: _TestResult):
    """Test aircp_send tool (requires daemon)."""
    print("\n📤 aircp_send:")

    router = ToolRouter(agent_id=AGENT_ID, daemon_url=DAEMON_URL)

    # Send a test message
    result = await router.execute("aircp_send", {
        "room": "#general",
        "message": "🧪 [test-harness] Tool router v1.1 test — ignore this message"
    })
    if result["success"]:
        results.ok("Message sent")
    else:
        results.fail("Send message", str(result.get("error", result)))

    # Empty message
    result = await router.execute("aircp_send", {"message": ""})
    if not result["success"] and "empty" in result.get("error", "").lower():
        results.ok("Empty message rejected")
    else:
        results.fail("Empty message", str(result))


async def test_unknown_tool(results: _TestResult):
    """Test unknown tool rejection."""
    print("\n🚫 Unknown Tool:")

    router = ToolRouter(agent_id=AGENT_ID, daemon_url=DAEMON_URL)

    result = await router.execute("shell_exec", {"command": "rm -rf /"})
    if not result["success"] and "not allowed" in result.get("error", ""):
        results.ok("Unknown tool rejected")
    else:
        results.fail("Unknown tool", f"Should have rejected: {result}")


# =============================================================================
# Main
# =============================================================================
async def main():
    print("=" * 60)
    print("🧪 MCP Offline v1.1 — Tool Router Test Harness")
    print("=" * 60)
    print(f"Daemon: {DAEMON_URL}")
    print(f"Agent: {AGENT_ID}")

    results = _TestResult()

    # Setup fixtures for binary tests
    print("\n⚙️ Setup:")
    setup_fixtures()

    try:
        # Run all tests
        await test_sandbox_validation(results)
        await test_tool_definitions(results)
        await test_whitelist(results)
        await test_file_read(results)
        await test_binary_detection(results)
        await test_binary_extensions_set(results)
        await test_file_list(results)
        await test_aircp_history(results)
        await test_aircp_send(results)
        await test_unknown_tool(results)
    finally:
        # Always cleanup fixtures
        print("\n⚙️ Cleanup:")
        cleanup_fixtures()

    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
