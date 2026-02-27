#!/usr/bin/env python3
"""
Test harness for Tool Loader (P5 v1.1)

Tests:
1. TOML spec loading (format, validation, error handling)
2. Ollama definition generation (output format, round-trip)
3. Handler map + tool names extraction
4. Integration: loader output matches what ToolRouter uses
5. Edge cases: missing file, bad format, empty specs

Usage:
    python3 -m pytest tests/test_tool_loader.py -v
    # or standalone:
    python3 tests/test_tool_loader.py
"""

import asyncio
import sys
import os
import tempfile
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.tool_loader import (
    load_tool_specs,
    generate_ollama_definitions,
    get_tool_names,
    get_handler_map,
    ToolSpec,
    ToolParam,
    DEFAULT_SPECS_PATH,
)
from agents.tool_router import TOOL_DEFINITIONS, TOOL_NAMES, ToolRouter


# =============================================================================
# Helpers
# =============================================================================
class TestResult:
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


# =============================================================================
# Tests
# =============================================================================
def test_specs_file_exists(results: TestResult):
    """tool_specs.toml must exist next to tool_loader.py."""
    print("\n📄 Specs File:")

    if DEFAULT_SPECS_PATH.exists():
        results.ok(f"tool_specs.toml exists at {DEFAULT_SPECS_PATH}")
    else:
        results.fail("Specs file missing", str(DEFAULT_SPECS_PATH))


def test_load_specs(results: TestResult):
    """Load specs and validate structure."""
    print("\n📋 Load Specs:")

    specs = load_tool_specs()

    if len(specs) == 4:
        results.ok(f"Loaded 4 tool specs")
    else:
        results.fail("Spec count", f"Expected 4, got {len(specs)}")

    expected = {"aircp_send", "aircp_history", "file_read", "file_list"}
    actual = {s.name for s in specs}
    if actual == expected:
        results.ok("All 4 expected tools present")
    else:
        results.fail("Tool names", f"Expected {expected}, got {actual}")

    # Check each spec has valid handler
    for spec in specs:
        if spec.handler in ("http", "filesystem"):
            results.ok(f"'{spec.name}' handler='{spec.handler}'")
        else:
            results.fail(f"'{spec.name}' handler", f"Invalid: '{spec.handler}'")

    # HTTP tools must have http_method and http_path
    for spec in specs:
        if spec.handler == "http":
            if spec.http_method and spec.http_path:
                results.ok(f"'{spec.name}' has http_method={spec.http_method} http_path={spec.http_path}")
            else:
                results.fail(f"'{spec.name}' HTTP config", "Missing http_method or http_path")


def test_params(results: TestResult):
    """Validate parameter specs."""
    print("\n🔧 Parameter Specs:")

    specs = load_tool_specs()
    spec_map = {s.name: s for s in specs}

    # aircp_send: message is required, room has default
    send = spec_map["aircp_send"]
    msg_param = next((p for p in send.params if p.name == "message"), None)
    room_param = next((p for p in send.params if p.name == "room"), None)

    if msg_param and msg_param.required:
        results.ok("aircp_send.message is required")
    else:
        results.fail("aircp_send.message", "Should be required")

    if room_param and room_param.default == "#general":
        results.ok("aircp_send.room default='#general'")
    else:
        results.fail("aircp_send.room default", f"Expected '#general'")

    # file_read: path is required, limit has default
    fread = spec_map["file_read"]
    path_param = next((p for p in fread.params if p.name == "path"), None)
    limit_param = next((p for p in fread.params if p.name == "limit"), None)

    if path_param and path_param.required:
        results.ok("file_read.path is required")
    else:
        results.fail("file_read.path", "Should be required")

    if limit_param and limit_param.default == 200:
        results.ok("file_read.limit default=200")
    else:
        results.fail("file_read.limit default", f"Got {limit_param.default if limit_param else 'None'}")


def test_ollama_generation(results: TestResult):
    """Test Ollama definition generation format."""
    print("\n🔄 Ollama Definition Generation:")

    specs = load_tool_specs()
    defs = generate_ollama_definitions(specs)

    if len(defs) == 4:
        results.ok("Generated 4 definitions")
    else:
        results.fail("Definition count", f"Expected 4, got {len(defs)}")

    # Check Ollama format structure
    for d in defs:
        name = d.get("function", {}).get("name", "?")

        if d.get("type") == "function":
            results.ok(f"'{name}' type='function'")
        else:
            results.fail(f"'{name}' type", f"Expected 'function', got '{d.get('type')}'")

        func = d.get("function", {})
        if "description" in func and "parameters" in func:
            results.ok(f"'{name}' has description + parameters")
        else:
            results.fail(f"'{name}' structure", "Missing fields")

        # Parameters must have JSON Schema format
        params = func.get("parameters", {})
        if params.get("type") == "object" and "properties" in params:
            results.ok(f"'{name}' params is valid JSON Schema object")
        else:
            results.fail(f"'{name}' params format", "Not a JSON Schema object")

        # Required field must be a list
        req = params.get("required", [])
        if isinstance(req, list):
            results.ok(f"'{name}' required is list: {req}")
        else:
            results.fail(f"'{name}' required", f"Expected list, got {type(req)}")


