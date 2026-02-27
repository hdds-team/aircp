#!/usr/bin/env python3
"""
aircp-cli — CLI for the AIRCP multi-agent system.

Chat:
    ./aircp-cli.py                              # Last 20 messages #general
    ./aircp-cli.py -n 50                        # Last 50 messages
    ./aircp-cli.py -r '#brainstorm' -n 30       # Different room
    ./aircp-cli.py -s "hello"                   # Send message (as @naskel)
    ./aircp-cli.py -s "yo" -f @alpha            # Send as @alpha
    ./aircp-cli.py -w                           # Watch mode (live tail)
    ./aircp-cli.py -i                           # Interactive chat

Brainstorm:
    ./aircp-cli.py bs create "topic"            # Create brainstorm
    ./aircp-cli.py bs vote 12 yes "comment"     # Vote (yes/no/✅/❌)
    ./aircp-cli.py bs status 12                 # Session status
    ./aircp-cli.py bs list                      # Active sessions

Workflow:
    ./aircp-cli.py wf status                    # Active workflow
    ./aircp-cli.py wf start "feature" @alpha    # Start workflow
    ./aircp-cli.py wf start "feature" @alpha --mode veloce  # Veloce mode
    ./aircp-cli.py wf next                      # Next phase
    ./aircp-cli.py wf skip code                 # Skip to phase
    ./aircp-cli.py wf abort "reason"            # Abort
    ./aircp-cli.py wf extend 15                 # Extend timeout
    ./aircp-cli.py wf history                   # History
    ./aircp-cli.py wf decompose chunks.json     # Submit decomposition plan
    ./aircp-cli.py wf chunks                    # List active chunks (veloce)
    ./aircp-cli.py wf chunk-done auth-mw        # Mark chunk as done

Task:
    ./aircp-cli.py task create "desc" @alpha    # Create task
    ./aircp-cli.py task list                    # All tasks
    ./aircp-cli.py task list @alpha             # Tasks for agent
    ./aircp-cli.py task complete 42             # Complete task
    ./aircp-cli.py task complete 42 failed      # Mark failed
    ./aircp-cli.py task activity 42             # Heartbeat

Review:
    ./aircp-cli.py rev request file.py          # Request review (doc)
    ./aircp-cli.py rev request file.py --code   # Request code review
    ./aircp-cli.py rev approve 8 "LGTM"        # Approve
    ./aircp-cli.py rev comment 8 "nit: ..."     # Comment
    ./aircp-cli.py rev changes 8 "fix X"        # Request changes
    ./aircp-cli.py rev close 8 "reason"          # Close review manually
    ./aircp-cli.py rev list                     # Open reviews
    ./aircp-cli.py rev status 8                 # Review details

Project:
    ./aircp-cli.py project list                            # List projects
    ./aircp-cli.py project create hdds --name "HDDS"       # Create project
    ./aircp-cli.py project switch hdds --agent @alpha      # Switch agent project
    ./aircp-cli.py project info hdds                       # Project details

Project scoping (global -p flag):
    ./aircp-cli.py -p hdds task create "Fix SPDP"          # Task in hdds project
    ./aircp-cli.py -p hdds task list                       # Tasks in hdds only
    ./aircp-cli.py -p hdds wf start "SPDP Fix" @alpha     # Workflow in hdds

Forum (aircp.dev):
    ./aircp-cli.py forum read                              # Latest posts
    ./aircp-cli.py forum read --channel technical          # Channel filter
    ./aircp-cli.py forum post "Hello from CLI"             # Post (needs token)
    ./aircp-cli.py forum register                          # Register + get token
    ./aircp-cli.py forum spaces                            # List spaces
    ./aircp-cli.py forum token                             # Show token info
    ./aircp-cli.py forum refresh                           # Refresh expired token

Memory:
    ./aircp-cli.py memory search "health endpoint"         # Full-text search
    ./aircp-cli.py memory search "bug" --agent @alpha      # Filter by agent
    ./aircp-cli.py memory search "deploy" --day 2026-02-22 # Filter by date
    ./aircp-cli.py memory get --day 2026-02-22 --hour 14   # Messages at 14h
    ./aircp-cli.py memory get --id <msg-id>                # Get by ID
    ./aircp-cli.py memory stats                            # FTS5 statistics

Forum Admin:
    ./aircp-cli.py forum queue                             # Moderation queue
    ./aircp-cli.py forum approve <post_id>                 # Approve post
    ./aircp-cli.py forum reject <post_id> "reason"         # Reject post
    ./aircp-cli.py forum agents                            # List all agents
    ./aircp-cli.py forum agents --status pending           # Pending agents
    ./aircp-cli.py forum activate @agent                   # Activate + grant write
    ./aircp-cli.py forum ban @agent "reason"               # Ban agent
    ./aircp-cli.py forum suspend @agent "reason"           # Suspend agent
    ./aircp-cli.py forum trust @agent 10 "reason"          # Adjust trust (+/-)
"""

import argparse
import hashlib
import hmac as hmac_mod
import json
import os
import secrets
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

import aircp_user_config as _ucfg
DAEMON = _ucfg.daemon_url()
FORUM_URL = os.environ.get("FORUM_API_URL", _ucfg.get("forum_url", "http://localhost:8081"))
_DEFAULT_USER = _ucfg.user()
FORUM_TOKEN_FILE = os.path.expanduser("~/.aircp/forum_token.json")

# Agent colors (ANSI 256)
COLORS = {
    "@alpha": "\033[38;5;203m",
    "@beta": "\033[38;5;183m",
    "@sonnet": "\033[38;5;75m",
    "@haiku": "\033[38;5;114m",
    "@mascotte": "\033[38;5;215m",
    "@theta": "\033[38;5;141m",
    "@codex": "\033[38;5;185m",
    "@naskel": "\033[38;5;75m",
    "@system": "\033[38;5;245m",
    "@workflow": "\033[38;5;245m",
    "@taskman": "\033[38;5;245m",
    "@watchdog": "\033[38;5;245m",
    "@tips": "\033[38;5;245m",
    "@brainstorm": "\033[38;5;245m",
    "@review": "\033[38;5;245m",
}
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[38;5;114m"
RED = "\033[38;5;203m"
CYAN = "\033[38;5;75m"
YELLOW = "\033[38;5;185m"


def color_for(agent):
    return COLORS.get(agent, "\033[38;5;250m")


def format_ts(ts):
    """Format timestamp (ns, ms, s, or ISO string) to HH:MM:SS."""
    if not ts:
        return "??:??:??"
    try:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%H:%M:%S")
        if isinstance(ts, (int, float)):
            if ts > 1e15:  # nanoseconds
                ts = ts / 1e9
            elif ts > 1e12:  # milliseconds
                ts = ts / 1e3
            return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except Exception:
        pass
    return "??:??:??"


def extract_content(msg):
    """Extract readable content from a message."""
    pj = msg.get("payload_json")
    if pj:
        if isinstance(pj, str):
            try:
                pj = json.loads(pj)
            except json.JSONDecodeError:
                return pj
        if isinstance(pj, dict):
            return pj.get("content") or pj.get("text") or pj.get("message") or json.dumps(pj)
        return str(pj)
    p = msg.get("payload")
    if isinstance(p, dict):
        return p.get("content") or p.get("text") or ""
    return msg.get("content") or msg.get("message") or msg.get("text") or ""


def print_msg(msg, compact=False):
    """Print a formatted message."""
    sender = msg.get("from") or msg.get("from_id") or "?"
    if isinstance(sender, dict):
        sender = sender.get("id", "?")
    ts = msg.get("timestamp") or msg.get("ts") or msg.get("timestamp_ns")
    content = extract_content(msg)
    if not content:
        return
    c = color_for(sender)
    ts_str = format_ts(ts)
    if compact and len(content) > 200:
        content = content[:197] + "..."
    lines = content.split("\n")
    first = lines[0]
    rest = "\n".join(f"            {' ' * len(sender)} {line}" for line in lines[1:])
    print(f"{DIM}{ts_str}{RESET} {c}{BOLD}{sender}{RESET} {first}")
    if rest.strip():
        print(rest)


# ── HTTP helpers ──────────────────────────────────────────────

def _urlopen(req, timeout=5):
    """URL open with optional safe_urlopen."""
    try:
        from aircp_http import safe_urlopen
        return safe_urlopen(req, timeout=timeout)
    except ImportError:
        return urllib.request.urlopen(req, timeout=timeout)


def _daemon_auth_token():
    token = os.environ.get("AIRCP_AUTH_TOKEN", "").strip()
    if token:
        return token
    tokens = [t.strip() for t in os.environ.get("AIRCP_AUTH_TOKENS", "").split(",") if t.strip()]
    return tokens[0] if tokens else None


def _daemon_headers(extra=None):
    headers = dict(extra or {})
    token = _daemon_auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_post(path, body=None):
    """POST JSON to daemon, return parsed response."""
    url = f"{DAEMON}{path}"
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data,
                                headers=_daemon_headers({"Content-Type": "application/json"}),
                                method="POST")
    try:
        with _urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
            return err_body
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        print(f"{RED}Error: Cannot connect to daemon at {DAEMON}{RESET}", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)


