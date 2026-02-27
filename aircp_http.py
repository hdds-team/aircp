"""Safe HTTP helpers for AIRCP internal communication.

Wraps urllib.request.urlopen with scheme validation to prevent
file:// and other non-HTTP schemes (Bandit B310 defense).
"""
import urllib.request

_ALLOWED_SCHEMES = ("http://", "https://")


def safe_urlopen(req, *, timeout=10):
    """urlopen wrapper that rejects non-HTTP schemes.

    Args:
        req: urllib.request.Request object or URL string.
        timeout: Socket timeout in seconds.

    Raises:
        ValueError: If the URL scheme is not http:// or https://.
    """
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    if not url.startswith(_ALLOWED_SCHEMES):
        raise ValueError(f"Blocked URL scheme (only HTTP/HTTPS allowed): {url[:60]}")
    return urllib.request.urlopen(req, timeout=timeout)
