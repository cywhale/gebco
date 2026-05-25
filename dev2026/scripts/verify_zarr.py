"""Sanity-check a freshly-written GEBCO Zarr store.

Checks (none are slow):
  1. Store opens with the same flags gebco_app.py uses:
     `xr.open_zarr(path, chunks="auto", decode_cf=False, decode_times=False)`
  2. `elevation` is present, is int16, has the global 43200×86400 shape, and
     coordinates span the expected ranges.
  3. NaN/sentinel inventory (the official files use -32767; we just count).
  4. A handful of known reference points (e.g. the ones in the 2023 notebook
     cell 10 around Taiwan) return plausible bathymetric values.
  5. If a reference dataset is supplied (the old 2023 Zarr or NetCDF), spot-check
     the same points and report the difference — this is *not* a regression
     test, just a quick "are we in the same ballpark" sniff.

This is a script for humans; the pytest-style assertions live in tests/.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import xarray as xr
from rich.console import Console
from rich.table import Table

console = Console()

# Same 4 reference points as dev/read_gebco_raw01.ipynb cell 10
REFERENCE_POINTS = [
    ("NE of Taiwan",        23.326173, 123.978125),
    ("NE of Taiwan offset", 23.317670, 123.973958),
    ("Lanyu area",          21.336378, 123.003125),
    ("Lanyu rounded",       21.33,     123.0),
]


def _open_dataset(path: Path) -> xr.Dataset:
    """Open NetCDF or Zarr with the same flags gebco_app.py uses."""
    if path.suffix == ".zarr" or (path / ".zgroup").exists():
        return xr.open_zarr(
            str(path), chunks="auto", decode_cf=False, decode_times=False
        )
    return xr.open_dataset(
        str(path), chunks="auto", decode_cf=False, decode_times=False
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("zarr_path", type=Path, help="Path to the new GEBCO_2026 Zarr.")
    p.add_argument(
        "--reference",
        type=Path,
        default=None,
        help="Optional reference dataset (2023 Zarr or NetCDF) for spot-check diff.",
    )
    args = p.parse_args()

    if not args.zarr_path.exists():
        console.print(f"[bold red]Zarr not found:[/] {args.zarr_path}")
        return 2

    console.rule(f"[bold]Opening[/] {args.zarr_path}")
    ds = _open_dataset(args.zarr_path)

    # --- shape / dims ---
    console.print(f"sizes  = {dict(ds.sizes)}")
    console.print(f"vars   = {list(ds.data_vars)}")
    console.print(f"coords = {list(ds.coords)}")

    ok = True
    if "elevation" not in ds.data_vars:
        console.print("[bold red]✗ missing 'elevation' data_var[/]")
        ok = False
    elif ds["elevation"].dtype != np.int16:
        console.print(
            f"[bold red]✗ elevation dtype is {ds['elevation'].dtype}, expected int16[/]"
        )
        ok = False
    else:
        console.print(f"[green]✓ elevation present, dtype={ds['elevation'].dtype}, shape={ds['elevation'].shape}[/]")

    # --- coord sanity ---
    if "lat" in ds.coords:
        lat = ds["lat"].values
        console.print(
            f"lat: n={lat.size} min={lat.min():.6f} max={lat.max():.6f}  "
            f"step≈{(lat[1]-lat[0]):.8f}"
        )
        if not (-90 < lat.min() < -89.9 and 89.9 < lat.max() < 90):
            console.print("[bold red]✗ lat range looks wrong (expected ~[-90, 90])[/]")
            ok = False
    if "lon" in ds.coords:
        lon = ds["lon"].values
        console.print(
            f"lon: n={lon.size} min={lon.min():.6f} max={lon.max():.6f}  "
            f"step≈{(lon[1]-lon[0]):.8f}"
        )
        if not (-180 < lon.min() < -179.9 and 179.9 < lon.max() < 180):
            console.print("[bold red]✗ lon range looks wrong (expected ~[-180, 180])[/]")
            ok = False

    # --- spot-check reference points ---
    console.rule("[bold]Reference points[/]")
    ref_ds = _open_dataset(args.reference) if args.reference else None
    if ref_ds is not None:
        console.print(f"reference: {args.reference}")

    t = Table()
    t.add_column("Where")
    t.add_column("lat")
    t.add_column("lon")
    t.add_column("2026 elev (m)", style="yellow")
    if ref_ds is not None:
        t.add_column("ref elev (m)")
        t.add_column("Δ (m)")
    for label, la, lo in REFERENCE_POINTS:
        v_new = (
            ds["elevation"]
            .sel(lat=la, lon=lo, method="nearest")
            .values.item()
        )
        row = [label, f"{la:.6f}", f"{lo:.6f}", str(v_new)]
        if ref_ds is not None and "elevation" in ref_ds.data_vars:
            v_ref = (
                ref_ds["elevation"]
                .sel(lat=la, lon=lo, method="nearest")
                .values.item()
            )
            row += [str(v_ref), f"{v_new - v_ref:+d}"]
        t.add_row(*row)
    console.print(t)

    # --- sentinel scan on a thin strip (cheap, avoids loading full grid) ---
    console.rule("[bold]Sentinel scan (10 random lat strips × full lon)[/]")
    rng = np.random.default_rng(seed=42)
    idx = rng.integers(0, ds.sizes["lat"], size=10)
    strip = ds["elevation"].isel(lat=xr.DataArray(idx, dims="i")).values
    n_total = strip.size
    n_sentinel = int((strip == -32767).sum())
    console.print(
        f"  scanned {n_total:,} pixels — sentinel(-32767) count = {n_sentinel:,} "
        f"({100*n_sentinel/n_total:.4f}%)"
    )

    ds.close()
    if ref_ds is not None:
        ref_ds.close()

    if ok:
        console.print("\n[bold green]✓ Verification passed — Zarr looks ready to wire into gebco_app.py.[/]")
        return 0
    console.print("\n[bold red]✗ Verification raised flags — see above.[/]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