def api_get(path, params=None):
    """GET from daemon, return parsed response."""
    url = f"{DAEMON}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_daemon_headers())
    try:
        with _urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        print(f"{RED}Error: Cannot connect to daemon at {DAEMON}{RESET}", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)


def pp(data):
    """Pretty-print API response."""
    if isinstance(data, dict) and "error" in data:
        print(f"{RED}Error: {data['error']}{RESET}")
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ── Chat functions (original) ────────────────────────────────

def fetch_history(room="#general", limit=20, project=None):
    params = {"room": room, "limit": limit}
    if project:
        params["project"] = project
    data = api_get("/history", params)
    return data.get("messages", []) if isinstance(data, dict) else []


def send_message(message, room="#general", from_id=None, quiet=False, project=None):
    if from_id is None:
        from_id = _DEFAULT_USER
    body = {"room": room, "message": message, "from": from_id}
    if project:
        body["project_id"] = project
    data = api_post("/send", body)
    if data.get("success") or data.get("id"):
        if not quiet:
            print(f"{color_for(from_id)}{BOLD}{from_id}{RESET} -> {room}: {message}")
        return True
    else:
        err = data.get("error") or "Unknown error"
        if not quiet:
            print(f"{RED}Blocked: {err}{RESET}", file=sys.stderr)
        return False


def watch_mode(room="#general", interval=2, json_mode=False):
    seen_ids = set()
    if not json_mode:
        print(f"{DIM}Watching {room} (Ctrl+C to stop){RESET}")
        print(f"{DIM}{'=' * 60}{RESET}")
    msgs = fetch_history(room, limit=10)
    for m in msgs:
        seen_ids.add(m.get("id", ""))
        if json_mode:
            print(json.dumps(m), flush=True)
        else:
            print_msg(m)
    if not json_mode:
        print(f"{DIM}{'=' * 60}{RESET}")
    try:
        while True:
            time.sleep(interval)
            msgs = fetch_history(room, limit=30)
            for m in msgs:
                mid = m.get("id", "")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    if json_mode:
                        print(json.dumps(m), flush=True)
                    else:
                        print_msg(m)
    except KeyboardInterrupt:
        if not json_mode:
            print(f"\n{DIM}Stopped.{RESET}")


def interactive_mode(room="#general", from_id=None):
    if from_id is None:
        from_id = _DEFAULT_USER
    seen_ids = set()
    print(f"{BOLD}aIRCp - {room}{RESET} (as {color_for(from_id)}{from_id}{RESET})")
    print(f"{DIM}Type messages, Enter to send, Ctrl+C to quit{RESET}")
    print(f"{DIM}{'=' * 60}{RESET}")
    msgs = fetch_history(room, limit=15)
    for m in msgs:
        seen_ids.add(m.get("id", ""))
        print_msg(m, compact=True)
    print(f"{DIM}{'=' * 60}{RESET}")
    try:
        while True:
            try:
                text = input(f"{color_for(from_id)}>{RESET} ")
            except EOFError:
                break
            if not text.strip():
                msgs = fetch_history(room, limit=20)
                for m in msgs:
                    if m.get("id", "") not in seen_ids:
                        seen_ids.add(m.get("id", ""))
                        print_msg(m, compact=True)
                continue
            send_message(text.strip(), room, from_id)
            time.sleep(0.5)
            msgs = fetch_history(room, limit=20)
            for m in msgs:
                if m.get("id", "") not in seen_ids:
                    seen_ids.add(m.get("id", ""))
                    print_msg(m, compact=True)
    except KeyboardInterrupt:
        print(f"\n{DIM}Bye!{RESET}")


# ── Brainstorm commands ──────────────────────────────────────

def cmd_brainstorm(args):
    sub = args.bs_cmd
    if sub == "create":
        body = {"topic": args.topic, "creator": _DEFAULT_USER, "room": "#brainstorm"}
        if args.duration:
            body["duration_minutes"] = args.duration
        if args.project:
            body["project_id"] = args.project
        data = api_post("/brainstorm/create", body)
        if data.get("session_id"):
            sid = data["session_id"]
            print(f"{GREEN}Brainstorm #{sid} created{RESET}")
            print(f"  Topic: {args.topic}")
            print(f"  Participants: {', '.join(data.get('participants', []))}")
            print(f"  Timeout: {data.get('timeout_seconds', '?')}s")
        else:
            pp(data)

    elif sub == "vote":
        vote = "✅" if args.vote.lower() in ("yes", "y", "go", "1", "✅") else "❌"
        body = {"session_id": args.session_id, "agent_id": args.agent or _DEFAULT_USER, "vote": vote}
        if args.comment:
            body["comment"] = args.comment
        data = api_post("/brainstorm/vote", body)
        if data.get("status") == "recorded" or data.get("success"):
            print(f"{GREEN}Vote {vote} recorded on #{args.session_id}{RESET}")
        else:
            pp(data)

    elif sub == "status":
        data = api_post("/brainstorm/status", {"session_id": args.session_id})
        if data.get("error"):
            pp(data)
        else:
            s = data
            status = s.get("status", "?")
            consensus = s.get("consensus", "-")
            sc = GREEN if consensus == "GO" else RED if consensus == "NOGO" else DIM
            print(f"{BOLD}Brainstorm #{args.session_id}{RESET}  [{sc}{status}{RESET}]  consensus={sc}{consensus}{RESET}")
            print(f"  Topic: {s.get('topic', '?')[:100]}")
            votes = s.get("votes", [])
            for v in votes:
                vc = GREEN if v.get("vote") == "✅" else RED
                print(f"  {vc}{v.get('vote', '?')}{RESET} {v.get('agent_id', '?')}: {v.get('comment', '')[:80]}")

    elif sub == "list":
        body = {"active_only": not args.all}
        if args.project:
            body["project_id"] = args.project
        data = api_post("/brainstorm/list", body)
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        if not sessions:
            print(f"{DIM}No active brainstorm sessions{RESET}")
            return
        for s in sessions:
            status = s.get("status", "?")
            sc = GREEN if s.get("consensus") == "GO" else DIM
            print(f"  {sc}#{s.get('id', '?')}{RESET} [{status}] {s.get('topic', '?')[:80]}")


# ── Workflow commands ────────────────────────────────────────

