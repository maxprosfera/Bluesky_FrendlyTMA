"""BlueSky plugin: TMA fuel consumption calculator using BADA 4.2.

Stack command:
    FUELCALC [csv_path]   — calculate fuel burn from most recent (or given) *_tracks.csv

Bound to C3 button in the Cust layout.

Physics: BADA 4 User Manual equations, mirrored from fuel_burn_v3.m / calculate_* functions.

Requires:
    - BADA 4.2 at Data/BADA/BADA_4.2/ (relative to repo root)
    - Optional: cdsapi + ~/.cdsapirc for ERA5 weather
    - Optional: netCDF4 or xarray for reading ERA5 .nc files
"""

import csv
import math
import os
import threading
from pathlib import Path

import numpy as np

from bluesky import stack

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT    = Path(__file__).parents[2]
_BADA_DIR     = _REPO_ROOT / 'Data' / 'BADA' / 'BADA_4.2'
_WEATHER_DIR  = _REPO_ROOT / 'Data' / 'Weather'
_ACTYPE_CACHE = _REPO_ROOT / 'cache' / 'opensky' / 'actype_cache.json'
_SCENARIO_DIR = _REPO_ROOT / 'scenario' / 'OpenSky'

# ---------------------------------------------------------------------------
# ISA constants (BADA 4 §2.1)
# ---------------------------------------------------------------------------

_g0     = 9.80665      # m/s²  standard gravity
_R      = 287.05287    # J/(kg·K)  gas constant air
_k      = 1.4          # ratio of specific heats
_T0     = 288.15       # K  MSL temperature
_p0     = 101325.0     # Pa MSL pressure
_FAP_ALT_M  = 457.2    # 1500 ft — MATLAB ends integration here (FAP altitude)
_MIN_TAS_MS = 77.0     # ~150 kt — below this BADA clean-config aerodynamics invalid
_rho0   = 1.225        # kg/m³ MSL density
_a0     = 340.294      # m/s  speed of sound at MSL
_beta_T = -0.0065      # K/m  temperature lapse rate troposphere
_H_trop = 11000.0      # m   tropopause altitude

# ESSA threshold (used for arrival classification)
_ESSA_LAT = 59.6519
_ESSA_LON = 17.9186


def _dist_deg(lat1, lon1, lat2, lon2):
    dlat = lat1 - lat2; dlon = lon1 - lon2
    return (dlat**2 + dlon**2) ** 0.5


def _classify_track(ac_rows: list) -> str:
    """Return 'arr', 'dep', or 'other' based on distance/altitude trend to ESSA.
    Uses the same logic as opensky_traces._classify().
    """
    wps = [(r['lat'], r['lon'], r['baro_alt_m'])
           for r in ac_rows
           if r['baro_alt_m'] > 0 and not r['on_ground']]
    if len(wps) < 2:
        return 'other'
    first_alt  = wps[0][2];  last_alt  = wps[-1][2]
    first_dist = _dist_deg(wps[0][0], wps[0][1], _ESSA_LAT, _ESSA_LON)
    last_dist  = _dist_deg(wps[-1][0], wps[-1][1], _ESSA_LAT, _ESSA_LON)
    if last_alt < first_alt and last_dist < first_dist:
        return 'arr'
    if last_alt > first_alt and last_dist > first_dist:
        return 'dep'
    return 'other'

# ---------------------------------------------------------------------------
# BADA 4.2 model lookup  (ICAO type → BADA folder)
# ---------------------------------------------------------------------------

_BADA_MAP = {
    'A318': 'A318-112', 'A319': 'A319-131', 'A320': 'A320-232',
    'A321': 'A321-131', 'A20N': 'A320-271N', 'A21N': 'A321-131',
    'A359': 'A350-941', 'A35K': 'A350-941', 'A333': 'A330-341',
    'A332': 'A330-321', 'A343': 'A340-313', 'A388': 'A380-861',
    'A306': 'A300B4-622',
    'B736': 'B73622',   'B737': 'B737W24',  'B738': 'B738W26',
    'B38M': 'B738W26',  'B739': 'B739ERW26','B733': 'B73320',
    'B732': 'B73215',   'B752': 'B752WRR40','B753': 'B753RR',
    'B744': 'B744GE',   'B748': 'B748F',
    'B788': 'B788RR70', 'B789': 'B789RR74', 'B78X': 'B789RR74',
    'B77W': 'B773ERGE115B', 'B77L': 'B772LR', 'B77F': 'B772LR',
    'B763': 'B763ERGE61', 'B742': 'B742RR',
    'AT76': 'ATR72-600', 'AT75': 'ATR72-500', 'AT72': 'ATR72-210',
    'AT45': 'ATR42-500', 'AT43': 'ATR42-300',
    'DH8D': 'ATR72-600', 'DH8C': 'ATR72-200', 'DH8A': 'ATR42-300',
    'E170': 'EMB-170STD', 'E175': 'EMB-175STD',
    'E190': 'EMB-190STD', 'E195': 'EMB-195STD',
    'CRJ9': 'EMB-190LR',  'CRJX': 'EMB-190LR',
    'BCS3': 'A319-131',   'BCS1': 'A318-112',
    'PC24': 'EMB-505',    'PC12': 'ATR42-300',
    'SB20': 'ATR72-200',  'SF34': 'ATR42-300',
    'SU95': 'EMB-175STD', 'G550': 'EMB-505', 'G650': 'EMB-505',
    'B190': 'ATR42-300',  'BE20': 'ATR42-300', 'BE9L': 'ATR42-300',
    'JS41': 'ATR42-300',  'JS32': 'ATR42-300',
    'SW4':  'ATR42-300',  'C208': 'ATR42-300',
    'F50':  'ATR72-200',  'F27':  'ATR72-200',
}

