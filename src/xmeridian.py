"""Cross-meridian breakpoint insertion for polylines.

v0.5.4 W1 (H9) — the algorithm is identical to v0.5.3; the only change
is that `newx / newy / newidx / autoFly` are built as Python lists and
converted to numpy arrays once at return time, instead of repeated
``np.append(...)`` reallocations. This drops the long-polyline path
from O(n²) to amortized O(n) (see ``dev2026/scripts/benchmark_long_polyline.py``).

The numerical content (break-point positions, autoFly classifications,
y-values inserted on cross-zero and cross-180) is byte-equal to the
v0.5.3 implementation. Geometric edge cases (|m| > 1 zeroloc shrink,
both-ends-closer-to-meridian shortcut) are preserved verbatim.
"""
import numpy as np
import src.config as config


def whichSide(l1a, l2a):
    """Classify a meridian crossing as 'cross-zero' (through 0°) or
    'away-zero' (through 180°). Returns None if the segment does not
    cross any meridian.
    """
    if l1a[0] != l2a[0] and np.sign(l1a[0]) != np.sign(l2a[0]):
        lonat1 = 180 + l1a[0]
        lonat2 = 180 + l2a[0]
        dift1 = np.absolute(lonat2 - lonat1)

        lonas1 = 180 - l1a[0] if l1a[0] >= 0 else -180 - l1a[0]
        lonas2 = 180 - l2a[0] if l2a[0] >= 0 else -180 - l2a[0]
        dift2 = np.absolute(lonas2 - lonas1)

        if dift1 < dift2:
            return "cross-zero"
        return "away-zero"
    return None