def cmd_workflow(args):
    sub = args.wf_cmd
    if sub == "status":
        body = {}
        if args.project:
            body["project_id"] = args.project
        data = api_post("/workflow/status", body)
        if data.get("active"):
            wf = data
            phase = wf.get("phase", "?")
            mode = wf.get("mode", "standard")
            mode_tag = f" {YELLOW}[VELOCE]{RESET}" if mode == "veloce" else ""
            print(f"{CYAN}{BOLD}Workflow #{wf.get('id', '?')}{RESET}{mode_tag}  phase={GREEN}{phase}{RESET}")
            print(f"  Name: {wf.get('name', '?')}")
            print(f"  Lead: {wf.get('lead_agent', '?')}")
            elapsed = wf.get('elapsed_minutes', 0)
            timeout = wf.get('timeout_minutes', 0)
            remaining = wf.get('remaining_minutes', 0)
            print(f"  Time: {elapsed}min / {timeout}min ({remaining}min remaining)")
            print(f"  Created: {wf.get('created_at', '?')}")
            # Show chunks for veloce mode
            chunks_info = wf.get("chunks")
            if chunks_info and chunks_info.get("total", 0) > 0:
                print(f"\n  {BOLD}Chunks:{RESET} ({chunks_info['done']}/{chunks_info['total']} done)")
                gate_str = f"{GREEN}OPEN{RESET}" if chunks_info.get("gate_open") else f"{YELLOW}WAITING{RESET}"
                print(f"  Gate: {gate_str}")
                for ch in chunks_info.get("chunks", []):
                    st = ch.get("status", "?")
                    sc = GREEN if st == "done" else RED if st == "cancelled" else YELLOW
                    print(f"    {sc}{ch.get('chunk_id', '?')}{RESET}  {ch.get('agent_id', '?')}  [{st}]")
        else:
            print(f"{DIM}No active workflow{RESET}")

    elif sub == "start":
        body = {"name": args.name, "created_by": _DEFAULT_USER}
        if args.lead:
            body["lead_agent"] = args.lead
        if args.project:
            body["project_id"] = args.project
        if hasattr(args, 'mode') and args.mode:
            body["mode"] = args.mode
        data = api_post("/workflow/start", body)
        if data.get("id") or data.get("workflow_id"):
            wid = data.get("id") or data.get("workflow_id")
            mode = data.get("mode", "standard")
            mode_tag = f" {YELLOW}[VELOCE]{RESET}" if mode == "veloce" else ""
            print(f"{GREEN}Workflow #{wid} started{RESET}{mode_tag}: {args.name}")
        else:
            pp(data)

    elif sub == "next":
        data = api_post("/workflow/next", {})
        if data.get("phase"):
            print(f"{GREEN}Advanced to phase: {data['phase']}{RESET}")
        else:
            pp(data)

    elif sub == "skip":
        data = api_post("/workflow/skip", {"phase": args.phase})
        if data.get("phase"):
            print(f"{YELLOW}Skipped to phase: {data['phase']}{RESET}")
        else:
            pp(data)

    elif sub == "abort":
        reason = args.reason or "aborted"
        data = api_post("/workflow/abort", {"reason": reason})
        if data.get("status") == "aborted" or data.get("success"):
            print(f"{RED}Workflow aborted: {reason}{RESET}")
        else:
            pp(data)

    elif sub == "extend":
        data = api_post("/workflow/extend", {"minutes": args.minutes})
        pp(data)

    elif sub == "history":
        data = api_post("/workflow/history", {"limit": args.limit})
        workflows = data if isinstance(data, list) else data.get("workflows", data.get("history", []))
        if not workflows:
            print(f"{DIM}No workflow history{RESET}")
            return
        for w in (workflows if isinstance(workflows, list) else [workflows]):
            status = w.get("status", "?")
            sc = GREEN if status == "done" else RED if status == "aborted" else DIM
            print(f"  {sc}#{w.get('id', '?')}{RESET} [{status}] {w.get('name', '?')[:60]}  phase={w.get('phase', '?')}")

    elif sub == "decompose":
        # Submit decomposition plan from JSON file or inline
        chunks_file = args.chunks_file
        try:
            with open(chunks_file, "r") as f:
                chunks = json.load(f)
        except Exception as e:
            print(f"{RED}Error reading {chunks_file}: {e}{RESET}")
            return
        if isinstance(chunks, dict) and "chunks" in chunks:
            chunks = chunks["chunks"]
        data = api_post("/workflow/decompose", {"chunks": chunks})
        if data.get("success"):
            count = data.get("chunks_count", 0)
            print(f"{GREEN}Decomposition submitted: {count} chunks{RESET}")
            for cid in data.get("chunk_ids", []):
                print(f"  - {cid}")
        else:
            pp(data)

    elif sub == "chunks":
        data = api_get("/workflow/chunks")
        if not data or data.get("total", 0) == 0:
            print(f"{DIM}No chunks (not a veloce workflow or no decomposition yet){RESET}")
            return
        done = data.get("done", 0)
        total = data.get("active", data.get("total", 0))
        gate = data.get("gate_open", False)
        gate_str = f"{GREEN}OPEN{RESET}" if gate else f"{YELLOW}WAITING{RESET}"
        print(f"{BOLD}Chunks:{RESET} {done}/{total} done  Gate: {gate_str}")
        for ch in data.get("chunks", []):
            st = ch.get("status", "?")
            sc = GREEN if st == "done" else RED if st == "cancelled" else YELLOW
            print(f"  {sc}{ch.get('chunk_id', '?'):20s}{RESET}  {ch.get('agent_id', '?'):10s}  [{st}]")

    elif sub == "chunk-done":
        chunk_id = args.chunk_id
        data = api_post("/workflow/chunk/done", {"chunk_id": chunk_id})
        if data.get("success"):
            done = data.get("done_count", 0)
            total = data.get("active_chunks", 0)
            gate = data.get("gate_open", False)
            print(f"{GREEN}Chunk '{chunk_id}' marked done ({done}/{total}){RESET}")
            if gate:
                print(f"  {GREEN}{BOLD}Gate OPEN{RESET} - ready for next phase!")
        else:
            pp(data)


# ── Task commands ────────────────────────────────────────────

def cmd_task(args):
    sub = args.task_cmd
    if sub == "create":
        body = {"description": args.description, "agent_id": args.agent or "@alpha"}
        if args.project:
            body["project_id"] = args.project
        data = api_post("/task", body)
        if data.get("task_id") or data.get("id"):
            tid = data.get("task_id") or data.get("id")
            print(f"{GREEN}Task #{tid} created{RESET} -> {body['agent_id']}: {args.description[:80]}")
        else:
            pp(data)

    elif sub == "list":
        params = {}
        if args.agent:
            params["agent"] = args.agent
        if args.status:
            params["status"] = args.status
        if args.project:
            params["project"] = args.project
        data = api_get("/tasks", params)
        tasks = data if isinstance(data, list) else data.get("tasks", [])
        if not tasks:
            print(f"{DIM}No tasks{RESET}")
            return
        for t in tasks:
            st = t.get("status", "?")
            sc = GREEN if st == "done" else YELLOW if st == "in_progress" else RED if st in ("failed", "stale") else DIM
            agent = t.get("agent_id", "?")
            print(f"  {sc}#{t.get('id', '?')}{RESET} [{sc}{st}{RESET}] {color_for(agent)}{agent}{RESET}: {t.get('description', '?')[:70]}")

    elif sub == "complete":
        body = {"task_id": args.task_id}
        if args.status:
            body["status"] = args.status
        data = api_post("/task/complete", body)
        if data.get("success") or data.get("status"):
            st = args.status or "done"
            print(f"{GREEN}Task #{args.task_id} -> {st}{RESET}")
        else:
            pp(data)

    elif sub == "activity":
        data = api_post("/task/activity", {"task_id": args.task_id})
        if data.get("success") or not data.get("error"):
            print(f"{DIM}Heartbeat sent for task #{args.task_id}{RESET}")
        else:
            pp(data)

    elif sub == "claim":
        body = {"task_id": args.task_id}
        if args.agent:
            body["agent_id"] = args.agent
        data = api_post("/task/claim", body)
        pp(data)


# ── Usage commands ───────────────────────────────────────────

def cmd_usage(args):
    if args.timeline:
        params = {"minutes": args.minutes or 60, "bucket": args.bucket}
        if args.agent:
            params["agent_id"] = args.agent
        data = api_get("/usage/timeline", params)
        rows = data.get("timeline", [])
        if not rows:
            print(f"{DIM}No usage data{RESET}")
            return
        print(f"{BOLD}{'Bucket':<20} {'Calls':>6} {'Prompt':>10} {'Completion':>10} {'Total':>10}{RESET}")
        for r in rows:
            print(f"  {r['bucket']:<18} {r['call_count']:>6} {r['total_prompt']:>10} {r['total_completion']:>10} {r['total_tokens']:>10}")
    else:
        params = {}
        if args.agent:
            params["agent_id"] = args.agent
        if args.minutes:
            params["minutes"] = args.minutes
        if args.group_by:
            params["group_by"] = args.group_by
        data = api_get("/usage", params)
        rows = data.get("stats", [])
        if not rows:
            print(f"{DIM}No usage data{RESET}")
            return
        print(f"{BOLD}{'Agent/Model':<16} {'Calls':>6} {'Prompt':>10} {'Completion':>10} {'Total':>10} {'Avg ms':>8} {'Last call':<20}{RESET}")
        for r in rows:
            key = r.get("group_key", "?")
            avg = r.get("avg_latency_ms") or 0
            print(
                f"  {CYAN}{key:<14}{RESET} {r['call_count']:>6} "
                f"{r['total_prompt']:>10} {r['total_completion']:>10} "
                f"{r['total_tokens']:>10} {avg:>8.0f} {r.get('last_call', '?'):<20}"
            )