# ICAO type prefixes / exact types that are piston/helicopter/ultralight —
# skip these entirely (no valid BADA4 jet/turboprop model available)
_GA_TYPES = {
    'C172', 'C182', 'C208', 'C206', 'C152', 'C150', 'C525', 'C56X',
    'P28A', 'P28B', 'PA46', 'PA31', 'PA34',
    'RV10', 'RV6', 'RV7', 'RV8', 'RV9',
    'GLAS', 'GLID', 'ULAC',
    'EC45', 'EC35', 'EC35', 'AS50', 'R44', 'R22', 'B06', 'B407',
    'A109', 'A169', 'H135', 'H145', 'S76', 'S92',
    'HIGH', 'ULAC', 'BALL', 'SHIP',
    'B429', 'BK17', 'EC30', 'EC25',
}
_GA_PREFIXES = ('EC', 'R4', 'R2', 'AS', 'S7', 'S9', 'H1', 'H6')

_BADA_FALLBACK = 'B738W26'
_MLW_FACTOR    = 0.9


def _is_ga(actype: str) -> bool:
    """Return True if the ICAO aircraft type is GA/helicopter/ultralight."""
    if not actype:
        return False
    t = actype.upper()
    return t in _GA_TYPES or t.startswith(_GA_PREFIXES)

# Module-level caches
_bada_cache:        dict = {}
_release_csv_cache: dict = {}
_actype_json_cache: dict = {}
_weather_cache:     dict = {}


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def init_plugin():
    _load_release_csv()
    _load_actype_cache()
    return {'plugin_name': 'FUEL_CALC', 'plugin_type': 'sim'}


# ---------------------------------------------------------------------------
# Stack command
# ---------------------------------------------------------------------------

