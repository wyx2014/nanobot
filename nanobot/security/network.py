"""Network security utilities — SSRF protection and internal URL detection."""

from __future__ import annotations

import ipaddress
import re
import socket
from contextlib import suppress
from contextvars import ContextVar, Token
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # unique local
    ipaddress.ip_network("fe80::/10"),         # link-local v6
]

# Explicitly configured MCP services may live on LANs, VPNs, or localhost.
# The exception is scoped to their origin; link-local/metadata stays blocked.
_CONFIGURED_SERVICE_NETWORKS = [
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "172.16.0.0/12",
        "192.168.0.0/16", "::1/128", "fc00::/7",
    )
]
_CLOUD_METADATA_ADDRESSES = {
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)

_allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
_configured_mcp_origins: frozenset[tuple[str, str, int]] = frozenset()
_domain_policy: ContextVar[tuple[bool, tuple[str, ...], tuple[str, ...]] | None] = (
    ContextVar("nanobot_network_domain_policy", default=None)
)


def set_network_policy(
    *,
    block_all: bool,
    allow_domains: list[str],
    deny_domains: list[str],
) -> Token:
    """Apply one tool call's domain policy without leaking it across tasks."""
    return _domain_policy.set((block_all, tuple(allow_domains), tuple(deny_domains)))


def reset_network_policy(token: Token) -> None:
    _domain_policy.reset(token)


def _domain_matches(hostname: str, rules: tuple[str, ...]) -> bool:
    host = hostname.lower().rstrip(".")
    for raw in rules:
        rule = raw.lower().rstrip(".")
        if rule.startswith("*."):
            rule = rule[2:]
            if host.endswith("." + rule):
                return True
            continue
        if host == rule or host.endswith("." + rule):
            return True
    return False


def validate_configured_network_target(url: str) -> tuple[bool, str]:
    """Enforce the active application-level domain policy for a URL."""
    policy = _domain_policy.get()
    if policy is None:
        return True, ""
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    if not hostname:
        return True, ""
    block_all, allow_domains, deny_domains = policy
    if _domain_matches(hostname, deny_domains):
        return False, f"Blocked by denied domain rule: {hostname}"
    if block_all and not _domain_matches(hostname, allow_domains):
        return False, f"Blocked by default network policy: {hostname}"
    return True, ""


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """Allow specific CIDR ranges to bypass SSRF blocking (e.g. Tailscale's 100.64.0.0/10)."""
    global _allowed_networks
    nets = []
    for cidr in cidrs:
        with suppress(ValueError):
            nets.append(ipaddress.ip_network(cidr, strict=False))
    _allowed_networks = nets


def _url_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
            return parsed.scheme, parsed.hostname.lower().rstrip("."), port
    except ValueError:
        pass
    return None


def configure_mcp_service_urls(urls: list[str]) -> None:
    """Replace trusted origins from enabled remote MCP configuration; discard secrets."""
    global _configured_mcp_origins
    _configured_mcp_origins = frozenset(origin for url in urls if (origin := _url_origin(url)))


def _is_configured_origin(url: str, configured_service_url: str | None = None) -> bool:
    origin = _url_origin(url)
    if origin is None:
        return False
    if configured_service_url is not None:
        # A transport's requests must stay scoped to its own service, including redirects.
        return origin == _url_origin(configured_service_url)
    return origin in _configured_mcp_origins


def _is_configured_service_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    normalized = _normalize_addr(addr)
    return normalized not in _CLOUD_METADATA_ADDRESSES and any(
        normalized in net for net in _CONFIGURED_SERVICE_NETWORKS
    )


def _normalize_addr(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Normalize IPv6-mapped IPv4 addresses to their IPv4 form.

    ``::ffff:127.0.0.1`` is semantically identical to ``127.0.0.1`` but
    Python's ipaddress treats it as an IPv6Address that matches neither
    ``127.0.0.0/8`` nor ``::1/128``.  Converting it to IPv4 ensures
    blocklist/allowlist checks work correctly.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    normalized = _normalize_addr(addr)
    if _allowed_networks and any(normalized in net for net in _allowed_networks):
        return False
    return any(normalized in net for net in _BLOCKED_NETWORKS)


def validate_url_target(
    url: str, *, allow_loopback: bool = False, configured_service_url: str | None = None,
) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    ``allow_loopback`` is intentionally narrow: it only permits literal
    loopback hosts (localhost, 127.0.0.0/8, ::1) when every resolved address is
    loopback. It does not allow RFC1918, link-local, metadata, or public DNS
    names that happen to resolve to loopback.

    ``configured_service_url`` permits LAN/VPN/loopback addresses only for
    that explicitly configured service's scheme, hostname, and port. Other
    destinations, including redirects, retain the default SSRF checks.
    Without it, enabled remote MCP origins loaded from config are recognized
    by the shared web and command guards as well.

    Returns (ok, error_message).  When ok is True, error_message is empty.
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "Missing domain"

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"

    policy_ok, policy_error = validate_configured_network_target(url)
    if not policy_ok:
        return False, policy_error

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        addrs.append(addr)
    if allow_loopback and _is_allowed_loopback_target(hostname, addrs):
        return True, ""
    configured_origin = _is_configured_origin(url, configured_service_url)
    for addr in addrs:
        if _is_private(addr):
            if configured_origin and _is_configured_service_address(addr):
                continue
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}"

    return True, ""


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """Validate an already-fetched URL (e.g. after redirect). Only checks the IP, skips DNS."""
    try:
        p = urlparse(url)
    except Exception:
        return True, ""

    hostname = p.hostname
    if not hostname:
        return True, ""

    policy_ok, policy_error = validate_configured_network_target(url)
    if not policy_ok:
        return False, policy_error

    configured_origin = _is_configured_origin(url)
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private(addr) and not (configured_origin and _is_configured_service_address(addr)):
            return False, f"Redirect target is a private address: {addr}"
    except ValueError:
        # hostname is a domain name, resolve it
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return True, ""
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_private(addr) and not (configured_origin and _is_configured_service_address(addr)):
                return False, f"Redirect target {hostname} resolves to private address {addr}"

    return True, ""


def contains_internal_url(command: str, *, allow_loopback: bool = False) -> bool:
    """Return True if the command string contains a URL targeting an internal/private address."""
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, _ = validate_url_target(url, allow_loopback=allow_loopback)
        if not ok:
            return True
    return False


def _is_allowed_loopback_target(
    hostname: str,
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
    if not addrs or not all(_normalize_addr(addr).is_loopback for addr in addrs):
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(hostname).is_loopback
    return False