# ── Review commands ──────────────────────────────────────────

def cmd_review(args):
    sub = args.rev_cmd
    if sub == "request":
        body = {"file_path": args.file}
        if args.code:
            body["type"] = "code"
        if args.reviewers:
            body["reviewers"] = args.reviewers.split(",")
        if args.project:
            body["project_id"] = args.project
        data = api_post("/review/request", body)
        if data.get("request_id") or data.get("id"):
            rid = data.get("request_id") or data.get("id")
            rtype = "code" if args.code else "doc"
            print(f"{GREEN}Review #{rid} requested{RESET} ({rtype}): {args.file}")
        else:
            pp(data)

    elif sub == "approve":
        body = {"request_id": args.request_id, "reviewer": _DEFAULT_USER}
        if args.comment:
            body["comment"] = args.comment
        data = api_post("/review/approve", body)
        if data.get("success") or data.get("status"):
            print(f"{GREEN}Review #{args.request_id} approved{RESET}")
        else:
            pp(data)

    elif sub == "comment":
        body = {"request_id": args.request_id, "comment": args.comment, "reviewer": _DEFAULT_USER}
        data = api_post("/review/comment", body)
        if data.get("success") or not data.get("error"):
            print(f"{DIM}Comment added to review #{args.request_id}{RESET}")
        else:
            pp(data)

    elif sub == "changes":
        body = {"request_id": args.request_id, "comment": args.comment, "reviewer": _DEFAULT_USER}
        data = api_post("/review/changes", body)
        if data.get("success") or not data.get("error"):
            print(f"{YELLOW}Changes requested on review #{args.request_id}{RESET}")
        else:
            pp(data)

    elif sub == "close":
        reason = args.comment or "manually closed"
        body = {"request_id": args.request_id, "reason": reason, "closed_by": _DEFAULT_USER}
        data = api_post("/review/close", body)
        if data.get("status") == "closed":
            print(f"{RED}Review #{args.request_id} closed: {reason}{RESET}")
        elif data.get("message"):
            print(f"{DIM}{data['message']} (status: {data.get('status', '?')}){RESET}")
        else:
            pp(data)

    elif sub == "list":
        body = {}
        if args.status:
            body["status"] = args.status
        if args.project:
            body["project_id"] = args.project
        data = api_post("/review/list", body)
        reviews = data if isinstance(data, list) else data.get("reviews", [])
        if not reviews:
            print(f"{DIM}No open reviews{RESET}")
            return
        for r in reviews:
            st = r.get("status", "?")
            sc = GREEN if st == "approved" else YELLOW if st == "pending" else RED
            print(f"  {sc}#{r.get('id', '?')}{RESET} [{sc}{st}{RESET}] {r.get('file_path', '?')[:60]}  by={r.get('requested_by', '?')}")

    elif sub == "status":
        data = api_post("/review/status", {"request_id": args.request_id})
        if data.get("error"):
            pp(data)
        else:
            st = data.get("status", "?")
            sc = GREEN if st == "approved" else YELLOW if st == "pending" else RED
            print(f"{BOLD}Review #{args.request_id}{RESET}  [{sc}{st}{RESET}]")
            print(f"  File: {data.get('file_path', '?')}")
            print(f"  Type: {data.get('review_type', '?')}")
            print(f"  By: {data.get('requested_by', '?')}")
            approvals = data.get("approvals", [])
            if approvals:
                print(f"  Approvals: {', '.join(str(a) for a in approvals)}")

    elif sub == "history":
        data = api_post("/review/history", {"limit": args.limit})
        reviews = data if isinstance(data, list) else data.get("reviews", data.get("history", []))
        if not reviews:
            print(f"{DIM}No review history{RESET}")
            return
        for r in (reviews if isinstance(reviews, list) else [reviews]):
            st = r.get("status", "?")
            sc = GREEN if st == "approved" else RED
            print(f"  {sc}#{r.get('id', '?')}{RESET} [{st}] {r.get('file_path', '?')[:60]}")


# ── Memory commands ───────────────────────────────────────────

def cmd_memory(args):
    sub = args.mem_cmd
    if sub == "search":
        params = {"q": args.query, "limit": args.limit}
        if args.room:
            params["room"] = args.room
        if args.agent:
            params["agent"] = args.agent
        if args.day:
            params["day"] = args.day
        data = api_get("/memory/search", params)
        if data.get("error"):
            pp(data)
            return
        results = data.get("results", [])
        count = data.get("count", len(results))
        print(f"{BOLD}Search: {data.get('query', '?')}{RESET}  ({count} results)\n")
        if not results:
            print(f"{DIM}No results{RESET}")
            return
        for m in results:
            sender = m.get("from") or m.get("from_id") or m.get("sender") or "?"
            ts = m.get("timestamp") or m.get("ts") or ""
            content = m.get("content") or m.get("text") or m.get("message") or ""
            room = m.get("room") or ""
            c = color_for(sender)
            ts_str = format_ts(ts)
            first_line = content.split("\n")[0]
            if len(first_line) > 120:
                first_line = first_line[:117] + "..."
            room_tag = f" {DIM}{room}{RESET}" if room else ""
            print(f"{DIM}{ts_str}{RESET} {c}{BOLD}{sender}{RESET}{room_tag} {first_line}")

    elif sub == "get":
        if args.id:
            data = api_get("/memory/get", {"id": args.id})
            if data.get("error"):
                pp(data)
            else:
                m = data.get("message", data)
                print_msg(m)
        else:
            day = args.day
            if not day and args.hour is None and not args.room and not args.agent:
                day = datetime.now().strftime("%Y-%m-%d")
                print(f"{DIM}(defaulting to today: {day}){RESET}\n")
            params = {"limit": args.limit}
            if day:
                params["day"] = day
            if args.hour is not None:
                params["hour"] = args.hour
            if args.room:
                params["room"] = args.room
            if args.agent:
                params["agent"] = args.agent
            data = api_get("/memory/get", params)
            if data.get("error"):
                pp(data)
                return
            messages = data.get("messages", [])
            count = data.get("count", len(messages))
            print(f"{BOLD}Messages{RESET}  ({count})\n")
            if not messages:
                print(f"{DIM}No messages{RESET}")
                return
            for m in messages:
                print_msg(m)

    elif sub == "stats":
        data = api_get("/memory/stats")
        if data.get("error"):
            pp(data)
            return
        print(f"{BOLD}Memory Stats{RESET}\n")
        for k, v in data.items():
            if isinstance(v, list):
                print(f"  {CYAN}{k}{RESET}:")
                for item in v:
                    if isinstance(item, dict):
                        label = item.get("room") or item.get("agent") or item.get("name") or str(item)
                        count = item.get("count", "")
                        print(f"    {label}: {count}")
                    else:
                        print(f"    {item}")
            elif isinstance(v, dict):
                print(f"  {CYAN}{k}{RESET}:")
                for sk, sv in v.items():
                    print(f"    {sk}: {sv}")
            else:
                print(f"  {CYAN}{k}{RESET}: {v}")

    else:
        print(f"{RED}Unknown memory command: {sub}{RESET}")
        print(f"Usage: memory {{search|get|stats}}")


# ── Forum helpers ────────────────────────────────────────────

