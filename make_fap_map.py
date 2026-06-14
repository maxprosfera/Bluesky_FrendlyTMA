"""
Generate a combined CDO-optimal trajectory map for tmaopt_20260512_083800
showing all 4 aircraft CDO tracks, TMA boundary, ESSA runways, and FAPs.
Output: scenario/TMAOpt/tmaopt_20260512_083800/Figures/tmaopt_20260512_083800_fap_map.png
"""

import csv, math
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pyproj import Transformer

# ── paths ─────────────────────────────────────────────────────────────────────
_REPO  = Path(__file__).parent
_SCN   = _REPO / 'scenario/TMAOpt/tmaopt_20260512_083800'
_CSV   = _SCN  / 'tmaopt_20260512_083800_cdo_opt.csv'
_OUT   = _SCN  / 'Figures/tmaopt_20260512_083800_fap_map.png'
_OUT.parent.mkdir(parents=True, exist_ok=True)

# ── TMA boundary (Stockholm TMA simplified) ───────────────────────────────────
_TMA_POLY = [
    (60.299444,18.213056),(60.266111,18.554722),(60.200000,18.900000),
    (60.050000,19.200000),(59.850000,19.350000),(59.550000,19.200000),
    (59.300000,18.900000),(59.100000,18.500000),(58.900000,18.100000),
    (58.900000,17.500000),(59.100000,16.900000),(59.400000,16.600000),
    (59.700000,16.500000),(60.000000,16.700000),(60.200000,17.200000),
    (60.299444,18.213056),
]

# ── ESSA runways (thr-to-thr pairs, lat/lon) ──────────────────────────────────
_RUNWAYS = [
    # 01L/19R  (main long runway)
    ((59.6191, 17.9316), (59.5114, 17.9132)),
    # 01R/19L  (parallel)
    ((59.6966, 17.9462), (59.5508, 17.9159)),
    # 08/26    (cross-wind)
    ((59.6485, 17.8685), (59.6412, 17.9880)),
]

# ── FAP points (from LFV IAIP charts, computed from DME/radial) ───────────────
_FAP_OFFSETS = {
    'FAP 01L': ( 8,  6),
    'FAP 19R': ( 8, -16),
    'FAP 01R': ( 8,  6),
    'FAP 19L': ( 8, -16),
    'FAP 26':  ( 8,  6),
    'FAF 08':  (-90, 6),
}

_FAPS = {
    'FAP 01L': (59.7298, 17.9454),
    'FAP 19R': (59.5724, 17.9126),
    'FAP 01R': (59.6966, 17.9462),
    'FAP 19L': (59.5508, 17.9159),
    'FAP 26':  (59.6822, 18.1129),
    'FAF 08':  (59.6310, 17.6738),
}

# ── TMA entry points ──────────────────────────────────────────────────────────
_ENTRIES = {
    'NILUG': (58.865, 17.780),
    'ELTOK': (59.755, 16.718),
    'XILAN': (59.683, 19.172),
    'HMR':   (60.300, 18.213),
    'BALVI': (59.583, 20.133),
}

# ── per-aircraft colours ──────────────────────────────────────────────────────
_COLORS = {
    'SAS2023': '#1f77b4',
    'NSZ8GH':  '#d62728',
    'SAS51N':  '#2ca02c',
    'FIN3EJ':  '#9467bd',
}

# ── load CDO tracks ───────────────────────────────────────────────────────────
tracks = {}
with open(_CSV) as f:
    for r in csv.DictReader(f):
        tracks.setdefault(r['callsign'], []).append(
            (float(r['lat']), float(r['lon']), float(r['baro_alt_m']))
        )

# ── coordinate transformer  WGS84 → Web Mercator ─────────────────────────────
t = Transformer.from_crs('EPSG:4326', 'EPSG:3857', always_xy=True)

def ll2xy(lat, lon):
    x, y = t.transform(lon, lat)
    return x, y

def lls2xy(pairs):
    xs = [ll2xy(la, lo)[0] for la, lo in pairs]
    ys = [ll2xy(la, lo)[1] for la, lo in pairs]
    return xs, ys

