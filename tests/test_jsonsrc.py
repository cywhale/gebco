"""Tests for src.jsonsrc — v0.5.3 H1 + v0.5.7 S1 (SSRF error-oracle fix).

Mocks `socket.getaddrinfo` and `requests.get` (via `pytest-mock`) so the
tests don't need network and don't depend on production hosts.

Two layers are covered here:

  * the loader contract — every remote failure raises `JsonSrcError`
    whose `public_message` is the fixed `PUBLIC_REMOTE_ERROR`, while the
    failure class stays in `.reason` / `.host` / `.detail`;
  * the *public endpoint* contract — `gebco_app.gebco()` must return the
    same status, the same body and the same byte length for a non-JSON
    public URL, an unresolvable host and a blocked loopback target, per
    specs/security/2026-09-22_jsonsrc_ssrf_error_oracle_evidence.md §4.
"""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient

# `gebco_app` is imported at module scope (as tests/test_line_observability.py
# does) because the endpoint-contract tests below call the handler directly.
# Importing it lazily inside a fixture works too, but creating the v0.5.3 H10
# dask `multiprocessing.Pool` mid-session leaves it to be finalised during
# interpreter teardown, which prints a benign `Pool.__del__` AttributeError.
import gebco_app

import src.config as config
from src.jsonsrc import (
    PUBLIC_REMOTE_ERROR,
    JsonSrcError,
    RemoteJsonSrcError,
    _normalize_host,
    load_jsonsrc,
)

# The exact bytes every remote failure must produce.
EXPECTED_PUBLIC_BODY = b'{"Error":"jsonsrc could not be retrieved"}'

UNRESOLVABLE = "definitely-not-a-real-host-12345.invalid"


def _expect_remote_failure(reason: str, jsonsrc: str) -> JsonSrcError:
    """Assert the fixed public message, return the exception for detail checks."""
    with pytest.raises(JsonSrcError) as excinfo:
        load_jsonsrc(jsonsrc)
    exc = excinfo.value
    assert exc.public_message == PUBLIC_REMOTE_ERROR
    assert str(exc) == PUBLIC_REMOTE_ERROR  # nothing leaks via str()
    assert exc.reason == reason
    return exc


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
    """v0.5.7 S1 keeps inline diagnostics: the caller's own payload is
    not a remote-host oracle."""
    mocker.patch("src.jsonsrc.requests.get")
    with pytest.raises(JsonSrcError, match="inline jsonsrc") as excinfo:
        load_jsonsrc("{not json}")
    assert excinfo.value.reason == "inline_invalid_json"
    assert excinfo.value.public_message != PUBLIC_REMOTE_ERROR


def test_empty_jsonsrc_keeps_its_message():
    with pytest.raises(JsonSrcError, match="empty") as excinfo:
        load_jsonsrc("")
    assert excinfo.value.reason == "empty_input"


# -------- host normalisation (v0.5.7 S1) ---------------------------------

def test_normalize_host_strips_trailing_root_dot():
    assert _normalize_host("exse-001.ntu.internal.") == ("exse-001.ntu.internal", False)
    assert _normalize_host("mail.ntu.edu.tw..") == ("mail.ntu.edu.tw", False)


def test_normalize_host_casefolds():
    assert _normalize_host("EX2016-002-1.NTU.Internal") == (
        "ex2016-002-1.ntu.internal", False,
    )


def test_normalize_host_idn_to_punycode():
    """IDN and its punycode form must fold to the same guard input."""
    unicode_form = _normalize_host("bücher.example")
    assert unicode_form == ("xn--bcher-kva.example", False)
    assert _normalize_host("XN--BCHER-KVA.EXAMPLE.") == unicode_form


# Hostnames whose IDNA treatment differs between implementations. The
# stdlib `"idna"` codec (IDNA 2003 + nameprep) and `str.casefold()` both
# fold `ß` to `ss`; `requests`/`urllib3` use the `idna` package
# (`uts46=True`) and dial `xn--strae-oqa.example` instead. A guard that
# validated `strasse.example` would be validating a different host from
# the one connected to.
IDNA_HOSTS = [
    "straße.example",
    "STRAßE.example",
    "faß.example",
    "bücher.example",
    "xn--bcher-kva.example",
    "ÉXAMPLE.com",
    "日本.example",
    "xn--wgv71a.example",
    "example.com",
    "EXAMPLE.COM",
    "my_host.example",
    "exse-001.ntu.internal",
]


