from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np

import src.config as config
from gebco_app import gebco
from src.polyhandler import polyhandler
from src.zprofile import zprofile


def test_zprofile_stats_out_single_line(configured_ds):
    stats = {}
    df = zprofile(
        np.array([-0.05, 0.05]),
        np.array([-0.03, 0.04]),
        "zonly,dataframe",
        1,
        stats_out=stats,
    )
    assert df.height > 0
    assert stats["use_sparse"] is False
    assert stats["bbox_subset_cells"] > 0
    assert stats["output_rows"] == df.height
    assert stats["unique_cells"] <= stats["output_rows"]
    assert stats["touched_chunks"] >= 0
    assert stats["projected_bytes"] >= 0


def test_polyhandler_stats_out_multilinestring(configured_ds):
    stats = {}
    geojson = {
        "type": "MultiLineString",
        "coordinates": [
            [[-0.08, -0.05], [-0.01, 0.01], [0.06, 0.08]],
            [[-0.02, 0.08], [0.03, 0.02], [0.08, -0.04]],
        ],
    }
    df, _ = polyhandler(geojson, mode="zonly,dataframe", stats_out=stats)
    assert df.height > 0
    assert stats["multi_parts"] == 2
    assert stats["bbox_subset_cells_sum"] > 0
    assert stats["output_rows_sum"] == df.height
    assert stats["output_rows_max"] > 0
    assert stats["touched_chunks_sum"] >= 0
    assert stats["touched_chunks_max"] >= 0
    assert stats["projected_bytes_sum"] >= 0
    assert stats["projected_bytes_max"] >= 0
    assert stats["any_sparse"] is False
    assert "_line_parts" not in stats


def test_gebco_logs_line_stats(configured_ds, monkeypatch):
    captured = {}

    def _info(msg):
        captured["msg"] = msg

    monkeypatch.setattr("gebco_app.gebco_logger.logger.info", _info)
    request = SimpleNamespace(client=SimpleNamespace(host="test-client"), headers={})
    resp = gebco(
        request,
        lon="-0.05,0.05",
        lat="-0.03,0.04",
        mode="zonly",
        sample=5,
        jsonsrc=None,
    )
    assert resp.status_code == 200
    payload = json.loads(captured["msg"])
    assert payload["rows"] > 0
    assert payload["line_stats"]["use_sparse"] is False
    assert payload["line_stats"]["output_rows"] == payload["rows"]


def test_gebco_bbox_error_includes_kind(configured_ds, monkeypatch):
    monkeypatch.setattr(config, "MAX_BBOX_CELLS_LINE", 1)
    request = SimpleNamespace(client=SimpleNamespace(host="test-client"), headers={})
    resp = gebco(
        request,
        lon="-179.0,179.0",
        lat="-89.0,89.0",
        mode="zonly",
        sample=5,
        jsonsrc=None,
    )
    body = json.loads(resp.body)
    assert resp.status_code == 413
    assert body["kind"] == "bbox_cells"
