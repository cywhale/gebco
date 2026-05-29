"""Parse the comma-separated `mode` query parameter into a set of tokens.

Pre-v0.5.3 the codebase did `"point" in mode.lower()` style substring
matches in 16 places across `gebco_app.py`, `src/zprofile.py`, and
`src/polyhandler.py`. That is fragile: `truncated` matches `truncate`,
`endpoint` matches `point`, `outlineid` matches `lineid`, etc. v0.5.3
H4 (see specs/v0.5.3_hardening_checklist_v2.md) centralises the parse.

Unknown tokens are **warning-only, never rejected** — clients may pass
forward-looking or third-party-extension tokens we do not recognise
yet. The structured warning is sufficient to discover real typos
without breaking backward compatibility.
"""
from __future__ import annotations

import logging
from typing import FrozenSet, Optional

logger = logging.getLogger("gebco")

KNOWN_MODES: FrozenSet[str] = frozenset({
    "point",
    "row",
    "zonly",
    "lineid",
    "truncate",
    "lon360",
    "dataframe",
    "connect_pt",
    "connect_pts",
})


def parse_modes(mode: Optional[str]) -> FrozenSet[str]:
    """Split a comma-separated mode string into a frozenset of lowercase tokens.

    Returns an empty frozenset when `mode` is None or empty / whitespace.
    Emits a single WARNING log line per unknown token, but always returns
    the full set so the caller can pass tokens through to anything that
    might handle them downstream.
    """
    if not mode:
        return frozenset()
    tokens = frozenset(m.strip().lower() for m in mode.split(",") if m.strip())
    unknown = tokens - KNOWN_MODES
    if unknown:
        logger.warning("unknown mode token(s): %s", sorted(unknown))
    return tokens
