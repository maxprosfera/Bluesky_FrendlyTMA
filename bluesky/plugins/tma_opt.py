"""
TMA Optimization Plugin — C5 button
Fetches live aircraft via OpenSky REST, estimates TMA entry crossing times,
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
_ESSA            = (59.6521, 17.9054)
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


# ── FrendlyTMA grid node coordinates (from Coordinates.txt) ─────────────────
# Format: node_id -> (lat, lon)
_GRID_COORDS = {
    1:(60.2794,16.6503),2:(60.2794,16.8929),3:(60.2794,17.1355),4:(60.2794,17.378),
    5:(60.2794,17.6206),6:(60.2794,17.8632),7:(60.2794,18.1058),8:(60.2794,18.3484),
    9:(60.2794,18.5909),10:(60.2794,18.8335),11:(60.2794,19.0761),
    12:(60.1749,16.6503),13:(60.1749,16.8929),14:(60.1749,17.1355),15:(60.1749,17.378),
    16:(60.1749,17.6206),17:(60.1749,17.8632),18:(60.1749,18.1058),19:(60.1749,18.3484),
    20:(60.1749,18.5909),21:(60.1749,18.8335),22:(60.1749,19.0761),
    23:(60.0703,16.6503),24:(60.0703,16.8929),25:(60.0703,17.1355),26:(60.0703,17.378),
    27:(60.0703,17.6206),28:(60.0703,17.8632),29:(60.0703,18.1058),30:(60.0703,18.3484),
    31:(60.0703,18.5909),32:(60.0703,18.8335),33:(60.0703,19.0761),
    34:(59.9658,16.6503),35:(59.9658,16.8929),36:(59.9658,17.1355),37:(59.9658,17.378),
    38:(59.9658,17.6206),39:(59.9658,17.8632),40:(59.9658,18.1058),41:(59.9658,18.3484),
    42:(59.9658,18.5909),43:(59.9658,18.8335),44:(59.9658,19.0761),
    45:(59.8612,16.6503),46:(59.8612,16.8929),47:(59.8612,17.1355),48:(59.8612,17.378),
    49:(59.8612,17.6206),50:(59.8612,17.8632),51:(59.8612,18.1058),52:(59.8612,18.3484),
    53:(59.8612,18.5909),54:(59.8612,18.8335),55:(59.8612,19.0761),
    56:(59.7567,16.6503),57:(59.7567,16.8929),58:(59.7567,17.1355),59:(59.7567,17.378),
    60:(59.7567,17.6206),61:(59.7567,17.8632),62:(59.7567,18.1058),63:(59.7567,18.3484),
    64:(59.7567,18.5909),65:(59.7567,18.8335),66:(59.7567,19.0761),
    67:(59.6521,16.6503),68:(59.6521,16.8929),69:(59.6521,17.1355),70:(59.6521,17.378),
    71:(59.6521,17.6206),72:(59.6521,17.8632),73:(59.6521,18.1058),74:(59.6521,18.3484),
    75:(59.6521,18.5909),76:(59.6521,18.8335),77:(59.6521,19.0761),
    78:(59.5476,16.6503),79:(59.5476,16.8929),80:(59.5476,17.1355),81:(59.5476,17.378),
    82:(59.5476,17.6206),83:(59.5476,17.8632),84:(59.5476,18.1058),85:(59.5476,18.3484),
    86:(59.5476,18.5909),87:(59.5476,18.8335),88:(59.5476,19.0761),
    89:(59.4431,16.6503),90:(59.4431,16.8929),91:(59.4431,17.1355),92:(59.4431,17.378),
    93:(59.4431,17.6206),94:(59.4431,17.8632),95:(59.4431,18.1058),96:(59.4431,18.3484),
    97:(59.4431,18.5909),98:(59.4431,18.8335),99:(59.4431,19.0761),
    100:(59.3385,16.6503),101:(59.3385,16.8929),102:(59.3385,17.1355),103:(59.3385,17.378),
    104:(59.3385,17.6206),105:(59.3385,17.8632),106:(59.3385,18.1058),107:(59.3385,18.3484),
    108:(59.3385,18.5909),109:(59.3385,18.8335),110:(59.3385,19.0761),
    111:(59.234,16.6503),112:(59.234,16.8929),113:(59.234,17.1355),114:(59.234,17.378),
    115:(59.234,17.6206),116:(59.234,17.8632),117:(59.234,18.1058),118:(59.234,18.3484),
    119:(59.234,18.5909),120:(59.234,18.8335),121:(59.234,19.0761),
    122:(59.1294,16.6503),123:(59.1294,16.8929),124:(59.1294,17.1355),125:(59.1294,17.378),
    126:(59.1294,17.6206),127:(59.1294,17.8632),128:(59.1294,18.1058),129:(59.1294,18.3484),
    130:(59.1294,18.5909),131:(59.1294,18.8335),132:(59.1294,19.0761),
    133:(59.0249,16.6503),134:(59.0249,16.8929),135:(59.0249,17.1355),136:(59.0249,17.378),
    137:(59.0249,17.6206),138:(59.0249,17.8632),139:(59.0249,18.1058),140:(59.0249,18.3484),
    141:(59.0249,18.5909),142:(59.0249,18.8335),143:(59.0249,19.0761),
    144:(58.9203,16.6503),145:(58.9203,16.8929),146:(58.9203,17.1355),147:(58.9203,17.378),
    148:(58.9203,17.6206),149:(58.9203,17.8632),150:(58.9203,18.1058),151:(58.9203,18.3484),
    152:(58.9203,18.5909),153:(58.9203,18.8335),154:(58.9203,19.0761),
    155:(58.8158,16.6503),156:(58.8158,16.8929),157:(58.8158,17.1355),158:(58.8158,17.378),
    159:(58.8158,17.6206),160:(58.8158,17.8632),161:(58.8158,18.1058),162:(58.8158,18.3484),
    163:(58.8158,18.5909),164:(58.8158,18.8335),165:(58.8158,19.0761),
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
def tmaopt(dtstr: str = '', duration: int = 60):
    """[datetime] [duration_min] — Optimise TMA entry. Uses Trino for historical, REST for recent."""
    stack.stack('ECHO TMAOPT: Starting ...')
    t = threading.Thread(target=_run_tmaopt, args=(dtstr, duration), daemon=True)
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


# ── Live REST fetch ───────────────────────────────────────────────────────────
def _fetch_live(now_unix):
    """Fetch current live state vectors via OpenSky REST /states/all."""
    try:
        import requests
    except ImportError:
        stack.stack('ECHO TMAOPT: requests not installed.')
        return []

    lat_margin = _FETCH_RADIUS_NM / 60.0
    lon_margin = _FETCH_RADIUS_NM / (60.0 * math.cos(math.radians(_ESSA[0])))
    lamin = _ESSA[0] - lat_margin
    lamax = _ESSA[0] + lat_margin
    lomin = _ESSA[1] - lon_margin
    lomax = _ESSA[1] + lon_margin

    url    = 'https://opensky-network.org/api/states/all'
    params = {'lamin': lamin, 'lomin': lomin, 'lamax': lamax, 'lomax': lomax}

    try:
        resp = requests.get(url, params=params, timeout=15)
    except Exception as e:
        stack.stack(f'ECHO TMAOPT: REST request failed: {e}')
        return []

    if resp.status_code != 200:
        stack.stack(f'ECHO TMAOPT: REST error {resp.status_code}')
        return []

    states = resp.json().get('states') or []

    # State vector column indices (OpenSky docs):
    # 0=icao24 1=callsign 5=lon 6=lat 7=baro_alt_m 8=on_ground
    # 9=velocity_ms 10=true_track 11=vertical_rate 3=time_position
    records = []
    for sv in states:
        try:
            lon      = sv[5];   lat  = sv[6]
            if lat is None or lon is None:
                continue
            records.append({
                'icao24':    str(sv[0] or '').strip().lower(),
                'callsign':  str(sv[1] or '').strip(),
                'lat':       float(lat),
                'lon':       float(lon),
                'baro_alt':  float(sv[7] or 0.0),
                'on_ground': bool(sv[8]),
                'velocity':  float(sv[9] or 200.0),
                'heading':   float(sv[10] or 0.0),
                'vertrate':  float(sv[11] or 0.0),
                'time':      int(sv[3]  or now_unix),
            })
        except (TypeError, ValueError, IndexError):
            continue

    return records

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


# ── Aircraft classification ──────────────────────────────────────────────────
def _is_arriving(heading, vertrate, lat, lon):
    """True if aircraft is heading toward ESSA and descending (or level)."""
    brg = _bearing((lat, lon), _ESSA)
    diff = abs(((heading - brg + 180) % 360) - 180)
    return diff < 75 and vertrate <= 0.5


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
def _run_optimisation(aircraft_by_entry, now_unix, epsilon=2):
    try:
        import gurobipy as gp
        from gurobipy import GRB
    except ImportError:
        stack.stack('ECHO TMAOPT: gurobipy not installed.')
        return None

    code_dir     = str(_FRENDLY_CODE)
    pkl_graph    = os.path.join(code_dir, 'may16-2018-graph.pkl')
    pkl_aircraft = os.path.join(code_dir, 'may16-2018-aircraft.pkl')
    pkl_paths    = os.path.join(code_dir, 'Paths_with_max14edges.pkl')

    for pf in [pkl_graph, pkl_aircraft, pkl_paths]:
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

    # ── Load aircraft meta ──
    with open(pkl_aircraft, 'rb') as f:
        LP1 = pickle.load(f); s = pickle.load(f); s1 = pickle.load(f); s2 = pickle.load(f)
        A_orig = pickle.load(f); C1_orig = pickle.load(f); C2_orig = pickle.load(f)
        AC_orig = pickle.load(f); AC1_orig = pickle.load(f); AC2_orig = pickle.load(f)
        in_traffic = pickle.load(f); ta1_orig = pickle.load(f); P = pickle.load(f); u = pickle.load(f)

    # ── Load paths ──
    with open(pkl_paths, 'rb') as f:
        all_paths_entry = pickle.load(f); all_paths = pickle.load(f)
        ent_i_paths_no  = pickle.load(f); all_paths_links = pickle.load(f)
        paths_on_links  = pickle.load(f); paths_node = pickle.load(f)

    # ── Map B node → direction string ──
    _node_to_dir = {9: 'N', 45: 'W', 66: 'E', 160: 'S'}
    _dir_to_node = {v: k for k, v in _node_to_dir.items()}

    # ── Build ta1 in minutes-from-midnight for real aircraft ──
    now_dt   = datetime.fromtimestamp(now_unix, tz=timezone.utc)
    midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    now_min  = (now_unix - midnight.timestamp()) / 60.0

    ta1       = {}
    AC        = {b: [] for b in B}
    AC1       = {b: [] for b in B}
    AC2       = {b: [] for b in B}
    callsign_map = {}  # model_ac_id → real callsign
    A_used    = []

    for direction, ac_list in aircraft_by_entry.items():
        node = _dir_to_node.get(direction)
        if node is None or not ac_list:
            continue
        available_ids = list(AC_orig[node])
        ac_list_use   = ac_list[:len(available_ids)]

        for i, ac in enumerate(ac_list_use):
            ac_id = available_ids[i]
            callsign_map[ac_id] = (ac.get('callsign') or f'AC{ac_id}').strip()

            # ETA to entry node in minutes
            eta_min  = _eta_to_entry_min(
                ac['lat'], ac['lon'], ac.get('velocity_ms', 200.0),
                _ENTRY_LATLON[direction]
            )
            arr_min = int(round(now_min + eta_min))
            ta1[node, ac_id] = arr_min

            # Wake turbulence category — preserve original assignment from pkl
            if ac_id in AC1_orig.get(node, []):
                AC1[node].append(ac_id)
            else:
                AC2[node].append(ac_id)
            AC[node].append(ac_id)
            A_used.append(ac_id)

    if not A_used:
        stack.stack('ECHO TMAOPT: No aircraft assigned to entry nodes.')
        return None

    # ── Filter C1/C2 to used aircraft only ──
    A_not_used = [i for i in A_orig if i not in A_used]
    C1 = [j for j in C1_orig if j not in A_not_used]
    C2 = [j for j in C2_orig if j not in A_not_used]

    # ── Time horizon: centred on earliest arrival, 70-min window ──
    all_arr    = list(ta1.values())
    T_start    = min(all_arr) - 5
    T_end      = T_start + 70

    # TimeLimit scales with epsilon and aircraft count
    n_ac = len(A_used)
    time_limit = min(60 + epsilon * 60 + n_ac * 5, 600)

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
        for t in range(T_start - 10, T_end + 10):
            for i in NODES:
                for j in range(len(B)):
                    for a in AC[B[j]]:
                        lst = []; lst_xi = []
                        for k in paths_node[i]:
                            if ent_i_paths_no[j] <= k < ent_i_paths_no[j + 1]:
                                if t <= xi[a, k, i] <= t + s - 1:
                                    lst.append(k)
                                if xi[a, k, i] == t:
                                    lst_xi.append(k)
                        pnt[a, i, t]    = lst
                        pnt_xi[a, i, t] = lst_xi
        return pnt, pnt_xi

    def _path_node_time_sigma(xi, sigma, ACj):
        pnt = {}
        for t in range(T_start - 10, T_end + 10):
            for i in NODES:
                for j in range(len(B)):
                    for a in ACj[B[j]]:
                        lst = []
                        for k in paths_node[i]:
                            if ent_i_paths_no[j] <= k < ent_i_paths_no[j + 1]:
                                if t <= xi[a, k, i] <= t + sigma - 1:
                                    lst.append(k)
                        pnt[a, i, t] = lst
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
                for idx, (i, j) in enumerate(result['tree_links']):
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
def _fetch_historical(begin_ts, end_ts):
    """Fetch aircraft tracks via pyopensky Trino for a time window in the past.
    Returns (records, raw_tracks):
      records    — list of dicts compatible with _fetch_live() format (one per aircraft)
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

    lat_margin = _FETCH_RADIUS_NM / 60.0
    lon_margin = _FETCH_RADIUS_NM / (60.0 * math.cos(math.radians(_ESSA[0])))
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

    mid_ts = (begin_ts + end_ts) // 2
    records = []
    for track in tracks:
        wps = [wp for wp in track.waypoints if not wp.on_ground]
        if not wps:
            continue
        wp = min(wps, key=lambda w: abs(w.time - mid_ts))
        records.append({
            'icao24':    track.icao24,
            'callsign':  track.callsign,
            'lat':       wp.lat,
            'lon':       wp.lon,
            'baro_alt':  wp.baro_alt_m or 0.0,
            'on_ground': False,
            'velocity':  wp.velocity_ms or 200.0,
            'heading':   wp.true_track or 0.0,
            'vertrate':  wp.vertical_rate_ms or 0.0,
            'time':      wp.time,
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
def _run_tmaopt(dtstr='', duration=60):
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
    use_trino = age_s > 3590  # data older than ~1 hour → use Trino

    ts      = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime('%Y%m%d_%H%M%S')
    out_dir = _SCENARIO_DIR / f'tmaopt_{ts}'
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch aircraft
    track_waypoints = {}  # callsign -> list of Waypoint (historical mode only)
    if use_trino:
        stack.stack(f'ECHO TMAOPT: Historical mode — fetching {duration} min window ending at {datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")} UTC ...')
        raw_ac, raw_tracks = _fetch_historical(begin_ts, end_ts)
        if raw_tracks:
            _save_historical_csv(out_dir, raw_tracks, ts)
            for t in raw_tracks:
                cs = t.callsign.strip().upper() or t.icao24.upper()
                track_waypoints[cs] = [wp for wp in t.waypoints if not wp.on_ground]
    else:
        stack.stack('ECHO TMAOPT: Live mode — fetching current state vectors via OpenSky REST ...')
        raw_ac = _fetch_live(now_unix)
    stack.stack(f'ECHO TMAOPT: {len(raw_ac)} state vectors received.')

    if not raw_ac:
        stack.stack('ECHO TMAOPT: No data received — aborting.')
        return

    # 2. Filter: must cross or be inside TMA boundary (historical) or heading toward ESSA (live)
    arriving = []
    seen = set()
    for ac in raw_ac:
        if ac['on_ground']:
            continue
        cs = (ac['callsign'] or ac['icao24']).strip().upper()
        if cs in seen:
            continue
        if use_trino:
            wps = track_waypoints.get(cs, [])
            # Accept if track crosses TMA boundary OR any waypoint is inside TMA
            if not (_crosses_tma_boundary(wps) or
                    any(_point_in_tma(wp.lat, wp.lon) for wp in wps)):
                continue
            # Skip GA / helicopters — no valid CDO profile
            if _is_ga_icao24(ac['icao24']):
                continue
        else:
            if not _is_arriving(ac['heading'], ac['vertrate'], ac['lat'], ac['lon']):
                continue
        seen.add(cs)
        arriving.append(ac)

    stack.stack(f'ECHO TMAOPT: {len(arriving)} aircraft crossed/inside TMA boundary.')

    if not arriving:
        stack.stack('ECHO TMAOPT: No aircraft found crossing TMA boundary.')
        return

    # 3. Assign to closest entry node; cap per entry
    aircraft_by_entry = {'N': [], 'E': [], 'S': [], 'W': []}
    for ac in arriving:
        direction, dist_ep = _assign_entry(ac['lat'], ac['lon'])
        aircraft_by_entry[direction].append({
            'callsign':    (ac['callsign'] or ac['icao24']).strip(),
            'icao24':      ac['icao24'],
            'lat':         ac['lat'],
            'lon':         ac['lon'],
            'alt_m':       ac['baro_alt'],
            'velocity_ms': ac['velocity'],
            'heading':     ac['heading'],
            'vertrate':    ac['vertrate'],
            'dist_to_entry_nm': dist_ep,
        })

    # Sort each entry by distance (closest first) and cap
    for d in aircraft_by_entry:
        aircraft_by_entry[d].sort(key=lambda a: a['dist_to_entry_nm'])
        aircraft_by_entry[d] = aircraft_by_entry[d][:_MAX_AC_PER_ENTRY]

    for d, acs in aircraft_by_entry.items():
        if acs:
            stack.stack(f'ECHO TMAOPT:   {d} (node {_ENTRY_NODES[d]}): {len(acs)} ac — {", ".join(a["callsign"] for a in acs[:5])}')

    # Save selected callsigns for traces plugin
    all_selected = {a['callsign'].upper() for acs in aircraft_by_entry.values() for a in acs}
    with open(out_dir / 'selected.txt', 'w') as f:
        f.write('\n'.join(sorted(all_selected)))

    # 4. Save aircraft CSV
    with open(out_dir / 'aircraft.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=[
            'callsign','icao24','entry','node','lat','lon',
            'alt_m','velocity_ms','heading','vertrate','dist_to_entry_nm'
        ])
        w.writeheader()
        for d, acs in aircraft_by_entry.items():
            for ac in acs:
                w.writerow({**ac, 'entry': d, 'node': _ENTRY_NODES[d]})

    # 5. Run optimisation — adaptive epsilon (0 → 2 → 3 → 4), matching run_scenario.py strategy
    stack.stack('ECHO TMAOPT: Running Gurobi optimisation ...')
    n_ac = sum(len(v) for v in aircraft_by_entry.values())
    # Epsilon sequence: start tight, widen until feasible
    eps_sequence = [0, 2, 3, 4]
    result = None
    for eps in eps_sequence:
        stack.stack(f'ECHO TMAOPT: Trying epsilon={eps} ({n_ac} aircraft) ...')
        result = _run_optimisation(aircraft_by_entry, ref_unix, epsilon=eps)
        if result is None:
            stack.stack('ECHO TMAOPT: Optimisation failed (model error).')
            return
        if result['feasible']:
            break
        stack.stack(f'ECHO TMAOPT: epsilon={eps} → status={result["status"]}, trying next ...')

    if result is None:
        stack.stack('ECHO TMAOPT: Optimisation failed.')
        return

    # 6. Save result pkl
    with open(out_dir / 'result.pkl', 'wb') as f:
        pickle.dump(result, f)

    # 7. Save .scn
    scn_path = _save_scn(out_dir, result, ts, aircraft_by_entry)

    # 8. Report
    eps_used = result.get('epsilon', '?')
    if result['feasible']:
        stack.stack(
            f'ECHO TMAOPT: FEASIBLE  obj={result["objective"]:.2f}  '
            f'ac={result["n_aircraft"]}  eps={eps_used}  '
            f'merges={len(result["merge_points"])}  links={len(result["tree_links"])}'
        )
    else:
        stack.stack(f'ECHO TMAOPT: INFEASIBLE after all epsilon values  ac={result["n_aircraft"]}')

    stack.stack(f'ECHO TMAOPT: Saved → {out_dir.relative_to(_REPO_ROOT)}')
    stack.stack(f'ECHO TMAOPT: SCN: {scn_path.name}')