def _requests_prepared_host(url: str) -> str | None:
    """The host `requests` would actually dial, or None if it refuses."""
    try:
        prepared = requests.Request("GET", url).prepare().url
    except requests.exceptions.RequestException:
        return None
    return urlparse(prepared).hostname


@pytest.mark.parametrize("raw", IDNA_HOSTS)
def test_guard_host_matches_requests_prepared_host(raw):
    """The name the SSRF guard validates IS the name `requests` dials."""
    url = f"http://{raw}/p"
    dialled = _requests_prepared_host(url)
    assert dialled is not None, "fixture host must be preparable by requests"
    guard, _is_ip = _normalize_host(urlparse(url).hostname)
    # requests keeps a trailing root dot; DNS-wise the names are equal.
    assert guard == dialled.rstrip(".")


def test_idna_2003_casefold_bypass_is_closed():
    """Regression for the review blocker, spelled out."""
    guard, _ = _normalize_host("straße.example")
    assert guard == "xn--strae-oqa.example"
    assert guard != "strasse.example"          # stdlib "idna" codec / casefold
    assert guard == _requests_prepared_host("http://straße.example/p")


def test_host_requests_refuses_still_fails_closed(mocker):
    """Where `requests` refuses to prepare the URL, nothing is dialled."""
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("93.184.216.34")],
    )
    mocker.patch(
        "src.jsonsrc.requests.get",
        side_effect=requests.exceptions.InvalidURL("URL has an invalid label."),
    )
    for raw in ("\uff45xample.com", "exa\u3002mple.com"):
        assert _requests_prepared_host(f"http://{raw}/p") is None
        _expect_remote_failure("connection_error", f"http://{raw}/p")


# -------- malformed URLs (v0.5.7 S1 blocker 2) ---------------------------

@pytest.mark.parametrize("url", [
    "http://[::1",              # urlparse: "Invalid IPv6 URL"
    "http://[not-an-ip]/",      # urlparse: "... does not appear to be an IPv4 ..."
    "http://[]/",
])
def test_malformed_url_does_not_leak_parser_error(url):
    """`urlparse()` / `.hostname` raise a bare ValueError; it must not
    reach the generic handler in gebco_app and echo the parser message."""
    exc = _expect_remote_failure("malformed_url", url)
    assert exc.detail == "ValueError"
    assert "IPv6" not in exc.public_message
    assert "not-an-ip" not in exc.public_message


def test_normalize_host_keeps_ip_literals():
    assert _normalize_host("127.0.0.1") == ("127.0.0.1", True)
    assert _normalize_host("127.0.0.1.") == ("127.0.0.1", True)
    assert _normalize_host("::1") == ("::1", True)
    # urlparse strips the [] from an IPv6 literal before we see it.
    assert _normalize_host("::FFFF:127.0.0.1")[1] is True


def test_normalize_host_rejects_empty_and_dot_only():
    for raw in ("", ".", "...", "   "):
        with pytest.raises(JsonSrcError) as excinfo:
            _normalize_host(raw)
        assert excinfo.value.public_message == PUBLIC_REMOTE_ERROR
        assert excinfo.value.reason == "missing_host"


def test_trailing_dot_host_takes_the_same_guard_path(mocker):
    """`host.` must be blocked exactly like `host` — not string-compared away."""
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("10.0.0.5")],
    )
    plain = _expect_remote_failure("blocked_private_address", "http://intranet.example/x")
    dotted = _expect_remote_failure("blocked_private_address", "http://intranet.example./x")
    assert plain.host == dotted.host == "intranet.example"
    assert plain.public_message == dotted.public_message


# -------- SSRF guard -----------------------------------------------------

