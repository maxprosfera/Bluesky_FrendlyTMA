"""
TMA Optimization Plugin — C5 button
Fetches aircraft via OpenSky Trino (historical), estimates TMA entry crossing times,
runs FrendlyTMA Gurobi optimization, saves results + .scn file.
"""

import os
import math
import time
import pickle
import copy
import threading
import csv
from datetime import datetime, timezone
from pathlib import Path

from bluesky import stack

# ── Paths ───────────────────────────────────────────────────────────────────
_REPO_ROOT    = Path(__file__).resolve().parents[2]
_FRENDLY_CODE = Path('/Users/maximmoroz/liuprojects/FrendlyTMA/Code')
_SCENARIO_DIR = _REPO_ROOT / 'scenario' / 'TMAOpt'
_SCENARIO_DIR.mkdir(parents=True, exist_ok=True)

# ── ESSA / fetch constants ───────────────────────────────────────────────────
_ESSA            = (59.6373, 17.9132)   # ESSA runway 01L threshold
_FETCH_RADIUS_NM = 200.0       # fetch box half-size (nm) — wide enough to catch inbounds
_MAX_ENTRY_DIST_NM = 60.0      # ignore aircraft more than this far from their entry node
_MAX_AC_PER_ENTRY  = 5         # cap per entry node to keep model tractable
_EARTH_R_M       = 6_371_000.0
_NM_M            = 1_852.0

_ACTYPE_CACHE_PATH = _REPO_ROOT / 'cache' / 'opensky' / 'actype_cache.json'
_actype_cache_tma: dict = {}
_actype_cache_tma_loaded: bool = False

_GA_TYPES_TMA = {
    'C172', 'C182', 'C208', 'C206', 'C152', 'C150', 'C525', 'C56X',
    'P28A', 'P28B', 'PA46', 'PA31', 'PA34',
    'RV10', 'RV6', 'RV7', 'RV8', 'RV9',
    'GLAS', 'GLID', 'ULAC',
    'EC45', 'EC35', 'AS50', 'R44', 'R22', 'B06', 'B407',
    'A109', 'A169', 'H135', 'H145', 'S76', 'S92',
    'HIGH', 'BALL', 'SHIP',
    'B429', 'BK17', 'EC30', 'EC25',
}
_GA_PREFIXES_TMA = ('EC', 'R4', 'R2', 'AS', 'S7', 'S9', 'H1', 'H6')


def _load_actype_cache_tma():
    global _actype_cache_tma, _actype_cache_tma_loaded
    if _actype_cache_tma_loaded:
        return
    _actype_cache_tma_loaded = True
    if _ACTYPE_CACHE_PATH.exists():
        try:
            import json
            with open(_ACTYPE_CACHE_PATH, encoding='utf-8') as f:
                _actype_cache_tma.update(json.load(f))
        except Exception:
            pass


def _is_ga_icao24(icao24: str) -> bool:
    _load_actype_cache_tma()
    key = icao24.lower() if icao24 else ''
    val = _actype_cache_tma.get(key, '')
    tc = val.get('typecode', '') if isinstance(val, dict) else str(val)
    tc = tc.strip().upper()
    if not tc:
        return False
    return tc in _GA_TYPES_TMA or tc.startswith(_GA_PREFIXES_TMA)


# ── ICAO wake turbulence categories ─────────────────────────────────────────
# H = Heavy (s1=2 min separation), M/L = Medium/Light (s2=3 min separation)
_WAKE_HEAVY = {
    'A124','A225','A306','A30B','A310','A318','A319','A320','A321',
    'A332','A333','A338','A339','A342','A343','A345','A346',
    'A359','A35K','A380','A388',
    'B703','B712','B721','B722','B732','B733','B734','B735','B736',
    'B737','B738','B739','B73G','B73H','B73W','B73X',
    'B741','B742','B743','B744','B747','B748',
    'B752','B753','B762','B763','B764','B772','B773','B77L','B77W',
    'B778','B779','B788','B789','B78X',
    'C17','C5','IL76','IL86','IL96',
    'MD11','MD81','MD82','MD83','MD87','MD88','MD90',
    'B38M','B39M','A20N','A21N','A20S',
    'E170','E175','E190','E195','E290','E295',
    'CRJ9','CRJ7','CRJ2','AT76','AT75','AT72','AT45',
    'BCS1','BCS3','BCI','BD500',
}

def _wake_cat_from_icao24(icao24: str) -> str:
    """Return 'H' (heavy) or 'M' (medium/light) based on actype_cache."""
    _load_actype_cache_tma()
    key = icao24.lower() if icao24 else ''
    val = _actype_cache_tma.get(key, '')
    tc = val.get('typecode', '') if isinstance(val, dict) else str(val)
    tc = tc.strip().upper()
    if tc in _WAKE_HEAVY:
        return 'H'
    return 'M'


# ── FrendlyTMA grid node coordinates (from Coordinates.txt) ─────────────────
# Format: node_id -> (lat, lon)
_GRID_COORDS = {
    1:(60.1387,16.7003),2:(60.1387,16.9429),3:(60.1387,17.1855),4:(60.1387,17.428),5:(60.1387,17.6706),6:(60.1387,17.9132),7:(60.1387,18.1558),8:(60.1387,18.3984),9:(60.1387,18.6409),10:(60.1387,18.8835),11:(60.1387,19.1261),
    12:(60.0342,16.7003),13:(60.0342,16.9429),14:(60.0342,17.1855),15:(60.0342,17.428),16:(60.0342,17.6706),17:(60.0342,17.9132),18:(60.0342,18.1558),19:(60.0342,18.3984),20:(60.0342,18.6409),21:(60.0342,18.8835),22:(60.0342,19.1261),
    23:(59.9296,16.7003),24:(59.9296,16.9429),25:(59.9296,17.1855),26:(59.9296,17.428),27:(59.9296,17.6706),28:(59.9296,17.9132),29:(59.9296,18.1558),30:(59.9296,18.3984),31:(59.9296,18.6409),32:(59.9296,18.8835),33:(59.9296,19.1261),
    34:(59.8251,16.7003),35:(59.8251,16.9429),36:(59.8251,17.1855),37:(59.8251,17.428),38:(59.8251,17.6706),39:(59.8251,17.9132),40:(59.8251,18.1558),41:(59.8251,18.3984),42:(59.8251,18.6409),43:(59.8251,18.8835),44:(59.8251,19.1261),
    45:(59.7205,16.7003),46:(59.7205,16.9429),47:(59.7205,17.1855),48:(59.7205,17.428),49:(59.7205,17.6706),50:(59.7205,17.9132),51:(59.7205,18.1558),52:(59.7205,18.3984),53:(59.7205,18.6409),54:(59.7205,18.8835),55:(59.7205,19.1261),
    56:(59.616,16.7003),57:(59.616,16.9429),58:(59.616,17.1855),59:(59.616,17.428),60:(59.616,17.6706),61:(59.616,17.9132),62:(59.616,18.1558),63:(59.616,18.3984),64:(59.616,18.6409),65:(59.616,18.8835),66:(59.616,19.1261),
    67:(59.5114,16.7003),68:(59.5114,16.9429),69:(59.5114,17.1855),70:(59.5114,17.428),71:(59.5114,17.6706),72:(59.5114,17.9132),73:(59.5114,18.1558),74:(59.5114,18.3984),75:(59.5114,18.6409),76:(59.5114,18.8835),77:(59.5114,19.1261),
    78:(59.4069,16.7003),79:(59.4069,16.9429),80:(59.4069,17.1855),81:(59.4069,17.428),82:(59.4069,17.6706),83:(59.4069,17.9132),84:(59.4069,18.1558),85:(59.4069,18.3984),86:(59.4069,18.6409),87:(59.4069,18.8835),88:(59.4069,19.1261),
    89:(59.3024,16.7003),90:(59.3024,16.9429),91:(59.3024,17.1855),92:(59.3024,17.428),93:(59.3024,17.6706),94:(59.3024,17.9132),95:(59.3024,18.1558),96:(59.3024,18.3984),97:(59.3024,18.6409),98:(59.3024,18.8835),99:(59.3024,19.1261),
    100:(59.1978,16.7003),101:(59.1978,16.9429),102:(59.1978,17.1855),103:(59.1978,17.428),104:(59.1978,17.6706),105:(59.1978,17.9132),106:(59.1978,18.1558),107:(59.1978,18.3984),108:(59.1978,18.6409),109:(59.1978,18.8835),110:(59.1978,19.1261),
    111:(59.0933,16.7003),112:(59.0933,16.9429),113:(59.0933,17.1855),114:(59.0933,17.428),115:(59.0933,17.6706),116:(59.0933,17.9132),117:(59.0933,18.1558),118:(59.0933,18.3984),119:(59.0933,18.6409),120:(59.0933,18.8835),121:(59.0933,19.1261),
    122:(58.9887,16.7003),123:(58.9887,16.9429),124:(58.9887,17.1855),125:(58.9887,17.428),126:(58.9887,17.6706),127:(58.9887,17.9132),128:(58.9887,18.1558),129:(58.9887,18.3984),130:(58.9887,18.6409),131:(58.9887,18.8835),132:(58.9887,19.1261),
    133:(58.8842,16.7003),134:(58.8842,16.9429),135:(58.8842,17.1855),136:(58.8842,17.428),137:(58.8842,17.6706),138:(58.8842,17.9132),139:(58.8842,18.1558),140:(58.8842,18.3984),141:(58.8842,18.6409),142:(58.8842,18.8835),143:(58.8842,19.1261),
    144:(58.7796,16.7003),145:(58.7796,16.9429),146:(58.7796,17.1855),147:(58.7796,17.428),148:(58.7796,17.6706),149:(58.7796,17.9132),150:(58.7796,18.1558),151:(58.7796,18.3984),152:(58.7796,18.6409),153:(58.7796,18.8835),154:(58.7796,19.1261),
    155:(58.6751,16.7003),156:(58.6751,16.9429),157:(58.6751,17.1855),158:(58.6751,17.428),159:(58.6751,17.6706),160:(58.6751,17.9132),161:(58.6751,18.1558),162:(58.6751,18.3984),163:(58.6751,18.6409),164:(58.6751,18.8835),165:(58.6751,19.1261),
}

