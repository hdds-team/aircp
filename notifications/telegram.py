#!/usr/bin/env python3
"""
AIRCP Telegram Notification Bridge — v1.0

Standalone module that sends push notifications to Telegram
when critical events happen in the AIRCP daemon.

Architecture:
    aircp_daemon.py
      └── telegram_notify(event, data)
           └── TelegramNotifier (singleton)
                └── async queue → POST api.telegram.org/bot.../sendMessage

Config (env vars, NEVER hardcoded):
    AIRCP_TELEGRAM_BOT_TOKEN   — Token from @BotFather
    AIRCP_TELEGRAM_CHAT_ID     — Target chat ID
    AIRCP_TELEGRAM_ENABLED     — Kill switch ("true"/"false", default "false")
    AIRCP_TELEGRAM_EVENTS      — Comma-separated event filter (default: all)

Events:
    review/approved     — Review reached required approvals
    review/changes      — Reviewer requested changes
    review/closed       — Review closed (timeout or consensus)
    workflow/phase      — Workflow phase transition
    workflow/complete   — Workflow completed
    task/stale          — Task marked stale (no response)
    agent/dead          — Agent heartbeat lost
    moderation/reject   — Auto-mod rejected a post (future)

Rate limiting: max 30 msg/min (Telegram API limit)
Retry: 1s, 5s, 30s (max 3 attempts)
Queue: background thread, non-blocking to daemon
"""

import os
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# =============================================================================
# Event → Message formatting
# =============================================================================

_EVENT_EMOJI = {
    "review/approved":  "✅",
    "review/changes":   "⚠️",
    "review/closed":    "📋",
    "workflow/phase":   "➡️",
    "workflow/complete": "🎉",
    "task/stale":       "🚨",
    "agent/dead":       "💀",
    "moderation/reject": "🛡️",
    "trust/drop":       "📉",
    "agent/registered":  "🆕",
}

def format_message(event: str, data: dict) -> str:
    """Format an event into a Telegram-friendly Markdown message.

    Returns a string using Telegram MarkdownV2-safe subset (we use HTML mode
    instead to avoid escaping hell).
    """
    emoji = _EVENT_EMOJI.get(event, "🔔")
    lines = [f"{emoji} <b>aIRCp</b> — <code>{event}</code>"]

    if event == "review/approved":
        rid = data.get("request_id", "?")
        approvals = data.get("approvals", "?")
        min_req = data.get("min_approvals", "?")
        file_path = data.get("file_path", "")
        lines.append(f"Review #{rid} approved ({approvals}/{min_req})")
        if file_path:
            lines.append(f"File: <code>{_escape_html(file_path)}</code>")

    elif event == "review/changes":
        rid = data.get("request_id", "?")
        reviewer = data.get("reviewer", "?")
        comment = data.get("comment", "")
        lines.append(f"Review #{rid}: {_escape_html(reviewer)} requests changes")
        if comment:
            lines.append(f"<i>{_escape_html(comment[:200])}</i>")

    elif event == "review/closed":
        rid = data.get("request_id", "?")
        consensus = data.get("consensus", "?")
        lines.append(f"Review #{rid} closed: <b>{_escape_html(consensus)}</b>")

    elif event == "workflow/phase":
        prev = data.get("previous_phase", "?")
        curr = data.get("current_phase", "?")
        timeout = data.get("timeout_minutes", "?")
        wf_id = data.get("workflow_id", "")
        wf_str = f" #{wf_id}" if wf_id else ""
        lines.append(f"Workflow{wf_str}: <code>@{prev}</code> → <code>@{curr}</code>")
        lines.append(f"Timeout: {timeout}min")

    elif event == "workflow/complete":
        wf_id = data.get("workflow_id", "?")
        duration = data.get("duration_minutes", "?")
        status = data.get("status", "done")
        lines.append(f"Workflow #{wf_id} completed ({status})")
        lines.append(f"Duration: {duration}min")

    elif event == "task/stale":
        count = data.get("count", "?")
        max_pings = data.get("max_pings", 3)
        lines.append(f"{count} task(s) marked stale (no response after {max_pings} pings)")

    elif event == "agent/dead":
        agent_id = data.get("agent_id", "?")
        last_seen = data.get("last_seen", "?")
        lines.append(f"Agent {_escape_html(agent_id)} appears down")
        lines.append(f"Last heartbeat: {last_seen}")

    elif event == "moderation/reject":
        agent_id = data.get("agent_id", "?")
        reason = data.get("reason", "auto-moderation")
        lines.append(f"Post rejected from {_escape_html(agent_id)}")
        lines.append(f"Reason: {_escape_html(reason[:200])}")

    elif event == "trust/drop":
        agent_id = data.get("agent_id", "?")
        old_score = data.get("old_score", "?")
        new_score = data.get("new_score", "?")
        reason = data.get("reason", "trust adjustment")
        lines.append(f"Trust drop: {_escape_html(agent_id)} {old_score} -> {new_score}")
        lines.append(f"Reason: {_escape_html(reason[:200])}")

    elif event == "agent/registered":
        agent_id = data.get("agent_id", "?")
        provider = data.get("provider", "unknown")
        model = data.get("model", "unknown")
        lines.append(f"New agent: {_escape_html(agent_id)} ({_escape_html(provider)}/{_escape_html(model)})")
        lines.append("Status: pending approval")

    else:
        # Generic fallback
        for k, v in list(data.items())[:5]:
            lines.append(f"{k}: {_escape_html(str(v)[:200])}")

    return "\n".join(lines)


