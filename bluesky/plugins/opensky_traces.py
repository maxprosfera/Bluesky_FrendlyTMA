"""BlueSky plugin: show/cycle aircraft trajectory traces from OpenSky CSV.

Stack command:
    TRACETOGGLE   — cycle display mode (bound to C2 button)

Cycle:
  0  Aircraft hidden,   all traces      (arrivals=lightgreen, departures=darkyellow)
  1  Aircraft hidden,   arrivals only
  2  Aircraft hidden,   departures only
  3  Aircraft hidden,   optimised aircraft only (from TMAOpt selected.txt, cyan)
  4  Aircraft visible,  no traces
  -> back to 0
"""
import csv
import json
import math
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
_COL_OPT = (0, 210, 255)    # cyan — optimised aircraft

# GA / helicopter / ultralight types to exclude from OPTIMISED mode
_GA_TYPES = {
    'C172', 'C182', 'C208', 'C206', 'C152', 'C150', 'C525', 'C56X',
    'P28A', 'P28B', 'PA46', 'PA31', 'PA34',
    'RV10', 'RV6', 'RV7', 'RV8', 'RV9',
    'GLAS', 'GLID', 'ULAC',
    'EC45', 'EC35', 'AS50', 'R44', 'R22', 'B06', 'B407',
    'A109', 'A169', 'H135', 'H145', 'S76', 'S92',
    'HIGH', 'BALL', 'SHIP',
    'B429', 'BK17', 'EC30', 'EC25',
}
_GA_PREFIXES = ('EC', 'R4', 'R2', 'AS', 'S7', 'S9', 'H1', 'H6')

_ACTYPE_CACHE_PATH = _REPO_ROOT / 'cache' / 'opensky' / 'actype_cache.json'
_actype_cache: dict = {}
_actype_cache_loaded: bool = False


def _load_actype_cache():
    global _actype_cache, _actype_cache_loaded
    if _actype_cache_loaded:
        return
    _actype_cache_loaded = True
    if _ACTYPE_CACHE_PATH.exists():
        try:
            with open(_ACTYPE_CACHE_PATH, encoding='utf-8') as f:
                _actype_cache.update(json.load(f))
        except Exception:
            pass


def _is_ga(icao24: str) -> bool:
    _load_actype_cache()
    key = icao24.lower()
    val = _actype_cache.get(key, '')
    tc = val.get('typecode', '') if isinstance(val, dict) else str(val)
    tc = tc.strip().upper()
    if not tc:
        return False
    return tc in _GA_TYPES or tc.startswith(_GA_PREFIXES)


# State
_mode: int = -1             # -1=off, 0=all, 1=arr, 2=dep, 3=opt, 4=off
_arr_names: list = []       # POLYLINE names for arrivals
_dep_names: list = []       # POLYLINE names for departures
_enroute_names: list = []   # POLYLINE names for enroute (pass-through)
_opt_names: list = []       # POLYLINE names for optimised-only traces
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

_ARR_DIST_NM  = 25.0   # track ends within this distance of ESSA = arrival
_DEP_DIST_NM  = 25.0   # track starts within this distance of ESSA = departure
_MIN_ALT_M    = -200.0  # allow ground-level points (ESSA baro alt can be slightly negative)


def _haversine_nm(lat1, lon1, lat2, lon2):
    R = 6_371_000.0; NM = 1_852.0
    la1, lo1 = math.radians(lat1), math.radians(lon1)
    la2, lo2 = math.radians(lat2), math.radians(lon2)
    dlat = la2 - la1; dlon = lo2 - lo1
    a = math.sin(dlat/2)**2 + math.cos(la1)*math.cos(la2)*math.sin(dlon/2)**2
    return 2*R*math.asin(math.sqrt(max(0.0, min(1.0, a)))) / NM