# Entry nodes B=[9,45,66,160] with directions
_ENTRY_NODES = {
    'N': 9,    # lat=60.2794 lon=18.5909 (top row, NE)
    'W': 45,   # lat=59.8612 lon=16.6503 (left column, W)
    'E': 66,   # lat=59.7567 lon=19.0761 (right column, E)
    'S': 160,  # lat=58.8158 lon=17.8632 (bottom row, S)
}

_ENTRY_LATLON = {d: _GRID_COORDS[n] for d, n in _ENTRY_NODES.items()}

# ── Stockholm TMA polygon ───────────────────────────────────────────────────
_STOCKHOLM_TMA_POLY = (
    '60.299444 18.213056 60.266111 18.554722 59.882778 18.847000 '
    '60.035278 19.313611 59.673611 19.830833 59.599444 19.273611 '
    '59.255000 18.968333 59.047500 18.754722 58.832500 18.539444 '
    '58.752500 18.457222 58.583056 17.932778 58.616389 17.456944 '
    '58.966111 17.407778 58.978611 17.223333 59.012500 16.707778 '
    '59.049444 16.267778 59.323889 16.318333 59.749444 16.446667 '
    '60.232778 17.596667'
)


# ── Plugin init ──────────────────────────────────────────────────────────────
def init_plugin():
    return {'plugin_name': 'TMA_OPT', 'plugin_type': 'sim'}


# ── Stack command ────────────────────────────────────────────────────────────
@stack.command
def tmaopt(dtstr: str = '', duration: int = 60, entries: str = 'NESW',
           max_ac: int = 15, max_ac_per_entry: int = 5,
           max_eps: int = 3, time_limit_per_eps: int = 120,
           s1: int = 2, s2: int = 3, fetch_radius: int = 50,
           cdo_args: 'string' = ''):
    """[datetime] [duration_min] [entries] [max_ac] [max_ac_per_entry] [max_eps]
    [time_limit_s] [s1] [s2] [fetch_radius_nm] [cdo_fap_alt cdo_ias_start
    cdo_ias_restrict cdo_mach cdo_mlw cdo_kt_per_sec cdo_wind cdo_c_v_min]
    Optimise TMA entry using OpenSky Trino historical data."""
    stack.stack('ECHO TMAOPT: Starting ...')
    # Parse CDO params from the trailing space-separated string
    _CDO_DEFAULTS = [2000, 200, 220, 0.84, 0.9, 1.0, 1, 1.23]
    _CDO_KEYS     = ['fap_alt_ft', 'ias_start_kt', 'ias_restrict_kt',
                     'mach', 'mlw_factor', 'kt_per_sec', 'wind_temp', 'c_v_min']
    parts = cdo_args.strip().split() if cdo_args.strip() else []
    vals  = []
    for i, default in enumerate(_CDO_DEFAULTS):
        try:
            vals.append(type(default)(parts[i]))
        except (IndexError, ValueError):
            vals.append(default)
    cdo_params = dict(zip(_CDO_KEYS, vals))
    cdo_params['wind_temp'] = bool(int(cdo_params['wind_temp']))
    t = threading.Thread(
        target=_run_tmaopt,
        args=(dtstr, duration, entries.upper(), max_ac, max_ac_per_entry,
              max_eps, time_limit_per_eps, s1, s2, fetch_radius, cdo_params),
        daemon=True,
    )
    t.start()


# ── Geo helpers ──────────────────────────────────────────────────────────────
def _haversine_nm(p1, p2):
    la1, lo1 = math.radians(p1[0]), math.radians(p1[1])
    la2, lo2 = math.radians(p2[0]), math.radians(p2[1])
    dlat = la2 - la1; dlon = lo2 - lo1
    a = math.sin(dlat/2)**2 + math.cos(la1)*math.cos(la2)*math.sin(dlon/2)**2
    return _EARTH_R_M * 2 * math.asin(math.sqrt(max(0.0, min(1.0, a)))) / _NM_M


def _bearing(p1, p2):
    la1, lo1 = math.radians(p1[0]), math.radians(p1[1])
    la2, lo2 = math.radians(p2[0]), math.radians(p2[1])
    x = math.sin(lo2-lo1)*math.cos(la2)
    y = math.cos(la1)*math.sin(la2) - math.sin(la1)*math.cos(la2)*math.cos(lo2-lo1)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _node_to_latlon(node_id, n=None, m=None):
    return _GRID_COORDS[node_id]

# ── TMA polygon helpers ───────────────────────────────────────────────────────
def _parse_tma_poly():
    """Parse _STOCKHOLM_TMA_POLY string into list of (lat, lon) vertices."""
    nums = [float(x) for x in _STOCKHOLM_TMA_POLY.split()]
    return [(nums[i], nums[i+1]) for i in range(0, len(nums)-1, 2)]

_TMA_VERTICES = _parse_tma_poly()


def _seg_intersect(p1, p2, p3, p4):
    """Return True if segment p1-p2 intersects segment p3-p4 (2-D lat/lon)."""
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