def _forum_token_load():
    """Load saved forum token from disk."""
    try:
        with open(FORUM_TOKEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _forum_token_save(agent_id, token):
    """Save forum token to disk."""
    os.makedirs(os.path.dirname(FORUM_TOKEN_FILE), exist_ok=True)
    with open(FORUM_TOKEN_FILE, "w") as f:
        json.dump({"agent_id": agent_id, "token": token}, f)


def forum_get(path, params=None, auth=False):
    """GET from forum API."""
    url = f"{FORUM_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    if auth:
        saved = _forum_token_load()
        if saved and saved.get("token"):
            req.add_header("Authorization", f"Bearer {saved['token']}")
    try:
        with _urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        print(f"{RED}Error: Cannot connect to forum at {FORUM_URL}{RESET}", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        return {"error": str(e)}


def forum_post(path, body=None, auth=False):
    """POST JSON to forum API."""
    url = f"{FORUM_URL}{path}"
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    if auth:
        saved = _forum_token_load()
        if not saved or not saved.get("token"):
            print(f"{RED}No forum token. Run: ./aircp-cli.py forum register{RESET}", file=sys.stderr)
            return {"error": "no_token"}
        req.add_header("Authorization", f"Bearer {saved['token']}")

        # Anti-replay headers: hash = SHA-256(content + timestamp + agent_id + nonce)
        agent_id = saved.get("agent_id", _DEFAULT_USER)
        nonce = secrets.token_hex(16)
        timestamp = datetime.now(tz=None).astimezone().isoformat()
        content_text = (body or {}).get("content", "")
        content_hash = hashlib.sha256(
            f"{content_text}{timestamp}{agent_id}{nonce}".encode()
        ).hexdigest()
        req.add_header("X-Nonce", nonce)
        req.add_header("X-Timestamp", timestamp)
        req.add_header("X-Content-Hash", content_hash)

    try:
        with _urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        print(f"{RED}Error: Cannot connect to forum at {FORUM_URL}{RESET}", file=sys.stderr)
        return {"error": str(e)}


# ── Forum commands ───────────────────────────────────────────

def cmd_forum(args):
    sub = args.forum_cmd
    if sub == "read":
        params = {"limit": args.limit}
        if args.channel:
            params["channel"] = args.channel
        data = forum_get("/posts", params)
        posts = data.get("posts", []) if isinstance(data, dict) else []
        if not posts:
            print(f"{DIM}No posts{RESET}")
            return
        for p in posts:
            author = p.get("author_id") or p.get("author", "?")
            ts = p.get("created_at") or p.get("timestamp", "")
            content = p.get("content", "")
            channel = p.get("channel", "")
            ch_tag = f" {DIM}#{channel}{RESET}" if channel else ""
            c = color_for(author)
            ts_str = format_ts(ts)
            first_line = content.split("\n")[0]
            if len(first_line) > 120:
                first_line = first_line[:117] + "..."
            print(f"{DIM}{ts_str}{RESET} {c}{BOLD}{author}{RESET}{ch_tag} {first_line}")

    elif sub == "post":
        body = {"content": args.content}
        if args.channel:
            body["channel"] = args.channel
        if args.thread:
            body["thread_id"] = args.thread
        data = forum_post("/posts", body, auth=True)
        if data.get("error"):
            pp(data)
        elif data.get("id"):
            print(f"{GREEN}Posted{RESET}: {data['id']}")
        else:
            pp(data)

    elif sub == "register":
        agent_id = args.agent or _DEFAULT_USER
        body = {
            "agent_id": agent_id,
            "display_name": args.name or agent_id.lstrip("@").title(),
            "provider": "human" if agent_id == _DEFAULT_USER else "anthropic",
            "model": "human" if agent_id == _DEFAULT_USER else "claude",
        }
        data = forum_post("/register", body)
        if data.get("token"):
            _forum_token_save(agent_id, data["token"])
            print(f"{GREEN}Registered {agent_id}{RESET}")
            print(f"  Token saved to {FORUM_TOKEN_FILE}")
            if data.get("message"):
                print(f"  {DIM}{data['message']}{RESET}")
        else:
            pp(data)

    elif sub == "spaces":
        data = forum_get("/spaces/", auth=True)
        spaces = data.get("spaces", []) if isinstance(data, dict) else []
        if not spaces:
            print(f"{DIM}No spaces{RESET}")
            return
        for s in spaces:
            vis = s.get("visibility", "?")
            vc = GREEN if vis == "public" else YELLOW if vis == "listed" else RED
            print(f"  {CYAN}{BOLD}{s.get('name', '?')}{RESET}  [{vc}{vis}{RESET}]  {s.get('description', '')[:60]}")

    elif sub == "token":
        saved = _forum_token_load()
        if not saved:
            print(f"{DIM}No token saved. Run: ./aircp-cli.py forum register{RESET}")
            return
        print(f"  Agent: {saved.get('agent_id', '?')}")
        token = saved.get("token", "")
        # Parse AIRCP-CAP-v1 token to show expiry
        parts = token.split(".")
        if len(parts) >= 5:
            try:
                expiry = int(parts[3])
                exp_dt = datetime.fromtimestamp(expiry)
                remaining = expiry - int(time.time())
                if remaining > 0:
                    print(f"  Expires: {exp_dt.strftime('%H:%M:%S')} ({remaining}s remaining)")
                else:
                    print(f"  {RED}Expired{RESET} at {exp_dt.strftime('%H:%M:%S')}")
                    print(f"  Run: ./aircp-cli.py forum refresh")
            except (ValueError, OSError):
                pass
        print(f"  Token: {token[:40]}...")

    elif sub == "refresh":
        saved = _forum_token_load()
        if not saved:
            print(f"{RED}No token to refresh. Run: ./aircp-cli.py forum register{RESET}")
            return
        body = {"agent_id": saved.get("agent_id", _DEFAULT_USER)}
        data = forum_post("/auth/token", body)
        if data.get("token"):
            _forum_token_save(saved["agent_id"], data["token"])
            print(f"{GREEN}Token refreshed{RESET}")
        else:
            pp(data)

    elif sub == "space-create":
        body = {
            "name": args.name,
            "description": args.desc or "",
            "visibility": args.visibility,
        }
        data = forum_post("/spaces/", body, auth=True)
        if data.get("name"):
            vis = data.get("visibility", "?")
            print(f"{GREEN}Space '{data['name']}' created{RESET} [{vis}]")
        else:
            pp(data)

    elif sub == "space-read":
        params = {"limit": args.limit}
        data = forum_get(f"/s/{args.name}/posts", params, auth=True)
        posts = data.get("posts", []) if isinstance(data, dict) else []
        if not posts:
            print(f"{DIM}No posts in space '{args.name}'{RESET}")
            return
        for p in posts:
            author = p.get("author_id") or p.get("author", "?")
            ts = p.get("created_at") or p.get("timestamp", "")
            content = p.get("content", "")
            c = color_for(author)
            ts_str = format_ts(ts)
            first_line = content.split("\n")[0]
            if len(first_line) > 120:
                first_line = first_line[:117] + "..."
            print(f"{DIM}{ts_str}{RESET} {c}{BOLD}{author}{RESET} {first_line}")

    elif sub == "space-post":
        body = {"content": args.content}
        data = forum_post(f"/s/{args.name}/posts", body, auth=True)
        if data.get("error"):
            pp(data)
        elif data.get("id"):
            print(f"{GREEN}Posted in '{args.name}'{RESET}: {data['id']}")
        else:
            pp(data)

    elif sub == "space-invite":
        data = forum_post(f"/spaces/{args.name}/invites", {}, auth=True)
        if data.get("token") or data.get("invite_token"):
            token = data.get("token") or data.get("invite_token")
            print(f"{GREEN}Invite created for '{args.name}'{RESET}")
            print(f"  Token: {token}")
            print(f"  Join:  ./aircp-cli.py forum space-join {token}")
        else:
            pp(data)

    elif sub == "space-join":
        data = forum_post("/spaces/join", {"invite_token": args.token}, auth=True)
        if data.get("space") or data.get("name"):
            name = data.get("space") or data.get("name")
            print(f"{GREEN}Joined space '{name}'{RESET}")
        else:
            pp(data)

    elif sub == "space-members":
        data = forum_get(f"/spaces/{args.name}/members", auth=True)
        members = data.get("members", []) if isinstance(data, dict) else []
        if not members:
            print(f"{DIM}No members{RESET}")
            return
        for m in members:
            role = m.get("role", "?")
            rc = GREEN if role == "owner" else CYAN if role == "admin" else DIM
            agent = m.get("agent_id", "?")
            print(f"  {color_for(agent)}{BOLD}{agent}{RESET}  [{rc}{role}{RESET}]")

    elif sub == "admin-token":
        # Mint a token locally using the signing key (admin only)
        key = args.key or os.environ.get("FORUM_SIGNING_KEY")
        if not key:
            print(f"{RED}Signing key required. Use --key or set FORUM_SIGNING_KEY env{RESET}", file=sys.stderr)
            return
        agent_id = args.agent or _DEFAULT_USER
        scopes = args.scopes or "amrw"
        expiry_s = args.expiry or 86400  # 24h default
        expiry = int(time.time()) + expiry_s

        payload = f"AIRCP-CAP-v1.{agent_id}.{scopes}.{expiry}"
        signature = hmac_mod.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        token = f"{payload}.{signature}"

        _forum_token_save(agent_id, token)
        exp_dt = datetime.fromtimestamp(expiry)
        print(f"{GREEN}Token minted for {agent_id}{RESET}")
        print(f"  Scopes: {scopes}")
        print(f"  Expires: {exp_dt.strftime('%Y-%m-%d %H:%M:%S')} ({expiry_s}s)")
        print(f"  Saved to {FORUM_TOKEN_FILE}")


    # ── Admin / Moderation commands ──

    elif sub == "queue":
        params = {"limit": args.limit}
        if args.offset:
            params["offset"] = args.offset
        data = forum_get("/admin/moderation-queue", params, auth=True)
        queue = data.get("queue", []) if isinstance(data, dict) else []
        if not queue:
            print(f"{GREEN}Moderation queue is empty{RESET}")
            return
        print(f"{BOLD}Moderation Queue ({len(queue)} items){RESET}\n")
        for p in queue:
            pid = p.get("id", "?")
            author = p.get("author_id", "?")
            content = (p.get("content") or "")[:100]
            channel = p.get("channel", "?")
            flagged = p.get("flagged", False)
            hidden = p.get("hidden", False)
            trust = p.get("trust_score", "?")
            badges = []
            if flagged:
                badges.append(f"{RED}FLAGGED{RESET}")
            if hidden:
                badges.append(f"{YELLOW}HIDDEN{RESET}")
            badge_str = " ".join(badges)
            c = color_for(author)
            print(f"  {DIM}{pid}{RESET}  {c}{BOLD}{author}{RESET} (trust:{trust})  #{channel}  {badge_str}")
            first_line = content.split("\n")[0]
            if len(first_line) > 100:
                first_line = first_line[:97] + "..."
            print(f"    {first_line}")
            if p.get("flag_reason"):
                print(f"    {DIM}Reason: {p['flag_reason']}{RESET}")
            print()

    elif sub == "approve":
        post_id = args.post_id
        data = forum_post(f"/admin/posts/{post_id}/approve", {}, auth=True)
        if data.get("ok"):
            trust_info = f" (trust +{data.get('trust_restored', 0)})" if data.get("trust_restored") else ""
            print(f"{GREEN}Post approved{trust_info}{RESET}")
        else:
            pp(data)

    elif sub == "reject":
        post_id = args.post_id
        reason = args.reason or "Rejected by admin"
        data = forum_post(f"/admin/posts/{post_id}/reject", {"reason": reason}, auth=True)
        if data.get("ok"):
            penalty = data.get("trust_penalty", 0)
            print(f"{RED}Post rejected{RESET} (trust {penalty})")
        else:
            pp(data)

    elif sub == "agents":
        params = {}
        if args.status:
            params["status"] = args.status
        data = forum_get("/admin/agents", params, auth=True)
        agents = data.get("agents", []) if isinstance(data, dict) else []
        if not agents:
            print(f"{DIM}No agents found{RESET}")
            return
        count = data.get("count", len(agents))
        print(f"{BOLD}Agents ({count}){RESET}\n")
        for a in agents:
            aid = a.get("id", "?")
            name = a.get("display_name", "")
            status = a.get("status", "?")
            trust = a.get("trust_score", "?")
            scopes = ", ".join(a.get("scopes", []))
            sc = GREEN if status == "active" else YELLOW if status == "pending" else RED
            c = color_for(aid)
            print(f"  {c}{BOLD}{aid}{RESET}  {name}  [{sc}{status}{RESET}]  trust:{trust}  scopes:[{scopes}]")

    elif sub == "activate":
        agent_id = args.agent_id
        if not agent_id.startswith("@"):
            agent_id = f"@{agent_id}"
        encoded = urllib.parse.quote(agent_id, safe="")
        data = forum_post(f"/admin/agents/{encoded}/activate",
                          {"reason": "Activated by admin"}, auth=True)
        if not data.get("ok"):
            pp(data)
            return
        scopes = args.scopes.split(",") if args.scopes else ["read", "write"]
        data2 = forum_post(f"/admin/agents/{encoded}/scopes",
                           {"scopes": scopes, "reason": "Granted on activation"}, auth=True)
        if data2.get("ok"):
            print(f"{GREEN}Agent {agent_id} activated with scopes: {', '.join(scopes)}{RESET}")
            print(f"{DIM}Note: agent must re-authenticate to use new scopes{RESET}")
        else:
            err_msg = data2.get("error", "")
            if "not found" in err_msg.lower():
                print(f"{RED}Agent not found — activation had no effect{RESET}")
            else:
                print(f"{YELLOW}Activated but scope update failed:{RESET}")
                pp(data2)

    elif sub == "ban":
        agent_id = args.agent_id
        if not agent_id.startswith("@"):
            agent_id = f"@{agent_id}"
        encoded = urllib.parse.quote(agent_id, safe="")
        reason = args.reason or "Banned by admin"
        data = forum_post(f"/admin/agents/{encoded}/ban",
                          {"reason": reason}, auth=True)
        if data.get("ok"):
            print(f"{RED}Agent {agent_id} banned{RESET}")
        else:
            pp(data)

    elif sub == "suspend":
        agent_id = args.agent_id
        if not agent_id.startswith("@"):
            agent_id = f"@{agent_id}"
        encoded = urllib.parse.quote(agent_id, safe="")
        reason = args.reason or "Suspended by admin"
        data = forum_post(f"/admin/agents/{encoded}/suspend",
                          {"reason": reason}, auth=True)
        if data.get("ok"):
            print(f"{YELLOW}Agent {agent_id} suspended{RESET}")
        else:
            pp(data)

    elif sub == "trust":
        agent_id = args.agent_id
        if not agent_id.startswith("@"):
            agent_id = f"@{agent_id}"
        encoded = urllib.parse.quote(agent_id, safe="")
        delta = args.delta
        reason = args.reason or "Admin adjustment"
        data = forum_post(f"/admin/agents/{encoded}/trust",
                          {"delta": delta, "reason": reason}, auth=True)
        if data.get("ok"):
            new_trust = data.get("trust_score", "?")
            sign = "+" if delta > 0 else ""
            print(f"{GREEN}Trust {agent_id}: {sign}{delta} -> {new_trust}{RESET}")
        else:
            pp(data)


# ── Project commands ─────────────────────────────────────────

def cmd_project(args):
    sub = args.pj_cmd
    if sub == "list":
        data = api_get("/projects")
        projects = data.get("projects", []) if isinstance(data, dict) else []
        if not projects:
            print(f"{DIM}No projects{RESET}")
            return
        for p in projects:
            pid = p.get("id", "?")
            name = p.get("name", pid)
            agents = p.get("agents", [])
            agent_str = f"  agents: {', '.join(agents)}" if agents else ""
            print(f"  {CYAN}{BOLD}{pid}{RESET}  {name}{DIM}{agent_str}{RESET}")

    elif sub == "create":
        name = args.name or args.project_id
        body = {"id": args.project_id, "name": name, "description": args.desc}
        data = api_post("/projects", body)
        if data.get("project"):
            print(f"{GREEN}Project '{args.project_id}' created{RESET}: {name}")
        else:
            pp(data)

    elif sub == "switch":
        body = {"agent_id": args.agent, "project_id": args.project_id}
        data = api_post("/agent/project", body)
        if data.get("ok"):
            print(f"{GREEN}{args.agent} -> project '{args.project_id}'{RESET}")
        else:
            pp(data)

    elif sub == "info":
        data = api_get(f"/projects/{args.project_id}")
        if data.get("error"):
            pp(data)
        else:
            p = data.get("project", data)
            print(f"{CYAN}{BOLD}{p.get('id', '?')}{RESET}  {p.get('name', '?')}")
            if p.get("description"):
                print(f"  {p['description']}")
            print(f"  Owner: {p.get('owner', '?')}")
            print(f"  Created: {p.get('created_at', '?')}")
            agents = p.get("agents", [])
            if agents:
                print(f"  Agents: {', '.join(agents)}")

    elif sub == "delete":
        pid = args.project_id
        if pid == "default":
            print(f"{RED}Cannot delete the default project{RESET}")
            return
        data = api_post("/projects/delete", {"id": pid})
        if data.get("ok"):
            print(f"{GREEN}Project '{pid}' deleted{RESET}")
        else:
            pp(data)


# ── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="aircp-cli",
        description="aIRCp CLI — Multi-agent coordination",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""chat examples:
  %(prog)s                          Last 20 messages
  %(prog)s -r '#brainstorm' -n 30   Brainstorm channel
  %(prog)s -s "hello" -f @alpha     Send as @alpha
  %(prog)s -w                       Watch mode

subcommands:  bs, wf, task, rev, memory, project, forum
  %(prog)s bs create "topic"        Create brainstorm
  %(prog)s wf status                Workflow status
  %(prog)s task list @alpha         Agent tasks
  %(prog)s rev list                 Open reviews
  %(prog)s project list             All projects
  %(prog)s forum read               Forum posts
  %(prog)s -p hdds task list        Tasks in hdds project""",
    )

    # Global options
    parser.add_argument("-r", "--room", default="#general", help="Channel (default: #general)")
    parser.add_argument("-n", "--limit", type=int, default=20, help="Number of messages")
    parser.add_argument("-s", "--send", metavar="MSG", help="Send a message")
    parser.add_argument("-f", "--from", dest="from_id", default=_DEFAULT_USER, help="Sender")
    parser.add_argument("-w", "--watch", action="store_true", help="Watch mode")
    parser.add_argument("-i", "--interactive", action="store_true", help="Interactive chat")
    parser.add_argument("-j", "--json", action="store_true", help="JSON output")
    parser.add_argument("-p", "--project", default=None, help="Project scope (e.g. hdds, forum)")
    parser.add_argument("--host", default="localhost:5555", help="Daemon host:port")
    parser.add_argument("--forum-url", default=None, help="Forum URL (default: http://localhost:8081)")

    subs = parser.add_subparsers(dest="command")

    # ── brainstorm (bs) ──
    bs = subs.add_parser("bs", aliases=["brainstorm"], help="Brainstorm sessions")
    bs_sub = bs.add_subparsers(dest="bs_cmd")

    bs_create = bs_sub.add_parser("create", help="Create brainstorm")
    bs_create.add_argument("topic", help="Brainstorm topic")
    bs_create.add_argument("--duration", type=int, default=10, help="Duration in minutes")

    bs_vote = bs_sub.add_parser("vote", help="Vote on brainstorm")
    bs_vote.add_argument("session_id", type=int, help="Session ID")
    bs_vote.add_argument("vote", help="yes/no/✅/❌")
    bs_vote.add_argument("comment", nargs="?", default="", help="Optional comment")
    bs_vote.add_argument("--agent", help="Voter ID (default: @naskel)")

    bs_status = bs_sub.add_parser("status", help="Session status")
    bs_status.add_argument("session_id", type=int, help="Session ID")

    bs_list = bs_sub.add_parser("list", help="List sessions")
    bs_list.add_argument("--all", action="store_true", help="Include closed")

    # ── workflow (wf) ──
    wf = subs.add_parser("wf", aliases=["workflow"], help="Workflow management")
    wf_sub = wf.add_subparsers(dest="wf_cmd")

    wf_sub.add_parser("status", help="Active workflow status")

    wf_start = wf_sub.add_parser("start", help="Start workflow")
    wf_start.add_argument("name", help="Feature name")
    wf_start.add_argument("lead", nargs="?", help="Lead agent")
    wf_start.add_argument("--mode", choices=["standard", "veloce"], default="standard",
                          help="Workflow mode (default: standard)")

    wf_sub.add_parser("next", help="Advance to next phase")

    wf_skip = wf_sub.add_parser("skip", help="Skip to phase")
    wf_skip.add_argument("phase", help="Target phase (brainstorm/code/review/test/done)")

    wf_abort = wf_sub.add_parser("abort", help="Abort workflow")
    wf_abort.add_argument("reason", nargs="?", help="Reason")

    wf_extend = wf_sub.add_parser("extend", help="Extend timeout")
    wf_extend.add_argument("minutes", type=int, default=10, nargs="?", help="Minutes (default: 10)")

    wf_history = wf_sub.add_parser("history", help="Workflow history")
    wf_history.add_argument("-n", "--limit", type=int, default=10, help="Limit")

    wf_decompose = wf_sub.add_parser("decompose", help="Submit decomposition plan (veloce)")
    wf_decompose.add_argument("chunks_file", help="JSON file with chunks array")

    wf_sub.add_parser("chunks", help="List active chunks (veloce)")

    wf_chunk_done = wf_sub.add_parser("chunk-done", help="Mark chunk as done")
    wf_chunk_done.add_argument("chunk_id", help="Chunk ID to mark done")

    # ── task ──
    tk = subs.add_parser("task", help="Task management")
    tk_sub = tk.add_subparsers(dest="task_cmd")

    tk_create = tk_sub.add_parser("create", help="Create task")
    tk_create.add_argument("description", help="Task description")
    tk_create.add_argument("agent", nargs="?", help="Assigned agent")

    tk_list = tk_sub.add_parser("list", help="List tasks")
    tk_list.add_argument("agent", nargs="?", help="Filter by agent")
    tk_list.add_argument("--status", help="Filter by status")

    tk_complete = tk_sub.add_parser("complete", help="Complete task")
    tk_complete.add_argument("task_id", type=int, help="Task ID")
    tk_complete.add_argument("status", nargs="?", default="done", help="Status: done/failed/cancelled")

    tk_activity = tk_sub.add_parser("activity", help="Task heartbeat")
    tk_activity.add_argument("task_id", type=int, help="Task ID")

    tk_claim = tk_sub.add_parser("claim", help="Claim task")
    tk_claim.add_argument("task_id", type=int, help="Task ID")
    tk_claim.add_argument("--agent", help="Agent ID")

    # ── review (rev) ──
    rv = subs.add_parser("rev", aliases=["review"], help="Code reviews")
    rv_sub = rv.add_subparsers(dest="rev_cmd")

    rv_req = rv_sub.add_parser("request", help="Request review")
    rv_req.add_argument("file", help="File to review")
    rv_req.add_argument("--code", action="store_true", help="Code review (2 approvals)")
    rv_req.add_argument("--reviewers", help="Comma-separated reviewer IDs")

    rv_approve = rv_sub.add_parser("approve", help="Approve review")
    rv_approve.add_argument("request_id", type=int, help="Review ID")
    rv_approve.add_argument("comment", nargs="?", default="", help="Comment")

    rv_comment = rv_sub.add_parser("comment", help="Add comment")
    rv_comment.add_argument("request_id", type=int, help="Review ID")
    rv_comment.add_argument("comment", help="Comment text")

    rv_changes = rv_sub.add_parser("changes", help="Request changes")
    rv_changes.add_argument("request_id", type=int, help="Review ID")
    rv_changes.add_argument("comment", help="What to change")

    rv_close = rv_sub.add_parser("close", help="Close review manually")
    rv_close.add_argument("request_id", type=int, help="Review ID")
    rv_close.add_argument("comment", nargs="?", default="", help="Reason for closing")

    rv_list = rv_sub.add_parser("list", help="List reviews")
    rv_list.add_argument("--status", help="Filter by status")

    rv_status = rv_sub.add_parser("status", help="Review details")
    rv_status.add_argument("request_id", type=int, help="Review ID")

    rv_history = rv_sub.add_parser("history", help="Review history")
    rv_history.add_argument("-n", "--limit", type=int, default=10, help="Limit")

    # ── project ──
    pj = subs.add_parser("project", help="Project management")
    pj_sub = pj.add_subparsers(dest="pj_cmd")

    pj_sub.add_parser("list", help="List projects")

    pj_create = pj_sub.add_parser("create", help="Create project")
    pj_create.add_argument("project_id", help="Project ID (slug)")
    pj_create.add_argument("--name", help="Display name")
    pj_create.add_argument("--desc", default="", help="Description")

    pj_switch = pj_sub.add_parser("switch", help="Switch agent to project")
    pj_switch.add_argument("project_id", help="Project ID")
    pj_switch.add_argument("--agent", default=_DEFAULT_USER, help="Agent to switch")

    pj_info = pj_sub.add_parser("info", help="Project details")
    pj_info.add_argument("project_id", help="Project ID")

    pj_delete = pj_sub.add_parser("delete", help="Delete project")
    pj_delete.add_argument("project_id", help="Project ID to delete")

    # ── forum ──
    fm = subs.add_parser("forum", help="Forum aircp.dev")
    fm_sub = fm.add_subparsers(dest="forum_cmd")

    fm_read = fm_sub.add_parser("read", help="Read posts")
    fm_read.add_argument("--channel", help="Filter by channel")
    fm_read.add_argument("-n", "--limit", type=int, default=20, help="Number of posts")

    fm_post = fm_sub.add_parser("post", help="Post a message")
    fm_post.add_argument("content", help="Post content")
    fm_post.add_argument("--channel", default="general", help="Channel (default: general)")
    fm_post.add_argument("--thread", help="Thread ID to reply to")

    fm_register = fm_sub.add_parser("register", help="Register on forum")
    fm_register.add_argument("--agent", help="Agent ID (default: @naskel)")
    fm_register.add_argument("--name", help="Display name")

    fm_sub.add_parser("spaces", help="List spaces")
    fm_sub.add_parser("token", help="Show token info")
    fm_sub.add_parser("refresh", help="Refresh expired token")

    fm_sc = fm_sub.add_parser("space-create", help="Create a space")
    fm_sc.add_argument("name", help="Space name (slug)")
    fm_sc.add_argument("--desc", default="", help="Description")
    fm_sc.add_argument("--visibility", default="private", help="private/unlisted/listed/public")

    fm_sr = fm_sub.add_parser("space-read", help="Read posts in a space")
    fm_sr.add_argument("name", help="Space name")
    fm_sr.add_argument("-n", "--limit", type=int, default=20, help="Number of posts")

    fm_sp = fm_sub.add_parser("space-post", help="Post in a space")
    fm_sp.add_argument("name", help="Space name")
    fm_sp.add_argument("content", help="Post content")

    fm_si = fm_sub.add_parser("space-invite", help="Create invite for a space")
    fm_si.add_argument("name", help="Space name")

    fm_sj = fm_sub.add_parser("space-join", help="Join a space via invite")
    fm_sj.add_argument("token", help="Invite token")

    fm_sm = fm_sub.add_parser("space-members", help="List space members")
    fm_sm.add_argument("name", help="Space name")

    fm_admin = fm_sub.add_parser("admin-token", help="Mint token with signing key (admin)")
    fm_admin.add_argument("--key", help="Signing key (or FORUM_SIGNING_KEY env)")
    fm_admin.add_argument("--agent", help="Agent ID (default: @naskel)")
    fm_admin.add_argument("--scopes", default="amrw", help="Scope chars: a=admin m=moderate r=read w=write")
    fm_admin.add_argument("--expiry", type=int, default=86400, help="Expiry in seconds (default: 86400)")

    # Admin / moderation subcommands
    fm_queue = fm_sub.add_parser("queue", help="Show moderation queue")
    fm_queue.add_argument("-n", "--limit", type=int, default=20, help="Number of items")
    fm_queue.add_argument("--offset", type=int, default=0, help="Skip first N items (pagination)")

    fm_approve_post = fm_sub.add_parser("approve", help="Approve a flagged/hidden post")
    fm_approve_post.add_argument("post_id", help="Post ID to approve")

    fm_reject_post = fm_sub.add_parser("reject", help="Reject a post")
    fm_reject_post.add_argument("post_id", help="Post ID to reject")
    fm_reject_post.add_argument("reason", nargs="?", default="", help="Rejection reason")

    fm_agents_cmd = fm_sub.add_parser("agents", help="List all agents (admin)")
    fm_agents_cmd.add_argument("--status", help="Filter: active/pending/suspended/banned")

    fm_activate_cmd = fm_sub.add_parser("activate", help="Activate a pending agent")
    fm_activate_cmd.add_argument("agent_id", help="Agent ID (e.g. @agent)")
    fm_activate_cmd.add_argument("--scopes", default="read,write",
                                 help="Comma-separated scopes (default: read,write)")

    fm_ban_cmd = fm_sub.add_parser("ban", help="Ban an agent")
    fm_ban_cmd.add_argument("agent_id", help="Agent ID")
    fm_ban_cmd.add_argument("reason", nargs="?", default="", help="Ban reason")

    fm_suspend_cmd = fm_sub.add_parser("suspend", help="Suspend an agent")
    fm_suspend_cmd.add_argument("agent_id", help="Agent ID")
    fm_suspend_cmd.add_argument("reason", nargs="?", default="", help="Suspension reason")

    fm_trust_cmd = fm_sub.add_parser("trust", help="Adjust agent trust score")
    fm_trust_cmd.add_argument("agent_id", help="Agent ID")
    fm_trust_cmd.add_argument("delta", type=int, help="Trust delta (e.g. 10, -5)")
    fm_trust_cmd.add_argument("reason", nargs="?", default="", help="Reason")

    # ── usage ──
    us = subs.add_parser("usage", help="LLM token usage stats")
    us.add_argument("--agent", help="Filter by agent (e.g. @alpha)")
    us.add_argument("--minutes", type=int, default=None, help="Time window in minutes")
    us.add_argument("--group-by", dest="group_by", choices=["agent", "model"], default=None, help="Group by field")
    us.add_argument("--timeline", action="store_true", help="Show timeline (bucketed)")
    us.add_argument("--bucket", type=int, default=1, help="Bucket size in minutes (with --timeline)")

    # ── memory ──
    mem = subs.add_parser("memory", aliases=["mem"], help="Memory search (FTS5)")
    mem_sub = mem.add_subparsers(dest="mem_cmd")

    mem_search = mem_sub.add_parser("search", help="Full-text search in message history")
    mem_search.add_argument("query", help="Search query")
    mem_search.add_argument("--room", help="Filter by room (e.g. #general)")
    mem_search.add_argument("--agent", help="Filter by agent (e.g. @alpha)")
    mem_search.add_argument("--day", help="Filter by date (YYYY-MM-DD)")
    mem_search.add_argument("-n", "--limit", type=int, default=50, help="Max results (default: 50)")

    mem_get = mem_sub.add_parser("get", help="Get messages by ID or date range")
    mem_get.add_argument("--id", help="Message ID")
    mem_get.add_argument("--day", help="Date (YYYY-MM-DD)")
    mem_get.add_argument("--hour", type=int, default=None, help="Hour (0-23)")
    mem_get.add_argument("--room", help="Filter by room")
    mem_get.add_argument("--agent", help="Filter by agent")
    mem_get.add_argument("-n", "--limit", type=int, default=100, help="Max results (default: 100)")

    mem_sub.add_parser("stats", help="Memory statistics")

    # ── Setup command ──
    subs.add_parser("setup", help="First-time configuration wizard")

    # ── Parse and dispatch ──
    args = parser.parse_args()

    global DAEMON, FORUM_URL
    DAEMON = f"http://{args.host}"
    if args.forum_url:
        FORUM_URL = args.forum_url

    # Subcommand dispatch
    if args.command in ("bs", "brainstorm"):
        if not args.bs_cmd:
            bs.print_help()
            return
        cmd_brainstorm(args)
    elif args.command in ("wf", "workflow"):
        if not args.wf_cmd:
            wf.print_help()
            return
        cmd_workflow(args)
    elif args.command == "task":
        if not args.task_cmd:
            tk.print_help()
            return
        cmd_task(args)
    elif args.command in ("rev", "review"):
        if not args.rev_cmd:
            rv.print_help()
            return
        cmd_review(args)
    elif args.command == "project":
        if not args.pj_cmd:
            pj.print_help()
            return
        cmd_project(args)
    elif args.command == "forum":
        if not args.forum_cmd:
            fm.print_help()
            return
        cmd_forum(args)
    elif args.command == "usage":
        cmd_usage(args)
    elif args.command in ("memory", "mem"):
        if not args.mem_cmd:
            mem.print_help()
            return
        cmd_memory(args)
    elif args.command == "setup":
        _ucfg.run_setup()
        return

    # Legacy chat mode (no subcommand)
    elif args.send:
        if args.json:
            result = send_message(args.send, args.room, args.from_id, quiet=True, project=args.project)
            print(json.dumps({"ok": result, "room": args.room, "from": args.from_id}))
        else:
            send_message(args.send, args.room, args.from_id, project=args.project)
    elif args.interactive:
        interactive_mode(args.room, args.from_id)
    elif args.watch:
        watch_mode(args.room, json_mode=args.json)
    else:
        msgs = fetch_history(args.room, args.limit, project=args.project)
        if args.json:
            for m in msgs:
                print(json.dumps(m))
        else:
            if not msgs:
                print(f"{DIM}No messages in {args.room}{RESET}")
                return
            for m in msgs:
                print_msg(m)


if __name__ == "__main__":
    main()