def _classify(wps):
    """Return 'arr', 'dep', or 'enroute'.
    wps: list of (lat, lon, alt_m).
    Strategy:
      - ARR : track's closest point to ESSA is in last 40%, within _ARR_DIST_NM,
              AND the mean altitude of the last 20% of the track is below 4000 m
              (ensures track is genuinely descending toward airport, not just passing)
      - DEP : track's closest point to ESSA is in first 40%, within _DEP_DIST_NM,
              track ends farther than it starts,
              AND mean altitude of first 20% is below 4000 m
      - ENROUTE: everything else
    """
    if len(wps) < 2:
        return 'enroute'
    dists = [_haversine_nm(w[0], w[1], _ESSA_LAT, _ESSA_LON) for w in wps]
    min_d = min(dists)
    if min_d > max(_ARR_DIST_NM, _DEP_DIST_NM):
        return 'enroute'
    min_idx = dists.index(min_d)
    frac = min_idx / max(len(wps) - 1, 1)
    tail_n = max(1, len(wps) // 5)
    head_n = max(1, len(wps) // 5)
    mean_alt_tail = sum(w[2] for w in wps[-tail_n:]) / tail_n
    mean_alt_head = sum(w[2] for w in wps[:head_n]) / head_n
    if min_d <= _ARR_DIST_NM and frac >= 0.6 and mean_alt_tail < 4000.0:
        return 'arr'
    if min_d <= _DEP_DIST_NM and frac <= 0.4 and dists[-1] > dists[0] and mean_alt_head < 4000.0:
        return 'dep'
    return 'enroute'


def _del_all():
    for name in _arr_names + _dep_names + _enroute_names + _opt_names:
        stack.stack(f'DEL {name}')
    _arr_names.clear()
    _dep_names.clear()
    _enroute_names.clear()
    _opt_names.clear()


def _csv_from_scenario() -> str:
    """Read ic.scn to find the current scenario file, then extract the
    historical tracks CSV from it (STARTREPLAY or LOADTRACES line).

    For OpenSky scenarios: STARTREPLAY points directly to *_tracks.csv.
    For TMAOpt scenarios:  STARTREPLAY points to *_tracks.csv (optimised),
                           so we look for *_historical.csv in the same folder.
    Returns '' if nothing useful is found.
    """
    ic_scn = _REPO_ROOT / 'scenario' / 'ic.scn'
    if not ic_scn.exists():
        return ''

    # Read last IC path from ic.scn
    scn_path = None
    try:
        for line in ic_scn.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('#'):
                continue
            if '>IC ' in line or '> IC ' in line:
                parts = line.split('IC ', 1)
                if len(parts) == 2:
                    scn_path = Path(parts[1].strip())
                    break
    except Exception:
        return ''

    if scn_path is None or not scn_path.exists():
        return ''

    # Scan the scenario file for STARTREPLAY or LOADTRACES
    replay_csv = None
    try:
        for line in scn_path.read_text(encoding='utf-8').splitlines():
            upper = line.upper()
            if 'STARTREPLAY' in upper or 'LOADTRACES' in upper:
                # Extract the path token after the command
                for kw in ('STARTREPLAY', 'LOADTRACES'):
                    idx = upper.find(kw)
                    if idx != -1:
                        token = line[idx + len(kw):].strip()
                        if token:
                            replay_csv = token
                            break
            if replay_csv:
                break
    except Exception:
        return ''

    if not replay_csv:
        return ''

    p = Path(replay_csv)
    if not p.is_absolute():
        p = _REPO_ROOT / p

    # TMAOpt scenario → use historical CSV from the same folder (Trino fetch)
    # If no historical CSV exists (live mode run), there is no historical data
    if 'TMAOpt' in str(p):
        for suffix in ('_tracks', '_cdo_opt'):
            if p.stem.endswith(suffix):
                hist = p.with_name(p.stem.replace(suffix, '_historical') + '.csv')
                if hist.exists():
                    return str(hist)
        return ''

    # OpenSky or already a historical CSV
    if p.exists():
        return str(p)
    return ''



def _auto_detect_csv() -> str:
    """Fall-back: find the most recently modified historical CSV across known folders."""
    import glob as _glob
    candidates = (
        _glob.glob(str(_REPO_ROOT / 'scenario' / 'OpenSky' / '*_tracks.csv'))
        + _glob.glob(str(_REPO_ROOT / 'scenario' / 'TMAOpt' / '**' / '*_historical.csv'), recursive=True)
    )
    if not candidates:
        return ''
    return max(candidates, key=lambda p: Path(p).stat().st_mtime)


def _load_selected() -> set:
    """Read selected.txt from the TMAOpt folder matching the current scenario.
    Returns a set of uppercase callsigns, or empty set if not found.
    """
    ic_scn = _REPO_ROOT / 'scenario' / 'ic.scn'
    if not ic_scn.exists():
        return set()
    try:
        for line in ic_scn.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('#'):
                continue
            if '>IC ' in line or '> IC ' in line:
                parts = line.split('IC ', 1)
                if len(parts) == 2:
                    scn_path = Path(parts[1].strip())
                    sel = scn_path.parent / 'selected.txt'
                    if sel.exists():
                        return {s.strip().upper() for s in sel.read_text().splitlines() if s.strip()}
                    break
    except Exception:
        pass
    return set()


def _draw_traces(show_arr: bool, show_dep: bool, only_callsigns: set = None):
    """Draw the requested subset of traces.
    only_callsigns: if given, draw only those callsigns regardless of arr/dep/enroute.
    """
    global _csv_path
    _del_all()

    # Always re-resolve from the current scenario so a newly loaded IC is picked up
    resolved = _csv_from_scenario()
    if resolved and resolved != _csv_path:
        _csv_path = resolved
        stack.stack(f'ECHO Traces: using {Path(_csv_path).name}')
    elif not _csv_path:
        _csv_path = _auto_detect_csv()
        if _csv_path:
            stack.stack(f'ECHO Traces: using {Path(_csv_path).name}')

    if not _csv_path:
        stack.stack('ECHO TRACETOGGLE: no historical CSV — run TMAOpt with a historical date/time first.')
        return

    path = Path(_csv_path)
    if not path.exists():
        stack.stack(f'ECHO TRACETOGGLE: CSV not found: {path}')
        return

    _MAX_PTS = 200

    tracks = {}
    icao24_map = {}
    try:
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                cs = row['callsign'].strip().upper() or 'UNKN'
                try:
                    alt = float(row['baro_alt_m']) if row['baro_alt_m'] else None
                    if alt is None or alt < _MIN_ALT_M:
                        continue
                    wp = (float(row['lat']), float(row['lon']), alt)
                    tracks.setdefault(cs, []).append(wp)
                    if cs not in icao24_map:
                        icao24_map[cs] = row.get('icao24', '').strip()
                except (ValueError, KeyError):
                    continue
    except Exception as exc:
        stack.stack(f'ECHO TRACETOGGLE load error: {exc}')
        return

    for cs, wps in tracks.items():
        if len(wps) < 2:
            continue

        if only_callsigns is not None:
            if cs not in only_callsigns:
                continue
            if _is_ga(icao24_map.get(cs, '')):
                continue
            color = _COL_OPT
            name = f'TR_{cs}'
            if len(wps) > _MAX_PTS:
                step = len(wps) // _MAX_PTS
                wps = wps[::step]
            coords = ' '.join(f'{w[0]:.5f} {w[1]:.5f}' for w in wps)
            stack.stack(f'POLYLINE {name} {coords}')
            stack.stack(f'COLOR {name} {color[0]} {color[1]} {color[2]}')
            _opt_names.append(name)
            continue

        kind = _classify(wps)

        if kind == 'arr' and not show_arr:
            continue
        if kind == 'dep' and not show_dep:
            continue
        if kind == 'enroute':
            if not (show_arr and show_dep):
                continue

        if len(wps) > _MAX_PTS:
            step = len(wps) // _MAX_PTS
            wps = wps[::step]

        name = f'TR_{cs}'
        coords = ' '.join(f'{w[0]:.5f} {w[1]:.5f}' for w in wps)
        stack.stack(f'POLYLINE {name} {coords}')

        if kind == 'dep':
            r, g, b = _COL_DEP
            _dep_names.append(name)
        elif kind == 'arr':
            r, g, b = _COL_ARR
            _arr_names.append(name)
        else:
            r, g, b = _COL_ARR
            _enroute_names.append(name)

        stack.stack(f'COLOR {name} {r} {g} {b}')


def _set_aircraft_visible(visible: bool):
    val = 1 if visible else 0
    stack.stack(f'SWRAD SYM {val}')


# ---------------------------------------------------------------------------
# Stack commands
# ---------------------------------------------------------------------------

def reset():
    """Called by BlueSky when a new scenario is loaded — clear cached state."""
    global _csv_path, _mode
    _del_all()
    _csv_path = ''
    _mode = -1


def _resolve_icase(csv_path: str) -> Path:
    """Resolve csv_path handling BlueSky's .scn uppercasing of arguments."""
    path = Path(csv_path)
    if not path.is_absolute():
        path = _REPO_ROOT / csv_path
    if path.exists():
        return path
    try:
        rel_parts = path.relative_to(_REPO_ROOT).parts
    except ValueError:
        return path
    parent = _REPO_ROOT
    for part in rel_parts[:-1]:
        matches = [d for d in parent.iterdir() if d.name.lower() == part.lower() and d.is_dir()]
        if not matches:
            return path
        parent = matches[0]
    fname   = rel_parts[-1]
    matches = [f for f in parent.iterdir() if f.name.lower() == fname.lower()]
    return matches[0] if matches else path


def loadtraces(csv_path: str):
    global _csv_path, _mode
    path = _resolve_icase(csv_path)
    if not path.exists():
        return False, f'LOADTRACES: file not found: {path}'
    _del_all()
    _csv_path = str(path)
    _mode = -1
    return True, f'Traces ready: {path.name}'


def tracetoggle():
    global _mode

    _mode = (_mode + 1) % 5

    if _mode == 0:
        _del_all()
        _set_aircraft_visible(False)
        _draw_traces(show_arr=True, show_dep=True)
        stack.stack(f'ECHO Traces: ALL ({len(_arr_names)} arr, {len(_dep_names)} dep, {len(_enroute_names)} enroute)')

    elif _mode == 1:
        _del_all()
        _set_aircraft_visible(False)
        _draw_traces(show_arr=True, show_dep=False)
        stack.stack(f'ECHO Traces: ARRIVALS only ({len(_arr_names)})')

    elif _mode == 2:
        _del_all()
        _set_aircraft_visible(False)
        _draw_traces(show_arr=False, show_dep=True)
        stack.stack(f'ECHO Traces: DEPARTURES only ({len(_dep_names)})')

    elif _mode == 3:
        _del_all()
        _set_aircraft_visible(False)
        selected = _load_selected()
        if not selected:
            stack.stack('ECHO Traces: no selected.txt found — run historical TMAOpt first.')
        else:
            _draw_traces(show_arr=True, show_dep=True, only_callsigns=selected)
            stack.stack(f'ECHO Traces: OPTIMISED aircraft only ({len(_opt_names)} traces)')

    elif _mode == 4:
        _del_all()
        _set_aircraft_visible(True)
        stack.stack('ECHO Traces: OFF -- aircraft visible')

    return True, ''
