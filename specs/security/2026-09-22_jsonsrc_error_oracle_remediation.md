# Remediation: `jsonsrc` SSRF error-message oracle (v0.5.7 S1)

Source report: `2026-09-22_jsonsrc_ssrf_error_oracle_evidence.md`.
Branch: `security/jsonsrc-error-oracle-20260922` (from `a5d2a97`).
Not deployed; no change to `main`.

## 1. What changed

| File | Change |
| --- | --- |
| `src/jsonsrc.py` | `JsonSrcError` now carries `public_message` + internal `reason` / `host` / `detail`. Every remote-path failure raises `RemoteJsonSrcError`, whose public message is the fixed constant `PUBLIC_REMOTE_ERROR`. New `_normalize_host()` runs before the SSRF decision, using the same IDNA rules as `requests`. `urlparse()` / `.hostname` are inside the guarded block. `requests` exceptions are caught and mapped instead of escaping as `IOError`. |
| `pyproject.toml` / `uv.lock` | Declares `idna` — now a direct import in `src/jsonsrc.py`, not only a `requests` transitive. These are the authoritative production manifests; see §6. |
| `AGENTS.md` | Invariants 15 (two-channel error contract) and 16 (host normalisation must agree with `requests`); "Dependency workflow — uv only". |
| `gebco_app.py` | `_error_response()` gained `log_extra`; the `JsonSrcError` handler returns `exc.public_message` (not `str(exc)`) and logs `exc.log_fields()`. |
| `tests/test_jsonsrc.py` | Loader tests now assert `reason` instead of message text; new endpoint-contract, normalisation, log-diagnostic and lon/lat regression tests. |

## 2. Public contract

Every failure while processing a remote `jsonsrc` URL returns exactly:

```text
HTTP 400, 42 bytes
{"Error":"jsonsrc could not be retrieved"}
```

Covered: malformed URL, unsupported scheme, missing hostname, invalid
host encoding, DNS failure, empty DNS answer, `localhost`, private /
loopback / link-local / unspecified / IPv4-mapped-IPv6 rejection, connect
or read timeout, connection failure, HTTP error status, redirect,
size-cap rejection, invalid UTF-8, invalid JSON.

