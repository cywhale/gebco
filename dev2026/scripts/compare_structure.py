"""Compare the structure of two GEBCO NetCDF files (e.g. 2023 vs 2026).

Usage:
    uv run python scripts/compare_structure.py \
        ../data_src/GEBCO_2023/GEBCO_2023.nc \
        ../data_src/GEBCO_2026/GEBCO_2026.nc

The goal is to surface any breaking changes in the schema before we touch the
existing 2023 → Zarr pipeline. We deliberately open with decode_cf=False and
decode_times=False to match how dev/read_gebco_raw01.ipynb opened the 2023 file,
so we are inspecting the raw on-disk representation rather than the CF-decoded
view.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import xarray as xr
from rich.console import Console
from rich.table import Table

console = Console()


def _open(path: Path) -> xr.Dataset:
    # The same combo of flags the existing notebook uses.
    return xr.open_dataset(
        path,
        decode_cf=False,
        decode_times=False,
        engine="netcdf4",
    )


def _scalarize(v: Any) -> str:
    """Render numpy / list-ish attribute values to a short string."""
    s = repr(v)
    if len(s) > 120:
        s = s[:117] + "..."
    return s


def _table(title: str, rows: list[tuple[str, str, str]]) -> Table:
    t = Table(title=title, show_lines=False, expand=True)
    t.add_column("Key", style="cyan", no_wrap=True)
    t.add_column("Old (2023)", style="white")
    t.add_column("New (2026)", style="yellow")
    for key, a, b in rows:
        style = None if a == b else "bold red"
        t.add_row(key, a, b, style=style)
    return t


def _compare_dicts(a: dict, b: dict) -> list[tuple[str, str, str]]:
    keys = sorted(set(a) | set(b))
    return [
        (k, _scalarize(a.get(k, "<MISSING>")), _scalarize(b.get(k, "<MISSING>")))
        for k in keys
    ]


def _diff_set(label: str, a: set[str], b: set[str]) -> None:
    only_a = a - b
    only_b = b - a
    if not only_a and not only_b:
        console.print(f"[green]✓ {label} identical[/]: {sorted(a)}")
    else:
        console.print(f"[bold red]✗ {label} differ[/]")
        if only_a:
            console.print(f"   only in OLD: {sorted(only_a)}")
        if only_b:
            console.print(f"   only in NEW: {sorted(only_b)}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("old_nc", type=Path, help="Path to old GEBCO NetCDF (e.g. 2023)")
    p.add_argument("new_nc", type=Path, help="Path to new GEBCO NetCDF (e.g. 2026)")
    args = p.parse_args()

    for f in (args.old_nc, args.new_nc):
        if not f.exists():
            console.print(f"[bold red]File not found:[/] {f}")
            return 2

    console.rule("[bold]Opening datasets[/] (decode_cf=False)")
    console.print(f"OLD: {args.old_nc}")
    console.print(f"NEW: {args.new_nc}")

    ds_old = _open(args.old_nc)
    ds_new = _open(args.new_nc)

    console.rule("[bold]Dimensions[/]")
    console.print(
        _table(
            "dims",
            _compare_dicts(dict(ds_old.sizes), dict(ds_new.sizes)),
        )
    )

    console.rule("[bold]Coordinates[/]")
    _diff_set("coord names", set(ds_old.coords), set(ds_new.coords))
    for c in sorted(set(ds_old.coords) & set(ds_new.coords)):
        co, cn = ds_old.coords[c], ds_new.coords[c]
        rows = [
            ("dtype", str(co.dtype), str(cn.dtype)),
            ("shape", str(co.shape), str(cn.shape)),
            ("min", _scalarize(co.values.min()), _scalarize(cn.values.min())),
            ("max", _scalarize(co.values.max()), _scalarize(cn.values.max())),
            (
                "first 3",
                _scalarize(co.values[:3].tolist()),
                _scalarize(cn.values[:3].tolist()),
            ),
            (
                "last 3",
                _scalarize(co.values[-3:].tolist()),
                _scalarize(cn.values[-3:].tolist()),
            ),
        ]
        rows += _compare_dicts(dict(co.attrs), dict(cn.attrs))
        console.print(_table(f"coord: {c}", rows))

    console.rule("[bold]Data variables[/]")
    _diff_set("data_var names", set(ds_old.data_vars), set(ds_new.data_vars))
    for v in sorted(set(ds_old.data_vars) & set(ds_new.data_vars)):
        vo, vn = ds_old[v], ds_new[v]
        rows = [
            ("dtype", str(vo.dtype), str(vn.dtype)),
            ("shape", str(vo.shape), str(vn.shape)),
            ("dims", str(vo.dims), str(vn.dims)),
        ]
        rows += _compare_dicts(dict(vo.attrs), dict(vn.attrs))
        # Encoding (chunking, compression on disk) — only set after the file is
        # opened, so this tells us how the source NetCDF is stored, not how we
        # later choose to chunk it on the way to Zarr.
        rows += [
            (
                f"enc.{k}",
                _scalarize(vo.encoding.get(k, "<MISSING>")),
                _scalarize(vn.encoding.get(k, "<MISSING>")),
            )
            for k in sorted(set(vo.encoding) | set(vn.encoding))
        ]
        console.print(_table(f"var: {v}", rows))

    console.rule("[bold]Global attributes[/]")
    console.print(_table("global attrs", _compare_dicts(dict(ds_old.attrs), dict(ds_new.attrs))))

    console.rule("[bold]Summary[/]")
    same_dims = dict(ds_old.sizes) == dict(ds_new.sizes)
    same_vars = set(ds_old.data_vars) == set(ds_new.data_vars)
    same_coords = set(ds_old.coords) == set(ds_new.coords)
    same_main_dtype = (
        "elevation" in ds_old.data_vars
        and "elevation" in ds_new.data_vars
        and ds_old["elevation"].dtype == ds_new["elevation"].dtype
    )
    bullets = [
        ("dims identical", same_dims),
        ("coord names identical", same_coords),
        ("data_var names identical", same_vars),
        ("elevation dtype identical", same_main_dtype),
    ]
    for label, ok in bullets:
        mark = "[green]✓[/]" if ok else "[bold red]✗[/]"
        console.print(f"  {mark} {label}")

    ds_old.close()
    ds_new.close()

    breaking = not all(ok for _, ok in bullets)
    if breaking:
        console.print("\n[bold red]⚠  Potential breaking changes detected[/] — review the red rows above before re-running the Zarr conversion.")
        return 1
    console.print("\n[bold green]✓ Schema looks compatible[/] — conversion can re-use the existing chunking strategy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
