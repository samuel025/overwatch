"""Find external dependencies (third-party APIs, SaaS, managed services) your services talk to.

Two clues, both read-only:
- container environment variables: services usually get external addresses as config
  (PAYMENT_API_URL=https://api.stripe.com, SMTP_HOST=smtp.sendgrid.net). Only the hostname is
  kept; values can contain credentials and are never stored or shown.
- recent error logs: failures usually name the host they could not reach
  ("POST https://psp.example.com/v1/charge returned 429").
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Iterable

_URL_HOST = re.compile(r"\b[a-z][a-z0-9+.-]*://(?:[^\s/@:]+(?::[^\s/@]*)?@)?([a-z0-9][a-z0-9.-]*[a-z0-9])", re.I)
_HOST_VAR = re.compile(r"(_HOST|_HOSTNAME|_ENDPOINT|_DOMAIN|_SERVER|_ADDR|_ADDRESS)$", re.I)
_BARE_HOST = re.compile(r"^([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)(?::\d+)?$", re.I)
_INTERNAL_SUFFIXES = (".local", ".internal", ".localdomain", ".svc", ".cluster.local", ".lan")


def _is_external(host: str, internal: set[str]) -> bool:
    h = host.lower().rstrip(".")
    if not h or h in internal or h in ("localhost",) or h.endswith(_INTERNAL_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(h)
        return False  # raw IPs: too ambiguous to name as a dependency
    except ValueError:
        pass
    return "." in h  # a bare name is almost always another container or a hostname alias


def hosts_in_env(env: Iterable[str]) -> dict[str, str]:
    """host -> the variable it came from (never the value)."""
    found: dict[str, str] = {}
    for item in env:
        key, _, value = item.partition("=")
        for m in _URL_HOST.finditer(value):
            found.setdefault(m.group(1).lower(), key)
        if _HOST_VAR.search(key):
            m = _BARE_HOST.match(value.strip())
            if m:
                found.setdefault(m.group(1).lower(), key)
    return found


def hosts_in_text(lines: Iterable[str]) -> dict[str, int]:
    """host -> how many lines mention it as a URL."""
    counts: dict[str, int] = {}
    for line in lines:
        for m in _URL_HOST.finditer(line):
            host = m.group(1).lower()
            counts[host] = counts.get(host, 0) + 1
    return counts


def merge_suggestions(env_hits: dict[str, dict[str, str]], log_hits: dict[str, dict[str, int]],
                      internal: set[str]) -> list[dict[str, Any]]:
    """env_hits: service -> {host: var}; log_hits: service -> {host: count}. One suggestion per host."""
    by_host: dict[str, dict[str, Any]] = {}
    for service, hosts in env_hits.items():
        for host, var in hosts.items():
            if _is_external(host, internal):
                s = by_host.setdefault(host, {"name": host, "callers": set(), "evidence": []})
                s["callers"].add(service)
                s["evidence"].append(f"{service} has it in the {var} setting")
    for service, hosts in log_hits.items():
        for host, n in hosts.items():
            if _is_external(host, internal):
                s = by_host.setdefault(host, {"name": host, "callers": set(), "evidence": []})
                if service:
                    s["callers"].add(service)
                s["evidence"].append(f"named in {n} recent error log line{'s' if n != 1 else ''}"
                                     + (f" from {service}" if service else ""))
    return [
        {"name": h, "callers": sorted(s["callers"]), "evidence": s["evidence"], "health_url": "",
         "log_text": h, "enabled": True, "suggested": True}
        for h, s in sorted(by_host.items())
    ]