Deliberately unchanged (no remote-host oracle, so §3.3 "do not alter
ordinary behaviour" wins):

* inline JSON parse errors — computed from the caller's own payload;
* `jsonsrc is empty`;
* `remote jsonsrc is disabled (...)` — a deployment constant, identical
  for every URL.

## 3. Internal diagnostics

`gebco_request_error` records gain `jsonsrc_reason`, `jsonsrc_host`
(normalised) and `jsonsrc_detail`. Reasons distinguish `dns_failure`,
`dns_no_records`, `blocked_localhost`, `blocked_private_address`,
`redirect_blocked`, `http_error`, `timeout`, `connection_error`,
`oversized_response`, `invalid_encoding`, `invalid_json`,
`unsupported_scheme`, `missing_host`, `invalid_host`, `malformed_url`,
`unexpected_error`.

`detail` is a bounded token (`status=403`, `addr=10.0.0.5`,
`gaierror errno=-2`, `limit=2000000`, the exception class name, or the
upstream content-type truncated to 64 chars). The request URL, its query
string and upstream response bodies are never logged. Values are
truncated, stripped of non-printable characters and serialised with
`json.dumps`, so a hostile hostname cannot forge a log line.

## 4. Host normalisation

`_normalize_host()` runs *before* the SSRF decision and before any
hostname comparison. Trailing DNS root dots are stripped, so
`exse-001.ntu.internal.` and `exse-001.ntu.internal` take the same guard
branch and produce byte-identical public responses. IPv4/IPv6 literals
are returned in canonical form and checked directly — no DNS round-trip,
so the address checked is the address dialled.

The ASCII / IDN split mirrors `requests.PreparedRequest.prepare_url`
exactly: ASCII hosts are lowercased and passed through, non-ASCII hosts
go through `idna.encode(host, uts46=True)`.

**Review round 1, blocker 1.** The first implementation used
`str.casefold()` plus the stdlib `"idna"` codec. Both are IDNA 2003 and
fold `ß` to `ss`, so the guard validated `strasse.example` while
`requests` dialled `xn--strae-oqa.example` — a different host, i.e. the
SSRF guard could be checking a name that is never connected to.
`tests/test_jsonsrc.py::test_guard_host_matches_requests_prepared_host`
now compares the guard's output against the host of a real
`requests.Request(...).prepare()` URL for a table of IDN, punycode,
uppercase and ASCII hosts.

Two host spellings (`ｅxample.com`, `exa。mple.com`) normalise
for us but are rejected outright by `urllib3.parse_url`, so `requests`
never dials them; the fetch fails closed with the same public error.
That is the safe direction of the asymmetry, and it is pinned by
`test_host_requests_refuses_still_fails_closed`.

**Review round 2.** `rstrip(".")` folded `host..` to `host`, but
`requests` dials `host..` verbatim — the same class of guard/dial
divergence as blocker 1. Exactly one trailing dot is now stripped and
anything still ending in `.` is rejected as `invalid_host`.

Chasing that uncovered two more escapes of the blocker-2 class, both
live before this round:

* `socket.getaddrinfo()` IDNA-encodes the name itself and raises
  `UnicodeError` ("label empty or too long"), **not** `socket.gaierror`,
  for an empty or over-long label. `http://a..b.example/` therefore
  returned an 85-byte body quoting the codec message.
* `urllib3.exceptions.LocationParseError` is a `ValueError` but **not** a
  `requests.RequestException`, so it slipped past the `requests` handler;
  `http://example.com../` returned a 69-byte body quoting the parse
  error.

`_resolved_addresses()` now catches `UnicodeError`, and `_fetch_url()`
ends with a catch-all mapped to `unexpected_error` — the fixed public
response is only fixed if nothing escapes the boundary. The catch-all
logs the exception class name so a genuine bug stays greppable rather
than silently becoming a 400.

**Review round 1, blocker 2.** `urlparse()` and the `.hostname` property
both raise a bare `ValueError` on a malformed URL (`http://[::1` ->
"Invalid IPv6 URL"; `http://[not-an-ip]/` -> "'not-an-ip' does not appear
to be an IPv4 or IPv6 address"). They sat outside the guarded block, so
the generic `ValueError` handler in `gebco_app` echoed the parser message
— still a 400, but not the fixed body. They are now mapped to
`malformed_url`.

## 5. Protections preserved

http/https allowlist; private, loopback, link-local, unspecified and
IPv4-mapped IPv6 blocking; all-address DNS checking; connect/read
timeouts; `allow_redirects=False`; streamed size cap that ignores
`Content-Length`. Legacy IPv4 spellings (`127.1`, `0x7f.0.0.1`,
`2130706433`) still reach the guard through `getaddrinfo` as before.

## 6. Dependency handling

`idna` is a direct import in `src/jsonsrc.py` (the SSRF guard must use
the same IDNA implementation `requests`/`urllib3` use to build the
hostname they dial), so it is declared in the **root `pyproject.toml` and
resolved in the root `uv.lock`** — the authoritative production manifests
since the v0.5.1 Pipenv-to-uv migration. `uv lock --check` verifies they
agree. `pyproj`, a direct dependency since v0.5.4, is likewise declared
in that root uv project.

`Pipfile`, `Pipfile.lock` and `requirements.txt` are **intentionally not
synchronized** with this change. They are archived pre-v0.5.1 artefacts,
retained for historical reference only and neither maintained nor tested;
an interim revision of this branch added `idna`/`pyproj` to them and that
was reverted. They are not mirrors and should not be read as such.
See `AGENTS.md` -> "Dependency workflow — uv only".

## 7. Residual risks

* **DNS rebinding is NOT fixed.** The address validated by
  `getaddrinfo()` is still not pinned to the socket `requests` opens, so
  a hostile resolver can answer public during validation and private at
  connect time. Fixing it needs a pinned-address transport adapter, or
  an allowlist, or disabling remote fetch
  (`GEBCO_JSONSRC_ALLOW_REMOTE=false`). Untouched by this patch.
* **Timing remains a side channel.** Bodies and statuses are now
  identical, but a DNS failure, a refused connection and a served page
  take measurably different times. Rate limiting (report §3.2) is the
  mitigation, not error-text uniformity.
* **No allowlist and no dedicated rate limit** for remote `jsonsrc`
  (report §3.2) — still open.
* **The guard/`requests` IDNA agreement is pinned by a test, not by
  construction.** We re-implement the branch `requests` takes rather than
  asking it for the host it would dial. A `requests` upgrade that changes
  `prepare_url` would be caught by
  `test_guard_host_matches_requests_prepared_host`, but only if the test
  is run.
