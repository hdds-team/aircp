"""
Tool Router for Offline Agents (MCP Offline v1.1-P5)

Routes Ollama/local LLM tool_calls to:
- AIRCP daemon HTTP API (port 5555) for communication tools
- Direct filesystem access for file_read (sandboxed)

Security:
- Read-only by default for local agents (no file_write, no shell)
- Sandbox enforced: only /projects/* paths allowed
- Tools whitelist per agent via config.toml
- Tool definitions auto-generated from tool_specs.toml (P5)
- Binary file detection (P3) — refuses to read non-text files

Usage:
    router = ToolRouter(agent_id="@mascotte", daemon_url="http://localhost:5555")
    result = await router.execute("aircp_send", {"room": "#general", "message": "Hello"})
"""

import ast
import httpx
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from .tool_loader import (
    load_tool_specs,
    generate_ollama_definitions,
    get_tool_names,
    get_handler_map,
    ToolSpec,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Sandbox configuration
# =============================================================================
SANDBOX_ROOTS = ["/projects/"]  # Allowed path prefixes for file operations
MAX_FILE_SIZE = 1_000_000  # 1MB max file read

# Binary file extensions — refuse to read_text() these
BINARY_EXTENSIONS = {
    # Compiled / bytecode
    '.pyc', '.pyo', '.so', '.o', '.a', '.dll', '.exe', '.bin', '.class', '.jar',
    # Images
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.tiff', '.raw',
    # Audio / Video
    '.mp3', '.mp4', '.wav', '.avi', '.mkv', '.mov', '.flac', '.ogg', '.webm',
    # Archives
    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar', '.whl', '.egg',
    # Documents (binary)
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    # Fonts
    '.ttf', '.otf', '.woff', '.woff2', '.eot',
    # WebAssembly
    '.wasm',
    # Databases
    '.db', '.sqlite', '.sqlite3',
}


# =============================================================================
# Tool definitions — auto-generated from tool_specs.toml (P5)
# =============================================================================
try:
    _TOOL_SPECS = load_tool_specs()
    TOOL_DEFINITIONS = generate_ollama_definitions(_TOOL_SPECS)
    TOOL_NAMES = get_tool_names(_TOOL_SPECS)
    _HANDLER_MAP = get_handler_map(_TOOL_SPECS)
    logger.info(
        f"Loaded {len(_TOOL_SPECS)} tools from tool_specs.toml: "
        f"{sorted(TOOL_NAMES)}"
    )
except FileNotFoundError:
    logger.warning("tool_specs.toml not found — no tools available")
    _TOOL_SPECS = []
    TOOL_DEFINITIONS = []
    TOOL_NAMES = set()
    _HANDLER_MAP = {}
except Exception as e:
    logger.error(f"Failed to load tool_specs.toml: {e}")
    _TOOL_SPECS = []
    TOOL_DEFINITIONS = []
    TOOL_NAMES = set()
    _HANDLER_MAP = {}


# =============================================================================
# Sandbox validation
# =============================================================================
def _validate_sandbox_path(path: str) -> str:
    """
    Validate and resolve a path against sandbox roots.

    Returns resolved absolute path or raises ValueError.
    """
    # Resolve to absolute, following symlinks
    resolved_path = Path(path).resolve()

    # Check against all allowed roots (path-aware, no prefix bypass like /projects2)
    for root in SANDBOX_ROOTS:
        root_path = Path(root).resolve()
        try:
            resolved_path.relative_to(root_path)
            return str(resolved_path)
        except ValueError:
            continue

    raise ValueError(
        f"Path '{path}' is outside sandbox. "
        f"Allowed: {', '.join(SANDBOX_ROOTS)}"
    )


# =============================================================================
# Tool Router
# =============================================================================
class ToolRouter:
    """
    Routes tool calls from local LLMs to appropriate backends.

    - AIRCP tools → daemon HTTP API
    - File tools → direct filesystem (sandboxed)

    Tool definitions are loaded from tool_specs.toml (P5).
    Routing uses the handler type from each ToolSpec:
    - handler="http" → forwards to daemon HTTP API
    - handler="filesystem" → dispatches to local file handlers
    """

    # Registry of filesystem handler methods (tool_name -> method_name)
    _FS_HANDLERS = {
        "file_read": "_exec_file_read",
        "file_list": "_exec_file_list",
        "code_summary": "_exec_code_summary",
    }

    def __init__(
        self,
        agent_id: str,
        daemon_url: str = "http://localhost:5555",
        allowed_tools: list[str] | None = None,
        timeout: float = 10.0,
    ):
        """
        Args:
            agent_id: Agent identifier (e.g., "@mascotte")
            daemon_url: AIRCP daemon HTTP URL
            allowed_tools: Whitelist of tool names (None = all defined tools)
            timeout: HTTP request timeout in seconds
        """
        self.agent_id = agent_id
        self.daemon_url = daemon_url.rstrip("/")
        self.timeout = timeout

        # Tool whitelist
        if allowed_tools is not None:
            self.allowed_tools = set(allowed_tools) & TOOL_NAMES
        else:
            self.allowed_tools = TOOL_NAMES.copy()

        logger.info(
            f"ToolRouter initialized: agent={agent_id}, "
            f"tools={sorted(self.allowed_tools)}"
        )

    def _auth_headers(self) -> dict[str, str]:
        token = os.environ.get("AIRCP_AUTH_TOKEN", "").strip()
        if not token:
            tokens = [t.strip() for t in os.environ.get("AIRCP_AUTH_TOKENS", "").split(",") if t.strip()]
            token = tokens[0] if tokens else ""
        return {"Authorization": f"Bearer {token}"} if token else {}

    def get_tool_definitions(self) -> list[dict]:
        """Return Ollama-compatible tool definitions for allowed tools only."""
        return [
            t for t in TOOL_DEFINITIONS
            if t["function"]["name"] in self.allowed_tools
        ]

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict:
        """
        Execute a tool call and return the result.

        Routing is driven by ToolSpec.handler from tool_specs.toml:
        - "http" → generic HTTP dispatch to daemon
        - "filesystem" → local handler method lookup

        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments

        Returns:
            Dict with keys:
            - success: bool
            - result: Any (tool output)
            - error: str | None
        """
        if tool_name not in self.allowed_tools:
            return {
                "success": False,
                "result": None,
                "error": f"Tool '{tool_name}' not allowed for this agent"
            }

        # Look up spec for routing
        spec = _HANDLER_MAP.get(tool_name)
        if not spec:
            return {
                "success": False,
                "result": None,
                "error": f"No spec found for tool '{tool_name}'"
            }

        try:
            if spec.handler == "http":
                # Generic HTTP dispatch based on spec metadata
                return await self._exec_http_tool(spec, arguments)
            elif spec.handler == "filesystem":
                # Dispatch to specific filesystem handler
                handler_name = self._FS_HANDLERS.get(tool_name)
                if handler_name and hasattr(self, handler_name):
                    handler = getattr(self, handler_name)
                    return handler(arguments)
                return {
                    "success": False,
                    "result": None,
                    "error": f"No filesystem handler for '{tool_name}'"
                }
            else:
                return {
                    "success": False,
                    "result": None,
                    "error": f"Unknown handler type '{spec.handler}' for '{tool_name}'"
                }
        except Exception as e:
            logger.error(f"Tool execution error ({tool_name}): {e}")
            return {
                "success": False,
                "result": None,
                "error": str(e)
            }

    # =========================================================================
    # Generic HTTP dispatch (P5 — spec-driven)
    # =========================================================================
    async def _exec_http_tool(self, spec: ToolSpec, args: dict) -> dict:
        """
        Generic HTTP tool execution based on ToolSpec metadata.

        Uses spec.http_method and spec.http_path to route to daemon.
        Falls back to legacy handlers for tools that need custom logic
        (e.g., aircp_send formats the payload, aircp_history formats output).
        """
        # Legacy handlers for tools with custom request/response logic
        if spec.name == "aircp_send":
            return await self._exec_aircp_send(args)
        elif spec.name == "aircp_history":
            return await self._exec_aircp_history(args)

        # Generic HTTP dispatch (for future http tools)
        method = (spec.http_method or "GET").upper()
        path = spec.http_path or f"/{spec.name}"
        headers = self._auth_headers()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if method == "POST":
                resp = await client.post(
                    f"{self.daemon_url}{path}",
                    json=args,
                    headers=headers,
                )
            else:
                resp = await client.get(
                    f"{self.daemon_url}{path}",
                    params=args,
                    headers=headers,
                )

            if resp.status_code == 200:
                data = resp.json()
                return {
                    "success": True,
                    "result": json.dumps(data, ensure_ascii=False),
                    "error": None,
                }
            else:
                return {
                    "success": False,
                    "result": None,
                    "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                }

    # =========================================================================
    # AIRCP tools — legacy handlers (custom request/response formatting)
    # =========================================================================
    async def _exec_aircp_send(self, args: dict) -> dict:
        """Send a message to an AIRCP channel via daemon."""
        room = args.get("room", "#general")
        message = args.get("message", "")

        if not message:
            return {"success": False, "result": None, "error": "Empty message"}

        headers = self._auth_headers()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.daemon_url}/send",
                json={
                    "room": room,
                    "message": message,
                    "from": self.agent_id,
                },
                headers=headers,
            )

            data = resp.json()

            if resp.status_code == 200 and data.get("success"):
                return {
                    "success": True,
                    "result": f"Message envoyé dans {room}",
                    "error": None,
                }
            else:
                error = data.get("error", data.get("message", f"HTTP {resp.status_code}"))
                return {
                    "success": False,
                    "result": None,
                    "error": f"Send error: {error}",
                }

    async def _exec_aircp_history(self, args: dict) -> dict:
        """Read AIRCP channel history via daemon."""
        room = args.get("room", "#general")
        limit = args.get("limit", 20)

        # Clamp limit
        limit = max(1, min(limit, 100))

        headers = self._auth_headers()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.daemon_url}/history",
                params={"room": room, "limit": limit},
                headers=headers,
            )

            if resp.status_code != 200:
                return {
                    "success": False,
                    "result": None,
                    "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                }

            data = resp.json()
            messages = data.get("messages", data.get("history", []))

            # Format messages for LLM consumption (compact)
            formatted = []
            for msg in messages:
                sender = msg.get("from", "?")
                content = msg.get("content", msg.get("payload", {}).get("content", ""))
                if content:
                    # Truncate long messages
                    if len(content) > 300:
                        content = content[:300] + "..."
                    formatted.append(f"[{sender}]: {content}")

            result_text = "\n".join(formatted) if formatted else "(aucun message)"

            return {
                "success": True,
                "result": result_text,
                "error": None,
            }

    # =========================================================================
    # File tools (direct filesystem, sandboxed)
    # =========================================================================
    def _exec_file_read(self, args: dict) -> dict:
        """Read a file from the sandboxed filesystem."""
        path = args.get("path", "")
        limit = args.get("limit", 200)
        offset = int(args.get("offset", 0))

        if not path:
            return {"success": False, "result": None, "error": "Missing 'path' argument"}

        # Validate sandbox
        try:
            safe_path = _validate_sandbox_path(path)
        except ValueError as e:
            return {"success": False, "result": None, "error": str(e)}

        # Check file exists
        p = Path(safe_path)
        if not p.exists():
            return {"success": False, "result": None, "error": f"File not found: {path}"}
        if not p.is_file():
            return {"success": False, "result": None, "error": f"Not a file: {path}"}

        # Check file size
        size = p.stat().st_size
        if size > MAX_FILE_SIZE:
            return {
                "success": False,
                "result": None,
                "error": f"File too large: {size} bytes (max {MAX_FILE_SIZE})"
            }

        # Detect binary files (P3 fix — v1.1)
        if self._is_binary(p):
            return {
                "success": False,
                "result": None,
                "error": (
                    f"Binary file detected: {p.name} "
                    f"({p.suffix or 'no extension'}, {size} bytes). "
                    f"Cannot read binary files — use file_list to browse directories instead."
                )
            }

        # Read with line limit and offset
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            limit = max(1, min(int(limit), 200))  # Clamp (aligned with spec)
            offset = max(0, min(offset, len(lines)))
            selected = lines[offset:offset + limit]
            truncated = (offset + limit) < len(lines)
            content = "\n".join(selected)
            if truncated:
                remaining = len(lines) - offset - limit
                content += f"\n\n[... tronque: {len(lines)} lignes total, affiche {offset+1}-{offset+len(selected)}, reste {remaining}]"
            elif offset > 0:
                content = f"[lignes {offset+1}-{offset+len(selected)} sur {len(lines)}]\n" + content

            return {
                "success": True,
                "result": content,
                "error": None,
            }
        except Exception as e:
            return {"success": False, "result": None, "error": f"Read error: {e}"}

    def _exec_file_list(self, args: dict) -> dict:
        """List files in a sandboxed directory."""
        path = args.get("path", "")

        if not path:
            return {"success": False, "result": None, "error": "Missing 'path' argument"}

        # Validate sandbox
        try:
            safe_path = _validate_sandbox_path(path)
        except ValueError as e:
            return {"success": False, "result": None, "error": str(e)}

        p = Path(safe_path)
        if not p.exists():
            return {"success": False, "result": None, "error": f"Directory not found: {path}"}
        if not p.is_dir():
            return {"success": False, "result": None, "error": f"Not a directory: {path}"}

        # List entries (max 100)
        try:
            all_entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
            total = len(all_entries)
            lines = []
            for i, entry in enumerate(all_entries):
                if i >= 100:
                    lines.append(f"... et {total - 100} autres")
                    break
                prefix = "📁 " if entry.is_dir() else "📄 "
                size = ""
                if entry.is_file():
                    s = entry.stat().st_size
                    if s < 1024:
                        size = f" ({s}B)"
                    elif s < 1024 * 1024:
                        size = f" ({s // 1024}KB)"
                    else:
                        size = f" ({s // (1024 * 1024)}MB)"
                lines.append(f"{prefix}{entry.name}{size}")

            result = "\n".join(lines) if lines else "(répertoire vide)"
            return {"success": True, "result": result, "error": None}
        except Exception as e:
            return {"success": False, "result": None, "error": f"List error: {e}"}

    # =========================================================================
    # Code analysis tools
    # =========================================================================
    def _exec_code_summary(self, args: dict) -> dict:
        """
        Analyze a Python file using AST and produce a structured summary.

        Output includes: imports, globals, classes (with methods+signatures),
        standalone functions, LOC stats, and TODO/FIXME comments.
        Designed to give ~50 lines of skeleton instead of 600 raw lines.
        """
        path = args.get("path", "")
        # Bool coercion: LLMs may send "false"/"true" as strings
        raw_docstrings = args.get("include_docstrings", False)
        if isinstance(raw_docstrings, str):
            include_docstrings = raw_docstrings.lower() in ("true", "1", "yes")
        else:
            include_docstrings = bool(raw_docstrings)

        if not path:
            return {"success": False, "result": None, "error": "Missing 'path' argument"}

        # Validate sandbox
        try:
            safe_path = _validate_sandbox_path(path)
        except ValueError as e:
            return {"success": False, "result": None, "error": str(e)}

        p = Path(safe_path)
        if not p.exists():
            return {"success": False, "result": None, "error": f"File not found: {path}"}
        if not p.is_file():
            return {"success": False, "result": None, "error": f"Not a file: {path}"}
        if p.suffix != ".py":
            return {"success": False, "result": None, "error": f"Not a Python file: {p.name}"}

        # Read source
        try:
            source = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"success": False, "result": None, "error": f"Read error: {e}"}

        # Parse AST
        try:
            tree = ast.parse(source, filename=str(p))
        except SyntaxError as e:
            return {
                "success": False,
                "result": None,
                "error": f"SyntaxError at line {e.lineno}: {e.msg}"
            }

        lines = source.splitlines()
        total_loc = len(lines)
        blank_lines = sum(1 for line in lines if not line.strip())
        comment_lines = sum(1 for line in lines if line.strip().startswith("#"))
        code_lines = total_loc - blank_lines - comment_lines

        # Extract TODO/FIXME comments
        todo_pattern = re.compile(r"#\s*(TODO|FIXME|HACK|XXX|BUG)\b[:\s]*(.*)", re.IGNORECASE)
        todos = []
        for i, line in enumerate(lines, 1):
            m = todo_pattern.search(line)
            if m:
                todos.append(f"  L{i}: {m.group(1).upper()}: {m.group(2).strip()}")

        # Walk AST
        imports = []
        globals_found = []
        classes = []
        functions = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
            elif isinstance(node, ast.ImportFrom):
                names = ", ".join(
                    a.name + (f" as {a.asname}" if a.asname else "")
                    for a in node.names
                )
                module = node.module or ""
                imports.append(f"from {module} import {names}")
            elif isinstance(node, ast.ClassDef):
                classes.append(self._summarize_class(node, include_docstrings))
            elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                functions.append(self._summarize_function(node, include_docstrings))
            elif isinstance(node, ast.Assign):
                # Top-level assignments = globals
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        globals_found.append(target.id)
                    elif isinstance(target, ast.Tuple):
                        for elt in target.elts:
                            if isinstance(elt, ast.Name):
                                globals_found.append(elt.id)

        # Build output
        out = []
        out.append(f"# === {p.name} ===")
        out.append(f"# LOC: {total_loc} total ({code_lines} code, {comment_lines} comments, {blank_lines} blank)")
        out.append("")

        if imports:
            out.append("## Imports")
            for imp in imports:
                out.append(f"  {imp}")
            out.append("")

        if globals_found:
            out.append(f"## Globals: {', '.join(globals_found)}")
            out.append("")

        if classes:
            out.append("## Classes")
            for cls_info in classes:
                bases = f"({cls_info['bases']})" if cls_info["bases"] else ""
                out.append(f"  class {cls_info['name']}{bases}:  # L{cls_info['line']}")
                if cls_info.get("docstring") and include_docstrings:
                    doc = cls_info["docstring"]
                    if len(doc) > 120:
                        doc = doc[:117] + "..."
                    out.append(f"    \"\"\"{doc}\"\"\"")
                for method in cls_info["methods"]:
                    prefix = "async " if method["is_async"] else ""
                    decorators = ""
                    if method["decorators"]:
                        decorators = " @" + ",".join(method["decorators"])
                    ret = method.get("ret_type", "")
                    out.append(f"    {prefix}def {method['name']}({method['signature']}){ret}  # L{method['line']}{decorators}")
                    if method.get("docstring") and include_docstrings:
                        doc = method["docstring"]
                        if len(doc) > 100:
                            doc = doc[:97] + "..."
                        out.append(f"      \"\"\"{doc}\"\"\"")
                out.append("")

        if functions:
            out.append("## Functions")
            for func in functions:
                prefix = "async " if func["is_async"] else ""
                decorators = ""
                if func["decorators"]:
                    decorators = " @" + ",".join(func["decorators"])
                ret = func.get("ret_type", "")
                out.append(f"  {prefix}def {func['name']}({func['signature']}){ret}  # L{func['line']}{decorators}")
                if func.get("docstring") and include_docstrings:
                    doc = func["docstring"]
                    if len(doc) > 100:
                        doc = doc[:97] + "..."
                    out.append(f"    \"\"\"{doc}\"\"\"")
            out.append("")

        if todos:
            out.append("## TODO/FIXME")
            for t in todos:
                out.append(t)
            out.append("")

        result_text = "\n".join(out)
        return {"success": True, "result": result_text, "error": None}

    @staticmethod
    def _format_signature(node: ast.FunctionDef) -> tuple[str, str]:
        """
        Format function arguments into a compact signature.

        Returns:
            (signature, return_annotation) -- e.g. ("self, x: int", " -> bool")
        """
        parts = []
        args = node.args

        # Count defaults to align them with args
        num_defaults = len(args.defaults)
        num_args = len(args.args)

        for i, arg in enumerate(args.args):
            name = arg.arg
            annotation = ""
            if arg.annotation:
                try:
                    annotation = f": {ast.unparse(arg.annotation)}"
                except Exception:
                    annotation = ": ..."

            # Check if this arg has a default
            default_idx = i - (num_args - num_defaults)
            default = ""
            if default_idx >= 0 and default_idx < len(args.defaults):
                try:
                    default = f"={ast.unparse(args.defaults[default_idx])}"
                except Exception:
                    default = "=..."

            parts.append(f"{name}{annotation}{default}")

        # *args
        if args.vararg:
            ann = ""
            if args.vararg.annotation:
                try:
                    ann = f": {ast.unparse(args.vararg.annotation)}"
                except Exception:
                    pass
            parts.append(f"*{args.vararg.arg}{ann}")
        elif args.kwonlyargs and not args.vararg:
            # Bare * separator for keyword-only args without *args
            parts.append("*")

        # keyword-only args (def foo(*, key=True, verbose=False))
        num_kw_defaults = len(args.kw_defaults)
        for i, arg in enumerate(args.kwonlyargs):
            name = arg.arg
            annotation = ""
            if arg.annotation:
                try:
                    annotation = f": {ast.unparse(arg.annotation)}"
                except Exception:
                    annotation = ": ..."
            default = ""
            if i < num_kw_defaults and args.kw_defaults[i] is not None:
                try:
                    default = f"={ast.unparse(args.kw_defaults[i])}"
                except Exception:
                    default = "=..."
            parts.append(f"{name}{annotation}{default}")

        # **kwargs
        if args.kwarg:
            ann = ""
            if args.kwarg.annotation:
                try:
                    ann = f": {ast.unparse(args.kwarg.annotation)}"
                except Exception:
                    pass
            parts.append(f"**{args.kwarg.arg}{ann}")

        sig = ", ".join(parts)

        # Return annotation — kept separate so callers can format as
        # "def name(sig) -> ret" with the closing paren before the arrow
        ret = ""
        if node.returns:
            try:
                ret = f" -> {ast.unparse(node.returns)}"
            except Exception:
                ret = " -> ..."

        return sig, ret

    @classmethod
    def _summarize_function(cls, node, include_docstrings: bool) -> dict:
        """Summarize a function/async function node."""
        decorators = []
        for dec in node.decorator_list:
            try:
                decorators.append(ast.unparse(dec))
            except Exception:
                decorators.append("?")

        sig, ret_type = cls._format_signature(node)

        result = {
            "name": node.name,
            "line": node.lineno,
            "signature": sig,
            "ret_type": ret_type,
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "decorators": decorators,
        }

        if include_docstrings:
            result["docstring"] = ast.get_docstring(node) or ""

        return result

    @classmethod
    def _summarize_class(cls, node: ast.ClassDef, include_docstrings: bool) -> dict:
        """Summarize a class node with its methods."""
        bases = []
        for base in node.bases:
            try:
                bases.append(ast.unparse(base))
            except Exception:
                bases.append("?")

        methods = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(cls._summarize_function(item, include_docstrings))

        result = {
            "name": node.name,
            "line": node.lineno,
            "bases": ", ".join(bases),
            "methods": methods,
        }

        if include_docstrings:
            result["docstring"] = ast.get_docstring(node) or ""

        return result

    # =========================================================================
    # Helpers
    # =========================================================================
    @staticmethod
    def _is_binary(path: Path) -> bool:
        """
        Detect if a file is binary using a 3-tier strategy:
        1. Extension check (fast, covers 95% of cases)
        2. MIME type check (covers less obvious extensions)
        3. Content sniffing (null byte detection in first 8KB)

        Returns True if file appears to be binary.
        """
        # Tier 1: Extension-based (fast path)
        if path.suffix.lower() in BINARY_EXTENSIONS:
            return True

        # Tier 2: MIME type heuristic
        mime, _ = mimetypes.guess_type(str(path))
        if mime and not mime.startswith("text/") and mime not in (
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-sh",
            "application/x-python",
            "application/toml",
            "application/yaml",
            "application/x-yaml",
        ):
            return True

        # Tier 3: Content sniffing — check for null bytes
        try:
            chunk = path.read_bytes()[:8192]
            if b'\x00' in chunk:
                return True
        except (OSError, PermissionError):
            pass  # If we can't read, let the caller handle it

        return False
