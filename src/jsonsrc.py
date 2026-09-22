"""Hardened loader for the `jsonsrc` query parameter (v0.5.3 H1, v0.5.6 S1).

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

v0.5.6 S1 — error-message oracle fix
------------------------------------
The layers above blocked the fetch but the *messages* still leaked which
layer fired: "could not resolve host X", "blocked host: X resolves to
private/loopback 10.1.2.3", "invalid JSON (content-type='text/html')".
That turns /gebco into an internal hostname-existence and reachability
oracle even though no upstream body is ever returned (see
`specs/security/2026-09-22_jsonsrc_ssrf_error_oracle_evidence.md`).

Every failure on the *remote* path now raises `RemoteJsonSrcError`, whose
public message is the fixed constant `PUBLIC_REMOTE_ERROR`. The failure
class lives in `.reason` / `.host` / `.detail`, which the caller emits to
the structured log and never to the client.

Inline-JSON failures keep their descriptive messages: they are computed
purely from the caller's own request body and create no remote oracle.
"remote disabled" likewise keeps its message — it is a constant of the
deployment, identical for every URL, so it tells an attacker nothing
about any host.

Public surface:
  * `load_jsonsrc(jsonsrc: str) -> object` — parsed JSON, or raises
    `JsonSrcError` (a subclass of `ValueError`).
  * `JsonSrcError.public_message` — the ONLY string that may reach a
    client. Use it instead of `str(exc)` at the response boundary.
  * `JsonSrcError.log_fields()` — bounded, non-secret diagnostics for
    the server-side structured log.

Residual risk — DNS rebinding (host resolves "safely" during the
allowlist check but to a private address at connect time) is NOT
mitigated: the validated address is not pinned to the socket that
`requests` subsequently opens. See spec §6.5 and
`specs/security/2026-09-22_jsonsrc_ssrf_error_oracle_evidence.md` §3.2.
"""
from __future__ import annotations

import ipaddress
import json
import socket
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests

import src.config as config

# The single public string returned for EVERY remote-path failure.
PUBLIC_REMOTE_ERROR = "jsonsrc could not be retrieved"

# Bounds for values that end up in a log line. Hostnames and upstream
# content types are attacker-controlled; they are truncated here and
# JSON-encoded by the caller, so they cannot forge a log record.
_MAX_HOST_CHARS = 253    # RFC 1035 maximum name length
_MAX_DETAIL_CHARS = 120


def _bounded(value: object, limit: int) -> Optional[str]:
    """Truncate `value` to `limit` chars and drop non-printable bytes.

    Control characters are what a log-injection attempt needs; the
    caller additionally `json.dumps()`es the record, so this is
    defence in depth rather than the only guard.
    """
    if value is None:
        return None
    text = str(value)[:limit]
    if not text.isprintable():
        text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text


class JsonSrcError(ValueError):
    """Raised by `load_jsonsrc()` on any rejected input.

    `public_message` is the only attribute safe to return to a client.
    `reason` / `host` / `detail` are internal diagnostics.
    """

    def __init__(
        self,
        public_message: str,
        *,
        reason: Optional[str] = None,
        host: Optional[str] = None,
        detail: object = None,
    ) -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.reason = reason
        self.host = _bounded(host, _MAX_HOST_CHARS)
        self.detail = _bounded(detail, _MAX_DETAIL_CHARS)

    def log_fields(self) -> dict:
        """Bounded diagnostics for the structured server-side log."""
        fields: dict = {"jsonsrc_reason": self.reason}
        if self.host is not None:
            fields["jsonsrc_host"] = self.host
        if self.detail is not None:
            fields["jsonsrc_detail"] = self.detail
        return fields


class RemoteJsonSrcError(JsonSrcError):
    """A failure on the remote-URL path — always the same public message."""

    def __init__(
        self,
        reason: str,
        *,
        host: Optional[str] = None,
        detail: object = None,
    ) -> None:
        super().__init__(
            PUBLIC_REMOTE_ERROR, reason=reason, host=host, detail=detail
        )


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


def _normalize_host(raw: Optional[str]) -> tuple[str, bool]:
    """Canonicalise a URL hostname *before* any SSRF decision.

    Returns `(host, is_ip_literal)`.

    v0.5.6 S1: `host.` and `host` are the same DNS name, and DNS labels
    are case-insensitive, so both must reach the same guard branch — a
    string-compare guard that misses `EXSE-001.NTU.INTERNAL.` would be a
    bypass. IDN hostnames are folded to punycode so the name the guard
    inspects is the name `requests` will later put on the wire.

    IP literals are returned in canonical form and flagged, so the caller
    can check them directly instead of round-tripping through DNS.
    """
    host = (raw or "").strip()
    # A single trailing dot is the DNS root; strip any number of them.
    host = host.rstrip(".")
    if not host:
        raise RemoteJsonSrcError("missing_host")

    # IPv4 / IPv6 literal (urlparse already stripped the [] from v6).
    try:
        return str(ipaddress.ip_address(host)), True
    except ValueError:
        pass

    host = host.casefold()
    try:
        # ToASCII per label: unicode -> punycode, punycode stays put.
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError) as exc:
        raise RemoteJsonSrcError(
            "invalid_host", detail=type(exc).__name__
        ) from exc
    return host.lower(), False


