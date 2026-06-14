"""CDO diagram generator — replicates the three Matlab figures from fuel_burn_v3.m.

fig1  — 7×2 subplot grid: Mach, GS [kt], TAS [kt], CAS [kt], Alt [FL],
         Vertical speed [ft/min], Fuel flow [kg/h]  vs  time-to-go [min] (left)
         and distance-to-go [NM] (right).
         Two lines: CDO (orange) and original flight (blue), matching Matlab style.
fig2  — 4×2 subplot grid: wind component [-], wind speed [kt],
         wind direction [deg], flight track [deg]  vs  ttg / dtg.
         Two lines where original flight data is available.
fig3  — geographic map with basemap tiles, TMA boundary, ESSA runways,
         entry points, FAP markers, STAR outlines, CDO track, original track.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import List, Dict, Any, Optional

_MS_TO_KT = 1.94384
_M_TO_FT  = 3.28084
_FT_TO_FL = 1.0 / 100.0

_TMA_LAT = [
    59.0, 59.0, 59.5, 60.0, 60.5, 61.0, 61.0, 60.5,
    60.0, 59.5, 59.0, 59.0,
]
_TMA_LON = [
    17.0, 20.5, 21.0, 21.0, 20.5, 20.5, 17.5, 17.0,
    17.0, 17.0, 17.0, 17.0,
]

_ESSA_RWY = [
    ((59.6526, 17.9180), (59.5114, 17.9132)),
    ((59.6498, 17.9468), (59.5178, 17.9468)),
    ((59.6412, 17.9180), (59.6412, 17.9880)),
]

_ENTRY_POINTS = {
    'NILUG': (58.75, 17.78),
    'ELTOK': (59.76, 16.82),
    'XILAN': (59.68, 19.17),
    'HMR':   (60.30, 18.49),
}

_FAP_POINTS = {
    'FAP 01L': (59.7298, 17.9454),
    'FAP 01R': (59.6966, 17.9462),
    'FAP 19R': (59.5724, 17.9126),
    'FAP 19L': (59.5508, 17.9159),
    'FAP 26':  (59.6822, 18.1129),
    'FAF 08':  (59.6310, 17.6738),
}

_STAR_ROUTES = {
    'ELTOK': [(59.76, 16.82), (59.65, 17.35), (59.58, 17.70)],
    'HMR':   [(60.30, 18.49), (60.10, 18.30), (59.80, 18.10)],
    'NILUG': [(58.75, 17.78), (59.00, 17.78), (59.30, 17.78)],
    'XILAN': [(59.68, 19.17), (59.65, 18.80), (59.60, 18.40)],
}


def _ttg(times):
    t_arr = times[-1]
    return [(t_arr - t) / 60.0 for t in times]


def _dtg_from_rows(rows):
    try:
        from bluesky.plugins.tma_opt import _haversine_nm
    except ImportError:
        def _haversine_nm(a, b):
            lat1, lon1 = math.radians(a[0]), math.radians(a[1])
            lat2, lon2 = math.radians(b[0]), math.radians(b[1])
            dlat, dlon = lat2 - lat1, lon2 - lon1
            h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
            return 2 * 3440.065 * math.asin(math.sqrt(h))
    cum = [0.0]
    for i in range(1, len(rows)):
        d = _haversine_nm(
            (rows[i-1]['lat'], rows[i-1]['lon']),
            (rows[i]['lat'],   rows[i]['lon'])
        )
        cum.append(cum[-1] + d)
    total = cum[-1]
    return [total - c for c in cum]


def _extract(rows, key, scale=1.0, default=0.0):
    return [float(r.get(key, default)) * scale for r in rows]


def _resample_to_dtg(src_dtg, src_vals, tgt_dtg):
    """Linear interpolation of src_vals (indexed by src_dtg) onto tgt_dtg grid."""
    if not src_dtg or not tgt_dtg:
        return [0.0] * len(tgt_dtg)
    result = []
    n = len(src_dtg)
    for d in tgt_dtg:
        if d >= src_dtg[0]:
            result.append(src_vals[0])
        elif d <= src_dtg[-1]:
            result.append(src_vals[-1])
        else:
            for i in range(n - 1):
                if src_dtg[i] >= d >= src_dtg[i + 1]:
                    t = (src_dtg[i] - d) / (src_dtg[i] - src_dtg[i + 1]) if src_dtg[i] != src_dtg[i + 1] else 0.0
                    result.append(src_vals[i] * (1 - t) + src_vals[i + 1] * t)
                    break
            else:
                result.append(src_vals[-1])
    return result


def generate_cdo_figures(
    rows: List[Dict[str, Any]],
    callsign: str,
    actype: str,
    out_dir: Path,
    stem: str,
    orig_rows: Optional[List[Dict[str, Any]]] = None,
):
    """Generate fig1, fig2, fig3 PNG files for one aircraft CDO profile.

    Parameters
    ----------
    rows      : CDO profile rows from _cdo_for_aircraft()
    callsign  : aircraft callsign
    actype    : ICAO type code
    out_dir   : TMAOpt output directory
    stem      : filename stem
    orig_rows : original (historical) flight rows for comparison (optional)
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return

    if not rows:
        return

    fig_dir = out_dir / 'Figures'
    fig_dir.mkdir(parents=True, exist_ok=True)

    title = f'{callsign} — {actype} — ESSA CDO'

    # ── CDO data ───────────────────────────────────────────────────────────
    cdo_ttg  = _ttg(_extract(rows, 'time'))
    cdo_dtg  = _dtg_from_rows(rows)
    cdo_mach = _extract(rows, 'mach')
    cdo_gs   = _extract(rows, 'velocity_ms', _MS_TO_KT)
    cdo_tas  = _extract(rows, 'velocity_ms', _MS_TO_KT)
    cdo_cas  = _extract(rows, 'cas_ms', _MS_TO_KT)
    cdo_alt  = [r.get('baro_alt_m', 0) * _M_TO_FT * _FT_TO_FL for r in rows]
    cdo_vs   = _extract(rows, 'vertical_rate_ms', _M_TO_FT * 60.0)
    cdo_ff   = _extract(rows, 'fuel_flow_kg_s', 3600.0)
    cdo_trk  = _extract(rows, 'true_track')
    cdo_lats = _extract(rows, 'lat')
    cdo_lons = _extract(rows, 'lon')

    # ── Original flight data (resampled to same dtg grid) ─────────────────
    has_orig = bool(orig_rows and len(orig_rows) > 2)
    if has_orig:
        orig_dtg  = _dtg_from_rows(orig_rows)
        orig_mach = _extract(orig_rows, 'mach')
        orig_gs   = _extract(orig_rows, 'velocity_ms', _MS_TO_KT)
        orig_tas  = _extract(orig_rows, 'velocity_ms', _MS_TO_KT)
        orig_cas  = _extract(orig_rows, 'cas_ms', _MS_TO_KT)
        orig_alt  = [r.get('baro_alt_m', 0) * _M_TO_FT * _FT_TO_FL for r in orig_rows]
        orig_vs   = _extract(orig_rows, 'vertical_rate_ms', _M_TO_FT * 60.0)
        orig_ff   = _extract(orig_rows, 'fuel_flow_kg_s', 3600.0)
        orig_trk  = _extract(orig_rows, 'true_track')
        orig_lats = _extract(orig_rows, 'lat')
        orig_lons = _extract(orig_rows, 'lon')
        orig_ttg  = _ttg(_extract(orig_rows, 'time'))
    else:
        orig_dtg = orig_ttg = []

    # ── Figure 1: flight performance ───────────────────────────────────────
    fig1, axes = plt.subplots(7, 2, figsize=(14, 22))
    fig1.suptitle(title, fontsize=13, fontweight='bold')

    _perf = [
        (cdo_mach, orig_mach if has_orig else None, 'M [-]'),
        (cdo_gs,   orig_gs   if has_orig else None, 'GS [kt]'),
        (cdo_tas,  orig_tas  if has_orig else None, 'TAS [kt]'),
        (cdo_cas,  orig_cas  if has_orig else None, 'CAS [kt]'),
        (cdo_alt,  orig_alt  if has_orig else None, 'Alt [FL]'),
        (cdo_vs,   orig_vs   if has_orig else None, 'Vertical speed [ft/min]'),
        (cdo_ff,   orig_ff   if has_orig else None, 'Fuel flow [kg/h]'),
    ]

    for row_i, (cdo_d, orig_d, ylabel) in enumerate(_perf):
        for col_i, (xdata_cdo, xdata_orig, xlabel) in enumerate([
            (cdo_ttg,  orig_ttg,  'Time to go [min]'),
            (cdo_dtg,  orig_dtg,  'Distance to go [NM]'),
        ]):
            ax = axes[row_i][col_i]
            if has_orig and orig_d and xdata_orig:
                ax.plot(xdata_orig, orig_d, linewidth=1.2, color='#1f77b4', label='Original')
            ax.plot(xdata_cdo, cdo_d, linewidth=1.2, color='#ff7f0e', label='CDO')
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.invert_xaxis()
            ax.grid(True, alpha=0.4)
            if row_i == 0 and col_i == 0 and has_orig:
                ax.legend(fontsize=7)

    fig1.tight_layout(rect=[0, 0, 1, 0.97])
    fig1.savefig(fig_dir / f'{stem}_{callsign}_{actype}_fig1.png', dpi=150)
    plt.close(fig1)

    # ── Figure 2: wind & track ─────────────────────────────────────────────
    fig2, axes2 = plt.subplots(4, 2, figsize=(12, 14))
    fig2.suptitle(title, fontsize=13, fontweight='bold')

    _wind = [
        (cdo_trk,  orig_trk  if has_orig else None, 'Flight track [deg]'),
        ([0.0]*len(rows), None, 'Wind comp [-]'),
        ([0.0]*len(rows), None, 'Wind speed [kt]'),
        ([0.0]*len(rows), None, 'Wind dir [deg]'),
    ]

    for row_i, (cdo_d, orig_d, ylabel) in enumerate(_wind):
        for col_i, (xdata_cdo, xdata_orig, xlabel) in enumerate([
            (cdo_ttg, orig_ttg,  'Time to go [min]'),
            (cdo_dtg, orig_dtg,  'Distance to go [NM]'),
        ]):
            ax = axes2[row_i][col_i]
            if has_orig and orig_d and xdata_orig:
                ax.plot(xdata_orig, orig_d, linewidth=1.2, color='#1f77b4', label='Original')
            ax.plot(xdata_cdo, cdo_d, linewidth=1.2, color='#ff7f0e', label='CDO')
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.invert_xaxis()
            ax.grid(True, alpha=0.4)

    fig2.tight_layout(rect=[0, 0, 1, 0.97])
    fig2.savefig(fig_dir / f'{stem}_{callsign}_{actype}_fig2.png', dpi=150)
    plt.close(fig2)

    # ── Figure 3: geographic map ────────────────────────────────────────────
    fig3, ax3 = plt.subplots(figsize=(10, 10))
    fig3.suptitle(title, fontsize=13, fontweight='bold')

    basemap_ok = False
    try:
        import contextily as ctx
        import pyproj
        from matplotlib.patches import Patch
        basemap_ok = True
    except ImportError:
        pass

    if basemap_ok:
        try:
            import numpy as np
            transformer = pyproj.Transformer.from_crs('EPSG:4326', 'EPSG:3857', always_xy=True)

            def _to_web(lons, lats):
                xs, ys = transformer.transform(lons, lats)
                return list(xs), list(ys)

            tma_xs, tma_ys = _to_web(_TMA_LON, _TMA_LAT)
            ax3.plot(tma_xs, tma_ys, color='#1870A2', linewidth=1.5, label='TMA boundary')

            for (lat1, lon1), (lat2, lon2) in _ESSA_RWY:
                xs, ys = _to_web([lon1, lon2], [lat1, lat2])
                ax3.plot(xs, ys, 'k-', linewidth=2.5)

            for name, (lat, lon) in _ENTRY_POINTS.items():
                xs, ys = _to_web([lon], [lat])
                ax3.plot(xs[0], ys[0], 'k^', markersize=9)
                ax3.annotate(name, (xs[0], ys[0]), xytext=(8, 4), textcoords='offset points', fontsize=8)

            for name, (lat, lon) in _FAP_POINTS.items():
                xs, ys = _to_web([lon], [lat])
                ax3.plot(xs[0], ys[0], 'k*', markersize=8)

            for name, pts in _STAR_ROUTES.items():
                slons = [p[1] for p in pts]
                slats = [p[0] for p in pts]
                xs, ys = _to_web(slons, slats)
                ax3.plot(xs, ys, '--o', color='#797979', markersize=4, linewidth=1)

            if has_orig:
                xs, ys = _to_web(orig_lons, orig_lats)
                ax3.plot(xs, ys, '-', color='#1f77b4', linewidth=1.5, label='Original track')

            cdo_xs, cdo_ys = _to_web(cdo_lons, cdo_lats)
            ax3.plot(cdo_xs, cdo_ys, 'k-', linewidth=2.0, label='CDO track')
            ax3.plot(cdo_xs[0], cdo_ys[0], 'ro', markersize=8, label='TMA entry')
            ax3.plot(cdo_xs[-1], cdo_ys[-1], 'rs', markersize=8, label='FAP/threshold')

            margin = 50000
            all_xs = tma_xs + cdo_xs + (list(_to_web(orig_lons, orig_lats)[0]) if has_orig else [])
            all_ys = tma_ys + cdo_ys + (list(_to_web(orig_lons, orig_lats)[1]) if has_orig else [])
            ax3.set_xlim(min(all_xs) - margin, max(all_xs) + margin)
            ax3.set_ylim(min(all_ys) - margin, max(all_ys) + margin)

            ctx.add_basemap(ax3, crs='EPSG:3857', source=ctx.providers.OpenStreetMap.Mapnik, zoom='auto')

            ax3.set_xlabel('Longitude')
            ax3.set_ylabel('Latitude')

            import matplotlib.ticker as mticker
            _inv = pyproj.Transformer.from_crs('EPSG:3857', 'EPSG:4326', always_xy=True)
            ax3.xaxis.set_major_formatter(mticker.FuncFormatter(
                lambda x, _: f'{_inv.transform(x, 0)[0]:.1f}°E'))
            ax3.yaxis.set_major_formatter(mticker.FuncFormatter(
                lambda y, _: f'{_inv.transform(0, y)[1]:.1f}°N'))

        except Exception as _e:
            basemap_ok = False
            import traceback as _tb; _tb.print_exc()

    if not basemap_ok:
        ax3.plot(_TMA_LON, _TMA_LAT, color='#1870A2', linewidth=1.5, label='TMA boundary')
        for (lat1, lon1), (lat2, lon2) in _ESSA_RWY:
            ax3.plot([lon1, lon2], [lat1, lat2], 'k-', linewidth=2.5)
        for name, (lat, lon) in _ENTRY_POINTS.items():
            ax3.plot(lon, lat, 'k^', markersize=9)
            ax3.annotate(name, (lon, lat), xytext=(3, 3), textcoords='offset points', fontsize=8)
        for name, (lat, lon) in _FAP_POINTS.items():
            ax3.plot(lon, lat, 'k*', markersize=8)
        for name, pts in _STAR_ROUTES.items():
            ax3.plot([p[1] for p in pts], [p[0] for p in pts], '--o', color='#797979', markersize=4, linewidth=1)
        if has_orig:
            ax3.plot(orig_lons, orig_lats, '-', color='#1f77b4', linewidth=1.5, label='Original track')
        ax3.plot(cdo_lons, cdo_lats, 'k-', linewidth=2.0, label='CDO track')
        ax3.plot(cdo_lons[0], cdo_lats[0], 'ro', markersize=8, label='TMA entry')
        ax3.plot(cdo_lons[-1], cdo_lats[-1], 'rs', markersize=8, label='FAP/threshold')
        ax3.set_xlabel('Longitude')
        ax3.set_ylabel('Latitude')
        ax3.set_aspect('equal', adjustable='datalim')
        ax3.grid(True, alpha=0.3)

    ax3.legend(fontsize=8, loc='lower right')
    fig3.tight_layout(rect=[0, 0, 1, 0.97])
    fig3.savefig(fig_dir / f'{stem}_{callsign}_{actype}_fig3.png', dpi=150)
    plt.close(fig3)