def _crosses_tma_boundary(waypoints):
    """Return True if the track crosses any edge of the TMA polygon."""
    verts = _TMA_VERTICES
    n = len(verts)
    pts = [(wp.lat, wp.lon) for wp in waypoints if wp.lat is not None and wp.lon is not None]
    if len(pts) < 2:
        return False
    for i in range(len(pts) - 1):
        for j in range(n):
            if _seg_intersect(pts[i], pts[i+1], verts[j], verts[(j+1) % n]):
                return True
    return False


def _point_in_tma(lat, lon):
    """Ray-casting point-in-polygon test for TMA polygon."""
    verts = _TMA_VERTICES
    n = len(verts)
    inside = False
    x, y = lon, lat
    j = n - 1
    for i in range(n):
        xi, yi = verts[i][1], verts[i][0]
        xj, yj = verts[j][1], verts[j][0]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


# Grid bounding box derived from _GRID_COORDS extremes
_GRID_LAT_MIN = 58.801
_GRID_LAT_MAX = 60.2646
_GRID_LON_MIN = 16.7003
_GRID_LON_MAX = 19.1261


def _point_in_grid(lat, lon):
    """Return True if (lat, lon) is inside the grid bounding box."""
    return (_GRID_LAT_MIN <= lat <= _GRID_LAT_MAX and
            _GRID_LON_MIN <= lon <= _GRID_LON_MAX)


def _grid_crossing_time(wps):
    """Return the interpolated Unix timestamp when the track crosses the grid boundary.

    Scans consecutive waypoint pairs for the first outside→inside transition and
    linearly interpolates the crossing time between those two waypoints.
    Falls back to the time of the first inside waypoint if no clean transition is found,
    or None if the track never enters the grid.
    """
    for i in range(len(wps) - 1):
        outside = not _point_in_grid(wps[i].lat, wps[i].lon)
        inside  = _point_in_grid(wps[i+1].lat, wps[i+1].lon)
        if outside and inside:
            t0, t1 = float(wps[i].time), float(wps[i+1].time)
            if t1 <= t0:
                return t1
            # Linear interpolation: fraction along segment where boundary is crossed
            # Use lon as proxy axis for E/W entries and lat for N/S entries
            la0, lo0 = wps[i].lat,   wps[i].lon
            la1, lo1 = wps[i+1].lat, wps[i+1].lon
            dlat = la1 - la0
            dlon = lo1 - lo0
            # Find fraction f such that (la0+f*dlat, lo0+f*dlon) is on the boundary
            fracs = []
            if abs(dlat) > 1e-9:
                fracs.append((_GRID_LAT_MIN - la0) / dlat)
                fracs.append((_GRID_LAT_MAX - la0) / dlat)
            if abs(dlon) > 1e-9:
                fracs.append((_GRID_LON_MIN - lo0) / dlon)
                fracs.append((_GRID_LON_MAX - lo0) / dlon)
            valid = [f for f in fracs if 0.0 <= f <= 1.0]
            f = min(valid) if valid else 0.0
            return t0 + f * (t1 - t0)
    # No outside→inside transition found; return time of first inside waypoint
    first_inside = next((wp for wp in wps if _point_in_grid(wp.lat, wp.lon)), None)
    return float(first_inside.time) if first_inside else None


def _assign_entry(lat, lon):
    """Assign aircraft to the closest FrendlyTMA entry node."""
    best_dir  = None
    best_dist = 1e9
    for direction, ep in _ENTRY_LATLON.items():
        d = _haversine_nm((lat, lon), ep)
        if d < best_dist:
            best_dist = d
            best_dir  = direction
    return best_dir, best_dist


def _eta_to_entry_min(lat, lon, velocity_ms, entry_latlon):
    """Estimate minutes until aircraft reaches entry node."""
    dist_nm  = _haversine_nm((lat, lon), entry_latlon)
    speed_ms = max(velocity_ms, 50.0)
    return (dist_nm * _NM_M / speed_ms) / 60.0


# ── Optimisation (exact replication of run_scenario.py logic) ────────────────
def _get_all_paths():
    """Load and return all_paths from the grid pkl (no MIP solving)."""
    pkl_paths = str(_FRENDLY_CODE / 'Paths_with_max14edges.pkl')
    with open(pkl_paths, 'rb') as f:
        _all_paths_entry = pickle.load(f)
        all_paths        = pickle.load(f)
    return all_paths


