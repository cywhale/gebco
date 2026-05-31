"""Live public A/B comparison between api.odb.ntu.edu.tw and ecodata.odb.ntu.edu.tw.

This script hits both public endpoints with a small curated query set
that exercises the v0.5.3 / v0.5.4 changes most directly while keeping
payloads small enough for routine smoke use.

The filename is historical. Treat this as a generic public-endpoint smoke
harness; do not infer the live deployed versions from the filename.

Queries:
  Q1 — cross-0 line
  Q2 — Taiwan polygon (sample=1)
  Q3 — cross-180 Fiji polygon (sample=5)
  Q4 — lon360 input acceptance
  Q5 — large multi-vertex line
  Q6 — large MultiLineString
  Q7 — small polygon baseline (sample=5)
  Q8 — near-cap diagonal transect

Run:
    uv run python dev2026/scripts/api_compare_v054_vs_v052.py
"""
from __future__ import annotations

import hashlib
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional


ENDPOINTS = {
    "api.odb": "https://api.odb.ntu.edu.tw/gebco",
    "ecodata.odb": "https://ecodata.odb.ntu.edu.tw/gebco",
}


@dataclass
class CallResult:
    status: int
    body: dict | list | str | None
    wall_s: float
    error: Optional[str] = None
    n_bytes: int = 0
    sha256: Optional[str] = None


