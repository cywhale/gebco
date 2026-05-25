"""Entry point that just points users at the real scripts in ./scripts/.

Real workflow:
    uv sync --extra dev
    uv run python scripts/compare_structure.py <old.nc> <new.nc>
    uv run python scripts/convert_to_zarr.py <new.nc> <out.zarr>
    uv run python scripts/verify_zarr.py <out.zarr> [--reference <old.zarr_or_nc>]
    uv run pytest
"""

from pathlib import Path


def main() -> None:
    here = Path(__file__).parent
    print("gebco-2026-tools — see README.md for the full workflow.")
    print()
    print("Scripts available:")
    for p in sorted((here / "scripts").glob("*.py")):
        print(f"  uv run python scripts/{p.name}")
    print()
    print("Tests:")
    print("  uv run pytest")


if __name__ == "__main__":
    main()
