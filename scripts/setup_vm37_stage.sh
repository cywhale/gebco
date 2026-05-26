#!/usr/bin/env bash
set -euo pipefail

# Prepare a no-downtime staging checkout on VM37.
#
# Intended usage on VM37:
#   cd ~/python/gebco
#   ./scripts/setup_vm37_stage.sh
#
# Optional overrides:
#   STAGE_DIR=.stage_v051
#   STAGE_BRANCH=gebco_2026_api
#   STAGE_PORT=18013
#   REPO_URL=https://github.com/cywhale/gebco.git
#   START_STAGE=1

ROOT_DIR="${ROOT_DIR:-$PWD}"
STAGE_DIR="${STAGE_DIR:-.stage_v051}"
STAGE_BRANCH="${STAGE_BRANCH:-gebco_2026_api}"
STAGE_PORT="${STAGE_PORT:-18013}"
REPO_URL="${REPO_URL:-https://github.com/cywhale/gebco.git}"
START_STAGE="${START_STAGE:-0}"
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"

if [[ ! -d "$ROOT_DIR/.git" ]]; then
  echo "run this from the live repo root on VM37, e.g. ~/python/gebco" >&2
  exit 2
fi

if [[ ! -x "$UV_BIN" ]]; then
  echo "uv not found at $UV_BIN" >&2
  exit 3
fi

cleanup_stage_listener() {
  python3 - <<'PY'
import os
import re
import signal
import subprocess

port = int(os.environ["STAGE_PORT"])
out = subprocess.check_output(["ss", "-ltnp"], text=True)
for line in out.splitlines():
    if f":{port}" not in line:
        continue
    for pid in re.findall(r"pid=(\d+)", line):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
PY
}

echo "[stage] root=$ROOT_DIR branch=$STAGE_BRANCH stage_dir=$STAGE_DIR port=$STAGE_PORT"

cd "$ROOT_DIR"

if [[ -d "$STAGE_DIR/.git" ]]; then
  echo "[stage] updating existing checkout"
  git -C "$STAGE_DIR" fetch origin "$STAGE_BRANCH"
  git -C "$STAGE_DIR" checkout "$STAGE_BRANCH"
  git -C "$STAGE_DIR" reset --hard "origin/$STAGE_BRANCH"
else
  echo "[stage] cloning fresh checkout"
  rm -rf "$STAGE_DIR"
  git clone --branch "$STAGE_BRANCH" --single-branch "$REPO_URL" "$STAGE_DIR"
fi

cd "$STAGE_DIR"
mkdir -p data tmp

echo "[stage] uv sync"
"$UV_BIN" sync --frozen --python 3.11

echo "[stage] install CPU-compatible polars"
./scripts/install_polars_variant.sh auto

if [[ ! -d data/GEBCO_2026_sub_ice_topo.zarr ]]; then
  cat <<'EOF'
[stage] canonical 2026 Zarr not found:
  data/GEBCO_2026_sub_ice_topo.zarr

Copy or rsync it into the staging checkout before starting the server.
EOF
  exit 4
fi

echo "[stage] staging checkout ready at $(pwd)"
echo "[stage] current commit $(git rev-parse --short HEAD)"

if [[ "$START_STAGE" == "1" ]]; then
  cleanup_stage_listener
  echo "[stage] starting gunicorn on 127.0.0.1:$STAGE_PORT"
  ./.venv/bin/gunicorn gebco_app:app \
    -w 2 \
    -k uvicorn.workers.UvicornWorker \
    -b "127.0.0.1:$STAGE_PORT"
else
  cat <<EOF
[stage] start manually when ready:

  cd $ROOT_DIR/$STAGE_DIR
  ./.venv/bin/gunicorn gebco_app:app \\
    -w 2 \\
    -k uvicorn.workers.UvicornWorker \\
    -b 127.0.0.1:$STAGE_PORT
EOF
fi
