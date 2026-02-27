# AIRCP Notifications — v1.0
# Pluggable notification backends for the AIRCP daemon.
#
# Usage:
#   from notifications.telegram import TelegramNotifier
#   notifier = TelegramNotifier()  # reads env vars
#   notifier.notify("review/approved", {"request_id": 8, "approvals": 2})

from .telegram import TelegramNotifier, telegram_notify

__all__ = ["TelegramNotifier", "telegram_notify"]