def _resolved_addresses(host: str) -> Iterable[str]:
    """Yield every IP address `getaddrinfo` returns for `host`.

    Iterating all records (not just the first) is necessary so that a
    multi-record DNS response with a public AND a private record is
    correctly rejected.
    """
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RemoteJsonSrcError(
            "dns_failure", host=host, detail=f"gaierror errno={exc.errno}"
        ) from exc
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


def _check_host(host: str, is_ip_literal: bool) -> None:
    """SSRF guard. `host` must already be normalised."""
    if host == "localhost":
        raise RemoteJsonSrcError("blocked_localhost", host=host)

    if is_ip_literal:
        # The literal IS the destination; no DNS round-trip needed, and
        # the address checked is exactly the address dialled.
        addrs = [host]
    else:
        addrs = list(_resolved_addresses(host))
        if not addrs:
            raise RemoteJsonSrcError("dns_no_records", host=host)

    for addr in addrs:
        if _is_private(addr):
            raise RemoteJsonSrcError(
                "blocked_private_address", host=host, detail=f"addr={addr}"
            )


def _fetch_url(url: str) -> object:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RemoteJsonSrcError(
            "unsupported_scheme", detail=f"scheme={parsed.scheme[:32]!r}"
        )
    if not parsed.hostname:
        raise RemoteJsonSrcError("missing_host")

    # v0.5.6 S1 — normalise BEFORE the guard, not after.
    host, is_ip_literal = _normalize_host(parsed.hostname)
    _check_host(host, is_ip_literal)

    timeout = (config.JSONSRC_CONNECT_TIMEOUT_S, config.JSONSRC_READ_TIMEOUT_S)
    max_bytes = config.JSONSRC_MAX_BYTES

    try:
        with requests.get(
            url,
            stream=True,
            timeout=timeout,
            allow_redirects=False,  # H1 layer 5
        ) as resp:
            if 300 <= resp.status_code < 400:
                raise RemoteJsonSrcError(
                    "redirect_blocked", host=host,
                    detail=f"status={resp.status_code}",
                )
            if resp.status_code >= 400:
                # Replaces resp.raise_for_status(): same rejection, but
                # the status stays internal instead of reaching the client.
                raise RemoteJsonSrcError(
                    "http_error", host=host, detail=f"status={resp.status_code}"
                )

            ctype = resp.headers.get("content-type", "")
            # Content-type is a hint, not a gate (server-controlled).

            # Streamed size cap (H1 layer 6) — never trust Content-Length.
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise RemoteJsonSrcError(
                        "oversized_response", host=host,
                        detail=f"limit={max_bytes}",
                    )
            try:
                text = bytes(buf).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RemoteJsonSrcError(
                    "invalid_encoding", host=host, detail=type(exc).__name__
                ) from exc
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                # The upstream body NEVER appears in a message; the
                # content-type hint is kept for operators only.
                raise RemoteJsonSrcError(
                    "invalid_json", host=host, detail=f"content_type={ctype[:64]!r}"
                ) from exc
    except requests.exceptions.Timeout as exc:
        raise RemoteJsonSrcError(
            "timeout", host=host, detail=type(exc).__name__
        ) from exc
    except requests.exceptions.RequestException as exc:
        # Connection refused / reset / TLS failure / chunked-encoding
        # error. Pre-v0.5.6 these escaped as IOError and surfaced as a
        # 500, which was itself an oracle.
        raise RemoteJsonSrcError(
            "connection_error", host=host, detail=type(exc).__name__
        ) from exc


def load_jsonsrc(jsonsrc: str) -> object:
    """Parse `jsonsrc` — either an inline JSON string or a URL.

    Returns the parsed JSON object on success. Raises `JsonSrcError`
    (subclass of `ValueError`) on any failure. Callers must return
    `exc.public_message` to the client, never `str(exc)` of a wrapped
    upstream exception.
    """
    if jsonsrc is None or jsonsrc == "":
        raise JsonSrcError("jsonsrc is empty", reason="empty_input")

    # H1 layer 1: inline-first dispatch. JSON objects/arrays start with
    # { or [ after whitespace; anything else is treated as a URL.
    stripped = jsonsrc.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(jsonsrc)
        except json.JSONDecodeError as exc:
            # Inline input is the caller's own payload: echoing the parse
            # position leaks nothing about our network.
            raise JsonSrcError(
                f"inline jsonsrc is not valid JSON: {exc}",
                reason="inline_invalid_json",
            ) from exc

    if not config.JSONSRC_ALLOW_REMOTE:
        # Deployment constant, identical for every URL -> not an oracle.
        raise JsonSrcError(
            "remote jsonsrc is disabled (GEBCO_JSONSRC_ALLOW_REMOTE=false)",
            reason="remote_disabled",
        )

    return _fetch_url(jsonsrc)
