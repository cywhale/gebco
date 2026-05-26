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
#   TARGET_PYTHON   defaults to ./.venv/bin/python

MODE="${1:-${POLARS_PACKAGE:-auto}}"
VERSION="${POLARS_VERSION:-1.26.0}"
UV_BIN="${UV_BIN:-uv}"
TARGET_PYTHON="${TARGET_PYTHON:-$(pwd)/.venv/bin/python}"

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
echo "[polars] target_python=$TARGET_PYTHON"

if [[ ! -x "$TARGET_PYTHON" ]]; then
  echo "target python does not exist or is not executable: $TARGET_PYTHON" >&2
  exit 3
fi

# Remove whichever variant might already be present to avoid name/module conflicts.
"$UV_BIN" pip uninstall --python "$TARGET_PYTHON" -y polars polars-lts-cpu >/dev/null 2>&1 || true
"$UV_BIN" pip install --python "$TARGET_PYTHON" "${PACKAGE}==${VERSION}"

"$TARGET_PYTHON" - <<'PY'
import polars as pl
print(f"[polars] import ok version={pl.__version__}")
PY
