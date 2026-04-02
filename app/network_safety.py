from __future__ import annotations

import ipaddress
from urllib.parse import quote_plus, urlparse


_ALLOWED_WEB_SCHEMES = frozenset({"https"})
_ALLOWED_BROWSER_SEARCH_HOST = "www.google.com"


def validate_public_https_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("URL is required.")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in _ALLOWED_WEB_SCHEMES:
        raise ValueError("Only HTTPS URLs are allowed.")
    if not parsed.hostname:
        raise ValueError("URL host is required.")
    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast):
        raise ValueError("Private or loopback destinations are not allowed.")
    return parsed.geturl()


def build_browser_search_url(query: str) -> str:
    text = (query or "").strip()
    if not text:
        raise ValueError("Search query is required.")
    return f"https://{_ALLOWED_BROWSER_SEARCH_HOST}/search?q={quote_plus(text)}"
