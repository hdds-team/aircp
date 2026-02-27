#!/usr/bin/env python3
"""
AIRCP Compact Engine — Chat compaction for multi-agent systems.

Architecture: Hybrid daemon-first + optional LLM summary (Ollama local).
- Classifies messages into KEEP / COMPACT / DELETE (rule-based, zero LLM)
- Applies agent-specific profiles (context window sizes differ)
- Generates summaries: LLM via Ollama if available, rule-based fallback
- Audit trail for traceability

v2.0 — 2026-02-06
  - LLM summary via Ollama (local, zero cloud cost) with rule-based fallback
  - /compact chat command support (parsed in daemon)
  - Dashboard widget data via GET /compact/status
"""

import re
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

try:
    import requests as _requests
except ImportError:
    _requests = None  # LLM summary disabled if requests not available

logger = logging.getLogger(__name__)

# =============================================================================
# Message Categories
# =============================================================================

class Category:
    """Message classification categories."""
    KEEP = "keep"           # 🔴 Never touch: decisions, claims, tasks, reviews, bugs
    COMPACT = "compact"     # 🟡 Summarize if thread is long: technical discussions
    DELETE = "delete"       # ⚫ Remove: watchdog, ACKs, duplicates, social noise


# =============================================================================
# Classification Rules (daemon-first, no LLM needed)
# =============================================================================

# Patterns for DELETE category (high confidence, regex-based)
DELETE_PATTERNS = [
    # Watchdog / system bots
    r"^⏰\s*\*\*WORKFLOW",                      # workflow timeout notifications
    r"^⚠️\s*\*\*WORKFLOW",                      # workflow warnings
    r"^🗳️\s*Rappel brainstorm",                 # brainstorm reminders
    r"^⏰.*watchdog",                            # watchdog pings
    r"ping\s*!?\s*$",                            # bare pings

    # ACKs and minimal responses
    r"^(Vu|Noté|OK|👍|✅|Reçu|Roger|Compris|Ack)\s*!?\s*$",
    r"^(Merci|Thanks|Thx)\s*!?\s*$",

    # Social noise / filler
    r"^(je suis (de retour|back|là)|I'?m back)\s*!?\s*$",
    r"dispo\s*!?\s*$",
    r"^(Salut|Hey|Hello|Bonjour)\s+tout le monde\s*!?\s*$",

    # Bot status messages (not from humans)
    r"daemon.*online",
    r"daemon.*en pause café",
]

# Patterns for KEEP category (high confidence)
KEEP_PATTERNS = [
    # Decisions
    r"(GO|NO[- ]GO|APPROVED|REJECTED|CONSENSUS|DECISION)\b",
    r"\*\*(GO|APPROVED|REJECTED)\*\*",

    # Tasks and reviews (structured commands)
    r"task/(create|complete|activity)",
    r"review/(request|approve|changes)",
    r"claim.*request|lock.*acquire",

    # Bug reports and fixes
    r"(bug|fix|error|crash|broken|regression)\b",

    # Architecture decisions
    r"(architecture|design|spec|RFC|proposal)\b.*:",

    # Explicit @mentions (someone is directly addressed → important)
    r"@(alpha|beta|sonnet|haiku|naskel|mascotte|all)\s",

    # Human messages (always keep)
    # → handled separately via from_id check

    # Action items
    r"(TODO|FIXME|ACTION|BLOCKER)\b",
    r"plan d'(action|implem)",
]

# From-IDs that are system bots (DELETE-biased)
SYSTEM_BOTS = {
    "@workflow", "@idea", "@review", "@taskman", "@watchdog",
    "@tips", "@brainstorm", "@compactor",
}

# From-IDs that are test/ghost accounts (always DELETE, never include in participants)
TEST_GHOST_IDS = {
    "@echo", "@test-client",
}
# Also match dynamic test IDs like @test-25285d82
import re as _re
_TEST_PATTERN = _re.compile(r"^@test-[a-f0-9]+$")

# From-IDs that are human (ALWAYS KEEP)
HUMAN_IDS = {
    "@naskel",
}

# From-IDs for agents (context-dependent classification)
# These are classified by content analysis
AGENT_IDS = {
    "@alpha", "@beta", "@sonnet", "@haiku", "@mascotte",
    "alpha", "beta", "sonnet", "haiku", "mascotte",
    "@claude-desktop", "@claude-web", "@codex",
}

