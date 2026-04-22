"""BlueSky plugin: replay OpenSky trajectory CSV with per-step interpolation.

Stack command:
    STARTREPLAY <csv_path>  — load a _tracks.csv and start interpolated replay
    STOPREPLAY              — stop replay and delete all managed aircraft

The plugin interpolates each aircraft's lat/lon/alt/hdg/spd between the 60-second
OpenSky snapshots every simulation step, then calls traf.move() so aircraft move
smoothly without any autopilot involvement.
"""
import csv
import math
import sys
from pathlib import Path

import numpy as np

import bluesky as bs
from bluesky import stack
from bluesky.core import timed_function

_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Dict: callsign -> sorted list of snapshot dicts
#   {t, lat, lon, alt_m, hdg, gs_ms, vs_ms}
_tracks: dict = {}

# Set of callsigns currently managed by this plugin
_managed: set = set()

# Scenario start wall-clock sim time (bs.sim.simt at STARTREPLAY call)
_start_simt: float = 0.0

# Scenario start UNIX timestamp (first snapshot time across all tracks)
_start_ts: int = 0

_active: bool = False


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def init_plugin():
    config = {
        'plugin_name': 'OPENSKY_REPLAY_PLAYER',
        'plugin_type': 'sim',
    }
    stackfunctions = {
        'STARTREPLAY': [
            'STARTREPLAY csv_path',
            'txt',
            start_replay,
            'Load an OpenSky tracks CSV and start interpolated aircraft replay.',
        ],
        'STOPREPLAY': [
            'STOPREPLAY',
            '',
            stop_replay,
            'Stop OpenSky replay and delete all managed aircraft.',
        ],
    }
    return config, stackfunctions


# ---------------------------------------------------------------------------
# Timed update — called every sim step
# ---------------------------------------------------------------------------

@timed_function(name='OPENSKY_REPLAY_PLAYER', dt=0)
def update():
    if not _active or not _tracks:
        return

    simt = bs.sim.simt
    # Current replay time offset in seconds from scenario start
    elapsed = simt - _start_simt
    current_ts = _start_ts + elapsed

    to_delete = []

    for cs, wps in _tracks.items():
        if not wps:
            continue

        first_t = wps[0]['t']
        last_t  = wps[-1]['t']

        # Not yet active
        if current_ts < first_t:
            continue

        # Past last snapshot — delete
        if current_ts > last_t + 30:
            if cs in _managed:
                to_delete.append(cs)
            continue

        # Find surrounding snapshots
        wp_a, wp_b = _bracket(wps, current_ts)

        if wp_b is None:
            wp = wp_a
            frac = 0.0
        else:
            dt_span = wp_b['t'] - wp_a['t']
            frac = (current_ts - wp_a['t']) / dt_span if dt_span > 0 else 0.0

        lat  = _lerp(wp_a['lat'],   wp_b['lat']   if wp_b else wp_a['lat'],   frac)
        lon  = _lerp_lon(wp_a['lon'], wp_b['lon']  if wp_b else wp_a['lon'],   frac)
        alt  = _lerp(wp_a['alt_m'], wp_b['alt_m'] if wp_b else wp_a['alt_m'], frac)  # metres
        hdg  = _lerp_hdg(wp_a['hdg'], wp_b['hdg'] if wp_b else wp_a['hdg'],   frac)
        gs   = _lerp(wp_a['gs_ms'], wp_b['gs_ms'] if wp_b else wp_a['gs_ms'], frac)  # m/s
        vs   = _lerp(wp_a['vs_ms'], wp_b['vs_ms'] if wp_b else wp_a['vs_ms'], frac)  # m/s

        cas = _gs_to_cas(gs, alt)  # m/s

        idx = bs.traf.id2idx(cs)

        if idx == -1:
            # cre() takes: acalt in metres, acspd as CAS in m/s
            actype = _get_actype(cs)
            bs.traf.cre(cs, actype, lat, lon, hdg, alt, cas)
            _managed.add(cs)
        else:
            # Direct array injection — bypasses autopilot entirely
            bs.traf.lat[idx] = lat
            bs.traf.lon[idx] = lon
            bs.traf.alt[idx] = alt       # metres
            bs.traf.hdg[idx] = hdg
            bs.traf.trk[idx] = hdg
            bs.traf.vs[idx]  = vs        # m/s
            # TAS from CAS
            from bluesky.tools.aero import vcasormach2tas
            bs.traf.tas[idx] = vcasormach2tas(cas, alt)
            bs.traf.gs[idx]  = gs        # m/s

    for cs in to_delete:
        idx = bs.traf.id2idx(cs)
        if idx >= 0:
            bs.traf.delete(idx)
        _managed.discard(cs)


# ---------------------------------------------------------------------------
# Stack commands
# ---------------------------------------------------------------------------