# ── figure ────────────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(12, 12))
fig.subplots_adjust(left=0.08, right=0.97, top=0.94, bottom=0.06)
fig.suptitle('CDO-Optimal Trajectories — ESSA Stockholm Arlanda\n'
             'tmaopt_20260512_083800', fontsize=13, fontweight='bold')

# basemap
try:
    import contextily as ctx
    # collect all projected coords for bounds
    all_x, all_y = [], []
    for pts in tracks.values():
        for la, lo, _ in pts:
            x, y = ll2xy(la, lo)
            all_x.append(x); all_y.append(y)
    for la, lo in _FAPS.values():
        x, y = ll2xy(la, lo); all_x.append(x); all_y.append(y)
    for la, lo in _ENTRIES.values():
        x, y = ll2xy(la, lo); all_x.append(x); all_y.append(y)

    margin = 20000
    ax.set_xlim(min(all_x)-margin, max(all_x)+margin)
    ax.set_ylim(min(all_y)-margin, max(all_y)+margin)
    ctx.add_basemap(ax, crs='EPSG:3857',
                    source=ctx.providers.OpenStreetMap.Mapnik, zoom='auto')
    basemap_ok = True
except Exception as e:
    print(f'Basemap skipped: {e}')
    basemap_ok = False

# ── TMA boundary ──────────────────────────────────────────────────────────────
tma_xs, tma_ys = lls2xy([(la, lo) for la, lo in _TMA_POLY])
ax.plot(tma_xs, tma_ys, color='#1870A2', linewidth=1.8, label='TMA boundary', zorder=3)

# ── ESSA runways ──────────────────────────────────────────────────────────────
for i, (thr1, thr2) in enumerate(_RUNWAYS):
    xs, ys = lls2xy([thr1, thr2])
    ax.plot(xs, ys, 'k-', linewidth=3.5, zorder=4, label='ESSA runway' if i == 0 else None)

# ── CDO tracks ────────────────────────────────────────────────────────────────
for cs, pts in tracks.items():
    color = _COLORS.get(cs, '#888888')
    xs = [ll2xy(la, lo)[0] for la, lo, _ in pts]
    ys = [ll2xy(la, lo)[1] for la, lo, _ in pts]
    ax.plot(xs, ys, '-', color=color, linewidth=2.0, label=cs, zorder=5)
    # TMA entry marker
    ax.plot(xs[0], ys[0], 'o', color=color, markersize=7, zorder=6)

# ── FAP markers ───────────────────────────────────────────────────────────────
fap_x0, fap_y0 = None, None
for name, (lat, lon) in _FAPS.items():
    x, y = ll2xy(lat, lon)
    ax.plot(x, y, 'k*', markersize=14, zorder=7,
            label='FAP / FAF' if fap_x0 is None else None)
    offx, offy = _FAP_OFFSETS.get(name, (8, 6))
    ax.annotate(name, (x, y), xytext=(offx, offy),
                textcoords='offset points',
                fontsize=8.5, fontweight='bold', zorder=8,
                arrowprops=dict(arrowstyle='->', lw=0.8, color='#333333'),
                bbox=dict(boxstyle='round,pad=0.2', fc='white', alpha=0.75, lw=0))
    fap_x0 = x

# ── TMA entry point markers ───────────────────────────────────────────────────
for name, (lat, lon) in _ENTRIES.items():
    x, y = ll2xy(lat, lon)
    ax.plot(x, y, 'k^', markersize=9, zorder=7)
    ax.annotate(name, (x, y), xytext=(5, 4), textcoords='offset points',
                fontsize=8, color='#333333', zorder=8)

# ── axes formatting ───────────────────────────────────────────────────────────
_inv = Transformer.from_crs('EPSG:3857', 'EPSG:4326', always_xy=True)
def fmt_lon(v, _):
    return f"{_inv.transform(v, 0)[0]:.1f}°E"
def fmt_lat(v, _):
    return f"{_inv.transform(0, v)[1]:.1f}°N"

ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_lon))
ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_lat))
ax.set_xlabel('Longitude', fontsize=10)
ax.set_ylabel('Latitude',  fontsize=10)
ax.legend(loc='lower left', fontsize=8, framealpha=0.85)

plt.savefig(_OUT, dpi=150)
print(f'Saved: {_OUT}')
