#!/usr/bin/env python3

import argparse
import pkg_resources as pkgr
import intake
import io
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from om4labs import m6plot
import palettable
import xarray as xr
import xoverturning
import warnings

from om4labs.om4common import horizontal_grid
from om4labs.om4common import read_topography
from om4labs.om4common import image_handler
from om4labs.om4common import generate_basin_masks
from om4labs.om4common import date_range
from om4labs.om4parser import default_diag_parser

warnings.filterwarnings("ignore", message=".*csr_matrix.*")
warnings.filterwarnings("ignore", message=".*dates out of range.*")


def parse(cliargs=None, template=False):
    """
    Function to capture the user-specified command line options
    """
    description = """ """

    parser = default_diag_parser(description=description, template=template)

    if template is True:
        return parser.parse_args(None).__dict__
    else:
        return parser.parse_args(cliargs)


def read(dictArgs, vcomp="vmo", ucomp="umo"):
    """Read required fields to plot MOC in om4labs

    Parameters
    ----------
    dictArgs : dict
        Dictionary containing argparse options
    vcomp : str, optional
        Name of meridional component of total residual
        freshwater transport, by default "vmo"
    ucomp : str, optional
        Name of zonal component of total residual
        freshwater transport, by default "umo"

    Returns
    -------
    xarray.DataSet
        Xarray dataset containing `umo`, `vmo`, `geolon`, 
        `geolat`, `depth`, `zmod`, `wet`, and `basin_masks`
    """

    # initialize an xarray.Dataset to hold the output
    dset = xr.Dataset()

    # read the infile and get u, v transport components
    infile = dictArgs["infile"]
    ds = xr.open_mfdataset(infile, combine="by_coords")
    dset["umo"] = ds[ucomp]
    dset["vmo"] = ds[vcomp]

    # determine vertical coordinate
    layer = "z_l" if "z_l" in ds.dims else "rho2_l" if "rho2_l" in ds.dims else None
    assert layer is not None, "Unrecognized vertical coordinate."

    # get vertical coordinate edges
    interface = "z_i" if layer == "z_l" else "rho2_i" if layer == "rho2_l" else None
    dset[interface] = ds[interface]

    # save layer and interface info for use later in the workflow
    dset.attrs["layer"] = layer
    dset.attrs["interface"] = interface

    # get horizontal t-cell grid info
    dsT = horizontal_grid(dictArgs, point_type="t")
    dset["geolon"] = xr.DataArray(dsT.geolon.values, dims=("yh", "xh"))
    dset["geolat"] = xr.DataArray(dsT.geolat.values, dims=("yh", "xh"))

    # get topography info
    _depth = read_topography(dictArgs, coords=ds.coords, point_type="t")
    depth = np.where(np.isnan(_depth.to_masked_array()), 0.0, _depth)
    dset["depth"] = xr.DataArray(depth, dims=("yh", "xh"))

    # replicates older get_z() func from m6plot
    zmod, _ = xr.broadcast(dset[interface], xr.ones_like(dset.geolat))
    zmod = xr.ufuncs.minimum(dset.depth, xr.ufuncs.fabs(zmod)) * -1.0
    zmod = zmod.transpose(interface, "yh", "xh")
    dset["zmod"] = zmod

    # grid wet mask based on model's topography
    wet = np.where(np.isnan(_depth.to_masked_array()), 0.0, 1.0)
    dset["wet"] = xr.DataArray(wet, dims=("yh", "xh"))

    # basin masks
    basin_code = dsT.basin.values
    basins = ["atlantic_arctic", "indo_pacific"]
    basins = [generate_basin_masks(basin_code, basin=x) for x in basins]
    basins = [xr.DataArray(x, dims=("yh", "xh")) for x in basins]
    basins = xr.concat(basins, dim="basin")
    dset["basin_masks"] = basins

    # date range
    dates = date_range(ds)
    dset.attrs["dates"] = dates

    return dset


def calculate(dset):
    """Main computational script"""

    basins = ["atl-arc", "indopac", "global"]
    otsfn = [
        xoverturning.calcmoc(
            dset, basin=x, layer=dset.layer, interface=dset.interface, verbose=False
        )
        for x in basins
    ]
    otsfn = xr.concat(otsfn, dim="basin")
    otsfn = otsfn.transpose(otsfn.dims[1], otsfn.dims[0], ...)

    return otsfn


