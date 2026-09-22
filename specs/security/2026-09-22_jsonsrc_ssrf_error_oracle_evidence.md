# Evidence: `jsonsrc` SSRF Error-Message Oracle

> Evidence received from the 2026-09-22 security investigation. This file is
> preserved in the repository as the source report for the remediation task.
>
> Scope: `github.com/cywhale/gebco`, branch `gebco_2026_perf_v054`, deployed
> checkout `/home/odbadmin/python/gebco` on VM37. This document records the
> reported observations; it is not itself a claim that the remediation is
> complete.

## 1. Background

On 2026-09-21, NTU IT detected the host behind `api.odb.ntu.edu.tw`
(`192.168.2.37`) making requests toward an academic administration system at
`140.112.161.108`. The host was not found to be compromised: no backdoor,
unexpected login, or webshell was found. The reported cause was abuse of
public endpoints that accept user-supplied URLs and fetch them server-side.

The attacker source was reported as `136.0.5.47`, with 3,246 requests. The
primary abuse involved another `/proxy` endpoint; `/gebco?jsonsrc=` and
`/ogcquery/capability?url=` were also used for internal reconnaissance. This
report covers only the GEBCO `jsonsrc` path.

## 2. Finding

The existing SSRF guard blocks private and loopback destinations, but its
error messages distinguish several failure classes. That makes the endpoint
an internal hostname-existence oracle even when response contents are not
returned.

The following observations were reported from VM37 using harmless targets:

```text
http://example.com/
  -> HTTP 400, 115B
  {"Error":"jsonsrc URL returned invalid JSON (content-type='text/html'): Expecting value: line 1 column 1 (char 0)"}

http://definitely-not-a-real-host-12345.invalid/
  -> HTTP 400, 115B
  {"Error":"could not resolve host 'definitely-not-a-real-host-12345.invalid': [Errno -2] Name or service not known"}

http://127.0.0.1/
  -> HTTP 400, 76B
  {"Error":"blocked host: '127.0.0.1' resolves to private/loopback 127.0.0.1"}
```

The response differences expose whether a host resolves, whether it is
reachable and non-JSON, and which address was returned by DNS. The reported
logs show enumeration of names such as `ex2016-*`, `exse-*`, and `mail.ntu.edu.tw`.

The report also recorded fully-qualified-name variants with a trailing dot:

```text
exse-001.ntu.internal.
mail.ntu.edu.tw.
ex2016-002-1.ntu.internal.
```

DNS treats `host.` and `host` as equivalent, but string-based checks can treat
them differently. Host normalization must therefore happen before the SSRF
decision and before any hostname comparison.

## 3. Required remediation

### 3.1 Required for this incident

1. Collapse all failures during remote `jsonsrc` retrieval into one fixed
   external response: the same HTTP status, the same response body, and no
   hostname, resolved IP, upstream content type, errno, URL, or parser detail.
2. Keep the detailed reason in server-side structured logs so operators can
   distinguish DNS failure, invalid upstream JSON, and SSRF blocking.
3. Normalize the URL hostname before SSRF checks: remove trailing root dots,
   case-normalize, and handle IDN/punycode consistently. `host.` and `host`
   must follow the same guard path and produce the same public response.
4. Do not weaken the existing private, loopback, link-local, IPv4-mapped IPv6,
   scheme, redirect, timeout, or streamed-size protections.

### 3.2 Recommended follow-up

* Consider an allowlist if remote JSON sources are limited to known providers.
* Consider a dedicated rate limit for remote `jsonsrc` requests. The existing
  `/gebco` NGINX limit should be checked rather than assumed sufficient.
* Revisit DNS rebinding: the address checked during DNS validation should be
  pinned to the address used for the actual connection, or remote fetching
  should be disabled/allowlisted.

### 3.3 Explicit non-goals

Do not alter ordinary `lon`, `lat`, `mode`, polygon, line, or data-source
behavior as part of this fix. Do not loosen the private/loopback block.

## 4. Acceptance evidence requested by the report

The following cases must return the same status, body, and response size:

* a public non-JSON URL (`http://example.com/`);
* an unresolvable hostname (`http://definitely-not-a-real-host-12345.invalid/`);
* a blocked loopback target (`http://127.0.0.1/`).

The dotted and non-dotted forms of the unresolvable hostname must also be
indistinguishable externally. Existing successful point and line requests
must remain HTTP 200 and preserve their response data. Server logs must still
retain the internal reason and normalized target host for each rejected remote
request.

Automated tests should cover the public response contract, trailing-dot
normalization, preservation of the SSRF guard, and internal diagnostic logging.

## 5. Deployment context recorded in the report

```text
VM37: /home/odbadmin/python/gebco
branch: gebco_2026_perf_v054
launcher: gunicorn gebco_app:app -w 2 -k uvicorn.workers.UvicornWorker \
          -b 127.0.0.1:8013
public endpoint: https://api.odb.ntu.edu.tw/gebco
```

The original incident report requested that the remediation be developed on a
feature branch, reviewed, tested, and only then deployed.