def _run_optimisation(aircraft_by_entry, now_unix, epsilon=2, time_limit_override=None, s1=2, s2=3,
                      u_override=None):
    try:
        import gurobipy as gp
        from gurobipy import GRB
    except ImportError:
        stack.stack('ECHO TMAOPT: gurobipy not installed.')
        return None

    code_dir     = str(_FRENDLY_CODE)
    pkl_graph    = os.path.join(code_dir, 'may16-2018-graph.pkl')
    pkl_paths    = os.path.join(code_dir, 'Paths_with_max14edges.pkl')

    for pf in [pkl_graph, pkl_paths]:
        if not os.path.exists(pf):
            stack.stack(f'ECHO TMAOPT: Missing file: {os.path.basename(pf)}')
            return None

    # ── Load graph ──
    with open(pkl_graph, 'rb') as f:
        n = pickle.load(f); m = pickle.load(f); NODES = pickle.load(f)
        multN = pickle.load(f); NODES_last = pickle.load(f); N_max = pickle.load(f)
        B = pickle.load(f); N_exit = pickle.load(f); B_and_exit = pickle.load(f)
        NODES_last_B = pickle.load(f); NODES1 = pickle.load(f); NODES2 = pickle.load(f)
        ent = pickle.load(f); LINKS = pickle.load(f); length = pickle.load(f)
        BAD = pickle.load(f); NBAD = pickle.load(f); GRAPH = pickle.load(f)

    # ── Load paths ──
    with open(pkl_paths, 'rb') as f:
        all_paths_entry = pickle.load(f); all_paths = pickle.load(f)
        ent_i_paths_no  = pickle.load(f); all_paths_links = pickle.load(f)
        paths_on_links  = pickle.load(f); paths_node = pickle.load(f)

    # ── Separation constants ──
    s  = s1  # general occupancy window — tied to Heavy–Heavy separation
    # s1, s2 come from caller (dialog parameters)

    # ── Map B node → direction string ──
    _node_to_dir = {9: 'N', 45: 'W', 66: 'E', 160: 'S'}
    _dir_to_node = {v: k for k, v in _node_to_dir.items()}

    # ── Build dynamic ta1, u, AC, AC1, AC2, C1, C2 from real aircraft ──
    now_dt   = datetime.fromtimestamp(now_unix, tz=timezone.utc)
    midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    now_min  = (now_unix - midnight.timestamp()) / 60.0

    # Grid edge length: approximate nm distance between adjacent nodes
    _grid_spacing_nm = _haversine_nm(_GRID_COORDS[72], _GRID_COORDS[61])  # one row up, same col

    ta1          = {}
    AC           = {b: [] for b in B}
    AC1          = {b: [] for b in B}   # Heavy
    AC2          = {b: [] for b in B}   # Medium/Light
    C1           = []                    # all Heavy ac ids
    C2           = []                    # all Medium/Light ac ids
    A_used       = []
    callsign_map = {}
    u            = {}

    ac_id_counter = 1
    for direction, ac_list in aircraft_by_entry.items():
        node = _dir_to_node.get(direction)
        if node is None or not ac_list:
            continue
        # cap by number of pre-computed path slots for this entry node
        max_slots = ent_i_paths_no[list(B).index(node) + 1] - ent_i_paths_no[list(B).index(node)]
        ac_list_use = ac_list[:min(len(ac_list), max_slots)]

        for ac in ac_list_use:
            ac_id = ac_id_counter
            ac_id_counter += 1

            callsign_map[ac_id] = (ac.get('callsign') or f'AC{ac_id}').strip()

            # ta1: use actual grid boundary crossing time if available,
            # otherwise fall back to ETA estimate from snapshot position.
            crossing_ts = ac.get('crossing_time')
            if crossing_ts is not None:
                crossing_min = (crossing_ts - midnight.timestamp()) / 60.0
                arr_min = int(round(crossing_min))
            else:
                eta_min = _eta_to_entry_min(
                    ac['lat'], ac['lon'], ac.get('velocity_ms', 200.0),
                    _ENTRY_LATLON[direction]
                )
                arr_min = int(round(now_min + eta_min))
            ta1[node, ac_id] = arr_min

            # Wake turbulence category from real aircraft type
            wake = _wake_cat_from_icao24(ac.get('icao24', ''))
            if wake == 'H':
                AC1[node].append(ac_id)
                C1.append(ac_id)
            else:
                AC2[node].append(ac_id)
                C2.append(ac_id)
            AC[node].append(ac_id)
            A_used.append(ac_id)

            # Edge travel time u[ac_id, path_length, step]:
            # If CDO-derived u_override provided, use per-path times; else use speed-based constant.
            if u_override and ac_id in u_override:
                cdo_u = u_override[ac_id]  # dict: path_idx -> [edge_times_min]
                for pl in range(5, 16):
                    for step in range(1, 16):
                        u[ac_id, pl, step] = s1  # default fallback — minimum = s1
                # Map all_paths index to Gurobi (path_length, step) keys
                for path_idx, edge_times in cdo_u.items():
                    if path_idx < len(all_paths):
                        pl = len(all_paths[path_idx]) - 1
                        for step_idx, et in enumerate(edge_times, start=1):
                            # Enforce minimum edge time >= s1 so mid-edge separation is guaranteed
                            u[ac_id, pl, step_idx] = max(s1, int(round(et)))
            else:
                spd_kts = ac.get('velocity_ms', 128.6) / 0.5144
                spd_kts = max(150.0, min(350.0, spd_kts))
                edge_nm = _grid_spacing_nm
                edge_min_real = (edge_nm / spd_kts) * 60.0
                edge_min = max(s1, round(edge_min_real))  # minimum = s1 to guarantee mid-edge separation
                for pl in range(5, 16):
                    for step in range(1, 16):
                        u[ac_id, pl, step] = edge_min

    if not A_used:
        stack.stack('ECHO TMAOPT: No aircraft assigned to entry nodes.')
        return None

    # ── Log wake category breakdown ──
    n_heavy  = len(C1)
    n_medium = len(C2)
    stack.stack(f'ECHO TMAOPT: Wake categories — Heavy: {n_heavy}, Medium/Light: {n_medium}')

    # ── Time horizon: span actual arrivals + small buffer ──
    all_arr = list(ta1.values())
    arr_min = min(all_arr)
    arr_max = max(all_arr)
    T_start = arr_min - 5
    # Window must cover latest arrival + max possible travel time (path_len * max_u * epsilon)
    max_path_len  = max((len(all_paths[k]) - 1) for k in range(len(all_paths))) if all_paths else 14
    max_u_val     = max((u.get((a, pl, st), 2) for a in A_used for pl in range(5,16) for st in range(1,16)), default=2)
    travel_buffer = max_path_len * max_u_val + epsilon * 2
    T_end   = max(arr_max + travel_buffer, arr_min + 30) + 5

    # TimeLimit: use override from dialog if provided, else scale with epsilon + aircraft count
    n_ac = len(A_used)
    time_limit = int(time_limit_override) if time_limit_override else min(60 + epsilon * 60 + n_ac * 5, 600)

    # ── Trajectory lookup tables (exact replication of run_scenario.py) ──
    def _find_trajectories(profile):
        xi = {}
        for i in range(len(B)):
            for a in AC[B[i]]:
                for k in range(ent_i_paths_no[i], ent_i_paths_no[i + 1]):
                    l_b = len(all_paths[k]) - 1
                    tt  = ta1[B[i], a]
                    for j in range(l_b):
                        xi[a, k, all_paths[k][j]] = tt
                        tt += profile[a, l_b, j + 1]
                    xi[a, k, all_paths[k][l_b]] = tt
        return xi

    def _path_node_time_exact(xi):
        pnt_xi = {}; pnt = {}
        # Build by inverting xi: group (a,k,i) -> t, then fill lookup dicts
        # Much faster than scanning all (t,node,ac) combinations
        by_a_i = {}  # (a,i) -> list of (t, k)
        for (a, k, i), t in xi.items():
            by_a_i.setdefault((a, i), []).append((t, k))
        # pre-fill empty
        for j in range(len(B)):
            for a in AC[B[j]]:
                for i in NODES:
                    pnt[a, i, 0]    = []   # sentinel; real keys filled below
                    pnt_xi[a, i, 0] = []
        for (a, i), pairs in by_a_i.items():
            for t_node, k in pairs:
                for t in range(max(T_start - 10, t_node - s + 1), t_node + 1):
                    if T_start - 10 <= t <= T_end + 10:
                        pnt.setdefault((a, i, t), []).append(k)
                pnt_xi.setdefault((a, i, t_node), []).append(k)
        # ensure every (a,i,t) key exists (empty list default)
        for j in range(len(B)):
            for a in AC[B[j]]:
                for i in NODES:
                    for t in range(T_start - 10, T_end + 10):
                        pnt.setdefault((a, i, t), [])
                        pnt_xi.setdefault((a, i, t), [])
        return pnt, pnt_xi

    def _path_node_time_sigma(xi, sigma, ACj):
        pnt = {}
        by_a_i = {}
        for (a, k, i), t in xi.items():
            by_a_i.setdefault((a, i), []).append((t, k))
        for j in range(len(B)):
            for a in ACj[B[j]]:
                for i in NODES:
                    for t_node, k in by_a_i.get((a, i), []):
                        for t in range(max(T_start - 10, t_node - sigma + 1), t_node + 1):
                            if T_start - 10 <= t <= T_end + 10:
                                pnt.setdefault((a, i, t), []).append(k)
                    for t in range(T_start - 10, T_end + 10):
                        pnt.setdefault((a, i, t), [])
        return pnt

    xi = _find_trajectories(u)
    path_node_time, path_node_time_Xi = _path_node_time_exact(xi)
    path_node_time_sigma12   = _path_node_time_sigma(xi, s2, AC2)
    path_node_time_sigma21   = _path_node_time_sigma(xi, s1, AC1)
    path_node_time_sigma11   = path_node_time_sigma21
    path_node_time_sigma21_2 = _path_node_time_sigma(xi, s2, AC1)
    path_node_time_sigma22   = _path_node_time_sigma(xi, s1, AC2)

    # ── Gurobi model (exact replication of run_scenario.py) ──
    alpha = 0.1
    omega = 3
    path_no = len(all_paths_links)

    model = gp.Model('TMAOpt')
    model.setParam('OutputFlag', 0)
    model.setParam('TimeLimit', time_limit)
    model.setParam('Threads', min(8, os.cpu_count() or 4))
    model.setParam('MIPGap', 0.01)
    model.setParam('MIPFocus', 1)
    model.setParam('Heuristics', 0.3)

    rho   = model.addVars(range(path_no), vtype=GRB.BINARY)
    X_new = model.addVars(LINKS, vtype=GRB.BINARY)

    Indices = [
        (a, k, t)
        for i in range(len(B))
        for a in AC[B[i]]
        for k in range(ent_i_paths_no[i], ent_i_paths_no[i + 1])
        for t in range(ta1[B[i], a] - epsilon, ta1[B[i], a] + epsilon + 1)
    ]
    tau = model.addVars(Indices, vtype=GRB.BINARY)

    # Objective
    model.setObjective(
        alpha * gp.quicksum(X_new[i, j] * length[i, j] for [i, j] in LINKS) +
        (1 - alpha) * gp.quicksum(
            len(AC[B[i1]]) * length[i, j] * rho[k]
            for i1 in range(len(B))
            for k in range(ent_i_paths_no[i1], ent_i_paths_no[i1 + 1])
            for [i, j] in all_paths_links[k]
        ),
        GRB.MINIMIZE
    )

    # Degree constraints
    model.addConstrs(rho[k] <= X_new[i, j]
                     for k in range(path_no) for (i, j) in all_paths_links[k])
    model.addConstrs(gp.quicksum(X_new[i, k1] for (i, k1) in LINKS if k1 == k) <= 2
                     for k in NODES)
    model.addConstrs(gp.quicksum(X_new[i1, j] for (i1, j) in LINKS if i1 == i) <= 1
                     for i in NODES if i != N_exit)
    model.addConstrs(
        gp.quicksum(rho[k] for k in range(ent_i_paths_no[i], ent_i_paths_no[i + 1])) == 1
        for i in range(len(B)) if AC[B[i]]
    )

    # Crossing constraints (exact replication)
    model.addConstrs(
        (X_new[i, i+1+n] + X_new[i+1+n, i] + X_new[i+n, i+1] + X_new[i+1, i+n]) <= 1
        for i in NODES_last_B
        if (i+1) not in B_and_exit if (i+n) not in B_and_exit if (i+1+n) not in B_and_exit
    )
    model.addConstrs(
        (X_new[i, i+1+n] + X_new[i+n, i+1] + X_new[i+1, i+n]) <= 1
        for i in NODES_last if i in B
    )
    model.addConstrs(
        (X_new[i, i+1+n] + X_new[i+1+n, i] + X_new[i+1, i+n]) <= 1
        for i in NODES_last if (i+1) in B
    )
    model.addConstrs(
        (X_new[i, i+1+n] + X_new[i+1+n, i] + X_new[i+n, i+1]) <= 1
        for i in NODES_last if (i+n) in B
    )
    model.addConstrs(
        (X_new[i+1+n, i] + X_new[i+n, i+1] + X_new[i+1, i+n]) <= 1
        for i in NODES_last if (i+n+1) in B
    )

    # Flexible entry time constraints
    model.addConstrs(
        gp.quicksum(tau[a, k, t]
                    for t in range(ta1[B[i], a] - epsilon, ta1[B[i], a] + epsilon + 1)) == rho[k]
        for i in range(len(B)) for a in AC[B[i]]
        for k in range(ent_i_paths_no[i], ent_i_paths_no[i + 1])
    )
    model.addConstrs(
        gp.quicksum(tau[a, k, t]
                    for k in range(ent_i_paths_no[i], ent_i_paths_no[i + 1])
                    for t in range(ta1[B[i], a] - epsilon, ta1[B[i], a] + epsilon + 1)) == 1
        for i in range(len(B)) for a in AC[B[i]]
    )

    # Wake turbulence separation (exact replication of run_scenario.py)
    model.addConstrs(
        gp.quicksum(
            tau[a, k, ta1[j, a] + t1]
            for j in B for a in AC1[j] for t1 in range(-epsilon, epsilon + 1)
            for k in path_node_time_sigma21[a, i, t - t1] if (t - t1) <= T_end
        ) <= omega * (1 - gp.quicksum(
            tau[a, k, ta1[j, a] + t1]
            for j in B for a in AC2[j] for t1 in range(-epsilon, epsilon + 1)
            for k in path_node_time_Xi[a, i, t - t1] if (t - t1) <= T_end
        ))
        for i in NODES for t in range(T_start, T_end)
    )
    model.addConstrs(
        gp.quicksum(
            tau[a, k, ta1[j, a] + t1]
            for j in B for a in AC2[j] for t1 in range(-epsilon, epsilon + 1)
            for k in path_node_time_sigma12[a, i, t - t1] if (t - t1) <= T_end
        ) <= omega * (1 - gp.quicksum(
            tau[a, k, ta1[j, a] + t1]
            for j in B for a in AC1[j] for t1 in range(-epsilon, epsilon + 1)
            for k in path_node_time_Xi[a, i, t - t1] if (t - t1) <= T_end
        ))
        for i in NODES for t in range(T_start, T_end)
    )
    model.addConstrs(
        gp.quicksum(
            tau[a, k, ta1[j, a] + t1]
            for j in B for a in AC1[j] if a != a1
            for t1 in range(-epsilon, epsilon + 1)
            for k in path_node_time_sigma11[a, i, t - t1] if (t - t1) <= T_end
        ) <= omega * (1 - gp.quicksum(
            tau[a1, k, ta1[j1, a1] + t1]
            for t1 in range(-epsilon, epsilon + 1)
            for k in path_node_time_Xi[a1, i, t - t1] if (t - t1) <= T_end
        ))
        for j1 in B for a1 in AC1[j1] for i in NODES for t in range(T_start, T_end)
    )
    model.addConstrs(
        gp.quicksum(
            tau[a, k, ta1[j, a] + t1]
            for j in B for a in AC2[j] if a != a1
            for t1 in range(-epsilon, epsilon + 1)
            for k in path_node_time_sigma22[a, i, t - t1] if (t - t1) <= T_end
        ) <= omega * (1 - gp.quicksum(
            tau[a1, k, ta1[j1, a1] + t1]
            for t1 in range(-epsilon, epsilon + 1)
            for k in path_node_time_Xi[a1, i, t - t1] if (t - t1) <= T_end
        ))
        for j1 in B for a1 in AC2[j1] for i in NODES for t in range(T_start, T_end)
    )

    model.update()
    stack.stack('ECHO TMAOPT: Solving Gurobi model ...')
    model.optimize()

    feasible = model.status in (gp.GRB.OPTIMAL, gp.GRB.SUBOPTIMAL) or model.SolCount > 0

    result = {
        'feasible':        feasible,
        'status':          model.status,
        'epsilon':         epsilon,
        'n_aircraft':      len(A_used),
        'callsign_map':    callsign_map,
        'ta1':             ta1,
        'T_start':         T_start,
        'T_end':           T_end,
        'tree_links':      [],
        'merge_points':    [],
        'optimal_paths':   [],
        'ac_path':         {},   # ac_id -> (path node list, actual entry time minutes)
        'objective':       None,
        'n': n, 'm': m,
        'NODES': NODES, 'LINKS': LINKS, 'N_exit': N_exit,
        'B': B, 'AC': AC,
        'u': u, 'all_paths': all_paths, 'ent_i_paths_no': ent_i_paths_no,
    }

    if feasible:
        result['objective']    = model.ObjVal
        result['tree_links']   = [(i, j) for (i, j) in LINKS if X_new[i, j].X > 0.01]
        result['merge_points'] = [
            k for k in NODES
            if sum(round(X_new[i, j].X) for (i, j) in LINKS if j == k) == 2
        ]
        result['optimal_paths'] = [k for k in range(path_no) if rho[k].X > 0.1]

        # For each aircraft find its chosen path and actual entry time
        for bi in range(len(B)):
            b = B[bi]
            for a in AC[b]:
                for k in range(ent_i_paths_no[bi], ent_i_paths_no[bi + 1]):
                    if rho[k].X > 0.1:
                        # Find the actual entry time from tau
                        actual_t = ta1[b, a]
                        for t in range(ta1[b, a] - epsilon, ta1[b, a] + epsilon + 1):
                            if (a, k, t) in tau and tau[a, k, t].X > 0.5:
                                actual_t = t
                                break
                        result['ac_path'][a] = (all_paths[k], actual_t)
                        break

    return result