def _gai_for(addr: str, family: int = socket.AF_INET):
    """Build a fake getaddrinfo return record."""
    if family == socket.AF_INET:
        return (family, socket.SOCK_STREAM, 0, "", (addr, 0))
    return (family, socket.SOCK_STREAM, 0, "", (addr, 0, 0, 0))


def test_block_literal_localhost(mocker):
    mocker.patch("src.jsonsrc.socket.getaddrinfo")  # never reached
    exc = _expect_remote_failure("blocked_localhost", "http://localhost/anything")
    assert exc.host == "localhost"


def test_block_loopback_ip(mocker):
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("127.0.0.1")],
    )
    exc = _expect_remote_failure("blocked_private_address", "http://127-0-0-1.nip.io/x")
    assert exc.detail == "addr=127.0.0.1"


def test_block_private_10_dot(mocker):
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("10.0.0.5")],
    )
    _expect_remote_failure("blocked_private_address", "http://intranet.example/x")


def test_block_link_local(mocker):
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("169.254.169.254")],  # AWS metadata
    )
    _expect_remote_failure("blocked_private_address", "http://meta.example/x")


def test_block_ipv4_mapped_ipv6(mocker):
    """Codex review: ::ffff:127.0.0.1 must be blocked."""
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[_gai_for("::ffff:127.0.0.1", family=socket.AF_INET6)],
    )
    _expect_remote_failure("blocked_private_address", "http://[::ffff:127.0.0.1]/x")


def test_block_ipv6_loopback_literal(mocker):
    mocker.patch("src.jsonsrc.socket.getaddrinfo")  # literal: no DNS needed
    _expect_remote_failure("blocked_private_address", "http://[::1]/x")


def test_ip_literal_is_checked_without_dns(mocker):
    """The literal IS the destination; don't round-trip it through DNS."""
    gai = mocker.patch("src.jsonsrc.socket.getaddrinfo")
    _expect_remote_failure("blocked_private_address", "http://127.0.0.1/")
    gai.assert_not_called()


def test_block_when_one_of_multiple_addresses_is_private(mocker):
    """Iterate-all-records: if ANY record is private, reject the whole host."""
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        return_value=[
            _gai_for("8.8.8.8"),         # public
            _gai_for("127.0.0.1"),       # private
        ],
    )
    _expect_remote_failure("blocked_private_address", "http://multihome.example/x")


def test_dns_failure_rejected(mocker):
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        side_effect=socket.gaierror(-2, "Name or service not known"),
    )
    exc = _expect_remote_failure("dns_failure", f"http://{UNRESOLVABLE}/x")
    assert exc.host == UNRESOLVABLE
    assert exc.detail == "gaierror errno=-2"


def test_dns_no_records_rejected(mocker):
    mocker.patch("src.jsonsrc.socket.getaddrinfo", return_value=[])
    _expect_remote_failure("dns_no_records", "http://empty.example/x")


# -------- scheme allowlist ------------------------------------------------

def test_block_file_scheme():
    exc = _expect_remote_failure("unsupported_scheme", "file:///etc/passwd")
    assert exc.detail == "scheme='file'"


def test_block_gopher_scheme():
    _expect_remote_failure("unsupported_scheme", "gopher://example/")


def test_url_without_host_rejected():
    _expect_remote_failure("missing_host", "http:///just/a/path")


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
    exc = _expect_remote_failure("redirect_blocked", "http://example.org/poly.json")
    assert exc.detail == "status=302"


def test_http_error_status_rejected(mocker):
    _public_addr_mock(mocker)
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(status_code=403),
    )
    exc = _expect_remote_failure("http_error", "http://example.org/poly.json")
    assert exc.detail == "status=403"


def test_timeout_rejected(mocker):
    """Pre-v0.5.7 a Timeout escaped as IOError and surfaced as a 500."""
    import requests as _requests
    _public_addr_mock(mocker)
    mocker.patch(
        "src.jsonsrc.requests.get",
        side_effect=_requests.exceptions.ConnectTimeout("boom"),
    )
    exc = _expect_remote_failure("timeout", "http://example.org/poly.json")
    assert exc.detail == "ConnectTimeout"


