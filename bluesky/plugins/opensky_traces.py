"""BlueSky plugin: show/cycle aircraft trajectory traces from OpenSky CSV.

Stack command:
    TRACETOGGLE   — cycle display mode (bound to C2 button)

Cycle:
  0  Aircraft visible,  no traces
  1  Aircraft hidden,   all traces  (arrivals=lightgreen, departures=darkyellow)
  2  Aircraft hidden,   arrivals only
  3  Aircraft hidden,   departures only
  -> back to 0

Arrivals:   last waypoint within _ESSA_RADIUS_DEG of ESSA threshold
Departures: first waypoint within _ESSA_RADIUS_DEG of ESSA threshold

Colors:
    Arrivals   — lightgreen  (0, 230, 118)
    Departures — darkyellow  (180, 160, 0)
"""
import csv
from pathlib import Path

import bluesky as bs
from bluesky import stack

_REPO_ROOT = Path(__file__).parents[2]

# ESSA Arlanda position
_ESSA_LAT = 59.6519
_ESSA_LON = 17.9186

# Colors (R, G, B)
_COL_ARR = (0, 230, 118)    # lightgreen
_COL_DEP = (180, 160, 0)    # darkyellow

# State
_mode: int = 0              # 0=aircraft, 1=all, 2=arr, 3=dep
_arr_names: list = []       # POLYLINE names for arrivals
_dep_names: list = []       # POLYLINE names for departures
_csv_path: str = ''         # last loaded CSV


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def init_plugin():
    config = {
        'plugin_name': 'OPENSKY_TRACES',
        'plugin_type': 'sim',
    }
    stackfunctions = {
        'TRACETOGGLE': [
            'TRACETOGGLE',
            '',
            tracetoggle,
            'Cycle aircraft trace display: none -> all -> arrivals -> departures.',
        ],
        'LOADTRACES': [
            'LOADTRACES csv_path',
            'txt',
            loadtraces,
            'Load trace data from an OpenSky tracks CSV.',
        ],
    }
    return config, stackfunctions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dist_deg(lat1, lon1, lat2, lon2):
    dlat = lat1 - lat2
    dlon = lon1 - lon2
    return (dlat ** 2 + dlon ** 2) ** 0.5


def _classify(wps):
    """Return 'arr', 'dep', or 'enroute' based on altitude trend and distance to ESSA."""
    if len(wps) < 2:
        return 'enroute'
    first_alt = wps[0][2]
    last_alt  = wps[-1][2]
    first_dist = _dist_deg(wps[0][0], wps[0][1], _ESSA_LAT, _ESSA_LON)
    last_dist  = _dist_deg(wps[-1][0], wps[-1][1], _ESSA_LAT, _ESSA_LON)
    # Descending AND getting closer to ESSA -> arrival
    if last_alt < first_alt and last_dist < first_dist:
        return 'arr'
    # Climbing AND moving away from ESSA -> departure
    if last_alt > first_alt and last_dist > first_dist:
        return 'dep'
    return 'enroute'


def _del_all():
    for name in _arr_names + _dep_names:
        stack.stack(f'DEL {name}')
    _arr_names.clear()
    _dep_names.clear()


def _auto_detect_csv() -> str:
    """Try to find a tracks CSV matching the currently loaded scenario."""
    import glob as _glob
    pattern = str(_REPO_ROOT / 'scenario' / 'OpenSky' / '*_tracks.csv')
    csvs = sorted(_glob.glob(pattern))
    if not csvs:
        return ''
    # Return the most recently modified one
    return max(csvs, key=lambda p: Path(p).stat().st_mtime)


def _draw_traces(show_arr: bool, show_dep: bool):
    """Draw the requested subset of traces."""
    global _csv_path
    _del_all()

    if not _csv_path:
        _csv_path = _auto_detect_csv()

    if not _csv_path:
        stack.stack('ECHO TRACETOGGLE: no CSV found in scenario/OpenSky/.')
        return

    path = Path(_csv_path)
    if not path.exists():
        stack.stack(f'ECHO TRACETOGGLE: CSV not found: {path}')
        return

    tracks = {}
    try:
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                cs = row['callsign'].strip().upper() or 'UNKN'
                try:
                    alt = float(row['baro_alt_m']) if row['baro_alt_m'] else None
                    if alt is None or alt <= 0:
                        continue
                    wp = (float(row['lat']), float(row['lon']), alt)
                    tracks.setdefault(cs, []).append(wp)
                except (ValueError, KeyError):
                    continue
    except Exception as exc:
        stack.stack(f'ECHO TRACETOGGLE load error: {exc}')
        return

    _MIN_ALT_M = 914.0

    for cs, wps in tracks.items():
        if not wps:
            continue
        if max(w[2] for w in wps) <= _MIN_ALT_M:
            continue
        if len(wps) < 2:
            continue

        kind = _classify(wps)

        if kind == 'arr' and not show_arr:
            continue
        if kind == 'dep' and not show_dep:
            continue
        if kind == 'enroute' and not show_arr:
            continue

        name = f'TR_{cs}'
        coords = ' '.join(f'{w[0]:.5f} {w[1]:.5f}' for w in wps)
        stack.stack(f'POLYLINE {name} {coords}')

        if kind == 'dep':
            r, g, b = _COL_DEP
            _dep_names.append(name)
        else:
            r, g, b = _COL_ARR
            _arr_names.append(name)

        stack.stack(f'COLOR {name} {r} {g} {b}')


def _set_aircraft_visible(visible: bool):
    val = 1 if visible else 0
    stack.stack(f'SWRAD SYM {val}')


# ---------------------------------------------------------------------------
# Stack commands
# ---------------------------------------------------------------------------

def loadtraces(csv_path: str):
    global _csv_path, _mode
    path = Path(csv_path)
    if not path.is_absolute():
        path = _REPO_ROOT / csv_path
    if not path.exists():
        return False, f'LOADTRACES: file not found: {path}'
    _del_all()
    _csv_path = str(path)
    _mode = 0
    return True, f'Traces ready: {path.name}'


def tracetoggle():
    global _mode

    _mode = (_mode + 1) % 4

    if _mode == 0:
        _del_all()
        _set_aircraft_visible(True)
        stack.stack('ECHO Traces: OFF -- aircraft visible')

    elif _mode == 1:
        _set_aircraft_visible(False)
        _draw_traces(show_arr=True, show_dep=True)
        stack.stack(f'ECHO Traces: ALL ({len(_arr_names)} arr, {len(_dep_names)} dep)')

    elif _mode == 2:
        _del_all()
        _draw_traces(show_arr=True, show_dep=False)
        stack.stack(f'ECHO Traces: ARRIVALS only ({len(_arr_names)})')

    elif _mode == 3:
        _del_all()
        _draw_traces(show_arr=False, show_dep=True)
        stack.stack(f'ECHO Traces: DEPARTURES only ({len(_dep_names)})')

    return True, ''
