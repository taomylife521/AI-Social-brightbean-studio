"""Shared validation utilities."""

import ipaddress
import socket
from urllib.parse import urlparse

MAX_TAGS = 25
MAX_TAG_LENGTH = 100

MAX_YT_TAGS_TOTAL_CHARS = 500
MAX_YT_TAG_LENGTH = 50


def is_safe_url(url: str) -> bool:
    """Validate that a URL is http(s) and does not resolve to a private/reserved IP.

    Returns False for non-http(s) schemes (file://, gopher://, etc.) and for URLs
    whose hostname resolves to any private, reserved, loopback, or link-local
    address. Checks every resolved address to defend against DNS responses that
    include multiple A/AAAA records.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False

        addr_infos = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
        for _family, _, _, _, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                return False

        return True
    except (socket.gaierror, ValueError, OSError):
        return False


def normalize_tags(raw) -> list[str]:
    """Strict tag normalization for JSON API endpoints.

    Raises ValueError on malformed input or overflow — caller returns HTTP 400.
    """
    if not isinstance(raw, list):
        raise ValueError("tags must be a list")
    if len(raw) > MAX_TAGS:
        raise ValueError(f"too many tags (max {MAX_TAGS})")
    out: list[str] = []
    seen: set[str] = set()
    for t in raw:
        if not isinstance(t, str):
            raise ValueError("each tag must be a string")
        t = t.strip()
        if not t:
            continue
        if len(t) > MAX_TAG_LENGTH:
            raise ValueError(f"tag too long (max {MAX_TAG_LENGTH} chars)")
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def parse_and_truncate_tag_string(raw: str) -> list[str]:
    """Lenient tag normalization for HTML form POSTs.

    Splits a comma-separated string, strips whitespace, truncates each tag to
    MAX_TAG_LENGTH, deduplicates, and caps total count at MAX_TAGS. Excess input
    is silently dropped — render-time HTML escape is the security boundary.
    """
    if not raw:
        return []
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for t in parts:
        if len(out) >= MAX_TAGS:
            break
        t = t[:MAX_TAG_LENGTH]
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def parse_and_truncate_youtube_tag_string(raw: str) -> list[str]:
    """YouTube-specific tag normalization for HTML form POSTs.

    YouTube's Data API caps total tag-list characters at 500 (delimiters counted);
    we cap individual tag length at MAX_YT_TAG_LENGTH as a defensive bound. Excess
    input is silently dropped.
    """
    if not raw:
        return []
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for t in parts:
        t = t[:MAX_YT_TAG_LENGTH]
        if t in seen:
            continue
        cost = len(t) + (1 if out else 0)
        if total + cost > MAX_YT_TAGS_TOTAL_CHARS:
            break
        seen.add(t)
        out.append(t)
        total += cost
    return out