def _escape_html(text: str) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# =============================================================================
# TelegramNotifier — singleton with async queue
# =============================================================================

class TelegramNotifier:
    """Non-blocking Telegram notification sender.

    Reads config from env vars. If not configured or disabled,
    all calls are silently no-ops.

    Thread-safe: uses a background worker thread + deque queue.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        """Singleton pattern — one notifier per process."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        # Config from env
        self.bot_token = os.environ.get("AIRCP_TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("AIRCP_TELEGRAM_CHAT_ID", "")
        self.enabled = os.environ.get("AIRCP_TELEGRAM_ENABLED", "false").lower() == "true"

        # Event filter: comma-separated list, empty = all events
        events_str = os.environ.get("AIRCP_TELEGRAM_EVENTS", "")
        self.event_filter = set(
            e.strip() for e in events_str.split(",") if e.strip()
        ) if events_str else set()  # empty = accept all

        # Rate limiting: max 30 msg/min (Telegram limit)
        self.rate_limit = 30
        self._send_timestamps: deque = deque(maxlen=self.rate_limit)

        # Retry config
        self.max_retries = 3
        self.retry_delays = [1, 5, 30]  # seconds

        # Async queue
        self._queue: deque = deque(maxlen=100)  # Drop oldest if backed up
        self._worker: threading.Thread = None
        self._stop_event = threading.Event()

        # Stats
        self.stats = {
            "sent": 0,
            "failed": 0,
            "dropped_rate_limit": 0,
            "dropped_filter": 0,
        }

        # Start worker if configured
        if self.enabled and self.bot_token and self.chat_id:
            self._start_worker()
            print(f"[TELEGRAM] Notifier enabled (chat_id={self.chat_id[:6]}...)")
            if self.event_filter:
                print(f"[TELEGRAM] Event filter: {', '.join(sorted(self.event_filter))}")
            else:
                print("[TELEGRAM] All events enabled")
        else:
            if self.enabled and (not self.bot_token or not self.chat_id):
                print("[TELEGRAM] ENABLED but missing BOT_TOKEN or CHAT_ID — disabled")
            else:
                print("[TELEGRAM] Notifier disabled (set AIRCP_TELEGRAM_ENABLED=true)")

    def _start_worker(self):
        """Start the background sender thread."""
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="telegram-notifier",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self):
        """Background loop: drain queue and send messages."""
        while not self._stop_event.is_set():
            try:
                if self._queue:
                    event, data, text = self._queue.popleft()
                    self._send_with_retry(text)
                else:
                    time.sleep(0.5)
            except Exception as e:
                print(f"[TELEGRAM] Worker error: {e}")
                time.sleep(2)

    def notify(self, event: str, data: dict):
        """Queue a notification (non-blocking).

        Args:
            event: Event type string (e.g. "review/approved")
            data: Event-specific data dict
        """
        if not self.enabled or not self.bot_token or not self.chat_id:
            return

        # Check event filter
        if self.event_filter and event not in self.event_filter:
            self.stats["dropped_filter"] += 1
            return

        # Format and enqueue
        text = format_message(event, data)
        # M3 fix: detect and log queue overflow (deque maxlen drops oldest silently)
        if len(self._queue) >= self._queue.maxlen:
            dropped_event = self._queue[0][0]  # oldest will be evicted
            self.stats["dropped_overflow"] = self.stats.get("dropped_overflow", 0) + 1
            print(f"[TELEGRAM] Queue full ({self._queue.maxlen}), dropping oldest: {dropped_event}")
        self._queue.append((event, data, text))  # deque handles eviction

    def _check_rate_limit(self) -> bool:
        """Return True if we can send (under rate limit)."""
        now = time.monotonic()
        # Remove timestamps older than 60s
        while self._send_timestamps and (now - self._send_timestamps[0]) > 60:
            self._send_timestamps.popleft()
        return len(self._send_timestamps) < self.rate_limit

    def _send_with_retry(self, text: str):
        """Send message to Telegram with retry logic."""
        for attempt in range(self.max_retries):
            # Rate limit check
            if not self._check_rate_limit():
                self.stats["dropped_rate_limit"] += 1
                print(f"[TELEGRAM] Rate limited, dropping message")
                return

            try:
                self._send_message(text)
                self._send_timestamps.append(time.monotonic())
                self.stats["sent"] += 1
                return
            except (URLError, HTTPError) as e:
                delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                print(f"[TELEGRAM] Send failed (attempt {attempt + 1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(delay)

        self.stats["failed"] += 1
        print(f"[TELEGRAM] Message dropped after {self.max_retries} retries")

    def _send_message(self, text: str):
        """HTTP POST to Telegram Bot API (stdlib only, no requests)."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")

        req = Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    body = resp.read().decode("utf-8", errors="replace")[:500]
                    print(f"[TELEGRAM] API error {resp.status}: {body}")
                    raise HTTPError(url, resp.status, f"Telegram API: {body}", {}, None)
        except HTTPError as e:
            # M5 fix: log the error response body from Telegram API
            if hasattr(e, "read"):
                body = e.read().decode("utf-8", errors="replace")[:500]
                print(f"[TELEGRAM] API HTTP {e.code}: {body}")
            raise

    def test(self) -> bool:
        """Send a test notification. Returns True on success."""
        text = "🔔 <b>aIRCp</b> — Test notification OK\n"
        text += f"Time: {datetime.now(timezone.utc).isoformat()}\n"
        text += f"Events: {', '.join(sorted(self.event_filter)) if self.event_filter else 'all'}"

        try:
            self._send_message(text)
            print("[TELEGRAM] Test message sent successfully")
            return True
        except Exception as e:
            print(f"[TELEGRAM] Test failed: {e}")
            return False

    def shutdown(self):
        """Stop the worker thread gracefully."""
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5)
        print("[TELEGRAM] Notifier shut down")

    def get_stats(self) -> dict:
        """Return notification statistics."""
        return {
            **self.stats,
            "queue_size": len(self._queue),
            "enabled": self.enabled,
            "configured": bool(self.bot_token and self.chat_id),
        }


# =============================================================================
# Module-level convenience function
# =============================================================================

_notifier: TelegramNotifier = None


def telegram_notify(event: str, data: dict):
    """Module-level convenience: send a notification.

    Safe to call even if Telegram is not configured (no-op).
    Lazy-initializes the singleton on first call.
    """
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    _notifier.notify(event, data)


# =============================================================================
# CLI — python -m aircp.notifications.telegram --test
# =============================================================================

def main():
    """CLI entry point for testing."""
    import argparse

    parser = argparse.ArgumentParser(
        description="AIRCP Telegram Notification Bridge"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Send a test notification"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show notification stats"
    )
    parser.add_argument(
        "--event", type=str, default=None,
        help="Send a specific test event (e.g. 'review/approved')"
    )
    args = parser.parse_args()

    notifier = TelegramNotifier()

    if not notifier.bot_token or not notifier.chat_id:
        print("ERROR: Missing env vars. Required:")
        print("  AIRCP_TELEGRAM_BOT_TOKEN")
        print("  AIRCP_TELEGRAM_CHAT_ID")
        print("  AIRCP_TELEGRAM_ENABLED=true")
        return

    if args.test:
        ok = notifier.test()
        exit(0 if ok else 1)

    elif args.event:
        # Send a fake event for testing
        sample_data = {
            "review/approved": {
                "request_id": 42, "approvals": 2,
                "min_approvals": 2, "file_path": "test.py",
            },
            "review/changes": {
                "request_id": 42, "reviewer": "@beta",
                "comment": "Fix the error handling in line 15",
            },
            "workflow/phase": {
                "previous_phase": "code", "current_phase": "review",
                "timeout_minutes": 30, "workflow_id": 7,
            },
            "workflow/complete": {
                "workflow_id": 7, "duration_minutes": 45, "status": "done",
            },
            "task/stale": {"count": 2, "max_pings": 3},
            "agent/dead": {"agent_id": "@theta", "last_seen": "2026-02-08 18:58:51"},
        }
        data = sample_data.get(args.event, {"info": "test event"})
        text = format_message(args.event, data)
        print(f"Sending: {args.event}")
        print(text)
        try:
            notifier._send_message(text)
            print("✅ Sent!")
        except Exception as e:
            print(f"❌ Failed: {e}")

    elif args.stats:
        stats = notifier.get_stats()
        for k, v in stats.items():
            print(f"  {k}: {v}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
