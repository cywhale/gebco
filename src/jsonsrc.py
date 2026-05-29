"""Hardened loader for the `jsonsrc` query parameter (v0.5.3 H1).

Pre-v0.5.3 the API did `requests.get(jsonsrc)` with no timeout, no scheme
restriction, no host restriction, and no response-size cap. For a public
service that's a DoS surface (slow URL stalls a worker, oversized
response exhausts memory) and an SSRF surface (an attacker can probe
the internal network via the API).

This module implements the seven-layer defence described in
`specs/v0.5.3_hardening_checklist_v2.md` H1, default-allow:

  1. Inline-first dispatch    (cuts wasted HTTP attempt on inline JSON)
  2. Scheme allowlist          (http / https only)
  3. Host allowlist + private-IP block (incl. IPv4-mapped IPv6)
  4. Connect + read timeouts   (config.JSONSRC_*_TIMEOUT_S)
  5. No-redirect               (allow_redirects=False)
  6. Streamed size cap         (config.JSONSRC_MAX_BYTES, header ignored)
  7. Content-type hint + JSON parse

Public surface: `load_jsonsrc(jsonsrc: str) -> object` — returns the
parsed JSON object, or raises `JsonSrcError` (a subclass of `ValueError`)
on any failure. The exception message is safe to include in a 400
response body.

Residual risk — DNS rebinding (host resolves "safely" during the
allowlist check but to a private address at connect time) is NOT
mitigated; see spec §6.5.
"""
from __future__ import annotations

import ipaddress
import json
import socket
from typing import Iterable

import requests

import src.config as config


class JsonSrcError(ValueError):
    """Raised by `load_jsonsrc()` on any rejected input."""


_PRIVATE_NETS_V4: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("0.0.0.0/8"),  # belt-and-suspenders against 0.0.0.0
)

_PRIVATE_NETS_V6: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),     # unique local
    ipaddress.IPv6Network("fe80::/10"),    # link-local
    ipaddress.IPv6Network("::/128"),       # unspecified
)


def _is_private(addr: str) -> bool:
    """Return True if `addr` is in any blocked CIDR.

    Also normalises IPv4-mapped IPv6 addresses (`::ffff:127.0.0.1`)
    via `ipv4_mapped` before checking against the IPv4 blocklist.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        # `getaddrinfo` shouldn't yield non-IP literals, but be safe.
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return any(mapped in net for net in _PRIVATE_NETS_V4)
        return any(ip in net for net in _PRIVATE_NETS_V6)
    return any(ip in net for net in _PRIVATE_NETS_V4)


def _resolved_addresses(host: str) -> Iterable[str]:
    """Yield every IP address `getaddrinfo` returns for `host`.

    Iterating all records (not just the first) is necessary so that a
    multi-record DNS response with a public AND a private record is
    correctly rejected.
    """
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise JsonSrcError(f"could not resolve host {host!r}: {exc}") from exc
    seen: set[str] = set()
    for record in records:
        family, _socktype, _proto, _canon, sockaddr = record
        if family == socket.AF_INET:
            addr = sockaddr[0]
        elif family == socket.AF_INET6:
            addr = sockaddr[0]
        else:
            continue
        if addr not in seen:
            seen.add(addr)
            yield addr


def _check_host(host: str) -> None:
    if not host:
        raise JsonSrcError("URL has no host")
    if host.lower() == "localhost":
        raise JsonSrcError("blocked host: localhost")
    addrs = list(_resolved_addresses(host))
    if not addrs:
        raise JsonSrcError(f"could not resolve host {host!r}")
    for addr in addrs:
        if _is_private(addr):
            raise JsonSrcError(
                f"blocked host: {host!r} resolves to private/loopback {addr}"
            )


def _fetch_url(url: str) -> object:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise JsonSrcError(f"unsupported scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise JsonSrcError("URL has no hostname")
    _check_host(parsed.hostname)

    timeout = (config.JSONSRC_CONNECT_TIMEOUT_S, config.JSONSRC_READ_TIMEOUT_S)
    max_bytes = config.JSONSRC_MAX_BYTES

    with requests.get(
        url,
        stream=True,
        timeout=timeout,
        allow_redirects=False,  # H1 layer 5
    ) as resp:
        if 300 <= resp.status_code < 400:
            raise JsonSrcError(
                f"redirect not allowed (got {resp.status_code} from upstream)"
            )
        resp.raise_for_status()

        ctype = resp.headers.get("content-type", "")
        # Content-type is a hint, not a gate (server-controlled). Logged
        # via the structured logger by the caller if the response parses.

        # Streamed size cap (H1 layer 6) — never trust Content-Length.
        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise JsonSrcError(
                    f"jsonsrc response too large: > {max_bytes} bytes"
                )
        try:
            return json.loads(buf.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JsonSrcError(
                f"jsonsrc URL returned invalid JSON "
                f"(content-type={ctype!r}): {exc}"
            ) from exc


def load_jsonsrc(jsonsrc: str) -> object:
    """Parse `jsonsrc` — either an inline JSON string or a URL.

    Returns the parsed JSON object on success. Raises `JsonSrcError`
    (subclass of `ValueError`) on any failure; the message is safe to
    include in a 400 response body.
    """
    if jsonsrc is None or jsonsrc == "":
        raise JsonSrcError("jsonsrc is empty")

    # H1 layer 1: inline-first dispatch. JSON objects/arrays start with
    # { or [ after whitespace; anything else is treated as a URL.
    stripped = jsonsrc.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(jsonsrc)
        except json.JSONDecodeError as exc:
            raise JsonSrcError(f"inline jsonsrc is not valid JSON: {exc}") from exc

    if not config.JSONSRC_ALLOW_REMOTE:
        raise JsonSrcError(
            "remote jsonsrc is disabled (GEBCO_JSONSRC_ALLOW_REMOTE=false)"
        )

    return _fetch_url(jsonsrc)