# =============================================================================
# Pre-computed lowercase sets (perf: avoid re-creating at every classify call)
# =============================================================================
_HUMAN_IDS_LOWER = {h.lower() for h in HUMAN_IDS}
_SYSTEM_BOTS_LOWER = {b.lower() for b in SYSTEM_BOTS}
_AGENT_IDS_LOWER = {a.lower() for a in AGENT_IDS}

# Short message patterns that are LEGITIMATE (not noise)
# These protect votes, approvals, and meaningful short responses from Rule 5
SHORT_LEGIT_PATTERNS = [
    r"^\+1\s*$",                          # Vote
    r"^-1\s*$",                           # Counter-vote
    r"^(LGTM|lgtm)\s*!?\s*$",            # Code review approval
    r"^(GO|NO[- ]GO)\s*!?\s*$",          # Decision
    r"^(approved|rejected)\s*!?\s*$",     # Formal decision
    r"^@\w+\s",                           # Starts with @mention → directed msg
    r"(✅|❌|🔴|🟢|👍|👎)\s*\w",         # Emoji + text = meaningful reaction
]


def classify_message(msg: Dict[str, Any]) -> str:
    """
    Classify a single message into KEEP / COMPACT / DELETE.

    Args:
        msg: Message dict with keys: from, content, timestamp, room, id

    Returns:
        Category.KEEP, Category.COMPACT, or Category.DELETE
    """
    from_id = msg.get("from", "").lower().strip()
    content = msg.get("content", "").strip()

    # Empty messages → DELETE
    if not content:
        return Category.DELETE

    # === Rule 0: Test/ghost accounts → ALWAYS DELETE ===
    if from_id in {s.lower() for s in TEST_GHOST_IDS} or _TEST_PATTERN.match(from_id):
        return Category.DELETE

    # === Rule 1: Human messages → ALWAYS KEEP ===
    if from_id in _HUMAN_IDS_LOWER:
        return Category.KEEP

    # === Rule 2: System bot messages → mostly DELETE ===
    if from_id in _SYSTEM_BOTS_LOWER:
        # Exception: bot messages with decisions or final results
        for pattern in KEEP_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                return Category.KEEP
        return Category.DELETE

    # === Rule 3: Check DELETE patterns (noise) ===
    for pattern in DELETE_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE | re.MULTILINE):
            return Category.DELETE

    # === Rule 4: Check KEEP patterns (important content) ===
    for pattern in KEEP_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return Category.KEEP

    # === Rule 5: Short messages from agents → likely noise ===
    if len(content) < 50 and from_id in _AGENT_IDS_LOWER:
        # Short + no KEEP pattern = probably an ACK or filler
        # BUT protect legitimate short messages (votes, approvals, etc.)
        is_legit = any(
            re.search(p, content, re.IGNORECASE) for p in SHORT_LEGIT_PATTERNS
        )
        if not is_legit:
            return Category.DELETE

    # === Rule 6: Duplicate/echo detection ===
    # (Handled at batch level in compact_messages, not per-message)

    # === Default: COMPACT (summarizable) ===
    return Category.COMPACT


# =============================================================================
# Agent Profiles
# =============================================================================

# Default profiles — can be overridden per agent in config.toml
PROFILES = {
    "minimal": {
        "description": "Large context (200k+). Keep more, compact less.",
        "max_age_minutes": 120,        # Compact messages older than 2h
        "delete_threshold": 0.3,       # Only delete if >30% is noise
        "keep_compact_ratio": 0.7,     # Keep 70% of compactable messages
        "max_messages_before_compact": 100,  # Trigger at 100+ messages
        "summary_max_lines": 15,       # Longer summaries OK
    },
    "moderate": {
        "description": "Medium context (~32-128k). Balanced approach.",
        "max_age_minutes": 60,
        "delete_threshold": 0.2,
        "keep_compact_ratio": 0.5,
        "max_messages_before_compact": 60,
        "summary_max_lines": 10,
    },
    "aggressive": {
        "description": "Small context (4-8k). Compact aggressively.",
        "max_age_minutes": 30,
        "delete_threshold": 0.1,       # Delete even with little noise
        "keep_compact_ratio": 0.3,     # Keep only 30% of compactable
        "max_messages_before_compact": 30,
        "summary_max_lines": 5,        # Very short summaries
    },
}

