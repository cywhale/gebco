#!/usr/bin/env bash
set -euo pipefail

# Install a CPU-compatible Polars wheel into the current uv-managed venv.
#
# Modes:
#   auto            detect CPU flags and choose polars or polars-lts-cpu
#   polars          force the modern wheel
#   polars-lts-cpu  force the legacy-compatible wheel
#
# Environment overrides:
#   POLARS_PACKAGE  same values as the first positional arg
#   POLARS_VERSION  defaults to 1.26.0 to match the repo's current production pin
#   UV_BIN          defaults to uv

MODE="${1:-${POLARS_PACKAGE:-auto}}"
VERSION="${POLARS_VERSION:-1.26.0}"
UV_BIN="${UV_BIN:-uv}"

have_linux_cpu_flags() {
  [[ -r /proc/cpuinfo ]]
}

cpu_supports_modern_polars() {
  if ! have_linux_cpu_flags; then
    return 0
  fi

  local flags
  flags="$(grep -m1 '^flags' /proc/cpuinfo || true)"
  [[ "$flags" == *" avx2 "* ]] || return 1
  [[ "$flags" == *" bmi1 "* ]] || return 1
  [[ "$flags" == *" bmi2 "* ]] || return 1
  [[ "$flags" == *" lzcnt "* ]] || return 1
}

resolve_package() {
  case "$MODE" in
    auto)
      if cpu_supports_modern_polars; then
        echo "polars"
      else
        echo "polars-lts-cpu"
      fi
      ;;
    polars|polars-lts-cpu)
      echo "$MODE"
      ;;
    *)
      echo "unsupported POLARS_PACKAGE mode: $MODE" >&2
      exit 2
      ;;
  esac
}

PACKAGE="$(resolve_package)"

echo "[polars] mode=$MODE resolved=$PACKAGE version=$VERSION"

# Remove whichever variant might already be present to avoid name/module conflicts.
"$UV_BIN" pip uninstall -y polars polars-lts-cpu >/dev/null 2>&1 || true
"$UV_BIN" pip install "${PACKAGE}==${VERSION}"

"$UV_BIN" run python - <<'PY'
import polars as pl
print(f"[polars] import ok version={pl.__version__}")
PY
