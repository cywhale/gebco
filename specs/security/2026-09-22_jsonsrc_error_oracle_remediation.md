# Remediation: `jsonsrc` SSRF error-message oracle (v0.5.6 S1)

Source report: `2026-09-22_jsonsrc_ssrf_error_oracle_evidence.md`.
Branch: `security/jsonsrc-error-oracle-20260922` (from `a5d2a97`).
Not deployed; no change to `main`.

## 1. What changed

| File | Change |
| --- | --- |
| `src/jsonsrc.py` | `JsonSrcError` now carries `public_message` + internal `reason` / `host` / `detail`. Every remote-path failure raises `RemoteJsonSrcError`, whose public message is the fixed constant `PUBLIC_REMOTE_ERROR`. New `_normalize_host()` runs before the SSRF decision. `requests` exceptions are caught and mapped instead of escaping as `IOError`. |
| `gebco_app.py` | `_error_response()` gained `log_extra`; the `JsonSrcError` handler returns `exc.public_message` (not `str(exc)`) and logs `exc.log_fields()`. |
| `tests/test_jsonsrc.py` | Loader tests now assert `reason` instead of message text; new endpoint-contract, normalisation, log-diagnostic and lon/lat regression tests. |

## 2. Public contract

Every failure while processing a remote `jsonsrc` URL returns exactly:

```text
HTTP 400, 42 bytes
{"Error":"jsonsrc could not be retrieved"}
```

Covered: unsupported scheme, missing hostname, invalid host encoding, DNS
failure, empty DNS answer, `localhost`, private / loopback / link-local /
unspecified / IPv4-mapped-IPv6 rejection, connect or read timeout,
connection failure, HTTP error status, redirect, size-cap rejection,
invalid UTF-8, invalid JSON.

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
`unsupported_scheme`, `missing_host`, `invalid_host`.

`detail` is a bounded token (`status=403`, `addr=10.0.0.5`,
`gaierror errno=-2`, `limit=2000000`, the exception class name, or the
upstream content-type truncated to 64 chars). The request URL, its query
string and upstream response bodies are never logged. Values are
truncated, stripped of non-printable characters and serialised with
`json.dumps`, so a hostile hostname cannot forge a log line.

## 4. Host normalisation

`_normalize_host()` runs *before* the SSRF decision and before any
hostname comparison: trailing DNS root dots are stripped, names are
casefolded, IDN labels are folded to punycode, and IPv4/IPv6 literals are
returned in canonical form and checked directly (no DNS round-trip, so
the address checked is the address dialled). `exse-001.ntu.internal.` and
`exse-001.ntu.internal` therefore take the same guard branch and produce
byte-identical public responses.

## 5. Protections preserved

http/https allowlist; private, loopback, link-local, unspecified and
IPv4-mapped IPv6 blocking; all-address DNS checking; connect/read
timeouts; `allow_redirects=False`; streamed size cap that ignores
`Content-Length`. Legacy IPv4 spellings (`127.1`, `0x7f.0.0.1`,
`2130706433`) still reach the guard through `getaddrinfo` as before.

## 6. Residual risks

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
