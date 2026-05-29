"""Input validation helpers for the GEBCO API (v0.5.3 H2).

Pre-v0.5.3 `numarr_query_validator()` only rejected non-numeric strings.
NaN, ±Inf, and out-of-range values (e.g. `lon=999`) passed through and
reached `gridded_arcsec()` which produced out-of-bounds indices →
silent wrong data or 500. The `jsonsrc` JSON-with-longitude/latitude
path had no validation at all.

`validate_lonlat()` centralises the checks for both entry points.
"""
from __future__ import annotations

from typing import Optional

import numpy as np


def validate_lonlat(
    loni: np.ndarray,
    lati: np.ndarray,
    *,
    allow_lon360: bool = False,
) -> Optional[str]:
    """Return an error message string, or None if the inputs are valid.

    * Both arrays must have identical shape (same length).
    * All values must be finite (no NaN / ±Inf).
    * `lat` ∈ [-90, 90]; `lon` ∈ [-180, 180] by default, or [0, 360]
      when `allow_lon360=True` (used after the H3 lon360-mode
      pre-validation, before normalisation).

    Boundary values (`lat == 90`, `lon == 180`, etc.) are accepted — the
    grid lookup downstream clamps internally to keep the index in range.
    """
    if loni.shape != lati.shape:
        return f"lon ({loni.shape}) and lat ({lati.shape}) must have equal length"
    if not (np.isfinite(loni).all() and np.isfinite(lati).all()):
        return "lon/lat contains non-finite values (NaN or Inf)"
    lo, hi = (0.0, 360.0) if allow_lon360 else (-180.0, 180.0)
    if (loni < lo).any() or (loni > hi).any():
        return f"lon out of [{lo}, {hi}]"
    if (lati < -90.0).any() or (lati > 90.0).any():
        return "lat out of [-90, 90]"
    return None


def numarr_query_validator(qry: str) -> np.ndarray:
    """Parse a comma-separated numeric query value into a float64 array.

    v0.5.3 H8 — raises `ValueError` on parse failure instead of returning
    the legacy `"Format Error"` string sentinel. The outer handler already
    catches `ValueError` and returns 400.
    """
    if "," in qry:
        try:
            return np.array([float(x.strip()) for x in qry.split(",")], dtype=np.float64)
        except ValueError as exc:
            raise ValueError(f"could not parse comma-separated numbers: {exc}") from exc
    try:
        return np.array([float(qry.strip())], dtype=np.float64)
    except ValueError as exc:
        raise ValueError(f"could not parse number: {exc}") from exc
