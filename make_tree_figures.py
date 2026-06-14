"""
Generate paper-quality figures showing the TMA optimisation tree for a given scenario.

Usage:
    python make_tree_figures.py <scenario_stem> [--out <output_dir>]

Example:
    python make_tree_figures.py tmaopt_20260512_083800

Each scenario produces one figure:
    <stem>_tree.png  —  basemap + full grid (grey) + optimal tree (coloured per entry)
                        + entry markers + merge points + airport
"""

import argparse
import pickle
import sys
from pathlib import Path

import contextily as ctx
import matplotlib
matplotlib.use('Agg')
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.lines as mlines
import numpy as np
from pyproj import Transformer

# ── paths ────────────────────────────────────────────────────────────────────
_HERE    = Path(__file__).parent
_TMA_DIR = _HERE / 'scenario' / 'TMAOpt'
sys.path.insert(0, str(_HERE))

from bluesky.plugins.tma_opt import _GRID_COORDS  # noqa: E402

# ── constants ─────────────────────────────────────────────────────────────────
_AIRPORT_LAT = 59.6519   # ESSA Arlanda
_AIRPORT_LON = 17.9186

_TMA_BOUNDARY = [
    (60.299444, 18.213056), (60.266111, 18.554722), (59.882778, 18.847000),
    (60.035278, 19.313611), (59.673611, 19.830833), (59.599444, 19.273611),
    (59.255000, 18.968333), (59.047500, 18.754722), (58.832500, 18.539444),
    (58.752500, 18.457222), (58.583056, 17.932778), (58.616389, 17.456944),
    (58.966111, 17.407778), (58.978611, 17.223333), (59.012500, 16.707778),
    (59.049444, 16.267778), (59.323889, 16.318333), (59.749444, 16.446667),
    (60.232778, 17.596667), (60.299444, 18.213056),
]

# entry point colours
_ENTRY_COLOURS = {
    'N': '#1f77b4',   # blue
    'W': '#2ca02c',   # green
    'E': '#d62728',   # red
    'S': '#ff7f0e',   # orange
}
_ENTRY_NODES = {9: 'N', 45: 'W', 66: 'E', 160: 'S'}

_WGS84_TO_WEB = Transformer.from_crs('EPSG:4326', 'EPSG:3857', always_xy=True)


def _to_web(lat, lon):
    x, y = _WGS84_TO_WEB.transform(lon, lat)
    return x, y


def _parse_scn_grid(scn_path):
    """Return list of ((lat1,lon1),(lat2,lon2)) from POLYLINE GRID_* lines."""
    edges = []
    with open(scn_path) as f:
        for line in f:
            if 'POLYLINE GRID_' in line:
                parts = line.strip().split()
                # format: timestamp> POLYLINE GRID_NNNN lat1 lon1 lat2 lon2
                idx = parts.index(next(p for p in parts if p.startswith('GRID_')))
                lat1, lon1, lat2, lon2 = map(float, parts[idx+1:idx+5])
                edges.append(((lat1, lon1), (lat2, lon2)))
    return edges


def _build_all_grid_edges():
    """Build grid edges from _GRID_COORDS + LINKS (neighbour grid logic)."""
    edges = []
    coords = _GRID_COORDS
    node_ids = set(coords.keys())
    for n, (lat, lon) in coords.items():
        for m, (mlat, mlon) in coords.items():
            if m <= n:
                continue
            dlat = abs(lat - mlat)
            dlon = abs(lon - mlon)
            if (dlat < 0.15 and dlon < 0.01) or (dlat < 0.01 and dlon < 0.30):
                edges.append((n, m))
    return edges


def _entry_colour(entry_node):
    direction = _ENTRY_NODES.get(entry_node, 'N')
    return _ENTRY_COLOURS[direction]