@stack.command
def fuelcalc(csv_path: 'txt' = ''):
    """Calculate TMA fuel consumption from OpenSky tracks CSV.
    Syntax: FUELCALC [csv_path]"""
    if not csv_path:
        csv_path = _csv_from_scenario() or _auto_detect_csv()
    if not csv_path:
        stack.stack('ECHO FUELCALC: No tracks CSV found for current scenario.')
        return
    stack.stack(f'ECHO FUELCALC: Starting for {Path(csv_path).name} ...')
    t = threading.Thread(target=_run_calc, args=(csv_path,), daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Scenario-aware CSV detection (mirrors opensky_traces.py logic)
# ---------------------------------------------------------------------------

def _csv_from_scenario() -> str:
    """Read ic.scn to find the current scenario, then return its historical tracks CSV."""
    ic_scn = _REPO_ROOT / 'scenario' / 'ic.scn'
    if not ic_scn.exists():
        return ''
    try:
        for line in ic_scn.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('#'):
                continue
            if '>IC ' in line or '> IC ' in line:
                parts = line.split('IC ', 1)
                if len(parts) < 2:
                    continue
                scn_path = Path(parts[1].strip())
                if not scn_path.exists():
                    return ''
                # Scan .scn for STARTREPLAY or LOADTRACES line
                for scn_line in scn_path.read_text(encoding='utf-8').splitlines():
                    upper = scn_line.upper()
                    for kw in ('STARTREPLAY', 'LOADTRACES'):
                        idx = upper.find(kw)
                        if idx != -1:
                            token = scn_line[idx + len(kw):].strip()
                            if not token:
                                continue
                            p = Path(token)
                            if not p.is_absolute():
                                p = _REPO_ROOT / p
                            # TMAOpt: swap optimised → historical CSV
                            if p.stem.endswith('_tracks') and 'TMAOpt' in str(p):
                                hist = p.with_name(
                                    p.stem.replace('_tracks', '_historical') + '.csv'
                                )
                                return str(hist) if hist.exists() else ''
                            # OpenSky: use directly
                            return str(p) if p.exists() else ''
                break
    except Exception:
        pass
    return ''


def _auto_detect_csv() -> str:
    """Fall-back: most recently modified historical tracks CSV across known folders."""
    import glob as _glob
    candidates = (
        _glob.glob(str(_REPO_ROOT / 'scenario' / 'OpenSky' / '*_tracks.csv'))
        + _glob.glob(str(_REPO_ROOT / 'scenario' / 'TMAOpt' / '**' / '*_historical.csv'),
                     recursive=True)
    )
    if not candidates:
        return ''
    return max(candidates, key=lambda p: Path(p).stat().st_mtime)


# ---------------------------------------------------------------------------
# Main calculation (background thread)
# ---------------------------------------------------------------------------

def _run_calc(csv_path: str):
    csv_path = Path(csv_path)
    stem     = csv_path.stem.replace('_tracks', '')

    try:
        rows = _read_tracks_csv(csv_path)
    except Exception as e:
        stack.stack(f'ECHO FUELCALC: CSV read error: {e}')
        return
    if not rows:
        stack.stack('ECHO FUELCALC: CSV is empty.')
        return

    date_str = _extract_date(stem)
    weather  = _get_era5(date_str) if date_str else None

    from collections import defaultdict
    groups: dict = defaultdict(list)
    for row in rows:
        groups[row['callsign']].append(row)

    _enrich_actype_cache(groups)

    results = []
    errors  = []

    for cs, ac_rows in groups.items():
        ac_rows.sort(key=lambda r: r['time'])
        icao24    = ac_rows[0].get('icao24', '')
        actype    = _resolve_actype(icao24, cs)

        # Skip GA/helicopter/ultralight — no valid BADA4 model
        if _is_ga(actype):
            continue

        # Skip departures and pass-through flights — arrivals only
        flight_type = _classify_track(ac_rows)
        if flight_type != 'arr':
            continue

        bada_name = _resolve_bada_model(actype)

        try:
            bada = _load_bada(bada_name)
        except Exception as e:
            errors.append(f'{cs}: BADA load failed ({bada_name}): {e}')
            try:
                bada      = _load_bada(_BADA_FALLBACK)
                bada_name = _BADA_FALLBACK
            except Exception:
                errors.append(f'{cs}: fallback BADA also failed, skipping.')
                continue

        try:
            res = _calc_fuel_aircraft(ac_rows, bada, weather)
            res.update(callsign=cs, icao24=icao24,
                       actype=actype, bada_model=bada_name,
                       flight_type=flight_type)
            results.append(res)
        except Exception as e:
            errors.append(f'{cs}: calc error: {e}')

    out_path = csv_path.parent / f'fuel_{stem}.csv'
    # Only save/report flights with non-zero fuel (had valid segments above FAP)
    results_valid = [r for r in results if r['fuel_kg'] > 0]
    _save_fuel_csv(out_path, results_valid)

    total_fuel = sum(r['fuel_kg'] for r in results_valid)
    skipped_ga    = sum(1 for cs, ac_rows in groups.items()
                        if _is_ga(_resolve_actype(ac_rows[0].get('icao24', ''), cs)))
    skipped_noarr = sum(1 for cs, ac_rows in groups.items()
                        if not _is_ga(_resolve_actype(ac_rows[0].get('icao24', ''), cs))
                        and _classify_track(ac_rows) != 'arr')
    skipped_zero  = len(results) - len(results_valid)
    lines = [f'--- FUEL CALC: {stem} ---']
    for r in sorted(results_valid, key=lambda x: x['fuel_kg'], reverse=True):
        fl   = int(r['mean_fl'])
        mins = int(r['duration_s'] / 60)
        lines.append(
            f'{r["callsign"]:<9} {r["actype"] or "?":<5} {r["bada_model"]:<14}'
            f'  {r["fuel_kg"]:6.1f} kg  FL{fl:03d}  {mins} min'
        )
    lines.append(
        f'TOTAL: {len(results_valid)} arrivals | {total_fuel:.1f} kg'
        f'  (skipped: {skipped_ga} GA, {skipped_noarr} dep/other, {skipped_zero} no-data)'
    )
    lines.append(f'Saved: {out_path.name}')
    for err in errors:
        lines.append(f'WARN: {err}')

    for line in lines:
        stack.stack(f"ECHO {line.replace(chr(39), '')}")


# ---------------------------------------------------------------------------
# CSV reader
# ---------------------------------------------------------------------------

def _read_tracks_csv(path: Path) -> list:
    rows = []
    with open(path, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                rows.append({
                    'icao24':           row.get('icao24', '').strip(),
                    'callsign':         row.get('callsign', '').strip(),
                    'time':             float(row.get('time', 0)),
                    'lat':              float(row.get('lat', 0)),
                    'lon':              float(row.get('lon', 0)),
                    'baro_alt_m':       float(row.get('baro_alt_m', 0)),
                    'true_track':       float(row.get('true_track', 0)),
                    'on_ground':        str(row.get('on_ground', '0')).strip()
                                        in ('1', 'True', 'true'),
                    'velocity_ms':      float(row.get('velocity_ms', 0)),
                    'vertical_rate_ms': float(row.get('vertical_rate_ms', 0)),
                })
            except (ValueError, KeyError):
                continue
    return rows


# ---------------------------------------------------------------------------
# Type / BADA resolution
# ---------------------------------------------------------------------------

def _load_release_csv():
    release = _BADA_DIR / 'release.csv'
    if not release.is_file():
        return
    try:
        with open(release, newline='') as f:
            reader = csv.DictReader(f, delimiter=';')
            for row in reader:
                icao  = row.get('ICAO', '').strip()
                fname = row.get('filename', '').strip()
                if icao and fname and icao not in _release_csv_cache:
                    _release_csv_cache[icao] = fname
    except Exception:
        pass


def _load_actype_cache():
    if not _ACTYPE_CACHE.is_file():
        return
    try:
        import json
        with open(_ACTYPE_CACHE) as f:
            _actype_json_cache.update(json.load(f))
    except Exception:
        pass


def _resolve_actype(icao24: str, callsign: str = '') -> str:
    key = icao24.lower() if icao24 else ''
    if key and key in _actype_json_cache:
        val = _actype_json_cache[key]
        tc = val.get('typecode', '') if isinstance(val, dict) else str(val)
        if tc:
            return tc.strip().upper()
    try:
        import sys
        sys.path.insert(0, str(_REPO_ROOT / 'utils'))
        from opensky_importer.actype_lookup import resolve_actype as _lookup
        tc = _lookup(icao24, callsign, {})
        if tc and tc != 'B738':
            _actype_json_cache[key] = tc
            return tc.strip().upper()
        if tc == 'B738':
            _actype_json_cache[key] = tc
            return 'B738'
    except Exception:
        pass
    return ''


def _enrich_actype_cache(groups: dict):
    """Pre-fetch aircraft types for all unknown icao24 codes via actype_lookup."""
    try:
        import sys
        sys.path.insert(0, str(_REPO_ROOT / 'utils'))
        from opensky_importer.actype_lookup import resolve_actype as _lookup
        for cs, ac_rows in groups.items():
            icao24 = ac_rows[0].get('icao24', '').lower()
            if not icao24:
                continue
            if icao24 in _actype_json_cache:
                continue
            _lookup(icao24, cs, {})
    except Exception:
        pass


def _resolve_bada_model(actype: str) -> str:
    if actype in _BADA_MAP:
        return _BADA_MAP[actype]
    if actype in _release_csv_cache:
        return _release_csv_cache[actype]
    return _BADA_FALLBACK


# ---------------------------------------------------------------------------
# BADA 4.2 XML parser
# Mirrors read_bada_xml_CDO() from fuel_burn_v3.m
# ---------------------------------------------------------------------------

import xml.etree.ElementTree as ET


def _ct_mcmb(M: float, b: list) -> float:
    """C_T_MCMB: 6th-degree polynomial in M (JET, from BADA4 TFM/MCMB/CT)."""
    if len(b) < 6:
        return 0.3
    return b[0] + b[1]*M + b[2]*M**2 + b[3]*M**3 + b[4]*M**4 + b[5]*M**5


def _load_bada(model_name: str) -> dict:
    if model_name in _bada_cache:
        return _bada_cache[model_name]

    xml_path = _BADA_DIR / model_name / f'{model_name}.xml'
    if not xml_path.is_file():
        raise FileNotFoundError(f'BADA XML not found: {xml_path}')

    tree = ET.parse(xml_path)
    root = tree.getroot()
    for el in root.iter():
        if '}' in el.tag:
            el.tag = el.tag.split('}', 1)[1]

    def floats(node, tag):
        return [float(e.text) for e in node.findall(tag)]

    def flt(node, tag, default=0.0):
        el = node.find(tag)
        return float(el.text) if el is not None else default

    bada = {}

    # Engine type
    bada['engine_type'] = (root.findtext('type') or 'JET').strip().upper()

    # Wing area S
    bada['S'] = flt(root, './/AFCM/S')

    # CD scalar
    clean_cfg = next(
        (c for c in root.findall('.//AFCM/Configuration') if c.get('HLid') == '0'),
        None,
    )
    bada['CD_scalar'] = flt(clean_cfg, './/LGUP/DPM_clean/scalar', 1e-6) \
        if clean_cfg is not None else 1e-6

    # CD clean coefficients (15 × d) — compressibility polar
    if clean_cfg is not None:
        dpm = clean_cfg.find('.//LGUP/DPM_clean/CD_clean')
        bada['d'] = floats(dpm, 'd') if dpm is not None else []
    else:
        bada['d'] = []

    # BLM_clean: CL_max polynomial (5 × bf) or scalar CL_max
    if clean_cfg is not None:
        blm_clean = clean_cfg.find('.//LGUP/BLM_clean')
        if blm_clean is not None:
            bada['bf']      = floats(blm_clean, 'CL_clean/bf')
            bada['CL_Mach0'] = flt(blm_clean, 'CL_Mach0', 1.0)
            bada['Mmin']     = flt(blm_clean, 'Mmin', 0.2)
            bada['Mmax']     = flt(blm_clean, 'Mmax', 0.82)
        else:
            blm = clean_cfg.find('.//LGUP/BLM')
            bada['bf']       = []
            bada['CL_max_scalar'] = flt(blm, 'CL_max', 1.5) if blm is not None else 1.5
            bada['Mmin']     = 0.2
            bada['Mmax']     = 0.82
    else:
        bada['bf']   = []
        bada['Mmin'] = 0.2
        bada['Mmax'] = 0.82

    # ALM/DLM/MLW — mass at landing
    alm = root.find('.//ALM/DLM/MLW')
    mlw_val = float(alm.text) if alm is not None else flt(root, './/PFM/MREF', 70000.0)
    bada['mass'] = mlw_val * _MLW_FACTOR

    # LHV
    bada['LHV'] = flt(root, './/PFM/LHV', 43217000.0)

    # n_eng
    bada['n_eng'] = int(flt(root, './/PFM/n_eng', 2.0))

    if bada['engine_type'] == 'JET':
        pfm = root.find('.//PFM/TFM')
        # f: 25 fuel coefficients C_F(CT, M)
        cf_node = pfm.find('CF') if pfm is not None else None
        bada['f']  = floats(cf_node, 'f') if cf_node is not None else []
        # fi: 9 idle fuel coefficients
        lidl = pfm.find('LIDL') if pfm is not None else None
        fi_node = lidl.find('CF') if lidl is not None else None
        bada['fi'] = floats(fi_node, 'fi') if fi_node is not None else []
        # ti: 12 idle thrust coefficients
        ti_node = lidl.find('CT') if lidl is not None else None
        bada['ti'] = floats(ti_node, 'ti') if ti_node is not None else []
        # b: 6 MCMB thrust coefficients C_T_MCMB(M)
        mcmb = pfm.find('MCMB') if pfm is not None else None
        ct_mcmb = mcmb.find('CT') if mcmb is not None else None
        bada['b'] = floats(ct_mcmb, 'b') if ct_mcmb is not None else []
    else:
        tpm = root.find('.//PFM/TPM')
        cf_node = tpm.find('CF') if tpm is not None else None
        bada['f']  = floats(cf_node, 'f') if cf_node is not None else []
        lidl = tpm.find('LIDL') if tpm is not None else None
        fi_node = lidl.find('CF') if lidl is not None else None
        bada['fi'] = floats(fi_node, 'fi') if fi_node is not None else []
        ti_node = lidl.find('CT') if lidl is not None else None
        bada['ti'] = floats(ti_node, 'ti') if ti_node is not None else []
        bada['n_eng'] = int(flt(root, './/PFM/n_eng', 2.0))
        bada['b'] = []

    _bada_cache[model_name] = bada
    return bada


# ---------------------------------------------------------------------------
# ISA atmosphere — returns (T_K, p_Pa, pressure_ratio, T_ratio)
# pressure_ratio δ = p/p₀;  T_ratio θ = T/T₀
# ---------------------------------------------------------------------------

def _isa(alt_m: float):
    if alt_m <= _H_trop:
        T = _T0 + _beta_T * alt_m
        p = _p0 * (T / _T0) ** (-_g0 / (_beta_T * _R))
    else:
        T_trop = _T0 + _beta_T * _H_trop
        p_trop = _p0 * (T_trop / _T0) ** (-_g0 / (_beta_T * _R))
        T = T_trop
        p = p_trop * math.exp(-_g0 * (alt_m - _H_trop) / (_R * T_trop))
    delta = p / _p0
    theta = T / _T0
    return T, p, delta, theta


# ---------------------------------------------------------------------------
# BADA 4 aerodynamics (fuel_burn_v3.m formulas verbatim)
# ---------------------------------------------------------------------------

def _cl_max(M: float, bf: list) -> float:
    """calculate_C_L_max: CL_max = bf1 + bf2·M + bf3·M² + bf4·M³ + bf5·M⁴"""
    if not bf:
        return 1.5
    return bf[0] + bf[1]*M + bf[2]*M**2 + bf[3]*M**3 + bf[4]*M**4


def _cl(mass_kg: float, delta: float, S: float, M: float, CL_max: float) -> float:
    """calculate_C_L: CL = 2·m·g / (δ·p₀·γ·S·M²)"""
    denom = delta * _p0 * _k * S * M**2
    if denom < 1.0:
        return CL_max
    CL = (2.0 * mass_kg * _g0) / denom
    return min(CL, CL_max)


def _cd(M: float, CL: float, d: list, CD_scalar: float) -> float:
    """calculate_C_D: Prandtl-Glauert compressibility polar.
    C0 + C2·CL² + C6·CL⁶, where each Ci depends on M via 1/sqrt(1-M²) terms.
    Mirrors calculate_C_D_CDO() exactly."""
    if len(d) < 15:
        return 0.025
    beta2 = max(1.0 - M**2, 1e-6)
    b1  = math.sqrt(beta2)
    b2  = beta2
    b32 = b2 ** 1.5
    b3  = beta2 ** 3
    b92 = beta2 ** 4.5
    b6  = beta2 ** 6
    b7  = beta2 ** 7
    b152= beta2 ** 7.5
    b8  = beta2 ** 8
    b172= beta2 ** 8.5

    C0 = d[0] + d[1]/b1 + d[2]/b2 + d[3]/b32 + d[4]/b2**2
    C2 = d[5] + d[6]/b32 + d[7]/b3 + d[8]/b92 + d[9]/b6
    C6 = d[10] + d[11]/b7 + d[12]/b152 + d[13]/b8 + d[14]/b172

    CD = CD_scalar * (C0 + C2 * CL**2 + C6 * CL**6)
    return max(CD, 0.001)


def _drag(delta: float, S: float, M: float, CD: float) -> float:
    """calculate_drag: D = ½·δ·p₀·γ·S·M²·CD"""
    return 0.5 * delta * _p0 * _k * S * M**2 * CD


def _ct_idle_jet(delta: float, M: float, ti: list) -> float:
    """calculate_C_T_idle (JET): 3×4 polynomial in (pressure_ratio, M)"""
    if len(ti) < 12:
        return 0.0
    ct = (ti[0]*delta**-1 + ti[1] + ti[2]*delta + ti[3]*delta**2
          + (ti[4]*delta**-1 + ti[5] + ti[6]*delta + ti[7]*delta**2)*M
          + (ti[8]*delta**-1 + ti[9] + ti[10]*delta + ti[11]*delta**2)*M**2)
    return ct


def _thr_idle(delta: float, mass_kg: float, CT_idle: float) -> float:
    """calculate_Thr_idle: T_idle = δ·m·g·CT_idle"""
    return delta * mass_kg * _g0 * CT_idle


def _ct(thrust_N: float, delta: float, mass_kg: float) -> float:
    """calculate_C_T: CT = T / (δ·m·g)"""
    denom = delta * mass_kg * _g0
    if abs(denom) < 1e-6:
        return 0.0
    return thrust_N / denom


def _cf_jet(CT: float, M: float, f: list) -> float:
    """calculate_C_F (JET): 5×5 polynomial in (CT, M).
    CF = Σ_{i=0}^{4} (Σ_{j=0}^{4} f[i*5+j] · CT^j) · M^i"""
    if len(f) < 25:
        return max(CT * 0.04, 0.0)
    CT_vec = [1.0, CT, CT**2, CT**3, CT**4]
    M_vec  = [1.0, M,  M**2,  M**3,  M**4]
    cf = 0.0
    for i in range(5):
        for j in range(5):
            cf += f[i*5 + j] * M_vec[i] * CT_vec[j]
    return cf


def _cf_idle_jet(fi: list, delta: float, M: float, theta: float) -> float:
    """calculate_CF_idle (JET): 3×3 polynomial in (pressure_ratio, M), scaled by θ^-0.5"""
    if len(fi) < 9:
        return 0.0
    cf_idle = ((fi[0] + fi[1]*delta + fi[2]*delta**2)
               + (fi[3] + fi[4]*delta + fi[5]*delta**2)*M
               + (fi[6] + fi[7]*delta + fi[8]*delta**2)*M**2) * (1.0/delta) * theta**-0.5
    return cf_idle


def _fuel_flow(delta: float, theta: float, mass_kg: float, LHV: float, CF: float) -> float:
    """calculate_fuel_flow: F = δ·√θ·m·g·a₀·(1/LHV)·CF  [kg/s]"""
    F = delta * math.sqrt(theta) * mass_kg * _g0 * _a0 * (1.0 / LHV) * CF
    return max(F, 0.0)


# ---------------------------------------------------------------------------
# ERA5 weather
# ---------------------------------------------------------------------------

def _extract_date(stem: str) -> str:
    for p in stem.split('_'):
        if len(p) == 8 and p.isdigit():
            return p
    return ''


def _get_era5(date_str: str):
    if not date_str or len(date_str) != 8:
        return None
    if date_str in _weather_cache:
        return _weather_cache[date_str]

    from datetime import datetime, timedelta
    req_date = datetime.strptime(date_str, '%Y%m%d')
    # ERA5 has ~5 day lag; clamp to latest available
    latest_avail = datetime.utcnow() - timedelta(days=6)
    if req_date > latest_avail:
        clamped = latest_avail.strftime('%Y%m%d')
        stack.stack(f'ECHO FUELCALC: ERA5 date {date_str} not yet available, using {clamped} instead.')
        date_str = clamped

    year, month, day = date_str[:4], date_str[4:6], date_str[6:8]
    nc_path = _WEATHER_DIR / f'era5_ESSA_{date_str}.nc'

    if not nc_path.is_file():
        try:
            import cdsapi
            c = cdsapi.Client(quiet=True)
            _WEATHER_DIR.mkdir(parents=True, exist_ok=True)
            c.retrieve(
                'reanalysis-era5-pressure-levels',
                {
                    'product_type': 'reanalysis',
                    'variable': ['temperature', 'u_component_of_wind',
                                 'v_component_of_wind'],
                    'pressure_level': [
                        '200', '250', '300', '350', '400', '450',
                        '500', '550', '600', '650', '700', '750',
                        '800', '850', '900', '925', '950', '975', '1000',
                    ],
                    'year': year, 'month': month, 'day': day,
                    'time': ['00:00', '06:00', '12:00', '18:00'],
                    'area': [60.5, 16.0, 58.4, 20.5],
                    'format': 'netcdf',
                },
                str(nc_path),
            )
        except Exception as e:
            stack.stack(f"ECHO FUELCALC: ERA5 download failed ({e}), using ISA-only.")
            _weather_cache[date_str] = None
            return None

    try:
        weather = _read_era5_nc(nc_path)
        _weather_cache[date_str] = weather
        return weather
    except Exception as e:
        stack.stack(f"ECHO FUELCALC: ERA5 read failed ({e}), using ISA-only.")
        _weather_cache[date_str] = None
        return None


def _read_era5_nc(nc_path: Path) -> dict:
    try:
        import netCDF4 as nc4
        ds    = nc4.Dataset(str(nc_path))
        lats  = np.array(ds.variables['latitude'][:])
        lons  = np.array(ds.variables['longitude'][:])
        press = np.array(ds.variables.get('pressure_level',
                         ds.variables.get('level'))[:])
        T     = np.array(ds.variables['t'][:]).mean(axis=0)
        u     = np.array(ds.variables['u'][:]).mean(axis=0)
        v     = np.array(ds.variables['v'][:]).mean(axis=0)
        ds.close()
        return {'lats': lats, 'lons': lons, 'press_hpa': press,
                'T': T, 'u': u, 'v': v}
    except ImportError:
        pass

    try:
        import xarray as xr
        ds    = xr.open_dataset(str(nc_path))
        lats  = ds['latitude'].values
        lons  = ds['longitude'].values
        pkey  = 'pressure_level' if 'pressure_level' in ds else 'level'
        press = ds[pkey].values
        tkey  = 'valid_time' if 'valid_time' in ds.dims else 'time'
        T     = ds['t'].mean(dim=tkey).values
        u     = ds['u'].mean(dim=tkey).values
        v     = ds['v'].mean(dim=tkey).values
        ds.close()
        return {'lats': lats, 'lons': lons, 'press_hpa': press,
                'T': T, 'u': u, 'v': v}
    except ImportError:
        raise RuntimeError('Neither netCDF4 nor xarray is installed.')


def _era5_at(weather, alt_m: float, lat: float, lon: float):
    if weather is None:
        return None, 0.0, 0.0
    _, p_Pa, _, _ = _isa(alt_m)
    p_hPa = p_Pa / 100.0
    pi = int(np.argmin(np.abs(weather['press_hpa'] - p_hPa)))
    li = int(np.argmin(np.abs(weather['lats'] - lat)))
    lo = int(np.argmin(np.abs(weather['lons'] - lon)))
    return (float(weather['T'][pi, li, lo]),
            float(weather['u'][pi, li, lo]),
            float(weather['v'][pi, li, lo]))


# ---------------------------------------------------------------------------
# Per-aircraft fuel integration
# ---------------------------------------------------------------------------

def _calc_fuel_aircraft(rows: list, bada: dict, weather) -> dict:
    S         = bada['S']
    CD_scalar = bada['CD_scalar']
    mass      = bada['mass']
    LHV       = bada['LHV']
    d         = bada['d']
    bf        = bada['bf']
    f         = bada['f']
    fi        = bada['fi']
    ti        = bada['ti']
    b         = bada.get('b', [])
    eng_type  = bada['engine_type']
    Mmin      = bada['Mmin']
    Mmax      = bada['Mmax']

    fuel_total   = 0.0
    dist_total_m = 0.0
    alt_sum      = 0.0
    valid_segs   = 0

    # Pre-compute TAS per row for dv/dt
    tas_arr = []
    for r in rows:
        alt = r['baro_alt_m']
        T_isa, _, _, _ = _isa(max(alt, 0.0))
        a = math.sqrt(_k * _R * T_isa)
        tas_arr.append(max(r['velocity_ms'], 30.0))  # gs ≈ TAS (no wind); refined per-segment below

    for i in range(len(rows) - 1):
        r0 = rows[i]
        r1 = rows[i + 1]

        dt = r1['time'] - r0['time']
        if dt <= 0 or dt > 120:
            continue
        if r0['on_ground'] or r1['on_ground']:
            continue

        alt0 = r0['baro_alt_m']
        alt1 = r1['baro_alt_m']
        alt  = (alt0 + alt1) / 2.0
        if alt < _FAP_ALT_M:          # stop at FAP — same as MATLAB
            continue
        if alt < 100.0:
            continue

        lat = (r0['lat'] + r1['lat']) / 2.0
        lon = (r0['lon'] + r1['lon']) / 2.0
        gs0 = r0['velocity_ms']
        gs1 = r1['velocity_ms']
        gs  = (gs0 + gs1) / 2.0
        trk = r0['true_track']

        T_isa, p_Pa, delta, theta = _isa(alt)

        # ERA5 temperature and wind correction
        T_era5, u_era5, v_era5 = _era5_at(weather, alt, lat, lon)
        if T_era5 is not None and T_era5 > 100.0:
            T_act = T_era5
            delta = (p_Pa / _p0) * (_T0 / T_act)
            theta = T_act / _T0
            a     = math.sqrt(_k * _R * T_act)
            trk_r = math.radians(trk)
            wind_along = u_era5 * math.sin(trk_r) + v_era5 * math.cos(trk_r)
        else:
            T_act      = T_isa
            a          = math.sqrt(_k * _R * T_isa)
            wind_along = 0.0

        TAS0 = max(gs0 - wind_along, _MIN_TAS_MS)
        TAS1 = max(gs1 - wind_along, _MIN_TAS_MS)
        TAS  = (TAS0 + TAS1) / 2.0
        if TAS < _MIN_TAS_MS:         # below min approach speed — skip
            continue
        M    = max(Mmin, min(Mmax, TAS / a))

        # dh/dt and dv/dt (MATLAB energy equation)
        dh_dt = (alt1 - alt0) / dt          # m/s  (negative = descent)
        dv_dt = (TAS1 - TAS0) / dt          # m/s² (negative = decelerating)

        CL_max = _cl_max(M, bf)
        CL     = _cl(mass, delta, S, M, CL_max)
        CD     = _cd(M, CL, d, CD_scalar)
        D      = _drag(delta, S, M, CD)

        # Full energy equation: T = (m·g/TAS)·dh_dt + m·dv_dt + D
        Thrust = (mass * _g0 / TAS) * dh_dt + mass * dv_dt + D

        # Clamp to [T_idle, T_MCMB]
        CT_idle  = _ct_idle_jet(delta, M, ti)
        T_idle   = _thr_idle(delta, mass, CT_idle)

        if b:
            CT_mcmb = _ct_mcmb(M, b)
            T_mcmb  = delta * mass * _g0 * CT_mcmb
        else:
            T_mcmb  = D * 2.5  # fallback: cap at 2.5× drag

        Thrust = max(T_idle, min(T_mcmb, Thrust))

        CT     = _ct(Thrust, delta, mass)
        CF     = _cf_jet(CT, M, f) if eng_type == 'JET' else 0.0

        CF_idle = _cf_idle_jet(fi, delta, M, theta) if eng_type == 'JET' else 0.0
        CF = max(CF, CF_idle)

        FF = _fuel_flow(delta, theta, mass, LHV, CF)   # kg/s

        fuel_total   += FF * dt
        dist_total_m += gs * dt
        alt_sum      += alt
        valid_segs   += 1

    duration_s = rows[-1]['time'] - rows[0]['time']
    dist_nm    = dist_total_m / 1852.0
    mean_fl    = (alt_sum / valid_segs / 30.48) if valid_segs > 0 else 0.0

    return {
        'fuel_kg':     round(fuel_total, 2),
        'duration_s':  round(duration_s, 0),
        'distance_nm': round(dist_nm, 1),
        'mean_fl':     round(mean_fl, 0),
        'n_segments':  valid_segs,
    }


# ---------------------------------------------------------------------------
# Save output CSV
# ---------------------------------------------------------------------------

def _save_fuel_csv(out_path: Path, results: list):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ['callsign', 'icao24', 'actype', 'bada_model',
              'fuel_kg', 'duration_s', 'distance_nm', 'mean_fl', 'n_segments']
    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(results)