# Agent → profile mapping (defaults, overridable in config.toml)
AGENT_PROFILE_MAP = {
    "@alpha": "minimal",
    "@beta": "minimal",
    "@sonnet": "moderate",
    "@haiku": "moderate",
    "@claude-desktop": "minimal",
    "@claude-web": "minimal",
    "@codex": "moderate",
    "@mascotte": "aggressive",
}


def get_profile(agent_id: str, config: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get compaction profile for an agent.

    Checks config.toml override first, then falls back to defaults.
    """
    # Check config override
    if config and "compactor" in config:
        profile_name = config["compactor"].get("profile", None)
        if profile_name and profile_name in PROFILES:
            return PROFILES[profile_name]

    # Default mapping
    profile_name = AGENT_PROFILE_MAP.get(agent_id.lower(), "moderate")
    return PROFILES[profile_name]


# =============================================================================
# Compaction Engine
# =============================================================================

def compact_messages(
    messages: List[Dict[str, Any]],
    profile: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Compact a list of messages according to a profile.

    Args:
        messages: List of message dicts (from, content, timestamp, room, id)
        profile: Agent profile dict (from PROFILES)
        now: Current time (for testing)

    Returns:
        {
            "kept": [...],          # Messages to keep as-is
            "summary": str,         # Generated summary of compacted messages
            "deleted_count": int,   # Number of deleted messages
            "compacted_count": int, # Number of compacted messages
            "total_before": int,    # Original message count
            "total_after": int,     # Kept + 1 summary message
            "audit": [...],         # Audit trail: what was removed and why
        }
    """
    if now is None:
        now = datetime.now(timezone.utc)

    max_age = timedelta(minutes=profile.get("max_age_minutes", 60))
    max_messages = profile.get("max_messages_before_compact", 60)

    # Step 1: Classify all messages
    classified = []
    for msg in messages:
        category = classify_message(msg)
        classified.append((msg, category))

    # Step 2: Apply age filter — recent messages get promoted
    for i, (msg, category) in enumerate(classified):
        ts = msg.get("timestamp", 0)
        msg_time = _parse_timestamp(ts)
        if msg_time and (now - msg_time) < timedelta(minutes=5):
            # Very recent messages: promote COMPACT → KEEP
            if category == Category.COMPACT:
                classified[i] = (msg, Category.KEEP)

    # Step 3: Deduplicate — detect near-identical messages
    seen_content = {}
    for i, (msg, category) in enumerate(classified):
        if category == Category.DELETE:
            continue
        content_key = _normalize_for_dedup(msg.get("content", ""))
        if content_key in seen_content:
            # Keep the first occurrence, delete duplicates
            classified[i] = (msg, Category.DELETE)
        else:
            seen_content[content_key] = i

    # Step 4: Separate by category
    kept = []
    to_compact = []
    to_delete = []
    audit = []

    for msg, category in classified:
        if category == Category.KEEP:
            kept.append(msg)
        elif category == Category.COMPACT:
            to_compact.append(msg)
        else:
            to_delete.append(msg)
            audit.append({
                "id": msg.get("id", "?"),
                "from": msg.get("from", "?"),
                "reason": "noise/duplicate/bot",
                "preview": (msg.get("content", ""))[:60],
            })

    # Step 5: Apply profile ratio to compactable messages
    keep_ratio = profile.get("keep_compact_ratio", 0.5)
    n_keep_from_compact = int(len(to_compact) * keep_ratio)

    # Keep the most recent compactable messages
    to_compact_sorted = sorted(
        to_compact,
        key=lambda m: m.get("timestamp", 0),
        reverse=True,
    )
    kept_from_compact = to_compact_sorted[:n_keep_from_compact]
    actually_compacted = to_compact_sorted[n_keep_from_compact:]

    kept.extend(kept_from_compact)

    for msg in actually_compacted:
        audit.append({
            "id": msg.get("id", "?"),
            "from": msg.get("from", "?"),
            "reason": "compacted (old/low priority)",
            "preview": (msg.get("content", ""))[:60],
        })

    # Step 6: Generate summary of compacted messages
    summary = _generate_summary(actually_compacted, to_delete, profile)

    # Step 7: Sort kept messages by timestamp
    kept.sort(key=lambda m: m.get("timestamp", 0))

    total_after = len(kept) + (1 if summary else 0)

    # Collect IDs of messages to physically delete from DB
    deleted_ids = [m.get("id") for m in to_delete if m.get("id")]
    compacted_ids = [m.get("id") for m in actually_compacted if m.get("id")]

    return {
        "kept": kept,
        "summary": summary,
        "deleted_count": len(to_delete),
        "compacted_count": len(actually_compacted),
        "deleted_ids": deleted_ids,
        "compacted_ids": compacted_ids,
        "total_before": len(messages),
        "total_after": total_after,
        "compression_ratio": f"{(1 - total_after / max(len(messages), 1)) * 100:.0f}%",
        "audit": audit,
    }


# =============================================================================
# LLM Summary Config (Ollama local)
# =============================================================================

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:1.7b"          # Light model, fast, local
OLLAMA_TIMEOUT = 15                    # seconds — abort if Ollama is slow
OLLAMA_MAX_INPUT_CHARS = 4000          # Truncate input to fit context window

_LLM_SUMMARY_PROMPT = """You are a chat compactor for a multi-agent AI team (AIRCP).
Summarize the following chat messages into a concise summary (max {max_lines} lines).

Rules:
- Keep decisions, action items, and important technical points
- Mention who said what for key decisions
- Use bullet points
- Be concise — this replaces {n_msgs} messages
- Write in the same language as the messages (usually French)
- Do NOT add commentary, just summarize

Messages:
{messages}

Summary:"""


def _generate_summary_llm(
    compacted: List[Dict[str, Any]],
    deleted: List[Dict[str, Any]],
    profile: Dict[str, Any],
) -> Optional[str]:
    """
    Generate summary using local Ollama LLM.
    Returns None if LLM is unavailable or fails (caller falls back to rule-based).

    Cost: ~500-1000 tokens per compaction (local, zero cloud cost).
    """
    if _requests is None:
        return None

    if not compacted:
        return None

    max_lines = profile.get("summary_max_lines", 10)

    # Format messages for the prompt
    msg_lines = []
    total_chars = 0
    for msg in compacted:
        line = f"[{msg.get('from', '?')}]: {msg.get('content', '')}"
        if total_chars + len(line) > OLLAMA_MAX_INPUT_CHARS:
            msg_lines.append("... (truncated)")
            break
        msg_lines.append(line)
        total_chars += len(line)

    prompt = _LLM_SUMMARY_PROMPT.format(
        max_lines=max_lines,
        n_msgs=len(compacted) + len(deleted),
        messages="\n".join(msg_lines),
    )

    try:
        t0 = time.time()
        resp = _requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 500,
                },
            },
            timeout=OLLAMA_TIMEOUT,
        )
        elapsed = time.time() - t0

        if resp.status_code != 200:
            logger.warning(f"Ollama returned {resp.status_code}, falling back to rule-based")
            return None

        data = resp.json()
        summary_text = data.get("response", "").strip()

        if not summary_text or len(summary_text) < 10:
            logger.warning("Ollama returned empty/short summary, falling back")
            return None

        # Prepend header + append stats
        total_removed = len(compacted) + len(deleted)
        agents_involved = set()
        for msg in compacted + deleted:
            fid = msg.get("from", "?")
            # Filter out test/ghost accounts from participants
            if fid.lower() not in {s.lower() for s in TEST_GHOST_IDS} and not _TEST_PATTERN.match(fid.lower()):
                agents_involved.add(fid)

        header = f"📦 **Compacted {total_removed} messages** _(LLM summary, {elapsed:.1f}s)_"
        footer = f"**Participants:** {', '.join(sorted(agents_involved))} | **Deleted:** {len(deleted)} noise | **Compacted:** {len(compacted)} discussion"

        return f"{header}\n{summary_text}\n{footer}"

    except _requests.exceptions.ConnectionError:
        logger.debug("Ollama not available (connection refused), using rule-based fallback")
        return None
    except _requests.exceptions.Timeout:
        logger.warning(f"Ollama timeout ({OLLAMA_TIMEOUT}s), using rule-based fallback")
        return None
    except Exception as e:
        logger.warning(f"LLM summary failed: {e}, using rule-based fallback")
        return None