# ── Grid drawing helper ───────────────────────────────────────────────────────
def _write_grid(f, links, B, N_exit):
    """Write grid POLYLINE + entry/exit circles to an open .scn file handle."""
    f.write('\n# --- Optimisation Grid ---\n')
    seen = set()
    idx = 0
    for (i, j) in links:
        key = (min(i, j), max(i, j))
        if key in seen:
            continue
        seen.add(key)
        la1, lo1 = _GRID_COORDS[i]
        la2, lo2 = _GRID_COORDS[j]
        f.write(f'00:00:00.00> POLYLINE GRID_{idx:04d} {la1:.4f} {lo1:.4f} {la2:.4f} {lo2:.4f}\n')
        f.write(f'00:00:00.00> COLOR GRID_{idx:04d} 80 80 80\n')
        idx += 1




# ── Save .scn ────────────────────────────────────────────────────────────────
def _save_scn(out_dir, result, timestamp_str, aircraft_by_entry):
    scn_path   = out_dir / f'tmaopt_{timestamp_str}.scn'
    csv_path   = out_dir / f'tmaopt_{timestamp_str}_tracks.csv'

    callsign_map = result.get('callsign_map', {})
    ac_path      = result.get('ac_path', {})
    u            = result.get('u', {})
    B            = result.get('B', [])
    N_exit       = result.get('N_exit', 72)

    ac_lookup = {}
    for acs in aircraft_by_entry.values():
        for ac in acs:
            ac_lookup[ac['callsign']] = ac

    # ── Build tracks CSV ──────────────────────────────────────────────────────
    # Base Unix timestamp = "now" rounded to minute
    base_ts = int(time.time() // 60) * 60

    if result.get('feasible') and ac_path:
        earliest_min = min(entry_min for _, (_, entry_min) in ac_path.items())

        rows = []
        for ac_id, (node_list, entry_min) in ac_path.items():
            cs = callsign_map.get(ac_id)
            if cs is None:
                continue
            ac = ac_lookup.get(cs)
            if ac is None:
                continue

            path_len = len(node_list) - 1
            delay_s  = (entry_min - earliest_min) * 60
            alt_m    = max(1524.0, ac['alt_m'])   # min 5000 ft
            spd_ms   = max(82.0,  min(180.0, ac['velocity_ms']))  # 160–350 kts

            t_offset_s = 0.0
            for step, node in enumerate(node_list):
                lat_wp, lon_wp = _node_to_latlon(node)
                ts_row = int(base_ts + delay_s + t_offset_s)

                # Heading toward next node
                if step < path_len:
                    next_lat, next_lon = _node_to_latlon(node_list[step + 1])
                    dx = next_lon - lon_wp
                    dy = next_lat - lat_wp
                    hdg = (math.degrees(math.atan2(dx, dy)) + 360) % 360
                    edge_min = u.get((ac_id, path_len, step + 1), 1)
                    # descent: spread alt loss evenly across edges
                    vs_ms = 0.0
                else:
                    hdg = 0.0
                    edge_min = 0
                    vs_ms = 0.0

                rows.append({
                    'icao24':           cs.lower(),
                    'callsign':         cs,
                    'est_departure':    '',
                    'est_arrival':      '',
                    'time':             ts_row,
                    'lat':              round(lat_wp, 6),
                    'lon':              round(lon_wp, 6),
                    'baro_alt_m':       round(alt_m, 2),
                    'true_track':       round(hdg, 2),
                    'on_ground':        0,
                    'velocity_ms':      round(spd_ms, 2),
                    'vertical_rate_ms': round(vs_ms, 2),
                })

                if step < path_len:
                    t_offset_s += edge_min * 60

        with open(csv_path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=[
                'icao24','callsign','est_departure','est_arrival',
                'time','lat','lon','baro_alt_m','true_track',
                'on_ground','velocity_ms','vertical_rate_ms'
            ])
            w.writeheader()
            for row in sorted(rows, key=lambda r: (r['callsign'], r['time'])):
                w.writerow(row)

    # ── Build .scn ────────────────────────────────────────────────────────────
    with open(scn_path, 'w') as f:
        f.write(f'# TMA Optimization — {timestamp_str}\n')
        f.write('00:00:00.00> TIME 00:00:00\n')
        f.write('00:00:00.00> STOPREPLAY\n')
        f.write('00:00:00.00> DEL ALL\n')
        f.write('00:00:00.00> PAN 59.574 17.9876\n')
        f.write('00:00:00.00> ZOOM 2.0\n')
        f.write('00:00:00.00> DT 1.0\n')
        f.write('00:00:00.00> TAXI OFF\n')
        f.write('00:00:00.00> SWRAD WPT 0\n')
        f.write('00:00:00.00> SWRAD APT 0\n')
        f.write('00:00:00.00> SWRAD SAT 0\n')
        f.write(f'00:00:00.00> POLY StockholmTMA {_STOCKHOLM_TMA_POLY}\n')
        _write_grid(f, result.get('LINKS', []), B, N_exit)

        if result.get('feasible'):
            f.write(f'\n00:00:00.00> STARTREPLAY {csv_path}\n')

            if result.get('tree_links'):
                f.write('\n# --- Optimised Merge Tree ---\n')
                tree_links = result['tree_links']
                for idx, (i, j) in enumerate(tree_links):
                    lat1, lon1 = _node_to_latlon(i)
                    lat2, lon2 = _node_to_latlon(j)
                    f.write(f'00:00:00.00> POLYLINE TREE_{idx:04d} {lat1:.6f} {lon1:.6f} {lat2:.6f} {lon2:.6f}\n')
                    f.write(f'00:00:00.00> COLOR TREE_{idx:04d} 0 190 255\n')

            if result.get('merge_points'):
                f.write('\n# --- Merge Points ---\n')
                for mp in result['merge_points']:
                    lat, lon = _node_to_latlon(mp)
                    f.write(f'00:00:00.00> CIRCLE MP_{mp} {lat:.6f} {lon:.6f} 1\n')
                    f.write(f'00:00:00.00> COLOR MP_{mp} 255 255 0\n')

        f.write('\n')

    return scn_path


# ── Datetime parser ───────────────────────────────────────────────────────────
def _parse_dtstr(dtstr):
    """Parse ISO datetime string → Unix timestamp (UTC).  Returns None on failure."""
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(dtstr.strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


# ── Trino historical fetch ────────────────────────────────────────────────────
def _fetch_historical(begin_ts, end_ts, fetch_radius_nm=50.0):
    """Fetch aircraft tracks via pyopensky Trino for a time window in the past.
    Returns (records, raw_tracks):
      records    — list of dicts (one per aircraft, snapshot at midpoint of window)
      raw_tracks — list of FlightTrack objects (all waypoints, for saving raw CSV)
    """
    import sys
    _REPO_ROOT_STR = str(Path(__file__).resolve().parents[2])
    if _REPO_ROOT_STR not in sys.path:
        sys.path.insert(0, _REPO_ROOT_STR)

    try:
        from utils.opensky_importer.fetcher import OpenSkyFetcher
    except ImportError:
        stack.stack('ECHO TMAOPT: Cannot import OpenSkyFetcher.')
        return [], []

    lat_margin = fetch_radius_nm / 60.0
    lon_margin = fetch_radius_nm / (60.0 * math.cos(math.radians(_ESSA[0])))
    lamin = _ESSA[0] - lat_margin
    lamax = _ESSA[0] + lat_margin
    lomin = _ESSA[1] - lon_margin
    lomax = _ESSA[1] + lon_margin

    fetcher = OpenSkyFetcher()
    stack.stack('ECHO TMAOPT: Fetching historical aircraft via OpenSky Trino ...')
    tracks = fetcher.fetch_area_flights_trino(begin_ts, end_ts, lamin, lomin, lamax, lomax)
    stack.stack(f'ECHO TMAOPT: {len(tracks)} tracks from Trino.')

    if not tracks:
        return [], []

    records = []
    for track in tracks:
        wps = [wp for wp in track.waypoints if not wp.on_ground and wp.baro_alt_m and wp.baro_alt_m > 300]
        if not wps:
            continue
        # Use the last waypoint OUTSIDE the grid bounding box as the snapshot.
        # The grid defines the actual optimisation domain — aircraft that have already
        # crossed into the grid have a known, precise entry time; those still outside
        # give a clean ETA and unambiguous entry node assignment.
        # Fallback: entire track already inside the grid → use earliest waypoint.
        outside_wps = [w for w in wps if not _point_in_grid(w.lat, w.lon)]
        if outside_wps:
            wp = outside_wps[-1]   # last outside point = closest to grid boundary
        else:
            wp = wps[0]            # all inside — use earliest available

        crossing_ts = _grid_crossing_time(wps)

        records.append({
            'icao24':        track.icao24,
            'callsign':      track.callsign,
            'lat':           wp.lat,
            'lon':           wp.lon,
            'baro_alt':      wp.baro_alt_m or 0.0,
            'on_ground':     False,
            'velocity':      wp.velocity_ms or 200.0,
            'heading':       wp.true_track or 0.0,
            'vertrate':      wp.vertical_rate_ms or 0.0,
            'time':          wp.time,
            'crossing_time': crossing_ts,   # exact grid boundary crossing Unix timestamp
        })
    return records, tracks


def _save_historical_csv(out_dir, tracks, timestamp_str):
    """Save raw FlightTrack waypoints as a tracks CSV in the TMAOpt output folder."""
    csv_path = out_dir / f'tmaopt_{timestamp_str}_historical.csv'
    try:
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow([
                'icao24', 'callsign', 'est_departure', 'est_arrival',
                'time', 'lat', 'lon', 'baro_alt_m', 'true_track',
                'on_ground', 'velocity_ms', 'vertical_rate_ms',
            ])
            for track in tracks:
                for wp in track.waypoints:
                    w.writerow([
                        track.icao24, track.callsign,
                        track.est_departure or '', track.est_arrival or '',
                        wp.time, wp.lat, wp.lon,
                        wp.baro_alt_m if wp.baro_alt_m is not None else '',
                        wp.true_track if wp.true_track is not None else '',
                        int(wp.on_ground),
                        wp.velocity_ms if wp.velocity_ms is not None else '',
                        wp.vertical_rate_ms if wp.vertical_rate_ms is not None else '',
                    ])
        stack.stack(f'ECHO TMAOPT: Historical CSV saved → {csv_path.name}')
    except Exception as exc:
        stack.stack(f'ECHO TMAOPT: Could not save historical CSV: {exc}')


# ── Main thread ───────────────────────────────────────────────────────────────
def _run_tmaopt(dtstr='', duration=60, entries='NESW', max_ac=15,
                max_ac_per_entry=5, max_eps=3, time_limit_per_eps=120,
                s1=2, s2=3, fetch_radius=50, cdo_params=None):
    now_unix = time.time()

    # Determine begin/end timestamps
    if dtstr:
        end_ts_parsed = _parse_dtstr(dtstr)
        if end_ts_parsed is None:
            stack.stack(f'ECHO TMAOPT: Cannot parse datetime "{dtstr}". Use YYYY-MM-DDTHH:MM.')
            return
        end_ts   = int(end_ts_parsed)
        begin_ts = end_ts - int(duration) * 60
        ref_unix = (begin_ts + end_ts) // 2
    else:
        begin_ts = int(now_unix) - int(duration) * 60
        end_ts   = int(now_unix)
        ref_unix = int(now_unix)

    age_s = now_unix - begin_ts
    _ = age_s  # always using Trino regardless of age

    ts      = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime('%Y%m%d_%H%M%S')
    out_dir = _SCENARIO_DIR / f'tmaopt_{ts}'
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch aircraft via Trino
    track_waypoints = {}
    stack.stack(f'ECHO TMAOPT: Fetching {duration} min window ending at {datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")} UTC via Trino ...')
    stack.stack(f'ECHO TMAOPT: Params — entries={entries} max_ac={max_ac} max_ac/entry={max_ac_per_entry} max_eps={max_eps} TL={time_limit_per_eps}s s1={s1} s2={s2} radius={fetch_radius}nm')
    raw_ac, raw_tracks = _fetch_historical(begin_ts, end_ts, fetch_radius_nm=float(fetch_radius))
    if raw_tracks:
        _save_historical_csv(out_dir, raw_tracks, ts)
        for t in raw_tracks:
            cs = t.callsign.strip().upper() or t.icao24.upper()
            track_waypoints[cs] = [wp for wp in t.waypoints if not wp.on_ground]
    stack.stack(f'ECHO TMAOPT: {len(raw_ac)} state vectors received.')

    if not raw_ac:
        stack.stack('ECHO TMAOPT: No data received — aborting.')
        return

    # 2+3. Filter arrivals and assign to entry nodes
    aircraft_by_entry, n_arriving = _build_aircraft_by_entry(
        raw_ac, track_waypoints, entries, max_ac, max_ac_per_entry, ref_unix)
    stack.stack(f'ECHO TMAOPT: {n_arriving} aircraft crossed/inside TMA boundary.')

    if not any(aircraft_by_entry.values()):
        stack.stack('ECHO TMAOPT: No aircraft found crossing TMA boundary.')
        return

    # 4. Save aircraft CSV + selected.txt for traces plugin
    all_selected = {a['callsign'].upper() for acs in aircraft_by_entry.values() for a in acs}
    (out_dir / 'selected.txt').write_text('\n'.join(sorted(all_selected)) + '\n', encoding='utf-8')

    with open(out_dir / 'aircraft.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=[
            'callsign','icao24','entry','node','lat','lon',
            'alt_m','velocity_ms','heading','vertrate','dist_to_entry_nm','crossing_time'
        ])
        w.writeheader()
        for d, acs in aircraft_by_entry.items():
            for ac in acs:
                w.writerow({**ac, 'entry': d, 'node': _ENTRY_NODES[d]})

    # 5. Run optimisation — adaptive epsilon up to max_eps
    stack.stack('ECHO TMAOPT: Running Gurobi optimisation ...')
    n_ac = sum(len(v) for v in aircraft_by_entry.values())
    _base = [0, 2]
    eps_sequence = sorted(set(_base + list(range(3, max_eps + 1))))
    eps_sequence = [e for e in eps_sequence if e <= max_eps]
    from bluesky.plugins.cdo_gen import _run_cdoprecompute_inline, _run_cdogenopt_inline

    # ── Phase 1: CDO precompute — all aircraft × all grid paths ─────────────
    stack.stack(f'ECHO TMAOPT: Phase 1 — CDO precompute ({n_ac} aircraft × all grid paths) ...')
    base_result = {
        'all_paths':         [],
        'cdo_params':        cdo_params or {},
        'aircraft_by_entry': aircraft_by_entry,
        'ref_unix':          ref_unix,
    }
    all_paths = _get_all_paths()
    base_result['all_paths'] = all_paths

    t_precompute_start = time.time()
    u_cdo = {}
    try:
        u_cdo, _fuel_cdo = _run_cdoprecompute_inline(base_result)
    except Exception as _e:
        stack.stack(f'ECHO TMAOPT: CDO precompute error: {_e} — falling back to speed-based u.')
    t_precompute = time.time() - t_precompute_start
    stack.stack(f'ECHO TMAOPT: CDO precompute done in {t_precompute:.1f}s '
                f'({len(u_cdo)} aircraft covered).')

    u_override = {ac_idx + 1: paths for ac_idx, paths in u_cdo.items()} if u_cdo else None

    # ── Phase 2: Gurobi with CDO-derived u ──────────────────────────────────
    stack.stack(f'ECHO TMAOPT: Phase 2 — Gurobi optimisation with CDO travel times ...')
    result = None
    t_opt_start = time.time()
    for eps in eps_sequence:
        stack.stack(f'ECHO TMAOPT: Trying epsilon={eps} ({n_ac} aircraft) ...')
        t_eps_start = time.time()
        result = _run_optimisation(aircraft_by_entry, ref_unix, epsilon=eps,
                                   time_limit_override=time_limit_per_eps,
                                   s1=s1, s2=s2,
                                   u_override=u_override)
        t_eps = time.time() - t_eps_start
        if result is None:
            stack.stack('ECHO TMAOPT: Optimisation failed (model error).')
            return
        if result['feasible']:
            stack.stack(f'ECHO TMAOPT: epsilon={eps} solved in {t_eps:.1f}s')
            break
        stack.stack(f'ECHO TMAOPT: epsilon={eps} → status={result["status"]}  {t_eps:.1f}s, trying next ...')
    t_opt_total = time.time() - t_opt_start

    if result is None:
        stack.stack('ECHO TMAOPT: Optimisation failed.')
        return

    result['cdo_params']        = cdo_params or {}
    result['aircraft_by_entry'] = aircraft_by_entry
    result['ref_unix']          = ref_unix

    with open(out_dir / 'result.pkl', 'wb') as f:
        pickle.dump(result, f)

    scn_path = _save_scn(out_dir, result, ts, aircraft_by_entry)

    eps_used = result.get('epsilon', '?')
    if result['feasible']:
        stack.stack(
            f'ECHO TMAOPT: FEASIBLE  obj={result["objective"]:.2f}  '
            f'ac={result["n_aircraft"]}  eps={eps_used}  '
            f'merges={len(result["merge_points"])}  links={len(result["tree_links"])}  '
            f'time={t_precompute + t_opt_total:.1f}s'
        )
    else:
        stack.stack(
            f'ECHO TMAOPT: INFEASIBLE after all epsilon values  '
            f'ac={result["n_aircraft"]}  time={t_precompute + t_opt_total:.1f}s'
        )

    stack.stack(f'ECHO TMAOPT: Saved → {out_dir.relative_to(_REPO_ROOT)}')
    stack.stack(f'ECHO TMAOPT: SCN: {scn_path.name}')

    # ── Phase 3: CDO on optimal routes → final CDO CSV + SCN ─────────────────
    if result['feasible']:
        stack.stack('ECHO TMAOPT: Phase 3 — generating CDO profiles on optimal routes ...')
        try:
            _run_cdogenopt_inline(result, out_dir)
        except Exception as _cdo_err:
            stack.stack(f'ECHO TMAOPT: CDOGENOPT error: {_cdo_err}')


def _build_aircraft_by_entry(raw_ac, track_waypoints, entries, max_ac,
                             max_ac_per_entry, ref_unix):
    """Filter arrivals from raw_ac and assign them to TMA entry nodes.

    Returns (aircraft_by_entry, arriving_count) where aircraft_by_entry is a
    dict keyed by direction ('N','E','S','W') and arriving_count is the number
    of aircraft that passed the arrival filter before capping.
    """
    active_entries = [d for d in ('N', 'E', 'S', 'W') if d in entries]

    arriving = []
    seen = set()
    for ac in raw_ac:
        if ac['on_ground']:
            continue
        if ac['baro_alt'] < 50.0:
            continue
        cs = (ac['callsign'] or ac['icao24']).strip().upper()
        if cs in seen:
            continue
        wps = track_waypoints.get(cs, [])
        if not (_crosses_tma_boundary(wps) or any(_point_in_tma(wp.lat, wp.lon) for wp in wps)):
            continue
        if _is_ga_icao24(ac['icao24']):
            continue
        airborne_wps = [wp for wp in wps if wp.baro_alt_m is not None and not wp.on_ground]
        if not airborne_wps:
            continue
        half = len(airborne_wps) // 2
        second_half = airborne_wps[half:]
        if len(second_half) >= 2:
            if second_half[0].baro_alt_m - second_half[-1].baro_alt_m < 500.0:
                continue
        mid_wp = second_half[len(second_half) // 2]
        brg_to_essa = _bearing((mid_wp.lat, mid_wp.lon), _ESSA)
        hdg = mid_wp.true_track or 0.0
        if abs(((hdg - brg_to_essa + 180) % 360) - 180) > 100:
            continue
        if ac['baro_alt'] > 12000.0:
            continue
        if ac.get('vertrate', 0.0) > 2.0:
            continue
        seen.add(cs)
        arriving.append(ac)

    aircraft_by_entry = {d: [] for d in active_entries}
    for ac in arriving:
        direction, dist_ep = _assign_entry(ac['lat'], ac['lon'])
        if direction not in active_entries:
            continue
        aircraft_by_entry[direction].append({
            'callsign':         (ac['callsign'] or ac['icao24']).strip(),
            'icao24':           ac['icao24'],
            'lat':              ac['lat'],
            'lon':              ac['lon'],
            'alt_m':            ac['baro_alt'],
            'velocity_ms':      ac['velocity'],
            'heading':          ac['heading'],
            'vertrate':         ac['vertrate'],
            'dist_to_entry_nm': dist_ep,
            'crossing_time':    ac.get('crossing_time'),
        })

    for d in aircraft_by_entry:
        aircraft_by_entry[d].sort(key=lambda a: a['dist_to_entry_nm'])
        aircraft_by_entry[d] = aircraft_by_entry[d][:max_ac_per_entry]

    all_ac_flat = [(d, a) for d in active_entries for a in aircraft_by_entry[d]]
    if len(all_ac_flat) > max_ac:
        all_ac_flat.sort(key=lambda x: x[1]['dist_to_entry_nm'])
        all_ac_flat = all_ac_flat[:max_ac]
        aircraft_by_entry = {d: [] for d in active_entries}
        for d, a in all_ac_flat:
            aircraft_by_entry[d].append(a)

    _ref_midnight = datetime.fromtimestamp(ref_unix, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    _now_min = (ref_unix - _ref_midnight.timestamp()) / 60.0

    for d, acs in aircraft_by_entry.items():
        if acs:
            ac_labels = []
            for a in acs[:5]:
                ct = a.get('crossing_time')
                if ct is not None:
                    ta1_min = int(round((ct - _ref_midnight.timestamp()) / 60.0))
                else:
                    eta = _eta_to_entry_min(a['lat'], a['lon'], a['velocity_ms'], _ENTRY_LATLON[d])
                    ta1_min = int(round(_now_min + eta))
                hh = (ta1_min // 60) % 24
                mm = ta1_min % 60
                ac_labels.append(f'{a["callsign"]}@{hh:02d}:{mm:02d}')
            stack.stack(f'ECHO TMAOPT:   {d} (node {_ENTRY_NODES[d]}): {len(acs)} ac — {", ".join(ac_labels)}')

    return aircraft_by_entry, len(arriving)