def make_figure(stem, out_dir):
    pkl_path = _TMA_DIR / stem / 'result.pkl'
    if not pkl_path.is_file():
        print(f'SKIP {stem} — no result.pkl')
        return

    with open(pkl_path, 'rb') as f:
        result = pickle.load(f)

    if not result.get('feasible', False):
        print(f'SKIP {stem} — not feasible')
        return

    tree_links   = result.get('tree_links', [])
    ac_path      = result.get('ac_path', {})
    callsign_map = result.get('callsign_map', {})
    B            = result.get('B', [])
    merge_pts    = result.get('merge_points', [])
    N_exit       = result.get('N_exit', 72)
    AC           = result.get('AC', {})

    if not tree_links:
        print(f'SKIP {stem} — empty tree_links')
        return

    # ── figure ────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 10), dpi=150)

    # ── map extent: tight around TMA + small outside margin ──────────────────
    lon_min, lon_max = 16.10, 20.10
    lat_min, lat_max = 58.42, 60.42
    x0, y0 = _to_web(lat_min, lon_min)
    x1, y1 = _to_web(lat_max, lon_max)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ctx.add_basemap(ax, crs='EPSG:3857',
                    source=ctx.providers.Esri.WorldStreetMap,
                    zoom=9, attribution_size=5)

    # ── TMA boundary ──────────────────────────────────────────────────────────
    bx = [_to_web(lat, lon)[0] for lat, lon in _TMA_BOUNDARY]
    by = [_to_web(lat, lon)[1] for lat, lon in _TMA_BOUNDARY]
    ax.plot(bx, by, color='#2255aa', lw=1.5, ls='-', zorder=3)

    # ── full grid (grey, thin) ─────────────────────────────────────────────────
    grid_edges = _build_all_grid_edges()
    for n1, n2 in grid_edges:
        la1, lo1 = _GRID_COORDS[n1]
        la2, lo2 = _GRID_COORDS[n2]
        xs = [_to_web(la1, lo1)[0], _to_web(la2, lo2)[0]]
        ys = [_to_web(la1, lo1)[1], _to_web(la2, lo2)[1]]
        ax.plot(xs, ys, color='#aaaaaa', lw=0.5, zorder=4, alpha=0.6)

    # ── tree links (coloured per entry subtree) ────────────────────────────────
    # Build node→entry mapping by traversing tree from each entry node
    node_entry = {}

    def _assign_entry(node, entry, tree_adj):
        if node in node_entry:
            return
        node_entry[node] = entry
        for child in tree_adj.get(node, []):
            _assign_entry(child, entry, tree_adj)

    tree_adj = {}
    for (n1, n2) in tree_links:
        tree_adj.setdefault(n1, []).append(n2)

    for b_node in B:
        _assign_entry(b_node, b_node, tree_adj)

    # merge and exit nodes get the last entry that reaches them — just colour grey
    for (n1, n2) in tree_links:
        la1, lo1 = _GRID_COORDS[n1]
        la2, lo2 = _GRID_COORDS[n2]
        xs = [_to_web(la1, lo1)[0], _to_web(la2, lo2)[0]]
        ys = [_to_web(la1, lo1)[1], _to_web(la2, lo2)[1]]
        col = _entry_colour(node_entry.get(n1, B[0] if B else 9))
        ax.plot(xs, ys, color=col, lw=2.8, zorder=6, solid_capstyle='round')
        ax.plot(xs, ys, color='white', lw=1.0, zorder=5,
                solid_capstyle='round', alpha=0.5)

    # ── grid nodes (tiny dots) ─────────────────────────────────────────────────
    for nid, (lat, lon) in _GRID_COORDS.items():
        x, y = _to_web(lat, lon)
        ax.plot(x, y, 'o', color='#888888', ms=1.5, zorder=7, alpha=0.5)

    # ── merge points ──────────────────────────────────────────────────────────
    for mp in merge_pts:
        lat, lon = _GRID_COORDS[mp]
        x, y = _to_web(lat, lon)
        ax.plot(x, y, 's', color='black', ms=8, zorder=10, mew=1.5,
                mfc='white')

    # ── entry nodes (triangles, labelled) ─────────────────────────────────────
    dir_label = {9: 'N', 45: 'W', 66: 'E', 160: 'S'}
    dir_offset = {
        9:   (+0.00, +0.04),
        45:  (-0.06, +0.00),
        66:  (+0.06, +0.00),
        160: (+0.00, -0.04),
    }
    for b_node in B:
        lat, lon = _GRID_COORDS[b_node]
        x, y = _to_web(lat, lon)
        col = _entry_colour(b_node)
        ax.plot(x, y, '^', color=col, ms=12, zorder=11, mew=1.2,
                mec='white')
        dlat, dlon = dir_offset.get(b_node, (0, 0.04))
        tx, ty = _to_web(lat + dlat, lon + dlon)
        direction = dir_label.get(b_node, '?')
        ax.text(tx, ty, direction, ha='center', va='center', fontsize=8,
                fontweight='bold', color=col,
                bbox=dict(fc='white', ec='none', alpha=0.7, pad=1),
                zorder=12)

    # ── airport (star) ─────────────────────────────────────────────────────────
    ax_x, ax_y = _to_web(_AIRPORT_LAT, _AIRPORT_LON)
    ax.plot(ax_x, ax_y, '*', color='black', ms=14, zorder=11, mew=0.8,
            mec='white')
    ax.text(ax_x, ax_y - 28000, 'ESSA', ha='center', va='top', fontsize=8,
            fontweight='bold', color='black',
            bbox=dict(fc='white', ec='none', alpha=0.7, pad=1), zorder=12)

    # ── legend ────────────────────────────────────────────────────────────────
    legend_handles = []
    for b_node in B:
        direction = dir_label.get(b_node, '?')
        col = _entry_colour(b_node)
        legend_handles.append(
            mlines.Line2D([], [], color=col, lw=2.5, label=f'Entry {direction}'))
    legend_handles.append(
        mlines.Line2D([], [], marker='s', color='black', ms=7, lw=0,
                      mfc='white', mew=1.5, label='Merge point'))
    legend_handles.append(
        mlines.Line2D([], [], marker='*', color='black', ms=10, lw=0,
                      label='Airport (ESSA)'))
    legend_handles.append(
        mlines.Line2D([], [], color='#aaaaaa', lw=1.0, alpha=0.8,
                      label='Full grid'))
    ax.legend(handles=legend_handles, loc='lower left', fontsize=7.5,
              framealpha=0.9, edgecolor='#cccccc')

    # ── axes labels & title ───────────────────────────────────────────────────
    # Convert x/y ticks back to lat/lon for labels
    _WEB_TO_WGS84 = Transformer.from_crs('EPSG:3857', 'EPSG:4326', always_xy=True)

    lon_ticks = np.arange(16.5, 20.1, 0.5)
    lat_ticks = np.arange(58.5, 60.5, 0.5)
    xt = [_to_web(59.0, lo)[0] for lo in lon_ticks]
    yt = [_to_web(la, 18.0)[1] for la in lat_ticks]
    ax.set_xticks(xt)
    ax.set_xticklabels([f'{lo:.1f}°E' for lo in lon_ticks], fontsize=8)
    ax.set_yticks(yt)
    ax.set_yticklabels([f'{la:.1f}°N' for la in lat_ticks], fontsize=8)
    ax.set_xlabel('Longitude', fontsize=9)
    ax.set_ylabel('Latitude', fontsize=9)

    n_ac = len(callsign_map)
    date_str = stem.replace('tmaopt_', '').split('_')[0]
    date_fmt = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}'
    ax.set_title(f'TMA Optimisation Tree — {date_fmt}',
                 fontsize=11, fontweight='bold', pad=10)

    plt.tight_layout()
    out_path = out_dir / f'{stem}_tree.png'
    fig.savefig(out_path, dpi=150, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    plt.close(fig)
    print(f'Saved: {out_path}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stems', nargs='*',
                        help='scenario stems (default: all in TMAOpt dir)')
    parser.add_argument('--out', default=None)
    args = parser.parse_args()

    if args.stems:
        stems = args.stems
    else:
        stems = sorted(p.name for p in _TMA_DIR.iterdir()
                       if p.is_dir() and (p / 'result.pkl').is_file())

    out_dir = Path(args.out) if args.out else _TMA_DIR / 'Figures_tree'
    out_dir.mkdir(parents=True, exist_ok=True)

    for stem in stems:
        make_figure(stem, out_dir)


if __name__ == '__main__':
    main()
