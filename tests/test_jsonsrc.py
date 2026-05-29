"""Tests for src.jsonsrc — v0.5.3 H1.

Mocks `socket.getaddrinfo` and `requests.get` (via `pytest-mock`) so the
tests don't need network and don't depend on production hosts.
"""
from __future__ import annotations

import json
import socket
from unittest.mock import MagicMock

import pytest

import src.config as config
from src.jsonsrc import JsonSrcError, load_jsonsrc


# -------- inline-first dispatch ------------------------------------------

def test_inline_json_object_parsed_without_network(mocker):
    """Layer 1: inline JSON should NOT trigger any HTTP call."""
    mocked_get = mocker.patch("src.jsonsrc.requests.get")
    obj = load_jsonsrc('{"longitude":[1,2],"latitude":[3,4]}')
    assert obj == {"longitude": [1, 2], "latitude": [3, 4]}
    mocked_get.assert_not_called()


def test_inline_json_array_parsed_without_network(mocker):
    mocked_get = mocker.patch("src.jsonsrc.requests.get")
    obj = load_jsonsrc("[1,2,3]")
    assert obj == [1, 2, 3]
    mocked_get.assert_not_called()


def test_inline_whitespace_then_brace(mocker):
    mocked_get = mocker.patch("src.jsonsrc.requests.get")
    obj = load_jsonsrc('  \n {"a":1}')
    assert obj == {"a": 1}
    mocked_get.assert_not_called()


def test_inline_bad_json_rejected(mocker):
    mocker.patch("src.jsonsrc.requests.get")
    with pytest.raises(JsonSrcError, match="inline jsonsrc"):
        load_jsonsrc("{not json}")


# -------- SSRF guard -----------------------------------------------------

def _gai_for(addr: str, family: int = socket.AF_INET):
    """Build a fake getaddrinfo return record."""
    if family == socket.AF_INET:
        return (family, socket.SOCK_STREAM, 0, "", (addr, 0))
    return (family, socket.SOCK_STREAM, 0, "", (addr, 0, 0, 0))


def test_block_literal_localhost(mocker):
    mocker.patch("src.jsonsrc.socket.getaddrinfo")  # never reached
    with pytest.raises(JsonSrcError, match="localhost"):
        load_jsonsrc("http://localhost/anything")


def test_block_loopback_ip(mocker):
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("127.0.0.1")],
    )
    with pytest.raises(JsonSrcError, match="private/loopback"):
        load_jsonsrc("http://127-0-0-1.nip.io/x")


def test_block_private_10_dot(mocker):
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("10.0.0.5")],
    )
    with pytest.raises(JsonSrcError, match="private/loopback"):
        load_jsonsrc("http://intranet.example/x")


def test_block_link_local(mocker):
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("169.254.169.254")],  # AWS metadata
    )
    with pytest.raises(JsonSrcError, match="private/loopback"):
        load_jsonsrc("http://meta.example/x")


def test_block_ipv4_mapped_ipv6(mocker):
    """Codex review: ::ffff:127.0.0.1 must be blocked."""
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("::ffff:127.0.0.1", family=socket.AF_INET6)],
    )
    with pytest.raises(JsonSrcError, match="private/loopback"):
        load_jsonsrc("http://[::ffff:127.0.0.1]/x")


def test_block_when_one_of_multiple_addresses_is_private(mocker):
    """Iterate-all-records: if ANY record is private, reject the whole host."""
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[
            _gai_for("8.8.8.8"),         # public
            _gai_for("127.0.0.1"),       # private
        ],
    )
    with pytest.raises(JsonSrcError, match="private/loopback"):
        load_jsonsrc("http://multihome.example/x")


def test_dns_failure_rejected(mocker):
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        side_effect=socket.gaierror("nxdomain"),
    )
    with pytest.raises(JsonSrcError, match="could not resolve"):
        load_jsonsrc("http://no-such-host.invalid/x")


# -------- scheme allowlist ------------------------------------------------

def test_block_file_scheme():
    with pytest.raises(JsonSrcError, match="unsupported scheme"):
        load_jsonsrc("file:///etc/passwd")


def test_block_gopher_scheme():
    with pytest.raises(JsonSrcError, match="unsupported scheme"):
        load_jsonsrc("gopher://example/")


# -------- no-redirect -----------------------------------------------------

def _public_addr_mock(mocker):
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("8.8.8.8")],
    )


def _response_mock(*, status_code=200, headers=None, body=b'{"ok":true}'):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {"content-type": "application/json"}
    resp.raise_for_status = MagicMock()
    resp.iter_content = MagicMock(return_value=iter([body]))
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_3xx_redirect_rejected(mocker):
    _public_addr_mock(mocker)
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(
            status_code=302,
            headers={"location": "http://evil.example/", "content-type": "text/html"},
        ),
    )
    with pytest.raises(JsonSrcError, match="redirect"):
        load_jsonsrc("http://example.org/poly.json")


# -------- response size cap (streamed) ------------------------------------

def test_size_cap_exceeded_rejected(mocker, monkeypatch):
    """Streamed size cap; never trusts Content-Length header."""
    monkeypatch.setattr(config, "JSONSRC_MAX_BYTES", 100)
    _public_addr_mock(mocker)

    # Body of 200 bytes in 2 chunks. The header lies "Content-Length: 1".
    big_chunks = [b"x" * 64, b"x" * 64, b"x" * 72]
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(
            headers={"content-type": "application/json", "content-length": "1"},
            body=b"",  # placeholder; iter_content overridden below
        ),
    )
    # Re-stub iter_content to yield real chunks.
    from src.jsonsrc import requests as _r
    _r.get.return_value.iter_content = MagicMock(return_value=iter(big_chunks))
    with pytest.raises(JsonSrcError, match="too large"):
        load_jsonsrc("http://example.org/poly.json")


def test_under_cap_succeeds(mocker, monkeypatch):
    monkeypatch.setattr(config, "JSONSRC_MAX_BYTES", 1000)
    _public_addr_mock(mocker)
    body = json.dumps({"longitude": [1.0], "latitude": [2.0]}).encode()
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(body=body),
    )
    out = load_jsonsrc("http://example.org/poly.json")
    assert out == {"longitude": [1.0], "latitude": [2.0]}


# -------- content-type as hint only --------------------------------------

def test_text_html_content_type_accepted_if_body_is_valid_json(mocker):
    """Server-controlled headers are a hint, not a gate."""
    _public_addr_mock(mocker)
    body = json.dumps({"longitude": [1.0], "latitude": [2.0]}).encode()
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(
            headers={"content-type": "text/html"},
            body=body,
        ),
    )
    out = load_jsonsrc("http://example.org/poly.json")
    assert out == {"longitude": [1.0], "latitude": [2.0]}


# -------- escape hatch --------------------------------------------------

def test_allow_remote_false_blocks_url(mocker, monkeypatch):
    monkeypatch.setattr(config, "JSONSRC_ALLOW_REMOTE", False)
    with pytest.raises(JsonSrcError, match="disabled"):
        load_jsonsrc("http://example.org/poly.json")


def test_allow_remote_false_does_not_block_inline(mocker, monkeypatch):
    monkeypatch.setattr(config, "JSONSRC_ALLOW_REMOTE", False)
    out = load_jsonsrc('{"a":1}')
    assert out == {"a": 1}