def start_replay(csv_path: str):
    global _tracks, _managed, _start_simt, _start_ts, _active

    path = Path(csv_path)
    if not path.is_absolute():
        path = _REPO_ROOT / csv_path
    if not path.exists():
        return False, f'STARTREPLAY: file not found: {path}'

    _tracks = _load_csv(path)
    if not _tracks:
        return False, f'STARTREPLAY: no valid tracks in {path}'

    _start_simt = bs.sim.simt
    _start_ts   = min(wps[0]['t'] for wps in _tracks.values() if wps)
    _managed    = set()
    _active     = True

    stack.stack(f'ECHO Replay loaded: {len(_tracks)} aircraft from {path.name}')
    return True, f'Replay started: {len(_tracks)} aircraft.'


def stop_replay():
    global _active, _tracks, _managed
    _active = False
    for cs in list(_managed):
        idx = bs.traf.id2idx(cs)
        if idx >= 0:
            bs.traf.delete(idx)
    _managed.clear()
    _tracks.clear()
    return True, 'Replay stopped.'


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------

def _load_csv(path: Path) -> dict:
    tracks = {}
    try:
        with open(path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                cs = row['callsign'].strip().upper()
                if not cs:
                    cs = 'UNKN'
                try:
                    alt_m = float(row['baro_alt_m']) if row['baro_alt_m'] else None
                    if alt_m is None or alt_m <= 0:
                        continue
                    wp = {
                        't':      int(row['time']),
                        'lat':    float(row['lat']),
                        'lon':    float(row['lon']),
                        'alt_m':  alt_m,
                        'hdg':    float(row['true_track'])    if row['true_track']      else 0.0,
                        'gs_ms':  float(row['velocity_ms'])   if row['velocity_ms']     else 0.0,
                        'vs_ms':  float(row['vertical_rate_ms']) if row['vertical_rate_ms'] else 0.0,
                        'icao24': row['icao24'].strip().lower(),
                    }
                    if cs not in tracks:
                        tracks[cs] = []
                    tracks[cs].append(wp)
                except (ValueError, KeyError):
                    continue
    except Exception as exc:
        stack.stack(f'ECHO STARTREPLAY load error: {exc}')
        return {}

    # Sort each track by time and drop low-altitude-only tracks (max < FL030)
    _MIN_ALT_M = 914.0
    result = {}
    for cs, wps in tracks.items():
        wps.sort(key=lambda w: w['t'])
        if max(w['alt_m'] for w in wps) > _MIN_ALT_M:
            result[cs] = wps
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bracket(wps: list, t: float):
    """Return (wp_before, wp_after) surrounding time t."""
    for i in range(len(wps) - 1):
        if wps[i]['t'] <= t <= wps[i + 1]['t']:
            return wps[i], wps[i + 1]
    # t is past the last waypoint
    return wps[-1], None


def _lerp(a, b, f):
    return a + (b - a) * f


def _lerp_lon(a, b, f):
    # Handle 180° wrap
    diff = b - a
    if diff > 180:
        diff -= 360
    elif diff < -180:
        diff += 360
    return a + diff * f


def _lerp_hdg(a, b, f):
    diff = ((b - a) + 180) % 360 - 180
    return (a + diff * f) % 360


# ISA constants
_P0 = 101325.0; _T0 = 288.15; _L = 0.0065
_R = 287.05287; _G = 9.80665; _KAPPA = 1.4; _A0 = 340.294


def _isa_pressure(alt_m):
    if alt_m < 11000:
        return _P0 * (_T0 / (_T0 + _L * alt_m)) ** (_G / (_R * _L))
    p11 = _P0 * (_T0 / (_T0 - _L * 11000)) ** (-_G / (_R * _L))
    return p11 * math.exp(-_G * (alt_m - 11000) / (_R * 216.65))


def _gs_to_cas(gs_ms: float, alt_m: float) -> float:
    """GS (m/s) -> CAS (m/s) via ISA."""
    if gs_ms <= 0 or alt_m <= 0:
        return max(gs_ms, 0.0)
    T = max(216.65, _T0 - _L * min(alt_m, 11000.0))
    a = math.sqrt(_KAPPA * _R * T)
    mach = min(gs_ms / a, 0.99)
    p = _isa_pressure(alt_m)
    qc = p * ((1.0 + (_KAPPA - 1.0) / 2.0 * mach ** 2) ** (_KAPPA / (_KAPPA - 1.0)) - 1.0)
    cas = _A0 * math.sqrt(2.0 / (_KAPPA - 1.0) * ((qc / _P0 + 1.0) ** ((_KAPPA - 1.0) / _KAPPA) - 1.0))
    return cas


_actype_cache: dict = {}


def _get_actype(cs: str) -> str:
    if cs in _actype_cache:
        return _actype_cache[cs]
    try:
        from utils.opensky_importer.actype_lookup import resolve_actype
        # Find icao24 from loaded tracks
        wps = _tracks.get(cs, [])
        icao24 = wps[0]['icao24'] if wps else ''
        t = resolve_actype(icao24, cs, {})
        _actype_cache[cs] = t
        return t
    except Exception:
        return 'B738'
