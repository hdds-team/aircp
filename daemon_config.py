"""Configuration constants for aircp daemon.

Phase 4 extraction: all pure config values (timers, thresholds, dispatch rules,
upload limits) moved from aircp_daemon.py. No runtime dependencies beyond
aircp_user_config for user identity.
"""

import os
from pathlib import Path

import aircp_user_config as _ucfg


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_AIRCP_HOME = os.environ.get("AIRCP_HOME", os.path.dirname(os.path.abspath(__file__)))

BRAINSTORM_CONFIG_PATH = Path(_AIRCP_HOME) / "brainstorm_config.toml"


# ---------------------------------------------------------------------------
# Compact engine thresholds
# ---------------------------------------------------------------------------

COMPACT_AUTO_THRESHOLD = 50    # Trigger compact after N messages
COMPACT_AUTO_INTERVAL = 1800   # Don't auto-compact more than once per 30min


# ---------------------------------------------------------------------------
# TaskManager (v0.7)
# ---------------------------------------------------------------------------

TASK_STALE_SECONDS = 60        # Ping agents after 60s of inactivity
TASK_WATCHDOG_INTERVAL = 30    # Check every 30s
TASK_MIN_PING_INTERVAL = 300   # Don't re-ping within 5 minutes
TASK_MAX_PINGS = 3             # Mark as stale after 3 pings without response

# v0.8: Lead wake-up
TASK_LEAD_WAKEUP_PINGS = 2    # Notify lead after this many pings without response
TASK_LEAD_ID = _ucfg.user()   # Who to notify when tasks are stuck
TASK_LEAD_STALE_MINUTES = 15  # Also notify lead if task inactive >15min total

# v4.3: Pending task reminder (unclaimed tasks)
TASK_PENDING_WARN_SECONDS = 600     # First ping after 10min unclaimed
TASK_PENDING_ESCALATE_SECONDS = 1800  # Escalate to lead after 30min unclaimed
TASK_PENDING_MAX_PINGS = 2          # Max pings before escalation (not stale -- just nudge)
TASK_PENDING_MIN_PING_INTERVAL = 600  # Don't re-ping pending within 10min


# ---------------------------------------------------------------------------
# Agent Heartbeat (v0.9)
# ---------------------------------------------------------------------------

AGENT_AWAY_SECONDS = 120       # Agent "away" after 2min without heartbeat
AGENT_DEAD_SECONDS = 300       # Agent "dead" after 5min without heartbeat
AGENT_HEARTBEAT_CHECK_INTERVAL = 60  # Check presence every 60s


# ---------------------------------------------------------------------------
# Brainstorm (v1.0)
# ---------------------------------------------------------------------------

BRAINSTORM_WATCHDOG_INTERVAL = 15   # Check deadlines every 15s
BRAINSTORM_REMINDER_INTERVAL = 60   # v1.2: Max reminder frequency
BRAINSTORM_MAX_REMINDERS = 3        # v1.2: Max reminders per session

HUMAN_AGENTS = _ucfg.human_ids()    # Excluded from brainstorm voting


# ---------------------------------------------------------------------------
# Workflow (v1.3)
# ---------------------------------------------------------------------------

WORKFLOW_WATCHDOG_INTERVAL = 30     # Check workflow timeouts every 30s


# ---------------------------------------------------------------------------
# Review system (v1.5 + v2.0 P7)
# ---------------------------------------------------------------------------

REVIEW_WATCHDOG_INTERVAL = 30       # Check review deadlines every 30s
REVIEW_REMINDER_SECONDS = 1800      # Legacy: DB-level reminder after 30min
REVIEW_TIMEOUT_SECONDS = 3600       # Auto-close after 1h
REVIEW_PING_DELAY = 120             # First ping after 2min
REVIEW_PING_INTERVAL = 120          # Subsequent pings every 2min
REVIEW_PING_MAX = 3                 # Max pings per review
REVIEW_ESCALATE_SECONDS = 300       # Escalate to #general after 5min


# ---------------------------------------------------------------------------
# Upload (Idea #19)
# ---------------------------------------------------------------------------

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
UPLOAD_MAX_BYTES = 10 * 1024 * 1024   # 10 MB
UPLOAD_BODY_MAX = 15 * 1024 * 1024    # 15 MB (base64 overhead + JSON wrapper)
UPLOAD_ALLOWED_MIME = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "application/pdf",
    "text/plain", "text/markdown", "text/csv",
    "application/json",
}


# ---------------------------------------------------------------------------
# Auto-Dispatcher (v0.4)
# ---------------------------------------------------------------------------

HUMAN_SENDERS = _ucfg.human_ids()

DISPATCH_RULES = {
    'alpha': ['code', 'implemente', 'implement', 'bug', 'fix', 'refactor',
              'patch', 'debug', 'error', 'crash', 'explore', 'rust', 'python'],
    'sonnet': ['analyse', 'analyze', 'synthese', 'resume', 'summarize',
               'compare', 'review', 'architecture', 'design', 'document'],
    'haiku': ['rapide', 'quick', 'c\'est quoi', 'what is', 'explique',
              'explain', 'definition', 'definition', 'triage'],
}

# v3.1: French stop words for language detection in #brainstorm
_FR_STOPWORDS = {
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles",
    "le", "la", "les", "un", "une", "des", "du", "au", "aux",
    "de", "et", "en", "est", "sont", "dans", "pour", "sur", "avec",
    "pas", "que", "qui", "ce", "cette", "ces", "mais", "ou", "donc",
    "car", "ni", "ne", "se", "sa", "son", "ses", "leur", "leurs",
    "mon", "ma", "mes", "ton", "ta", "tes", "notre", "votre",
    "aussi", "comme", "etre", "avoir", "fait", "faire", "peut",
    "plus", "tres", "bien", "tout", "tous", "toute", "toutes",
    "ca", "cela", "celui", "celle", "ceux", "celles",
    "quand", "comment", "pourquoi", "ou", "ici",
    "oui", "non", "merci", "alors", "encore", "deja", "meme",
    "voici", "voila", "apres", "avant", "entre", "depuis",
    "je suis", "c'est", "il y a", "on peut", "il faut",
}
_FR_THRESHOLD = 3  # Min French stop words to trigger


# ---------------------------------------------------------------------------
# CORS (v3.0)
# ---------------------------------------------------------------------------

ALLOWED_ORIGINS = {
    "https://aircp.dev",
    "https://www.aircp.dev",
    "http://localhost:4321",      # Astro dev server
    "http://localhost:3000",      # Dashboard dev
    "http://localhost:5173",      # Vite dev server (Svelte dashboard)
}
