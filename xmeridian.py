import numpy as np


def whichSide(l1a, l2a):  # , direction='auto'):
    # autoFly = 'cross-zero' #cross prime meridian #otherwise, "away-zero" (cross 180-degree)
    # (deprecated) dirFlag: get direction (clockwise or counter-) when autoFly
    if l1a[0] != l2a[0] and np.sign(l1a[0]) != np.sign(l2a[0]):
        lonat1 = 180 + l1a[0]
        lonat2 = 180 + l2a[0]
        dift1 = np.absolute(lonat2 - lonat1)

        lonas1 = 180 - l1a[0] if l1a[0] >= 0 else -180 - l1a[0]
        lonas2 = 180 - l2a[0] if l2a[0] >= 0 else -180 - l2a[0]
        dift2 = np.absolute(lonas2 - lonas1)

        if dift1 < dift2:
            #dirFlag = 'clockwise' if np.sign(l1a[0]) == 1 else 'counterclockwise'
            autoFly = 'cross-zero'
        else:
            #dirFlag = 'counterclockwise' if np.sign(l1a[0]) == 1 else 'clockwise'
            autoFly = 'away-zero'
        return(autoFly)  # , dirFlag)
    else:
        return(None)  # , None)


def crossBoundary(lon, lat):
    if len(lon) < 2:
        # , []) #lon, lat, idx, autoFly, (deprecated) dirFlag
        return(lon, lat, [-1], [])

    idx = np.ravel(np.where(np.sign(lon[:-1]) != np.sign(lon[1:])))
    if len(idx) == 0:
        return(lon, lat, [-1], [])  # , [])

    basex = 180
    # zeroidx=43200
    arcsec = 15
    arc = int(3600/arcsec)  # 15 arc-second
    zeroloc = 0.5/arc
    autoFly = np.empty(shape=[0, 1], dtype=str)
    #dirFlag = np.empty(shape=[0, 1], dtype=str)
    newx = np.empty(shape=[0, 1], dtype=float)
    newy = np.empty(shape=[0, 1], dtype=float)
    # recalculate break-points index in newx
    newidx = np.empty(shape=[0, 1], dtype=int)
    preidx = 0
    for k in range(len(idx)):
        idx0 = idx[k]  # .item()
        idx1 = -1 if k == len(idx)-1 else idx[k+1]  # .item()
        newx = np.append(newx, lon[preidx:idx0+1], axis=None)
        newy = np.append(newy, lat[preidx:idx0+1], axis=None)
        tf = whichSide([lon[idx0]], [lon[idx0+1]])
        autoFly = np.append(autoFly, tf, axis=None)  # tf[0]
        #dirFlag = np.append(dirFlag, tf[1], axis=None)
        if tf == 'cross-zero':
            m = (lat[idx1]-lat[idx0])/(lon[idx1]-lon[idx0])
            b = lat[idx0] - m * lon[idx0]  # y = mx + b
            # if m > 1, even two-ends closer to zero, we still need append points
            absm = np.absolute(m)
            # if larger m > 1, need a smaller step near 0-line
            absxdelta = np.absolute((0.499/arc)/m)
            if np.sign(newx[-1]) == -1:
                # i.e, both-ends closer to zero
                if absm <= 1 and newx[-1] >= -zeroloc and lon[idx0+1] <= zeroloc:
                    # breaks in newx[-1], but no new x needed to insert
                    newidx = np.append(newidx, len(newx)-1, axis=None)
                elif absm <= 1 and newx[-1] >= -zeroloc:
                    newx = np.append(newx, zeroloc, axis=None)
                    # breaks before zeroloc
                    newidx = np.append(newidx, len(newx)-2, axis=None)
                    newy = np.append(newy, m * zeroloc + b, axis=None)
                elif absm <= 1 and lon[idx0+1] <= zeroloc:
                    newx = np.append(newx, -zeroloc, axis=None)
                    # breaks after -zeroloc
                    newidx = np.append(newidx, len(newx)-1, axis=None)
                    newy = np.append(newy, m * (-zeroloc) + b, axis=None)
                else:
                    if absm > 1:
                        zeroloc = absxdelta
                    # a much closer-zero value but still in negative side
                    leftx = -zeroloc if newx[-1] < -zeroloc else newx[-1]*0.1
                    rightx = zeroloc if lon[idx0 +
                                            1] > zeroloc else lon[idx0+1]*0.1
                    zeroin = [leftx, rightx]
                    newx = np.append(newx, zeroin, axis=None)
                    # breaks in -zeroloc - zeroloc
                    newidx = np.append(newidx, len(newx)-2, axis=None)
                    newy = np.append(
                        newy, [m * zeroin[0] + b, m * zeroin[1] + b], axis=None)
            else:  # np.sign(newx[-1]) == 1:
                # i.e, both-ends closer to zero
                if absm <= 1 and newx[-1] <= zeroloc and lon[idx0+1] >= -zeroloc:
                    # breaks in newx[-1], but no new x needed to insert
                    newidx = np.append(newidx, len(newx)-1, axis=None)
                elif absm <= 1 and newx[-1] <= zeroloc:
                    newx = np.append(newx, -zeroloc, axis=None)
                    # breaks before -zeroloc
                    newidx = np.append(newidx, len(newx)-2, axis=None)
                    newy = np.append(newy, m * (-zeroloc) + b, axis=None)
                elif absm <= 1 and lon[idx0+1] >= -zeroloc:
                    newx = np.append(newx, zeroloc, axis=None)
                    # breaks after zeroloc
                    newidx = np.append(newidx, len(newx)-1, axis=None)
                    newy = np.append(newy, m * zeroloc + b, axis=None)
                else:
                    if absm > 1:
                        zeroloc = absxdelta
                    # a much closer-zero value but still in positive side
                    leftx = zeroloc if newx[-1] > zeroloc else newx[-1]*0.1
                    rightx = -zeroloc if lon[idx0+1] < - \
                        zeroloc else lon[idx0+1]*0.1
                    zeroin = [leftx, rightx]
                    newx = np.append(newx, zeroin, axis=None)
                    # breaks in zeroloc - -zeroloc
                    newidx = np.append(newidx, len(newx)-2, axis=None)
                    newy = np.append(
                        newy, [m * zeroin[0] + b, m * zeroin[1] + b], axis=None)
        else:
            endat = [-(basex-zeroloc), basex-zeroloc] if np.sign(newx[-1]
                                                                 ) == -1 else [basex-zeroloc, -(basex-zeroloc)]
            # Note: -179 <--> 179 is a big disrupt, cannot use to calculate y = mx + b
            # use formula in function whichSide()
            lonas0 = basex - \
                lon[idx0] if lon[idx0] >= 0 else -basex - lon[idx0]
            lonas1 = basex - \
                lon[idx1] if lon[idx1] >= 0 else -basex - lon[idx1]
            m = (lat[idx1]-lat[idx0])/(lonas1-lonas0)
            b = lat[idx0] - m * lonas0
            lonat0 = basex - endat[0] if endat[0] >= 0 else -basex - endat[0]
            lonat1 = basex - endat[1] if endat[1] >= 0 else -basex - endat[1]
            absm = np.absolute(m)
            absxdelta = np.absolute((0.499/arc)/m)
            if np.sign(newx[-1]) == -1:
                # i.e, both-ends closer to 180-line
                if absm <= 1 and newx[-1] <= -(basex-zeroloc) and lon[idx0+1] >= basex-zeroloc:
                    # breaks in newx[-1], but no new x needed to insert
                    newidx = np.append(newidx, len(newx)-1, axis=None)
                elif absm <= 1 and newx[-1] <= -(basex-zeroloc):
                    newx = np.append(newx, basex-zeroloc, axis=None)
                    # breaks before 180-zeroloc
                    newidx = np.append(newidx, len(newx)-2, axis=None)
                    # shold positive
                    lonat1 = basex - \
                        endat[1] if endat[1] >= 0 else -basex - endat[1]
                    newy = np.append(newy, m * lonat1 + b, axis=None)
                elif absm <= 1 and lon[idx0+1] >= basex-zeroloc:
                    newx = np.append(newx, -(basex-zeroloc), axis=None)
                    # breaks after -(180-zeroloc)
                    newidx = np.append(newidx, len(newx)-1, axis=None)
                    lonat0 = basex - \
                        endat[0] if endat[0] >= 0 else -basex - endat[0]
                    newy = np.append(newy, m * lonat0 + b, axis=None)
                else:
                    if absm > 1:
                        zeroloc = absxdelta
                    # a much closer-180 value but still in negative side
                    leftx = - \
                        (basex-zeroloc) if newx[-1] > - \
                        (basex-zeroloc) else 0.5*(-basex+newx[-1])
                    rightx = basex - \
                        zeroloc if lon[idx0+1] < basex - \
                        zeroloc else 0.5*(basex+lon[idx0+1])
                    endat = [leftx, rightx]
                    lonat0 = basex - \
                        endat[0] if endat[0] >= 0 else -basex - endat[0]
                    lonat1 = basex - \
                        endat[1] if endat[1] >= 0 else -basex - endat[1]
                    newx = np.append(newx, endat, axis=None)
                    newy = np.append(
                        newy, [m * lonat0 + b, m * lonat1 + b], axis=None)
                    # breaks in -endat, endat
                    newidx = np.append(newidx, len(newx)-2, axis=None)
            else:  # np.sign(newx[-1]) == 1:
                # i.e, both-ends closer to 180-line
                if absm <= 1 and newx[-1] >= basex-zeroloc and lon[idx0+1] <= -(basex-zeroloc):
                    # breaks in newx[-1], but no new x needed to insert
                    newidx = np.append(newidx, len(newx)-1, axis=None)
                elif absm <= 1 and newx[-1] >= basex-zeroloc:
                    newx = np.append(newx, -(basex-zeroloc), axis=None)
                    # breaks before -(180-zeroloc)
                    newidx = np.append(newidx, len(newx)-2, axis=None)
                    # shold negative
                    lonat1 = basex - \
                        endat[1] if endat[1] >= 0 else -basex - endat[1]
                    newy = np.append(newy, m * lonat1 + b, axis=None)
                elif absm <= 1 and lon[idx0+1] <= -(basex-zeroloc):
                    newx = np.append(newx, basex-zeroloc, axis=None)
                    # breaks after basex-zeroloc
                    newidx = np.append(newidx, len(newx)-1, axis=None)
                    lonat0 = basex - \
                        endat[0] if endat[0] >= 0 else -basex - endat[0]
                    newy = np.append(newy, m * lonat0 + b, axis=None)
                else:
                    if absm > 1:
                        zeroloc = absxdelta
                    # a much closer-180 value but still in positive side
                    leftx = basex - \
                        zeroloc if newx[-1] < basex - \
                        zeroloc else 0.5*(basex+newx[-1])
                    rightx = - \
                        (basex-zeroloc) if lon[idx0+1] > - \
                        (basex-zeroloc) else 0.5*(-basex+lon[idx0+1])
                    endat = [leftx, rightx]
                    lonat0 = basex - \
                        endat[0] if endat[0] >= 0 else -basex - endat[0]
                    lonat1 = basex - \
                        endat[1] if endat[1] >= 0 else -basex - endat[1]
                    newx = np.append(newx, endat, axis=None)
                    newy = np.append(
                        newy, [m * lonat0 + b, m * lonat1 + b], axis=None)
                    # breaks in -endat, endat
                    newidx = np.append(newidx, len(newx)-2, axis=None)

        preidx = idx0+1

    newx = np.append(newx, lon[preidx:], axis=None)
    newy = np.append(newy, lat[preidx:], axis=None)
    newidx = np.append(newidx, -1, axis=None)
    return(newx, newy, newidx, autoFly)  # , dirFlag)
