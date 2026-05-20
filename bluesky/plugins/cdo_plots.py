"""CDO diagram generator — replicates the three Matlab figures from fuel_burn_v3.m.

fig1  — 6×2 subplot grid: Mach, TAS [kt], CAS [kt], Alt [FL],
         ROD [ft/min], Fuel flow [kg/h]  vs  time-to-go [min] (left)
         and distance-to-go [NM] (right).
fig2  — 4×2 subplot grid: wind component, wind speed [kt],
         wind direction [deg], flight track [deg]  vs  ttg / dtg.
         (wind data from ERA5 if available, else omitted)
fig3  — geographic map with TMA boundary, ESSA runways, entry points,
         FAP/FAF markers, STAR outlines, and the CDO track.

All figures are saved as PNG in a Figures/ sub-directory next to the CDO CSVs.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import List, Dict, Any

_MS_TO_KT = 1.94384
_M_TO_FT  = 3.28084
_FT_TO_FL = 1.0 / 100.0

# ── ESSA geographic constants (same as fuel_burn_v3.m) ─────────────────────
_TMA_LAT = [
    59.0, 59.0, 59.5, 60.0, 60.5, 61.0, 61.0, 60.5,
    60.0, 59.5, 59.0, 59.0,
]
_TMA_LON = [
    17.0, 20.5, 21.0, 21.0, 20.5, 20.5, 17.5, 17.0,
    17.0, 17.0, 17.0, 17.0,
]

_ESSA_RWY = [
    ((59.6526, 17.9180), (59.5114, 17.9132)),  # 01L / 19R
    ((59.6498, 17.9468), (59.5178, 17.9468)),  # 01R / 19L
    ((59.6412, 17.9180), (59.6412, 17.9880)),  # 08 / 26
]

_ENTRY_POINTS = {
    'NILUG': (58.75, 17.78),
    'ELTOK': (59.76, 16.82),
    'XILAN': (59.68, 19.17),
    'HMR':   (60.30, 18.49),
}

_FAP_POINTS = {
    'FAP_01L': (59.5114, 17.9132),
    'FAP_01R': (59.5178, 17.9468),
    'FAP_19R': (59.6526, 17.9180),
    'FAP_19L': (59.6498, 17.9468),
    'FAP_26':  (59.6412, 17.9880),
    'FAF_08':  (59.6412, 17.9180),
}

_STAR_ROUTES = {
    'ELTOK': [(59.76, 16.82), (59.65, 17.35), (59.58, 17.70)],
    'HMR':   [(60.30, 18.49), (60.10, 18.30), (59.80, 18.10)],
    'NILUG': [(58.75, 17.78), (59.00, 17.78), (59.30, 17.78)],
    'XILAN': [(59.68, 19.17), (59.65, 18.80), (59.60, 18.40)],
}


def _ttg(times):
    """Minutes to go (reversed so 0 = landing)."""
    t_arr = times[-1]
    return [(t_arr - t) / 60.0 for t in times]


def _dtg(lats, lons):
    """Cumulative distance to go in NM (reversed)."""
    from bluesky.plugins.tma_opt import _haversine_nm
    cum = [0.0]
    for i in range(1, len(lats)):
        cum.append(cum[-1] + _haversine_nm((lats[i-1], lons[i-1]), (lats[i], lons[i])))
    total = cum[-1]
    return [total - c for c in cum]


def generate_cdo_figures(
    rows: List[Dict[str, Any]],
    callsign: str,
    actype: str,
    out_dir: Path,
    stem: str,
    weather_rows: List[Dict[str, Any]] | None = None,
):
    """Generate fig1, fig2, fig3 PNG files for one aircraft CDO profile.

    Parameters
    ----------
    rows        : list of row-dicts from _cdo_for_aircraft() (extended fields)
    callsign    : aircraft callsign
    actype      : ICAO type code
    out_dir     : TMAOpt output directory (Figures/ sub-dir created here)
    stem        : filename stem (e.g. 'tmaopt_20260507_132700')
    weather_rows: optional list of ERA5 weather rows aligned to CDO rows
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
    except ImportError:
        return

    if not rows:
        return

    fig_dir = out_dir / 'Figures'
    fig_dir.mkdir(parents=True, exist_ok=True)

    times   = [r['time'] for r in rows]
    lats    = [r['lat'] for r in rows]
    lons    = [r['lon'] for r in rows]
    alts_fl = [r['baro_alt_m'] * _M_TO_FT * _FT_TO_FL for r in rows]
    rod     = [r.get('vertical_rate_ms', 0) * _M_TO_FT * 60.0 for r in rows]
    tas_kt  = [r.get('velocity_ms', 0) * _MS_TO_KT for r in rows]
    cas_kt  = [r.get('cas_ms', 0) * _MS_TO_KT for r in rows]
    mach    = [r.get('mach', 0) for r in rows]
    ff_kgh  = [r.get('fuel_flow_kg_s', 0) * 3600.0 for r in rows]
    tracks  = [r.get('true_track', 0) for r in rows]

    ttg = _ttg(times)
    dtg = _dtg(lats, lons)

    title = f'{callsign} — {actype} — ESSA 01L CDO'

    # ── Figure 1: flight performance profile ───────────────────────────────
    fig1, axes = plt.subplots(6, 2, figsize=(14, 20))
    fig1.suptitle(title, fontsize=13, fontweight='bold')

    _pairs = [
        (mach,   'M [-]'),
        (tas_kt, 'TAS [kt]'),
        (cas_kt, 'CAS [kt]'),
        (alts_fl,'Alt [FL]'),
        (rod,    'ROD [ft/min]'),
        (ff_kgh, 'Fuel flow [kg/h]'),
    ]

    for row_i, (data, ylabel) in enumerate(_pairs):
        ax_l = axes[row_i][0]
        ax_r = axes[row_i][1]

        ax_l.plot(ttg, data, linewidth=1.3, color='#1f77b4')
        ax_l.set_xlabel('Time to go [min]')
        ax_l.set_ylabel(ylabel)
        ax_l.invert_xaxis()
        ax_l.grid(True, alpha=0.4)

        ax_r.plot(dtg, data, linewidth=1.3, color='#1f77b4')
        ax_r.set_xlabel('Distance to go [NM]')
        ax_r.set_ylabel(ylabel)
        ax_r.invert_xaxis()
        ax_r.grid(True, alpha=0.4)

    fig1.tight_layout(rect=[0, 0, 1, 0.97])
    fig1.savefig(fig_dir / f'{stem}_{callsign}_{actype}_fig1.png', dpi=150)
    plt.close(fig1)

    # ── Figure 2: wind & track profiles ────────────────────────────────────
    fig2, axes2 = plt.subplots(4, 2, figsize=(12, 14))
    fig2.suptitle(title, fontsize=13, fontweight='bold')

    if weather_rows and len(weather_rows) == len(rows):
        wind_comp = [r.get('wind_comp', 0) for r in weather_rows]
        wind_spd  = [r.get('wind_speed_ms', 0) * _MS_TO_KT for r in weather_rows]
        wind_dir  = [r.get('wind_dir_deg', 0) for r in weather_rows]
    else:
        wind_comp = [0.0] * len(rows)
        wind_spd  = [0.0] * len(rows)
        wind_dir  = [0.0] * len(rows)

    _wind_pairs = [
        (wind_comp, 'Wind comp [-]'),
        (wind_spd,  'Wind speed [kt]'),
        (wind_dir,  'Wind dir [deg]'),
        (tracks,    'Flight track [deg]'),
    ]

    for row_i, (data, ylabel) in enumerate(_wind_pairs):
        ax_l = axes2[row_i][0]
        ax_r = axes2[row_i][1]

        ax_l.plot(ttg, data, linewidth=1.3, color='#2ca02c')
        ax_l.set_xlabel('Time to go [min]')
        ax_l.set_ylabel(ylabel)
        ax_l.invert_xaxis()
        ax_l.grid(True, alpha=0.4)

        ax_r.plot(dtg, data, linewidth=1.3, color='#2ca02c')
        ax_r.set_xlabel('Distance to go [NM]')
        ax_r.set_ylabel(ylabel)
        ax_r.invert_xaxis()
        ax_r.grid(True, alpha=0.4)

    fig2.tight_layout(rect=[0, 0, 1, 0.97])
    fig2.savefig(fig_dir / f'{stem}_{callsign}_{actype}_fig2.png', dpi=150)
    plt.close(fig2)

    # ── Figure 3: geographic map ────────────────────────────────────────────
    fig3, ax3 = plt.subplots(figsize=(9, 9))
    fig3.suptitle(title, fontsize=13, fontweight='bold')

    ax3.plot(_TMA_LON, _TMA_LAT, color='#1870A2', linewidth=1.2, label='TMA boundary')

    for (lat1, lon1), (lat2, lon2) in _ESSA_RWY:
        ax3.plot([lon1, lon2], [lat1, lat2], 'k-', linewidth=2)

    for name, (lat, lon) in _ENTRY_POINTS.items():
        ax3.plot(lon, lat, 'k^', markersize=8, linewidth=2)
        ax3.text(lon + 0.03, lat, name, fontsize=8)

    for name, (lat, lon) in _FAP_POINTS.items():
        ax3.plot(lon, lat, 'k*', markersize=6)

    for name, pts in _STAR_ROUTES.items():
        slat = [p[0] for p in pts]
        slon = [p[1] for p in pts]
        ax3.plot(slon, slat, '--o', color='#797979', markersize=4, linewidth=1)

    ax3.plot(lons, lats, 'k-', linewidth=1.8, label='CDO track')
    ax3.plot(lons[0], lats[0], 'ro', markersize=7, label='TMA entry')
    ax3.plot(lons[-1], lats[-1], 'rs', markersize=7, label='FAP')

    ax3.set_xlabel('Longitude')
    ax3.set_ylabel('Latitude')
    ax3.legend(fontsize=8, loc='lower right')
    ax3.grid(True, alpha=0.3)
    ax3.set_aspect('equal', adjustable='datalim')

    fig3.tight_layout(rect=[0, 0, 1, 0.97])
    fig3.savefig(fig_dir / f'{stem}_{callsign}_{actype}_fig3.png', dpi=150)
    plt.close(fig3)


def generate_all_cdo_figures(
    all_cdo_data: Dict[str, Dict],
    out_dir: Path,
    stem: str,
):
    """Generate figures for all aircraft in a TMAOpt run.

    Parameters
    ----------
    all_cdo_data : {callsign: {'rows': [...], 'actype': '...'}}
    out_dir      : TMAOpt output directory
    stem         : filename stem
    """
    for cs, data in all_cdo_data.items():
        rows   = data.get('rows', [])
        actype = data.get('actype', 'UNKN')
        try:
            generate_cdo_figures(rows, cs, actype, out_dir, stem)
        except Exception as exc:
            pass
