# VM37 Pre-Cutover Checklist

Scope: final checks to complete on VM37 before replacing the live `gebco`
process with the `gebco_2026_api` branch.

This checklist assumes:

* production must remain online until explicit cutover approval
* staging is done in `/home/odbadmin/python/gebco/.stage_v051`
* live production remains in `/home/odbadmin/python/gebco`
* live app name remains `gebco`
* live bind remains `127.0.0.1:8013`

## 1. Confirm staging revision

```bash
cd ~/python/gebco/.stage_v051
git rev-parse --abbrev-ref HEAD
git rev-parse --short HEAD
```

Expected:

* branch = `gebco_2026_api`
* commit matches the reviewed remote head

## 2. Confirm canonical 2026 Zarr exists in staging

```bash
cd ~/python/gebco/.stage_v051
ls -ld data/GEBCO_2026_sub_ice_topo.zarr
du -sh data/GEBCO_2026_sub_ice_topo.zarr
```

Expected:

* canonical path exists
* size is consistent with the reviewed Blosc store (~3.5G on VM37 staging)

## 3. Confirm root uv env is healthy

```bash
cd ~/python/gebco/.stage_v051
~/.local/bin/uv sync --frozen --python 3.11
./scripts/install_polars_variant.sh auto
./.venv/bin/python - <<'PY'
import importlib.metadata as im
print([d.metadata["Name"] + "==" + d.version
       for d in im.distributions()
       if d.metadata["Name"] in {"polars", "polars-lts-cpu"}])
PY
```

Expected:

* sync succeeds without modifying the lockfile
* exactly one Polars distribution is installed
* on older VM37-class CPUs, `auto` should resolve to `polars-lts-cpu==1.26.0`

## 4. Confirm no-production-port smoke still passes

Start staging only on `127.0.0.1:18013` and run:

* point smoke
* line smoke
* polygon smoke

Expected:

* all return HTTP 200
* point response shape looks normal
* polygon request returns expected downsampled row counts

Reference:

* `dev2026/TESTING.md`, Phase D
* `dev2026/README.md`, “VM37 no-downtime staging recipe”

## 5. Confirm 1M-row polygon benchmark parity

Re-run the local loopback benchmark from `dev2026/TESTING.md`, Phase D.

Expected:

* OLD and NEW both complete
* returned row count is ~1,016,064
* `NEW / OLD mean <= 1.10x`

Current reviewed result:

* `NEW / OLD mean = 0.983x`

## 6. Inspect live production process before cutover

```bash
pm2 status gebco
pm2 describe gebco
ss -ltnp | grep 8013
```

Expected:

* current live process still online
* bind still on `127.0.0.1:8013`
* no accidental restart occurred during staging work

## 7. Decide cutover style

Before touching production, choose one:

1. in-place repo update in `~/python/gebco`
2. swap production to use the staged checkout

Recommendation:

* prefer the cleanest operation with the smallest moving parts
* avoid leaving `--reload` enabled in the final production command unless
  there is an operational reason to keep it

## 8. Freeze the cutover command set

Before execution, write down the exact commands for:

* updating the production checkout
* building `.venv`
* running `./scripts/install_polars_variant.sh auto`
* updating `conf/ecosystem.config.js`
* restarting `pm2 gebco`
* verifying local and public endpoint health

No ad hoc shell edits during cutover.

## 9. Prepare rollback

Rollback must be rehearsed conceptually before cutover:

* previous git commit or tag to restore
* previous `ecosystem.config.js`
* previous runtime env path if needed
* `pm2 restart gebco` rollback command

## 10. Post-cutover verification targets

After cutover, verify in this order:

1. `pm2 status gebco`
2. local loopback point request
3. local loopback polygon request
4. public `https://api.odb.ntu.edu.tw/gebco/` point request
5. one representative large polygon request

If any of 1–3 fail, rollback immediately before checking public traffic.