def _get(url: str, *, timeout: float = 30.0) -> CallResult:
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
            wall = time.monotonic() - t0
            ct = resp.headers.get("content-type", "")
            try:
                body = json.loads(raw) if "json" in ct else raw.decode("utf-8", errors="replace")
            except json.JSONDecodeError:
                body = raw.decode("utf-8", errors="replace")
            return CallResult(
                status=resp.status,
                body=body,
                wall_s=wall,
                n_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
    except urllib.error.HTTPError as e:
        wall = time.monotonic() - t0
        try:
            body_txt = e.read().decode("utf-8", errors="replace")
        except Exception:
            body_txt = ""
        return CallResult(status=e.code, body=body_txt, wall_s=wall, error=f"HTTP {e.code}")
    except Exception as e:
        wall = time.monotonic() - t0
        return CallResult(status=-1, body=None, wall_s=wall, error=f"{type(e).__name__}: {e}")


def _z_fingerprint(body: dict | list | str | None) -> Optional[dict]:
    if body is None or isinstance(body, str):
        return None
    if isinstance(body, dict):
        z = body.get("z")
    elif isinstance(body, list) and body and isinstance(body[0], dict):
        z = [row.get("z") for row in body]
    else:
        return None
    if z is None or not isinstance(z, list) or not z:
        return {"n": 0}
    finite = [v for v in z if v is not None]
    finite_sorted = sorted(finite)
    n = len(finite)
    return {
        "n": n,
        "min": finite_sorted[0] if n else None,
        "max": finite_sorted[-1] if n else None,
        "mean": sum(finite) / n if n else None,
        "p50": finite_sorted[n // 2] if n else None,
        "p95": finite_sorted[int(n * 0.95)] if n else None,
        "sum_mod_1e9": sum(int(v) for v in finite if isinstance(v, (int, float))) % 1_000_000_000,
    }


def _query(label: str, feature: str, params: dict) -> dict:
    return {"label": label, "feature": feature, "params": params}


QUERIES = [
    _query(
        "Q1 cross-0 line (Gulf of Guinea, sample=1, zonly)",
        "crossBoundary(0°) data consistency + latency",
        {"lon": "-2,2", "lat": "0.5,1.5", "mode": "zonly", "sample": "1"},
    ),
    _query(
        "Q2 W2-B polygon (Taiwan 0.5°, sample=1, zonly)",
        "small polygon latency + exact body equality",
        {
            "mode": "zonly",
            "sample": "1",
            "jsonsrc": json.dumps(
                {"type": "Polygon", "coordinates": [[[121.5, 23.0], [121.5, 23.5], [122.0, 23.5], [122.0, 23.0], [121.5, 23.0]]]},
                separators=(",", ":"),
            ),
        },
    ),
    _query(
        "Q3 W2-B cross-180 polygon (Fiji 1°×0.5°, sample=5)",
        "cross-180 polygon latency + exact body equality",
        {
            "mode": "zonly",
            "sample": "5",
            "jsonsrc": json.dumps(
                {"type": "Polygon", "coordinates": [[[179.5, -17.5], [179.5, -17.0], [-179.5, -17.0], [-179.5, -17.5], [179.5, -17.5]]]},
                separators=(",", ":"),
            ),
        },
    ),
    _query(
        "Q4 H3 lon360 input (lon=250,260)",
        "lon360 data compatibility / legacy behavior difference",
        {"lon": "250,260", "lat": "0,0", "mode": "lon360,zonly"},
    ),
    _query(
        "Q5 large multi-vertex line (under line-cap, sample=3, zonly)",
        "large line bbox via lon/lat multi-vertex input",
        {
            "lon": "-150,-138,-126,-114,-102,-90",
            "lat": "-28,-16,-4,8,18,26",
            "mode": "zonly",
            "sample": "3",
        },
    ),
    _query(
        "Q6 large MultiLineString (Pacific/Atlantic, sample=3, zonly)",
        "jsonsrc MultiLineString over large spatial scale",
        {
            "mode": "zonly",
            "sample": "3",
            "jsonsrc": json.dumps(
                {
                    "type": "MultiLineString",
                    "coordinates": [
                        [[-170.0, -25.0], [-145.0, -10.0], [-120.0, 5.0], [-95.0, 18.0]],
                        [[-20.0, -30.0], [5.0, -10.0], [28.0, 8.0], [42.0, 24.0]],
                    ],
                },
                separators=(",", ":"),
            ),
        },
    ),
    _query(
        "Q7 baseline polygon (Taiwan 0.5°, sample=5, zonly)",
        "small-polygon baseline / latency smoke",
        {
            "mode": "zonly",
            "sample": "5",
            "jsonsrc": json.dumps(
                {"type": "Polygon", "coordinates": [[[121.5, 23.0], [121.5, 23.5], [122.0, 23.5], [122.0, 23.0], [121.5, 23.0]]]},
                separators=(",", ":"),
            ),
        },
    ),
    _query(
        "Q8 line bbox guard (over-cap, sample=3, zonly)",
        "v0.5.3+ line bbox guard behavior difference",
        {
            "lon": "-155,-143,-131,-118,-105,-92",
            "lat": "-30,-18,-6,6,18,28",
            "mode": "zonly",
            "sample": "3",
        },
    ),
]


def _build_url(base: str, params: dict) -> str:
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{base}?{qs}"


def _run_query(query: dict, trials: int = 3) -> dict:
    print()
    print(f"=== {query['label']} ===")
    print(f"  feature: {query['feature']}")
    summary = {"label": query["label"], "endpoints": {}}

    for ep_name, ep_url in ENDPOINTS.items():
        url = _build_url(ep_url, query["params"])
        walls = []
        last = None
        for _ in range(trials):
            r = _get(url)
            walls.append(r.wall_s)
            last = r
        median = statistics.median(walls) * 1000
        p95 = (sorted(walls)[int(len(walls) * 0.95)] if len(walls) > 1 else walls[0]) * 1000
        fingerprint = _z_fingerprint(last.body) if last and last.status == 200 else None
        ep_summary = {
            "status": last.status if last else None,
            "median_ms": median,
            "p95_ms": p95,
            "n_bytes": last.n_bytes if last else 0,
            "fingerprint": fingerprint,
            "error": last.error if last else "no result",
        }
        summary["endpoints"][ep_name] = ep_summary
        if last and last.status == 200:
            fp = fingerprint or {}
            print(
                f"    {ep_name:24s}  HTTP {last.status}  median={median:7.1f} ms  "
                f"n={fp.get('n', '?')}  z[mean={fp.get('mean', '?')!r:>9} p50={fp.get('p50', '?')!r:>7}]  "
                f"bytes={last.n_bytes:>7,d}  sha256={last.sha256[:12] if last.sha256 else 'n/a'}"
            )
        else:
            tail = ""
            if last and isinstance(last.body, str):
                tail = " body=" + last.body[:80].replace("\n", " ")
            print(
                f"    {ep_name:24s}  HTTP {last.status if last else '?'}  median={median:7.1f} ms  "
                f"err={last.error if last else '?'}{tail}"
            )

    eps = list(summary["endpoints"].values())
    if len(eps) == 2 and eps[0]["status"] == 200 == eps[1]["status"]:
        fp0 = eps[0]["fingerprint"] or {}
        fp1 = eps[1]["fingerprint"] or {}
        n_match = fp0.get("n") == fp1.get("n")
        hash_match = eps[0].get("sha256") == eps[1].get("sha256")
        z_match = (
            fp0.get("sum_mod_1e9") == fp1.get("sum_mod_1e9")
            and fp0.get("p50") == fp1.get("p50")
            and fp0.get("p95") == fp1.get("p95")
        )
        speed_ratio = eps[0]["median_ms"] / max(eps[1]["median_ms"], 0.001)
        if hash_match:
            data_verdict = "body exact"
        elif n_match and z_match:
            data_verdict = "fingerprint match"
        else:
            data_verdict = "DIFFER"
        names = list(summary["endpoints"].keys())
        verdict = f"data {data_verdict}  /  {names[0]} is {speed_ratio:.2f}× {names[1]} wall"
    else:
        left_status = eps[0]["status"] if eps else "?"
        right_status = eps[1]["status"] if len(eps) > 1 else "?"
        names = list(summary["endpoints"].keys())
        verdict = f"endpoint statuses differ: {names[0]}={left_status} {names[1]}={right_status}"
    summary["verdict"] = verdict
    print(f"  -> verdict: {verdict}")
    return summary


def main() -> int:
    print("=" * 78)
    print("api.odb.ntu.edu.tw vs ecodata.odb.ntu.edu.tw")
    print("=" * 78)
    print("trials per query: 3   (median + P95 reported)")
    print(f"queries: {len(QUERIES)}")

    results = [_run_query(q, trials=3) for q in QUERIES]

    print()
    print("=" * 78)
    print("summary")
    print("=" * 78)
    for result in results:
        print(f"  {result['label']:55s}  ->  {result['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