def test_connection_error_rejected(mocker):
    import requests as _requests
    _public_addr_mock(mocker)
    mocker.patch(
        "src.jsonsrc.requests.get",
        side_effect=_requests.exceptions.ConnectionError("refused http://host/secret?q=1"),
    )
    exc = _expect_remote_failure("connection_error", "http://example.org/poly.json")
    # Class name only — the requests message embeds the URL.
    assert exc.detail == "ConnectionError"


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
    exc = _expect_remote_failure("oversized_response", "http://example.org/poly.json")
    assert exc.detail == "limit=100"


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


def test_invalid_json_keeps_upstream_body_internal(mocker):
    _public_addr_mock(mocker)
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(
            headers={"content-type": "text/html"},
            body=b"<!doctype html><title>Outlook Web App</title>",
        ),
    )
    exc = _expect_remote_failure("invalid_json", "http://example.org/poly.json")
    assert exc.detail == "content_type='text/html'"
    assert "Outlook" not in exc.public_message


def test_invalid_encoding_rejected(mocker):
    _public_addr_mock(mocker)
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(body=b"\xff\xfe\x00bad utf-8"),
    )
    exc = _expect_remote_failure("invalid_encoding", "http://example.org/poly.json")
    assert exc.detail == "UnicodeDecodeError"


# -------- escape hatch --------------------------------------------------

def test_allow_remote_false_blocks_url(mocker, monkeypatch):
    """Deployment constant — identical for every URL, so not an oracle."""
    monkeypatch.setattr(config, "JSONSRC_ALLOW_REMOTE", False)
    with pytest.raises(JsonSrcError, match="disabled") as excinfo:
        load_jsonsrc("http://example.org/poly.json")
    assert excinfo.value.reason == "remote_disabled"


def test_allow_remote_false_does_not_block_inline(mocker, monkeypatch):
    monkeypatch.setattr(config, "JSONSRC_ALLOW_REMOTE", False)
    out = load_jsonsrc('{"a":1}')
    assert out == {"a": 1}


# -------- internal diagnostics are bounded and injection-safe ------------

def test_log_fields_cannot_inject_a_log_line():
    """Attacker-controlled host/detail must stay inside one JSON record."""
    exc = RemoteJsonSrcError(
        "dns_failure",
        host='evil\n{"event":"gebco_request_error","status":200}\n',
        detail="D" * 500,
    )
    line = json.dumps({"event": "gebco_request_error", **exc.log_fields()})
    assert "\n" not in line
    assert len(line.splitlines()) == 1
    assert json.loads(line)["jsonsrc_reason"] == "dns_failure"
    assert len(exc.detail) <= 120


def test_log_fields_omit_absent_values():
    exc = RemoteJsonSrcError("unsupported_scheme")
    assert exc.log_fields() == {"jsonsrc_reason": "unsupported_scheme"}


# =========================================================================
# Public endpoint contract (evidence file §4)
# =========================================================================
#
# These drive the real ASGI app through starlette's TestClient, so they
# assert the bytes and Content-Length an actual client receives, via real
# routing, Query parsing and JSONResponse serialisation. The app is
# deliberately NOT entered as a context manager: that skips `lifespan()`
# and its production Zarr open, which these paths never need.
# `configured_ds` supplies the toy grid for the 200-response regressions.


@pytest.fixture
def api(monkeypatch):
    """Real ASGI client + the structured WARNING lines the app emits."""
    records: list[str] = []
    monkeypatch.setattr("gebco_app.gebco_logger.logger.warning", records.append)
    client = TestClient(gebco_app.app)

    def _get(**params):
        return client.get(
            "/gebco", params={k: v for k, v in params.items() if v is not None}
        )

    return SimpleNamespace(
        get=_get,
        records=records,
        parsed=lambda: [json.loads(rec) for rec in records],
    )


