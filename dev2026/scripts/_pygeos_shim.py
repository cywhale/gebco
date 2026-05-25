"""shapely-2.x-backed `pygeos` shim, registered into sys.modules.

`src/polyhandler.py` was written against pygeos 0.14, which has no Python 3.13
wheels and isn't maintained — its API was absorbed wholesale into shapely 2.0
(pygeos was a precursor of shapely 2's vectorised C-backed geometry layer).
Every symbol polyhandler imports has a 1:1 replacement in shapely 2.x.

Importing this module installs the shim in `sys.modules['pygeos']` so any
subsequent `import pygeos` from src/polyhandler.py picks it up. The shim is a
**functional** stub (forwards to shapely), not the previous no-op stub.
Call it like:

    from _pygeos_shim import install_shim
    install_shim()
    # then import gebco_app / src.polyhandler safely

Only the subset polyhandler actually calls is mapped — anything else will
raise AttributeError loudly.
"""
from __future__ import annotations

import sys
import types

import shapely


def install_shim() -> None:
    if "pygeos" in sys.modules and getattr(sys.modules["pygeos"], "__shapely_shim__", False):
        return  # idempotent

    mod = types.ModuleType("pygeos")
    mod.__shapely_shim__ = True

    # Direct delegation to shapely 2.x
    mod.get_coordinates = shapely.get_coordinates
    mod.bounds = shapely.bounds
    mod.get_type_id = shapely.get_type_id
    mod.get_parts = shapely.get_parts
    mod.points = shapely.points
    mod.contains = shapely.contains

    # In pygeos this converted a shapely.geometry object to a pygeos.Geometry.
    # In shapely 2.x they are the same objects, so this is identity.
    def from_shapely(geom):
        return geom
    mod.from_shapely = from_shapely

    # `pygeos.lib.Geometry` is used by polyhandler in an isinstance check.
    lib = types.ModuleType("pygeos.lib")
    lib.Geometry = shapely.Geometry
    mod.lib = lib

    sys.modules["pygeos"] = mod
    sys.modules["pygeos.lib"] = lib
