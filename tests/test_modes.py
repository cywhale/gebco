"""Tests for src.modes.parse_modes — v0.5.3 H4."""
from __future__ import annotations

import logging

from src.modes import KNOWN_MODES, parse_modes


def test_empty_inputs_return_empty_set():
    assert parse_modes(None) == frozenset()
    assert parse_modes("") == frozenset()
    assert parse_modes("   ") == frozenset()
    assert parse_modes(",") == frozenset()


def test_single_known_token():
    assert parse_modes("row") == {"row"}
    assert parse_modes("ROW") == {"row"}  # case-insensitive


def test_multiple_known_tokens():
    assert parse_modes("row,zonly") == {"row", "zonly"}
    assert parse_modes("  row , zonly  ") == {"row", "zonly"}


def test_known_modes_inventory():
    # Sanity that all advertised modes still parse.
    advertised = {
        "point", "row", "zonly", "lineid", "truncate", "lon360",
        "dataframe", "connect_pt", "connect_pts",
    }
    assert advertised == KNOWN_MODES


def test_unknown_token_warning_only(caplog):
    """Unknown tokens are accepted (no rejection) but emit a WARNING.

    This is the backward-compat contract — future / typo / third-party
    extension tokens must not break clients.
    """
    with caplog.at_level(logging.WARNING, logger="gebco"):
        result = parse_modes("futureproof_xyz")
    assert result == {"futureproof_xyz"}
    assert any("futureproof_xyz" in rec.message for rec in caplog.records)


def test_unknown_token_alongside_known(caplog):
    with caplog.at_level(logging.WARNING, logger="gebco"):
        result = parse_modes("futureproof_xyz,row")
    assert result == {"futureproof_xyz", "row"}
    # WARNING fires only for the unknown token, not "row".
    msgs = [rec.message for rec in caplog.records]
    assert any("futureproof_xyz" in m for m in msgs)
    assert not any(m.endswith("['row']") for m in msgs)


def test_substring_false_positive_no_longer_triggers():
    """The whole point of H4: 'outlineid' must not match 'lineid'."""
    modes = parse_modes("outlineid")
    assert "lineid" not in modes


def test_endpoint_does_not_match_point():
    modes = parse_modes("endpoint")
    assert "point" not in modes


def test_truncated_does_not_match_truncate():
    modes = parse_modes("truncated")
    assert "truncate" not in modes