# =============================================================================
# Summary Generator (hybrid: LLM first, rule-based fallback)
# =============================================================================

def _generate_summary(
    compacted: List[Dict[str, Any]],
    deleted: List[Dict[str, Any]],
    profile: Dict[str, Any],
) -> str:
    """
    Generate a text summary of compacted/deleted messages.

    v2.0: Tries LLM (Ollama local) first for semantic summary.
    Falls back to rule-based extraction if LLM unavailable/fails.
    """
    if not compacted and not deleted:
        return ""

    # === v2.0: Try LLM summary first ===
    llm_summary = _generate_summary_llm(compacted, deleted, profile)
    if llm_summary:
        return llm_summary

    # === Fallback: rule-based summary (v1.0 logic) ===
    max_lines = profile.get("summary_max_lines", 10)
    lines = []

    # Header
    total_removed = len(compacted) + len(deleted)
    lines.append(f"📦 **Compacted {total_removed} messages**")

    # Extract key topics from compacted messages
    topics = _extract_topics(compacted)
    if topics:
        lines.append(f"**Topics discussed:** {', '.join(topics[:5])}")

    # Extract decisions/actions mentioned
    decisions = _extract_decisions(compacted)
    if decisions:
        lines.append("**Decisions/Actions:**")
        for d in decisions[:max_lines - 3]:
            lines.append(f"  - {d}")

    # Stats (filter test/ghost accounts)
    agents_involved = set()
    for msg in compacted + deleted:
        fid = msg.get("from", "?")
        if fid.lower() not in {s.lower() for s in TEST_GHOST_IDS} and not _TEST_PATTERN.match(fid.lower()):
            agents_involved.add(fid)
    lines.append(f"**Participants:** {', '.join(sorted(agents_involved))}")
    lines.append(f"**Deleted:** {len(deleted)} noise msgs | **Compacted:** {len(compacted)} discussion msgs")

    return "\n".join(lines[:max_lines])