def crossBoundary(lon, lat):
    """Insert break-points where the polyline crosses 0° or 180°.

    Returns ``(newx, newy, newidx, autoFly)`` where ``newx`` / ``newy``
    are the rewritten polyline coordinates, ``newidx`` indexes the break
    positions inside the rewritten arrays (terminated with -1), and
    ``autoFly`` is the per-crossing classification.
    """
    if len(lon) < 2:
        return (lon, lat, np.asarray([-1], dtype=int), [])

    idx = np.ravel(np.where(np.sign(lon[:-1]) != np.sign(lon[1:])))
    if len(idx) == 0:
        return (lon, lat, np.asarray([-1], dtype=int), [])

    basex = config.basex   # 180
    arc = config.arc       # int(3600/arcsec)
    zeroloc = 0.5 / arc

    # v0.5.4 H9: Python-list accumulators replace per-iteration np.append.
    newx_buf: list[float] = []
    newy_buf: list[float] = []
    newidx_buf: list[int] = []
    autoFly: list[str] = []
    preidx = 0

    for k in range(len(idx)):
        idx0 = int(idx[k])
        idx1 = -1 if k == len(idx) - 1 else int(idx[k + 1])

        # Extend with the run of unmodified vertices up to the crossing.
        newx_buf.extend(float(v) for v in lon[preidx : idx0 + 1])
        newy_buf.extend(float(v) for v in lat[preidx : idx0 + 1])
        tf = whichSide([lon[idx0]], [lon[idx0 + 1]])
        autoFly.append(tf)

        if tf == "cross-zero":
            m = (lat[idx1] - lat[idx0]) / (lon[idx1] - lon[idx0])
            b = lat[idx0] - m * lon[idx0]  # y = mx + b
            absm = abs(m)
            absxdelta = abs((0.499 / arc) / m)

            if np.sign(newx_buf[-1]) == -1:
                # both-ends closer to zero
                if absm <= 1 and newx_buf[-1] >= -zeroloc and lon[idx0 + 1] <= zeroloc:
                    newidx_buf.append(len(newx_buf) - 1)
                elif absm <= 1 and newx_buf[-1] >= -zeroloc:
                    newx_buf.append(zeroloc)
                    newidx_buf.append(len(newx_buf) - 2)
                    newy_buf.append(m * zeroloc + b)
                elif absm <= 1 and lon[idx0 + 1] <= zeroloc:
                    newx_buf.append(-zeroloc)
                    newidx_buf.append(len(newx_buf) - 1)
                    newy_buf.append(m * (-zeroloc) + b)
                else:
                    if absm > 1:
                        zeroloc = absxdelta
                    leftx = -zeroloc if newx_buf[-1] < -zeroloc else newx_buf[-1] * 0.1
                    rightx = zeroloc if lon[idx0 + 1] > zeroloc else lon[idx0 + 1] * 0.1
                    newx_buf.append(leftx)
                    newx_buf.append(rightx)
                    newidx_buf.append(len(newx_buf) - 2)
                    newy_buf.append(m * leftx + b)
                    newy_buf.append(m * rightx + b)
            else:  # newx_buf[-1] sign is +1
                if absm <= 1 and newx_buf[-1] <= zeroloc and lon[idx0 + 1] >= -zeroloc:
                    newidx_buf.append(len(newx_buf) - 1)
                elif absm <= 1 and newx_buf[-1] <= zeroloc:
                    newx_buf.append(-zeroloc)
                    newidx_buf.append(len(newx_buf) - 2)
                    newy_buf.append(m * (-zeroloc) + b)
                elif absm <= 1 and lon[idx0 + 1] >= -zeroloc:
                    newx_buf.append(zeroloc)
                    newidx_buf.append(len(newx_buf) - 1)
                    newy_buf.append(m * zeroloc + b)
                else:
                    if absm > 1:
                        zeroloc = absxdelta
                    leftx = zeroloc if newx_buf[-1] > zeroloc else newx_buf[-1] * 0.1
                    rightx = (
                        -zeroloc if lon[idx0 + 1] < -zeroloc else lon[idx0 + 1] * 0.1
                    )
                    newx_buf.append(leftx)
                    newx_buf.append(rightx)
                    newidx_buf.append(len(newx_buf) - 2)
                    newy_buf.append(m * leftx + b)
                    newy_buf.append(m * rightx + b)
        else:
            # away-zero (cross 180°): use the mirrored coordinate system
            # documented in whichSide() to avoid the -179<->179 discontinuity.
            endat = (
                [-(basex - zeroloc), basex - zeroloc]
                if np.sign(newx_buf[-1]) == -1
                else [basex - zeroloc, -(basex - zeroloc)]
            )
            lonas0 = basex - lon[idx0] if lon[idx0] >= 0 else -basex - lon[idx0]
            lonas1 = basex - lon[idx1] if lon[idx1] >= 0 else -basex - lon[idx1]
            m = (lat[idx1] - lat[idx0]) / (lonas1 - lonas0)
            b = lat[idx0] - m * lonas0
            lonat0 = basex - endat[0] if endat[0] >= 0 else -basex - endat[0]
            lonat1 = basex - endat[1] if endat[1] >= 0 else -basex - endat[1]
            absm = abs(m)
            absxdelta = abs((0.499 / arc) / m)

            if np.sign(newx_buf[-1]) == -1:
                if (
                    absm <= 1
                    and newx_buf[-1] <= -(basex - zeroloc)
                    and lon[idx0 + 1] >= basex - zeroloc
                ):
                    newidx_buf.append(len(newx_buf) - 1)
                elif absm <= 1 and newx_buf[-1] <= -(basex - zeroloc):
                    newx_buf.append(basex - zeroloc)
                    newidx_buf.append(len(newx_buf) - 2)
                    lonat1 = basex - endat[1] if endat[1] >= 0 else -basex - endat[1]
                    newy_buf.append(m * lonat1 + b)
                elif absm <= 1 and lon[idx0 + 1] >= basex - zeroloc:
                    newx_buf.append(-(basex - zeroloc))
                    newidx_buf.append(len(newx_buf) - 1)
                    lonat0 = basex - endat[0] if endat[0] >= 0 else -basex - endat[0]
                    newy_buf.append(m * lonat0 + b)
                else:
                    if absm > 1:
                        zeroloc = absxdelta
                    leftx = (
                        -(basex - zeroloc)
                        if newx_buf[-1] > -(basex - zeroloc)
                        else 0.5 * (-basex + newx_buf[-1])
                    )
                    rightx = (
                        basex - zeroloc
                        if lon[idx0 + 1] < basex - zeroloc
                        else 0.5 * (basex + lon[idx0 + 1])
                    )
                    endat = [leftx, rightx]
                    lonat0 = basex - endat[0] if endat[0] >= 0 else -basex - endat[0]
                    lonat1 = basex - endat[1] if endat[1] >= 0 else -basex - endat[1]
                    newx_buf.append(leftx)
                    newx_buf.append(rightx)
                    newy_buf.append(m * lonat0 + b)
                    newy_buf.append(m * lonat1 + b)
                    newidx_buf.append(len(newx_buf) - 2)
            else:  # newx_buf[-1] sign is +1
                if (
                    absm <= 1
                    and newx_buf[-1] >= basex - zeroloc
                    and lon[idx0 + 1] <= -(basex - zeroloc)
                ):
                    newidx_buf.append(len(newx_buf) - 1)
                elif absm <= 1 and newx_buf[-1] >= basex - zeroloc:
                    newx_buf.append(-(basex - zeroloc))
                    newidx_buf.append(len(newx_buf) - 2)
                    lonat1 = basex - endat[1] if endat[1] >= 0 else -basex - endat[1]
                    newy_buf.append(m * lonat1 + b)
                elif absm <= 1 and lon[idx0 + 1] <= -(basex - zeroloc):
                    newx_buf.append(basex - zeroloc)
                    newidx_buf.append(len(newx_buf) - 1)
                    lonat0 = basex - endat[0] if endat[0] >= 0 else -basex - endat[0]
                    newy_buf.append(m * lonat0 + b)
                else:
                    if absm > 1:
                        zeroloc = absxdelta
                    leftx = (
                        basex - zeroloc
                        if newx_buf[-1] < basex - zeroloc
                        else 0.5 * (basex + newx_buf[-1])
                    )
                    rightx = (
                        -(basex - zeroloc)
                        if lon[idx0 + 1] > -(basex - zeroloc)
                        else 0.5 * (-basex + lon[idx0 + 1])
                    )
                    endat = [leftx, rightx]
                    lonat0 = basex - endat[0] if endat[0] >= 0 else -basex - endat[0]
                    lonat1 = basex - endat[1] if endat[1] >= 0 else -basex - endat[1]
                    newx_buf.append(leftx)
                    newx_buf.append(rightx)
                    newy_buf.append(m * lonat0 + b)
                    newy_buf.append(m * lonat1 + b)
                    newidx_buf.append(len(newx_buf) - 2)

        preidx = idx0 + 1

    # Tail vertices beyond the last crossing.
    newx_buf.extend(float(v) for v in lon[preidx:])
    newy_buf.extend(float(v) for v in lat[preidx:])
    newidx_buf.append(-1)

    return (
        np.asarray(newx_buf, dtype=float),
        np.asarray(newy_buf, dtype=float),
        np.asarray(newidx_buf, dtype=int),
        autoFly,
    )