def plot(dset, otsfn, label=None):
    """Plotting script"""

    def _findExtrema(
        ax, y, z, psi, min_lat=-90.0, max_lat=90.0, min_depth=0.0, mult=1.0
    ):
        """Function to annotate the max/min values on the MOC plot"""
        psiMax = mult * np.amax(
            mult * np.ma.array(psi)[(y >= min_lat) & (y <= max_lat) & (z < -min_depth)]
        )
        idx = np.argmin(np.abs(psi - psiMax))
        (j, i) = np.unravel_index(idx, psi.shape)
        ax.plot(y[j, i], z[j, i], "kx")
        ax.text(y[j, i], z[j, i], "%.1f" % (psi[j, i]))

    def _plotPsi(
        ax,
        y,
        z,
        psi,
        ci,
        title,
        cmap=None,
        xlim=None,
        topomask=None,
        yh=None,
        zz=None,
        dates=None,
    ):
        """Function to plot zonal mean streamfunction"""
        if topomask is not None:
            psi = np.array(np.where(psi.mask, 0.0, psi))
        else:
            psi = np.array(np.where(psi.mask, np.nan, psi))

        cs = ax.contourf(y, z, psi, levels=ci, cmap=cmap, extend="both")
        ax.contour(y, z, psi, levels=ci, colors="k", linewidths=0.4)
        ax.contour(y, z, psi, levels=[0], colors="k", linewidths=0.8)

        # shade topography
        if topomask is not None:
            cMap = mpl.colors.ListedColormap(["gray"])
            ax.pcolormesh(yh, -1.0 * zz, topomask, cmap=cMap, shading="auto")

        # set latitude limits
        if xlim is not None:
            ax.set_xlim(xlim)

        # set vertical split scale
        ax.set_yscale("splitscale", zval=[0, -2000, -6500])
        ax.invert_yaxis()

        # add colorbar
        cbar = plt.colorbar(cs)
        cbar.set_label("[Sv]")

        # add labels
        ax.text(0.02, 1.02, title, ha="left", fontsize=10, transform=ax.transAxes)
        plt.ylabel("Elevation [m]")
        if dates is not None:
            assert isinstance(dates, tuple), "Year range should be provided as a tuple."
            datestring = f"Years {dates[0]} - {dates[1]}"
            ax.text(
                0.98, 1.02, datestring, ha="right", fontsize=10, transform=ax.transAxes
            )

    def _create_topomask(depth, yh, mask=None):
        if mask is not None:
            depth = np.where(mask == 1, depth, 0.0)
        topomask = depth.max(axis=-1)
        _y = yh
        _z = np.arange(0, 7100, 100)
        _yy, _zz = np.meshgrid(_y, _z)
        topomask = np.tile(topomask[None, :], (len(_z), 1))
        topomask = np.ma.masked_where(_zz < topomask, topomask)
        topomask = topomask * 0.0
        return topomask, _z

    # get y-coord from geolat
    y = dset.geolat.values
    z = dset.zmod.values
    yh = dset.yh.values
    depth = dset.depth.values
    atlantic_arctic_mask = dset.basin_masks.isel(basin=0)
    indo_pacific_mask = dset.basin_masks.isel(basin=1)
    dates = dset.dates

    if len(z.shape) != 1:
        z = z.min(axis=-1)
    yy = y[:, :].max(axis=-1) + 0 * z

    psi = otsfn.to_masked_array()

    atlantic_topomask, zz = _create_topomask(depth, yh, atlantic_arctic_mask)
    indo_pacific_topomask, zz = _create_topomask(depth, yh, indo_pacific_mask)
    global_topomask, zz = _create_topomask(depth, yh)

    ci = m6plot.formatting.pmCI(0.0, 43.0, 3.0)
    cmap = palettable.cmocean.diverging.Balance_20.get_mpl_colormap()

    fig = plt.figure(figsize=(8.5, 11))
    ax1 = plt.subplot(3, 1, 1, facecolor="gray")
    psiPlot = psi[0, 0]
    _plotPsi(
        ax1,
        yy,
        z,
        psiPlot,
        ci,
        "a. Atlantic MOC [Sv]",
        cmap=cmap,
        xlim=(-40, 90),
        topomask=atlantic_topomask,
        yh=yh,
        zz=zz,
        dates=dates,
    )
    _findExtrema(ax1, yy, z, psiPlot, min_lat=26.5, max_lat=27.0)
    _findExtrema(ax1, yy, z, psiPlot, max_lat=-33.0)
    _findExtrema(ax1, yy, z, psiPlot)

    ax2 = plt.subplot(3, 1, 2, facecolor="gray")
    psiPlot = psi[0, 1]
    _plotPsi(
        ax2,
        yy,
        z,
        psiPlot,
        ci,
        "b. Indo-Pacific MOC [Sv]",
        cmap=cmap,
        xlim=(-40, 65),
        topomask=indo_pacific_topomask,
        yh=yh,
        zz=zz,
    )
    _findExtrema(ax2, yy, z, psiPlot, min_depth=2000.0, mult=-1.0)
    _findExtrema(ax2, yy, z, psiPlot)

    ax3 = plt.subplot(3, 1, 3, facecolor="gray")
    psiPlot = psi[0, 2]
    _plotPsi(
        ax3,
        yy,
        z,
        psiPlot,
        ci,
        "c. Global MOC [Sv]",
        cmap=cmap,
        topomask=global_topomask,
        yh=yh,
        zz=zz,
    )
    _findExtrema(ax3, yy, z, psiPlot, max_lat=-30.0)
    _findExtrema(ax3, yy, z, psiPlot, min_lat=25.0)
    _findExtrema(ax3, yy, z, psiPlot, min_depth=2000.0, mult=-1.0)
    plt.xlabel(r"Latitude [$\degree$N]")

    plt.subplots_adjust(hspace=0.2)

    if label is not None:
        plt.suptitle(label)

    return fig


def run(dictArgs):
    """Function to call read, calc, and plot in sequence"""

    # set visual backend
    if dictArgs["interactive"] is False:
        plt.switch_backend("Agg")

    # read in data
    dset = read(dictArgs)

    # calculate otsfn
    otsfn = calculate(dset)

    # make the plots
    fig = plot(dset, otsfn, dictArgs["label"],)
    # ---------------------

    filename = f"{dictArgs['outdir']}/moc"
    imgbufs = image_handler([fig], dictArgs, filename=filename)

    return imgbufs


def parse_and_run(cliargs=None):
    args = parse(cliargs)
    args = args.__dict__
    imgbuf = run(args)
    return imgbuf


if __name__ == "__main__":
    parse_and_run()