def _extract_topics(messages: List[Dict[str, Any]]) -> List[str]:
    """Extract main topics from a list of messages (keyword extraction)."""
    # Simple keyword extraction — look for **bold** terms and headers
    topics = set()
    for msg in messages:
        content = msg.get("content", "")
        # Extract **bold** terms
        bold_matches = re.findall(r"\*\*([^*]+)\*\*", content)
        for match in bold_matches:
            clean = match.strip()
            if 3 < len(clean) < 50:  # Reasonable length for a topic
                topics.add(clean)
        # Extract ## headers
        header_matches = re.findall(r"^##\s+(.+)$", content, re.MULTILINE)
        for match in header_matches:
            clean = match.strip()
            if len(clean) < 60:
                topics.add(clean)
    return list(topics)[:10]


def _extract_decisions(messages: List[Dict[str, Any]]) -> List[str]:
    """Extract decision-like statements from messages."""
    decisions = []
    decision_patterns = [
        r"(?:→|->|==>)\s*(.{10,80})",           # Arrow-prefixed conclusions
        r"(?:conclusion|decision|verdict)\s*:\s*(.{10,80})",
        r"(?:on\s+(?:va|fait|garde|prend))\s+(.{10,60})",  # French action verbs
        r"(?:retenu|choisi|validé)\s*:?\s*(.{10,80})",
    ]
    for msg in messages:
        content = msg.get("content", "")
        for pattern in decision_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for m in matches:
                clean = m.strip().rstrip(".")
                if clean and clean not in decisions:
                    decisions.append(clean)
    return decisions[:8]


# =============================================================================
# Helpers
# =============================================================================

def _parse_timestamp(ts: Any) -> Optional[datetime]:
    """Parse various timestamp formats to datetime."""
    if isinstance(ts, (int, float)):
        # Nanosecond timestamp (HDDS format)
        if ts > 1e18:
            return datetime.fromtimestamp(ts / 1e9, tz=timezone.utc)
        elif ts > 1e15:
            return datetime.fromtimestamp(ts / 1e6, tz=timezone.utc)
        elif ts > 1e12:
            return datetime.fromtimestamp(ts / 1e3, tz=timezone.utc)
        else:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    elif isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            # Ensure timezone-aware (some ISO strings lack tz info)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            try:
                return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


def _normalize_for_dedup(content: str) -> str:
    """Normalize content for duplicate detection."""
    # Remove emojis, extra whitespace, markdown formatting
    cleaned = re.sub(r"[*_~`#>]", "", content)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip().lower()
    # Truncate for comparison (first 100 chars)
    return cleaned[:100]


# =============================================================================
# Audit Trail
# =============================================================================