def _three_failure_mocks(mocker):
    """example.com resolves publicly and serves HTML; everything else NXDOMAIN."""
    def _dispatch(host, *args, **kwargs):
        if host == "example.com":
            return [_gai_for("93.184.216.34")]
        raise socket.gaierror(-2, "Name or service not known")

    mocker.patch("src.jsonsrc.socket.getaddrinfo", side_effect=_dispatch)
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(
            headers={"content-type": "text/html"},
            body=b"<!doctype html><html><body>Example Domain</body></html>",
        ),
    )


THREE_CASES = (
    "http://example.com/",                      # public, reachable, non-JSON
    f"http://{UNRESOLVABLE}/",                  # DNS failure
    "http://127.0.0.1/",                        # SSRF block
)


def test_three_remote_failures_are_indistinguishable(api, mocker):
    """Evidence §4: same status, same body, same byte length."""
    _three_failure_mocks(mocker)
    responses = [api.get(jsonsrc=url) for url in THREE_CASES]

    assert {r.status_code for r in responses} == {400}
    assert {r.content for r in responses} == {EXPECTED_PUBLIC_BODY}
    assert {len(r.content) for r in responses} == {len(EXPECTED_PUBLIC_BODY)}
    assert {r.headers["content-length"] for r in responses} == {
        str(len(EXPECTED_PUBLIC_BODY))
    }


def test_public_body_leaks_no_diagnostics(api, mocker):
    """No hostname, IP, errno, content-type, parser text or URL echo."""
    _three_failure_mocks(mocker)
    leaks = (
        "example.com", UNRESOLVABLE, "127.0.0.1", "invalid", "Errno",
        "text/html", "Expecting value", "loopback", "private", "resolve",
        "http://", "content-type",
    )
    for url in THREE_CASES:
        body = api.get(jsonsrc=url).content.decode()
        assert body == EXPECTED_PUBLIC_BODY.decode()
        for needle in leaks:
            assert needle not in body


def test_malformed_urls_are_indistinguishable_too(api, mocker):
    """Blocker 2 regression, over real HTTP: a parser error must look
    exactly like every other remote failure."""
    _three_failure_mocks(mocker)
    malformed = ["http://[::1", "http://[not-an-ip]/", "http://[]/"]
    responses = [api.get(jsonsrc=url) for url in malformed + list(THREE_CASES)]

    assert {r.status_code for r in responses} == {400}
    assert {r.content for r in responses} == {EXPECTED_PUBLIC_BODY}
    assert {r.headers["content-length"] for r in responses} == {
        str(len(EXPECTED_PUBLIC_BODY))
    }
    for body in (r.content.decode() for r in responses):
        assert "IPv6" not in body
        assert "not-an-ip" not in body
        assert "does not appear" not in body
    assert [rec["jsonsrc_reason"] for rec in api.parsed()][:3] == [
        "malformed_url", "malformed_url", "malformed_url",
    ]


def test_dotted_and_undotted_host_are_indistinguishable(api, mocker):
    """`host.` and `host` must be byte-identical to the outside."""
    mocker.patch(
        "src.jsonsrc.socket.getaddrinfo",
        side_effect=socket.gaierror(-2, "Name or service not known"),
    )
    plain = api.get(jsonsrc=f"http://{UNRESOLVABLE}/")
    dotted = api.get(jsonsrc=f"http://{UNRESOLVABLE}./")

    assert plain.status_code == dotted.status_code == 400
    assert plain.content == dotted.content == EXPECTED_PUBLIC_BODY
    assert len(plain.content) == len(dotted.content)
    assert plain.headers["content-length"] == dotted.headers["content-length"]

    # ...and internally both land on the same normalised host.
    hosts = [rec["jsonsrc_host"] for rec in api.parsed()]
    assert hosts == [UNRESOLVABLE, UNRESOLVABLE]


def test_internal_logs_distinguish_the_failure_reasons(api, mocker):
    """Operators keep what the client no longer sees."""
    _three_failure_mocks(mocker)
    for url in THREE_CASES:
        api.get(jsonsrc=url)

    records = api.parsed()
    assert [rec["jsonsrc_reason"] for rec in records] == [
        "invalid_json", "dns_failure", "blocked_private_address",
    ]
    assert [rec["jsonsrc_host"] for rec in records] == [
        "example.com", UNRESOLVABLE, "127.0.0.1",
    ]
    assert [rec["jsonsrc_detail"] for rec in records] == [
        "content_type='text/html'", "gaierror errno=-2", "addr=127.0.0.1",
    ]
    for rec in records:
        assert rec["status"] == 400
        assert rec["error"] == PUBLIC_REMOTE_ERROR
        assert rec["event"] == "gebco_request_error"