def test_round_trip_consistency(results: TestResult):
    """Generated definitions must match what ToolRouter exposes."""
    print("\n🔁 Round-Trip Consistency:")

    # TOOL_DEFINITIONS from tool_router.py (generated at import time)
    # should be identical to generating them fresh
    specs = load_tool_specs()
    fresh_defs = generate_ollama_definitions(specs)

    if len(TOOL_DEFINITIONS) == len(fresh_defs):
        results.ok(f"Same count: {len(TOOL_DEFINITIONS)}")
    else:
        results.fail("Count mismatch", f"Module: {len(TOOL_DEFINITIONS)}, Fresh: {len(fresh_defs)}")

    # Tool names must match
    module_names = {d["function"]["name"] for d in TOOL_DEFINITIONS}
    fresh_names = {d["function"]["name"] for d in fresh_defs}
    if module_names == fresh_names:
        results.ok(f"Same tool names: {sorted(module_names)}")
    else:
        results.fail("Name mismatch", f"Module: {module_names}, Fresh: {fresh_names}")

    # TOOL_NAMES set must match
    if TOOL_NAMES == get_tool_names(specs):
        results.ok("TOOL_NAMES set matches")
    else:
        results.fail("TOOL_NAMES mismatch", f"{TOOL_NAMES} vs {get_tool_names(specs)}")


def test_handler_map(results: TestResult):
    """Test handler map generation."""
    print("\n🗺️ Handler Map:")

    specs = load_tool_specs()
    hmap = get_handler_map(specs)

    if len(hmap) == 4:
        results.ok("Handler map has 4 entries")
    else:
        results.fail("Handler map size", f"Expected 4, got {len(hmap)}")

    # HTTP tools
    for name in ["aircp_send", "aircp_history"]:
        if name in hmap and hmap[name].handler == "http":
            results.ok(f"'{name}' → http handler")
        else:
            results.fail(f"'{name}' handler", "Should be 'http'")

    # Filesystem tools
    for name in ["file_read", "file_list"]:
        if name in hmap and hmap[name].handler == "filesystem":
            results.ok(f"'{name}' → filesystem handler")
        else:
            results.fail(f"'{name}' handler", "Should be 'filesystem'")


def test_error_handling(results: TestResult):
    """Test error cases: missing file, bad format."""
    print("\n⚠️ Error Handling:")

    # Missing file
    try:
        load_tool_specs(Path("/projects/aircp/tests/DOES_NOT_EXIST.toml"))
        results.fail("Missing file", "Should raise FileNotFoundError")
    except FileNotFoundError:
        results.ok("FileNotFoundError on missing specs")

    # Bad TOML content (write temp file)
    bad_toml = Path("/projects/aircp/tests/_bad_specs.toml")
    try:
        bad_toml.write_text("this is not [valid toml {{{")
        try:
            load_tool_specs(bad_toml)
            results.fail("Bad TOML", "Should raise exception")
        except Exception as e:
            results.ok(f"Bad TOML raises error: {type(e).__name__}")
    finally:
        bad_toml.unlink(missing_ok=True)

    # Valid TOML but missing required fields
    incomplete_toml = Path("/projects/aircp/tests/_incomplete_specs.toml")
    try:
        incomplete_toml.write_text('[[tools]]\ndescription = "no name"\nhandler = "http"\n')
        try:
            load_tool_specs(incomplete_toml)
            results.fail("Missing name", "Should raise ValueError")
        except ValueError as e:
            results.ok(f"Missing name raises ValueError: {e}")
    finally:
        incomplete_toml.unlink(missing_ok=True)

    # Invalid handler type
    bad_handler = Path("/projects/aircp/tests/_bad_handler.toml")
    try:
        bad_handler.write_text('[[tools]]\nname = "test"\ndescription = "x"\nhandler = "magic"\n')
        try:
            load_tool_specs(bad_handler)
            results.fail("Bad handler", "Should raise ValueError")
        except ValueError as e:
            results.ok(f"Bad handler raises ValueError: {e}")
    finally:
        bad_handler.unlink(missing_ok=True)


def test_router_integration(results: TestResult):
    """ToolRouter must work correctly with loader-generated definitions."""
    print("\n🔗 Router Integration:")

    router = ToolRouter(agent_id="@test", daemon_url="http://localhost:5555")

    # get_tool_definitions returns loader-generated defs
    defs = router.get_tool_definitions()
    names = {d["function"]["name"] for d in defs}
    expected = {"aircp_send", "aircp_history", "file_read", "file_list"}

    if names == expected:
        results.ok("Router exposes all 4 tools from loader")
    else:
        results.fail("Router tools", f"Expected {expected}, got {names}")

    # Whitelist still works with loader
    restricted = ToolRouter(
        agent_id="@test",
        daemon_url="http://localhost:5555",
        allowed_tools=["file_read", "file_list"],
    )
    rdefs = restricted.get_tool_definitions()
    rnames = {d["function"]["name"] for d in rdefs}
    if rnames == {"file_read", "file_list"}:
        results.ok("Whitelist works with loader-generated defs")
    else:
        results.fail("Whitelist + loader", f"Expected file_read/file_list, got {rnames}")


# =============================================================================
# Main
# =============================================================================
def main():
    print("=" * 60)
    print("🧪 Tool Loader P5 — Test Harness")
    print("=" * 60)

    results = TestResult()

    test_specs_file_exists(results)
    test_load_specs(results)
    test_params(results)
    test_ollama_generation(results)
    test_round_trip_consistency(results)
    test_handler_map(results)
    test_error_handling(results)
    test_router_integration(results)

    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