def save_audit_log(
    result: Dict[str, Any],
    room: str,
    agent_id: str,
    output_dir: str = "/projects/aircp/summaries",
) -> Optional[str]:
    """
    Save compaction audit log to file.

    Returns the file path, or None on error.
    """
    try:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        filename = f"{now.strftime('%Y-%m-%d-%H%M')}-{room.replace('#', '')}.json"
        filepath = output_path / filename

        audit_data = {
            "timestamp": now.isoformat(),
            "room": room,
            "triggered_by": agent_id,
            "total_before": result.get("total_before", 0),
            "total_after": result.get("total_after", 0),
            "deleted_count": result.get("deleted_count", 0),
            "compacted_count": result.get("compacted_count", 0),
            "compression_ratio": result.get("compression_ratio", "0%"),
            "summary": result.get("summary", ""),
            "audit_trail": result.get("audit", []),
        }

        with open(filepath, "w") as f:
            json.dump(audit_data, f, indent=2, ensure_ascii=False)

        logger.info(f"✓ Audit log saved: {filepath}")
        return str(filepath)

    except Exception as e:
        logger.error(f"Failed to save audit log: {e}")
        return None


# =============================================================================
# High-Level API (called by daemon)
# =============================================================================

def compact_room(
    messages: List[Dict[str, Any]],
    room: str,
    agent_id: str,
    agent_config: Optional[Dict] = None,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Compact messages for a specific room.

    This is the main entry point called by the daemon.

    Args:
        messages: All messages in the room
        room: Room name (e.g. "#general")
        agent_id: Who triggered the compaction
        agent_config: Optional agent config dict (from config.toml)
        force: Force compaction even below threshold

    Returns:
        Compaction result dict, or None if compaction not needed.
    """
    profile = get_profile(agent_id, agent_config)
    max_messages = profile.get("max_messages_before_compact", 60)

    # Check threshold
    if not force and len(messages) < max_messages:
        logger.debug(
            f"Compaction not needed for {room}: {len(messages)} < {max_messages} messages"
        )
        return None

    # Run compaction
    result = compact_messages(messages, profile)

    # Save audit trail
    audit_path = save_audit_log(result, room, agent_id)
    if audit_path:
        result["audit_file"] = audit_path

    logger.info(
        f"✓ Compacted {room}: {result['total_before']} → {result['total_after']} "
        f"({result['compression_ratio']} reduction)"
    )

    return result


# =============================================================================
# CLI / Debug
# =============================================================================

if __name__ == "__main__":
    """Quick test with sample messages."""
    logging.basicConfig(level=logging.INFO)

    sample_messages = [
        {"id": "1", "from": "@workflow", "content": "⏰ **WORKFLOW #1** - Phase timeout!", "timestamp": 1770000000000000000},
        {"id": "2", "from": "@naskel", "content": "@all : ok cool, on fait le compactor", "timestamp": 1770000001000000000},
        {"id": "3", "from": "@haiku", "content": "✅ GO!", "timestamp": 1770000002000000000},
        {"id": "4", "from": "@alpha", "content": "Je prends le lead sur le compactor. **Architecture retenue : Hybride daemon-first**", "timestamp": 1770000003000000000},
        {"id": "5", "from": "@watchdog", "content": "ping! @alpha task #3 stale", "timestamp": 1770000004000000000},
        {"id": "6", "from": "@beta", "content": "Vu", "timestamp": 1770000005000000000},
        {"id": "7", "from": "@sonnet", "content": "Je suis de retour!", "timestamp": 1770000006000000000},
        {"id": "8", "from": "@mascotte", "content": "Le daemon est en pleine forme !", "timestamp": 1770000007000000000},
        {"id": "9", "from": "@alpha", "content": "**DECISION** → On part sur daemon rules + profils agent. LLM en v2.", "timestamp": 1770000008000000000},
        {"id": "10", "from": "@beta", "content": "**Review** du compact_engine.py : LGTM, 2 suggestions mineures dans le classify_message()", "timestamp": 1770000009000000000},
    ]

    # Test with different profiles
    for profile_name in ["minimal", "moderate", "aggressive"]:
        print(f"\n{'='*60}")
        print(f"Profile: {profile_name}")
        print(f"{'='*60}")

        profile = PROFILES[profile_name]
        result = compact_messages(sample_messages, profile)

        print(f"Before: {result['total_before']} → After: {result['total_after']}")
        print(f"Deleted: {result['deleted_count']}, Compacted: {result['compacted_count']}")
        print(f"Compression: {result['compression_ratio']}")
        print(f"\nSummary:\n{result['summary']}")
        print(f"\nKept messages:")
        for msg in result['kept']:
            print(f"  [{msg['from']}] {msg['content'][:60]}")