def test_error_log_never_contains_url_or_query(api, mocker):
    """Bounded fields only — no full query string, no upstream body."""
    _three_failure_mocks(mocker)
    api.get(
        jsonsrc="http://example.com/secret/path?token=SUPERSECRET&x=1",
        mode="zonly",
    )
    raw = api.records[0]
    assert "SUPERSECRET" not in raw
    assert "secret/path" not in raw
    assert "Example Domain" not in raw
    assert len(raw.splitlines()) == 1


def test_size_cap_rejection_uses_the_same_public_body(api, mocker, monkeypatch):
    monkeypatch.setattr(config, "JSONSRC_MAX_BYTES", 100)
    _public_addr_mock(mocker)
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(body=b"y" * 400),
    )
    resp = api.get(jsonsrc="http://example.org/big.json")
    assert resp.status_code == 400
    assert resp.content == EXPECTED_PUBLIC_BODY
    assert api.parsed()[0]["jsonsrc_reason"] == "oversized_response"


def test_redirect_rejection_uses_the_same_public_body(api, mocker):
    _public_addr_mock(mocker)
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(
            status_code=302,
            headers={"location": "http://169.254.169.254/", "content-type": "text/html"},
        ),
    )
    resp = api.get(jsonsrc="http://example.org/poly.json")
    assert resp.status_code == 400
    assert resp.content == EXPECTED_PUBLIC_BODY
    assert "169.254.169.254" not in resp.content.decode()
    assert api.parsed()[0]["jsonsrc_reason"] == "redirect_blocked"


def test_inline_json_error_still_explains_itself(api, mocker):
    """Requirement 2: inline parse errors are unchanged."""
    mocker.patch("src.jsonsrc.requests.get")
    resp = api.get(jsonsrc='{"longitude":[1,2],')
    assert resp.status_code == 400
    assert resp.json()["Error"].startswith("inline jsonsrc is not valid JSON")
    assert api.parsed()[0]["jsonsrc_reason"] == "inline_invalid_json"


# -------- regression: ordinary lon/lat traffic is untouched ---------------

def test_normal_line_request_unaffected(configured_ds, api):
    resp = api.get(lon="-0.05,0.05", lat="-0.03,0.04", mode="zonly")
    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload) == {"longitude", "latitude", "z"}
    assert len(payload["z"]) > 1
    assert len(payload["longitude"]) == len(payload["z"])


def test_normal_point_request_unaffected(configured_ds, api):
    resp = api.get(lon="-0.05,0.05", lat="-0.03,0.04", mode="point,zonly")
    assert resp.status_code == 200
    assert len(resp.json()["z"]) == 2


def test_normal_inline_jsonsrc_request_unaffected(configured_ds, api, mocker):
    mocked_get = mocker.patch("src.jsonsrc.requests.get")
    resp = api.get(
        jsonsrc='{"longitude":[-0.05,0.05],"latitude":[-0.03,0.04]}', mode="zonly"
    )
    assert resp.status_code == 200
    assert len(resp.json()["z"]) > 1
    mocked_get.assert_not_called()


def test_remote_jsonsrc_success_still_works(configured_ds, api, mocker):
    _public_addr_mock(mocker)
    mocker.patch(
        "src.jsonsrc.requests.get",
        return_value=_response_mock(
            body=json.dumps(
                {"longitude": [-0.05, 0.05], "latitude": [-0.03, 0.04]}
            ).encode()
        ),
    )
    resp = api.get(jsonsrc="http://example.org/points.json", mode="zonly")
    assert resp.status_code == 200
    assert len(resp.json()["z"]) > 1
    assert api.records == []  # no warning line on the happy path
