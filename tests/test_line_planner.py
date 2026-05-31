from __future__ import annotations

import numpy as np

import src.config as config
from src.line_planner import LinePlan, plan_line_cells
from src.zprofile import zprofile


def test_line_planner_single_point():
    plan = plan_line_cells(np.array([122.36]), np.array([25.02]), 240, 180, 90)
    assert plan.idx.shape == (1, 2)
    assert plan.loc.shape == (1, 2)
    assert plan.breakpoint_indices.size == 0
    assert np.array_equal(plan.unique_idx, plan.idx)
    assert np.array_equal(plan.unique_idx[plan.inverse], plan.idx)


def test_line_planner_matches_zprofile_dataframe(configured_ds):
    loni = np.array([-0.05, 0.05])
    lati = np.array([-0.03, 0.04])
    plan = plan_line_cells(loni, lati, config.arc, config.basex, config.basey)
    df = zprofile(loni, lati, "zonly,dataframe", 1)

    assert df.height == plan.loc.shape[0]
    assert df["longitude"].to_list() == plan.loc[:, 0].tolist()
    assert df["latitude"].to_list() == plan.loc[:, 1].tolist()


def test_line_planner_unique_inverse_contract(configured_ds):
    loni = np.array([-0.08, -0.01, 0.06])
    lati = np.array([-0.06, 0.01, 0.08])
    plan = plan_line_cells(loni, lati, config.arc, config.basex, config.basey)
    assert np.array_equal(plan.unique_idx[plan.inverse], plan.idx)
    assert plan.unique_idx.shape[0] <= plan.idx.shape[0]
    assert plan.inner_iters > 0


def test_line_planner_inverse_reconstructs_legacy_z(configured_ds):
    loni = np.array([-0.05, 0.05])
    lati = np.array([-0.03, 0.04])
    plan = plan_line_cells(loni, lati, config.arc, config.basex, config.basey)
    df = zprofile(loni, lati, "zonly,dataframe", 1)
    z_legacy = df["z"].to_numpy()

    elev = config.ds["elevation"].values
    uniq_global = plan.unique_idx + np.array([[plan.mlatbase, plan.mlonbase]])
    uniq_z = elev[uniq_global[:, 0], uniq_global[:, 1]]
    z_reconstructed = uniq_z[plan.inverse]

    np.testing.assert_array_equal(z_reconstructed, z_legacy)


def test_line_planner_cross_meridian_breakpoints():
    loni = np.array([179.8, -179.8])
    lati = np.array([0.0, 1.0])
    plan = plan_line_cells(loni, lati, 240, 180, 90)
    assert plan.breakpoint_indices.size >= 1
    assert plan.loc.shape[0] > 2
    assert np.array_equal(plan.unique_idx[plan.inverse], plan.idx)


def test_line_planner_degenerate_tiny_segment(configured_ds):
    loni = np.array([0.0, 0.0001])
    lati = np.array([0.0, 0.0001])
    plan = plan_line_cells(loni, lati, config.arc, config.basex, config.basey)
    df = zprofile(loni, lati, "zonly,dataframe", 1)
    assert df.height == plan.loc.shape[0]
    assert np.array_equal(plan.unique_idx[plan.inverse], plan.idx)
