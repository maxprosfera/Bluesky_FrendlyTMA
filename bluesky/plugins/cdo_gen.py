"""BlueSky plugin: CDO (Continuous Descent Operations) profile generator.

Stack command:
    CDOGEN [csv_path]   — generate CDO trajectories for all arrivals in
                          the most recent (or given) *_tracks.csv

Bound to C4 button in the Cust layout.

Algorithm: CDO_profile_predictor() from fuel_burn_v3.m translated to Python.
Physics:   BADA 4.2 aerodynamics via fuel_calc.py (imported directly).
Output:    scenario/OpenSky/<stem>_cdo.csv        — per-second CDO tracks
           scenario/OpenSky/<stem>_cdo_summary.csv — per-aircraft summary
"""

import csv
import math
import threading
from pathlib import Path

import numpy as np

from bluesky import stack
from bluesky.plugins.fuel_calc import (
    _auto_detect_csv,
    _read_tracks_csv,
    _load_bada,
    _isa,
    _cl_max,
    _cl,
    _cd,
    _drag,
    _ct_idle_jet,
    _thr_idle,
    _cf_idle_jet,
    _cf_jet,
    _fuel_flow,
    _resolve_actype,
    _resolve_bada_model,
    _get_era5,
    _era5_at,
    _load_actype_cache,
    _load_release_csv,
    _is_ga,
    _g0, _p0, _k, _T0, _a0, _R,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT    = Path(__file__).parents[2]
_SCENARIO_DIR = _REPO_ROOT / 'scenario' / 'OpenSky'

# ---------------------------------------------------------------------------
# Physical / CDO constants
# ---------------------------------------------------------------------------

_KT_TO_MS   = 0.514444
_MS_TO_KT   = 1.0 / _KT_TO_MS
_FT_TO_M    = 0.3048
_M_TO_NM    = 1.0 / 1852.0
_KT_PER_SEC = 1.0        # speed reduction rate [kt/s] (kt_per_sec_reduce)
_C_V_MIN    = 1.3        # stall margin factor
_VD_DES     = [5.0, 10.0, 10.0, 10.0]   # [kt] speed deltas for bands 1-4

# CDO start altitude — from MATLAB alt_start = 2000 ft (the FAP/TMA entry limit)
# The CDO profile is computed from the first track point DOWN to landing.
# The horizontal (lat/lon) path is kept IDENTICAL to the observed track.
# Only the vertical profile (alt, rocd) and speed are replaced by the
# ideal idle-thrust CDO schedule.
_CDO_FAP_ALT_M   = 2000.0 * _FT_TO_M    # 609.6 m  — FAP (Final Approach Point)
_CDO_END_ALT_M   = 0.0                   # ground level

# ESSA airport coordinates
_ESSA_LAT = 59.6519
_ESSA_LON = 17.9186

# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def init_plugin():
    _load_actype_cache()
    _load_release_csv()
    return {'plugin_name': 'CDO_GEN', 'plugin_type': 'sim'}


# ---------------------------------------------------------------------------
# Stack command
# ---------------------------------------------------------------------------

@stack.command
def cdogen(csv_path: 'txt' = ''):
    """Generate CDO profiles for all arrivals in *_tracks.csv.
    Syntax: CDOGEN [csv_path]"""
    if not csv_path:
        scenname = stack.get_scenname()
        if scenname:
            stem = Path(scenname).stem.replace('_cdo', '').replace('_tracks', '')
            candidate = _SCENARIO_DIR / f'{stem}_tracks.csv'
            if candidate.is_file():
                csv_path = str(candidate)
        if not csv_path:
            csv_path = _auto_detect_csv()
    if not csv_path:
        stack.stack('ECHO CDOGEN: No *_tracks.csv found in scenario/OpenSky/')
        return
    stack.stack(f'ECHO CDOGEN: Starting for {Path(csv_path).name} ...')
    t = threading.Thread(target=_run_cdo, args=(csv_path,), daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Main CDO runner (background thread)
# ---------------------------------------------------------------------------

def _run_cdo(csv_path: str):
    csv_path = Path(csv_path)
    stem     = csv_path.stem.replace('_tracks', '')

    try:
        rows = _read_tracks_csv(csv_path)
    except Exception as e:
        stack.stack(f'ECHO CDOGEN: CSV read error: {e}')
        return
    if not rows:
        stack.stack('ECHO CDOGEN: CSV is empty.')
        return

    # Group by callsign
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for row in rows:
        groups[row['callsign']].append(row)

    # ERA5 weather
    date_str = _extract_date(stem)
    weather  = _get_era5(date_str) if date_str else None

    # Load original fuel results if available
    orig_fuel = _load_orig_fuel(stem)

    cdo_rows_all = []
    summary      = []
    errors       = []

    for cs, ac_rows in groups.items():
        ac_rows.sort(key=lambda r: r['time'])
        if _classify(ac_rows) != 'arr':
            continue

        icao24    = ac_rows[0].get('icao24', '')
        actype    = _resolve_actype(icao24, cs)
        if _is_ga(actype):
            continue
        bada_name = _resolve_bada_model(actype)

        try:
            bada = _load_bada(bada_name)
        except Exception as e:
            errors.append(f'{cs}: BADA load failed ({bada_name}): {e}')
            try:
                bada      = _load_bada('B738W26')
                bada_name = 'B738W26'
            except Exception:
                errors.append(f'{cs}: fallback BADA also failed, skipping.')
                continue

        try:
            cdo_pts = _cdo_for_aircraft(ac_rows, bada, weather)
        except Exception as e:
            errors.append(f'{cs}: CDO calc error: {e}')
            continue

        if not cdo_pts:
            errors.append(f'{cs}: CDO produced no points.')
            continue

        cdo_fuel = cdo_pts[-1]['cum_fuel_kg']
        orig_f   = orig_fuel.get(cs, None)
        if orig_f is None:
            orig_f = _orig_fuel_from_rows(ac_rows, bada, weather)

        saving    = orig_f - cdo_fuel
        saving_pc = 100.0 * saving / orig_f if orig_f > 0 else 0.0
        dist_nm   = cdo_pts[-1]['cum_dist_nm']
        dur_s     = len(cdo_pts)

        for pt in cdo_pts:
            pt['actype']     = actype
            pt['bada_model'] = bada_name
        cdo_rows_all.extend(cdo_pts)

        summary.append({
            'callsign':       cs,
            'actype':         actype,
            'bada_model':     bada_name,
            'cdo_fuel_kg':    round(cdo_fuel, 2),
            'orig_fuel_kg':   round(orig_f,   2),
            'fuel_saving_kg': round(saving,   2),
            'fuel_saving_pct':round(saving_pc, 1),
            'duration_s':     dur_s,
            'distance_nm':    round(dist_nm,  1),
        })

    if not summary:
        stack.stack(f'ECHO CDOGEN: {stem} — no arriving aircraft found, nothing to save.')
        for err in errors:
            stack.stack(f"ECHO WARN: {err}")
        return

    out_path     = _SCENARIO_DIR / f'{stem}_cdo.csv'
    summary_path = _SCENARIO_DIR / f'{stem}_cdo_summary.csv'
    scn_path     = _SCENARIO_DIR / f'{stem}_cdo.scn'
    _save_cdo_csv(out_path, cdo_rows_all)
    _save_summary_csv(summary_path, summary)
    _save_scn(scn_path, stem, out_path)

    total_cdo  = sum(r['cdo_fuel_kg']  for r in summary)
    total_orig = sum(r['orig_fuel_kg'] for r in summary)
    total_save = total_orig - total_cdo
    total_pc   = 100.0 * total_save / total_orig if total_orig > 0 else 0.0

    lines = [f'--- CDO GEN: {stem} ---']
    for r in sorted(summary, key=lambda x: x['cdo_fuel_kg'], reverse=True):
        lines.append(
            f'{r["callsign"]:<9} {r["actype"] or "?":<5} {r["bada_model"]:<14}'
            f'  CDO:{r["cdo_fuel_kg"]:6.1f} kg'
            f'  ORIG:{r["orig_fuel_kg"]:6.1f} kg'
            f'  SAVE:{r["fuel_saving_kg"]:5.1f} kg ({r["fuel_saving_pct"]:4.1f}%)'
        )
    lines.append(
        f'TOTAL: {len(summary)} arrivals'
        f' | CDO:{total_cdo:.1f} kg'
        f'  ORIG:{total_orig:.1f} kg'
        f'  SAVE:{total_save:.1f} kg ({total_pc:.1f}%)'
    )
    lines.append(f'Saved: {out_path.name}  |  SCN: {scn_path.name}')
    for err in errors:
        lines.append(f'WARN: {err}')

    for line in lines:
        stack.stack(f"ECHO {line.replace(chr(39), '')}")


# ---------------------------------------------------------------------------
# Arrival classification (mirrors opensky_traces.py logic)
# ---------------------------------------------------------------------------

def _haversine_nm(lat1, lon1, lat2, lon2) -> float:
    R_nm = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R_nm * 2 * math.asin(math.sqrt(a))


_ARR_DIST_NM = 25.0
_DEP_DIST_NM = 25.0
_MIN_ALT_M   = 300.0


def _classify(rows: list) -> str:
    """Return 'arr', 'dep', or 'enroute'.

    Uses the same strategy as opensky_traces._classify():
      - ARR : closest approach to ESSA is in last 60% of airborne track,
              within _ARR_DIST_NM, AND mean alt of last 20% < 4000 m
      - DEP : closest approach in first 40%, within _DEP_DIST_NM,
              track ends farther than it starts, AND mean alt of first 20% < 4000 m
      - ENROUTE: everything else
    """
    wps = [(r['lat'], r['lon'], r['baro_alt_m'])
           for r in rows
           if not r['on_ground'] and r['baro_alt_m'] is not None and r['baro_alt_m'] >= _MIN_ALT_M]
    if len(wps) < 2:
        return 'enroute'
    dists = [_haversine_nm(w[0], w[1], _ESSA_LAT, _ESSA_LON) for w in wps]
    min_d = min(dists)
    if min_d > max(_ARR_DIST_NM, _DEP_DIST_NM):
        return 'enroute'
    min_idx = dists.index(min_d)
    frac    = min_idx / max(len(wps) - 1, 1)
    tail_n  = max(1, len(wps) // 5)
    head_n  = max(1, len(wps) // 5)
    mean_alt_tail = sum(w[2] for w in wps[-tail_n:]) / tail_n
    mean_alt_head = sum(w[2] for w in wps[:head_n]) / head_n
    if min_d <= _ARR_DIST_NM and frac >= 0.6 and mean_alt_tail < 4000.0:
        return 'arr'
    if min_d <= _DEP_DIST_NM and frac <= 0.4 and dists[-1] > dists[0] and mean_alt_head < 4000.0:
        return 'dep'
    return 'enroute'


# ---------------------------------------------------------------------------
# CAS / TAS / GS conversion helpers
# (mirrors calculate_*_CDO functions from fuel_burn_v3.m)
# ---------------------------------------------------------------------------

def _tas_from_cas(CAS_ms: float, p_Pa: float, rho: float) -> float:
    u = (_k - 1.0) / _k
    rho0 = _p0 / (_R * _T0)
    val = (1.0 + (_p0 / p_Pa) * ((1.0 + u/2.0 * (rho0 / _p0) * CAS_ms**2)**(1.0/u) - 1.0))**u - 1.0
    inner = (2.0 / u) * (p_Pa / rho) * val
    return math.sqrt(max(inner, 0.0))


def _cas_from_tas(TAS_ms: float, p_Pa: float, rho: float) -> float:
    u = (_k - 1.0) / _k
    rho0 = _p0 / (_R * _T0)
    val = (1.0 + (p_Pa / _p0) * ((1.0 + u/2.0 * rho / p_Pa * TAS_ms**2)**(1.0/u) - 1.0))**u - 1.0
    inner = (2.0 / u) * (_p0 / rho0) * val
    return math.sqrt(max(inner, 0.0))


def _tas_from_mach(M: float, T_K: float) -> float:
    return M * math.sqrt(_k * _R * T_K)


def _mach_from_tas(TAS_ms: float, T_K: float) -> float:
    a = math.sqrt(_k * _R * T_K)
    return TAS_ms / a if a > 0 else 0.0


def _gs_from_tas(TAS_ms: float, wind_comp_ms: float) -> float:
    return TAS_ms - wind_comp_ms


def _rho_0() -> float:
    return _p0 / (_R * _T0)


# ---------------------------------------------------------------------------
# CAS stall approximation for landing config
# ---------------------------------------------------------------------------

def _cas_stall_approx(bada: dict, mass_kg: float, delta: float) -> float:
    """Approximate stall CAS [m/s] using CL_max at approach Mach ~0.20."""
    M_app = 0.20
    bf    = bada.get('bf', [])
    CL_max = _cl_max(M_app, bf) if bf else 1.5
    CL_max = max(CL_max, 0.8)
    S = bada['S']
    dyn_q = delta * _p0 * _k * S * M_app**2
    if dyn_q < 1.0:
        return 50.0
    CL_stall = (2.0 * mass_kg * _g0) / dyn_q
    T_stall, p_stall, _, _ = _isa(0.0)
    rho0 = _p0 / (_R * _T0)
    if CL_stall <= 0 or S <= 0:
        return 50.0
    TAS_stall = math.sqrt(2.0 * mass_kg * _g0 / (rho0 * S * CL_max))
    return _cas_from_tas(TAS_stall, p_stall, rho0)


# ---------------------------------------------------------------------------
# ROCD (Rate of Climb/Descent) — BADA 4 energy share method
# mirrors fuel_burn_v3.m lines 4104-4116
# ---------------------------------------------------------------------------

def _rocd(Thr_idle_N: float, Drag_N: float, TAS_ms: float,
          mass_kg: float, M: float, is_cruise: bool = False) -> float:
    if is_cruise:
        return 0.0
    beta = -0.0065
    f_m_inv = 1.0 + (_k * _R * beta / (2.0 * _g0)) * M**2
    if f_m_inv <= 0:
        f_m_inv = 1.0
    f_m = 1.0 / f_m_inv
    rocd = ((Thr_idle_N - Drag_N) * TAS_ms / (mass_kg * _g0)) * f_m
    return rocd


# ---------------------------------------------------------------------------
# CDO speed schedule (7 bands, mirrors CDO_profile_predictor)
# ---------------------------------------------------------------------------

def _cdo_speed_step(alt_m: float, M_prev: float, CAS_prev_ms: float,
                    M_descent: float, CAS_start_ms: float,
                    reduce_ms: list, exit_flags: list,
                    kt_per_sec_ms: float) -> tuple:
    """
    Returns (new_CAS_ms, new_exit_flags).
    reduce_ms: [r1, r2, r3, r4, r5, r6, r7]  target CAS [m/s] per band
    exit_flags: [e2, e3, e4, e5, e6, e7]  True once the band CAS has been reached
    """
    alt_ft = alt_m / _FT_TO_M
    ef = list(exit_flags)

    def step_reduce(band_idx, target_ms):
        i = band_idx - 2
        if not ef[i]:
            if CAS_prev_ms * _MS_TO_KT < target_ms * _MS_TO_KT:
                new_cas = min(CAS_start_ms + kt_per_sec_ms,
                              CAS_prev_ms + kt_per_sec_ms)
            else:
                new_cas = min(CAS_start_ms, target_ms)
                ef[i] = True
        else:
            new_cas = min(CAS_start_ms, target_ms)
        return new_cas

    if alt_ft < 1000.0:
        new_cas = min(CAS_start_ms, reduce_ms[0])
    elif alt_ft < 1500.0:
        new_cas = step_reduce(2, reduce_ms[1])
    elif alt_ft < 2000.0:
        new_cas = step_reduce(3, reduce_ms[2])
    elif alt_ft < 3000.0:
        new_cas = step_reduce(4, reduce_ms[3])
    elif alt_ft < 6000.0:
        new_cas = min(CAS_start_ms, reduce_ms[4])
    elif alt_ft < 10000.0:
        new_cas = min(CAS_start_ms, reduce_ms[5])
    elif M_prev < M_descent:
        new_cas = step_reduce(7, reduce_ms[6])
    else:
        new_cas = None  # hold Mach

    return new_cas, ef


# ---------------------------------------------------------------------------
# Haversine track bearing [deg]
# ---------------------------------------------------------------------------

def _bearing(lat1, lon1, lat2, lon2) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


# ---------------------------------------------------------------------------
# Cumulative distance along track
# ---------------------------------------------------------------------------

def _build_cum_dist_nm(rows: list):
    lats = np.array([r['lat'] for r in rows])
    lons = np.array([r['lon'] for r in rows])
    cum  = np.zeros(len(rows))
    for i in range(1, len(rows)):
        cum[i] = cum[i-1] + _haversine_nm(lats[i-1], lons[i-1], lats[i], lons[i])
    return cum, lats, lons


def _interp_latlon(cum_nm, lats, lons, query_nm):
    query_nm = max(0.0, min(query_nm, cum_nm[-1]))
    idx = np.searchsorted(cum_nm, query_nm)
    if idx == 0:
        return float(lats[0]), float(lons[0])
    if idx >= len(cum_nm):
        return float(lats[-1]), float(lons[-1])
    t = (query_nm - cum_nm[idx-1]) / max(cum_nm[idx] - cum_nm[idx-1], 1e-9)
    lat = lats[idx-1] + t * (lats[idx] - lats[idx-1])
    lon = lons[idx-1] + t * (lons[idx] - lons[idx-1])
    return float(lat), float(lon)


# ---------------------------------------------------------------------------
# Main CDO propagation loop for one aircraft
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Main CDO propagation loop for one aircraft
#
# MATLAB approach (fuel_burn_v3.m / CDO_profile_predictor.m):
#   - Horizontal path = identical to observed OpenSky track (same lat/lon)
#   - Vertical profile = recomputed: idle thrust CDO from entry alt → FAP (2000ft)
#     then level at FAP.  The fuel for the level segment below FAP is from ORIG.
#   - "CDO fuel" = idle-thrust fuel from TMA entry altitude down to FAP
#   - "ORIG fuel" = BADA-modelled fuel along the actual observed track from TMA
#     entry to FAP (non-idle, follows observed ROCD)
# ---------------------------------------------------------------------------

def _cdo_for_aircraft(rows: list, bada: dict, weather) -> list:
    """Compute CDO vertical profile along the observed horizontal track.

    Returns a list of per-second state dicts with the CDO altitude profile.
    The lat/lon at each second is interpolated from the original track by
    cumulative distance — same horizontal path, only vertical replaced.
    """
    S         = bada['S']
    CD_scalar = bada['CD_scalar']
    mass      = bada['mass']
    LHV       = bada['LHV']
    d         = bada['d']
    bf        = bada['bf']
    fi        = bada['fi']
    ti        = bada['ti']
    Mmin      = bada['Mmin']
    Mmax      = bada['Mmax']

    # Use only the portion of the track from the first point down to FAP (2000 ft)
    # — same convention as MATLAB: CDO starts at TMA entry, ends at FAP.
    fap_rows = []
    for r in rows:
        fap_rows.append(r)
        if r['baro_alt_m'] <= _CDO_FAP_ALT_M:
            break
    if len(fap_rows) < 2:
        return []

    cum_d, lats, lons = _build_cum_dist_nm(fap_rows)
    TMA_dist_nm = float(cum_d[-1])
    if TMA_dist_nm < 0.1:
        return []

    r0   = fap_rows[0]
    alt0 = r0['baro_alt_m']
    fap_alt = _CDO_FAP_ALT_M

    T_isa0, p0_Pa, delta0, theta0 = _isa(alt0)
    a0_ms  = math.sqrt(_k * _R * T_isa0)
    rho0   = p0_Pa / (_R * T_isa0)

    gs0       = max(float(r0['velocity_ms']), 30.0)
    TAS0      = _tas_from_cas(gs0, p0_Pa, rho0)
    M_descent = min(Mmax, max(Mmin, TAS0 / a0_ms))
    CAS_start_ms = _cas_from_tas(TAS0, p0_Pa, rho0)

    cas_stall_ms = _cas_stall_approx(bada, mass, delta0)
    reduce_ms = [
        _C_V_MIN * cas_stall_ms + _VD_DES[0] * _KT_TO_MS,
        _C_V_MIN * cas_stall_ms + _VD_DES[1] * _KT_TO_MS,
        _C_V_MIN * cas_stall_ms + _VD_DES[2] * _KT_TO_MS,
        _C_V_MIN * cas_stall_ms + _VD_DES[3] * _KT_TO_MS,
        min(CAS_start_ms, 220.0 * _KT_TO_MS),
        min(CAS_start_ms, 250.0 * _KT_TO_MS),
        CAS_start_ms,
    ]
    kt_per_sec_ms = _KT_PER_SEC * _KT_TO_MS

    t0_abs     = int(float(r0['time']))
    alt        = alt0
    CAS_ms     = CAS_start_ms
    M          = M_descent
    cum_dist   = 0.0
    lat, lon   = float(lats[0]), float(lons[0])
    exit_flags = [False] * 6
    cruise     = False

    output   = []
    MAX_ITER = 7200

    for step in range(MAX_ITER):
        T_isa, p_Pa, delta, theta = _isa(alt)
        rho = p_Pa / (_R * T_isa)
        a   = math.sqrt(_k * _R * T_isa)

        T_era5, u_era5, v_era5 = _era5_at(weather, alt, lat, lon)
        if T_era5 and T_era5 > 100.0:
            dT    = T_era5 - T_isa
            T_act = T_isa + dT
            delta = (p_Pa / _p0) * (_T0 / T_act)
            theta = T_act / _T0
            a     = math.sqrt(_k * _R * T_act)
            rho   = p_Pa / (_R * T_act)
        T_sound = T_era5 if (T_era5 and T_era5 > 100) else T_isa

        trk_use = output[-1]['true_track'] if output else float(r0['true_track'])
        trk_r   = math.radians(trk_use)
        wind_along = (u_era5 * math.sin(trk_r) + v_era5 * math.cos(trk_r)) if u_era5 else 0.0

        new_cas, exit_flags = _cdo_speed_step(
            alt, M, CAS_ms, M_descent, CAS_start_ms,
            reduce_ms, exit_flags, kt_per_sec_ms,
        )
        if new_cas is None:
            M      = M_descent
            TAS    = _tas_from_mach(M, math.sqrt(_k * _R * T_sound))
            CAS_ms = _cas_from_tas(TAS, p_Pa, rho)
        else:
            CAS_ms = new_cas
            TAS    = _tas_from_cas(CAS_ms, p_Pa, rho)
            M      = _mach_from_tas(TAS, math.sqrt(_k * _R * T_sound))

        M   = max(Mmin, min(Mmax, M))
        TAS = max(TAS, 30.0)
        GS  = max(_gs_from_tas(TAS, wind_along), 10.0)

        CL_v = _cl_max(M, bf)
        CL   = _cl(mass, delta, S, M, CL_v)
        CD   = _cd(M, CL, d, CD_scalar)
        D    = _drag(delta, S, M, CD)

        CT_idle    = _ct_idle_jet(delta, M, ti)
        Thr_idle_N = _thr_idle(delta, mass, CT_idle)
        CF_idle    = _cf_idle_jet(fi, delta, M, theta)
        FF         = _fuel_flow(delta, theta, mass, LHV, CF_idle)

        rocd = _rocd(Thr_idle_N, D, TAS, mass, M)

        new_alt = alt + rocd * 1.0
        if new_alt <= fap_alt:
            new_alt = fap_alt

        cum_dist += GS * _M_TO_NM
        cum_fuel  = (output[-1]['cum_fuel_kg'] if output else 0.0) + FF

        prev_lat, prev_lon = lat, lon
        lat, lon = _interp_latlon(cum_d, lats, lons, cum_dist)
        trk = _bearing(prev_lat, prev_lon, lat, lon) if step > 0 else float(r0['true_track'])

        output.append({
            'icao24':           r0.get('icao24', ''),
            'callsign':         r0['callsign'],
            'est_departure':    '',
            'est_arrival':      '',
            'time':             t0_abs + step,
            'lat':              round(lat, 6),
            'lon':              round(lon, 6),
            'baro_alt_m':       round(alt, 1),
            'true_track':       round(trk, 1),
            'on_ground':        False,
            'velocity_ms':      round(GS, 2),
            'vertical_rate_ms': round(rocd, 3),
            'cas_ms':           round(CAS_ms, 2),
            'mach':             round(M, 4),
            'fuel_flow_kg_s':   round(FF, 5),
            'cum_fuel_kg':      round(cum_fuel, 3),
            'cum_dist_nm':      round(cum_dist, 3),
        })

        alt = new_alt

        if cum_dist >= TMA_dist_nm or new_alt <= fap_alt:
            break

    return output


# ---------------------------------------------------------------------------
# Load original fuel results (from fuel_calc.py output CSV)
# ---------------------------------------------------------------------------

def _load_orig_fuel(stem: str) -> dict:
    fuel_path = _SCENARIO_DIR / f'fuel_{stem}.csv'
    result = {}
    if not fuel_path.is_file():
        return result
    try:
        with open(fuel_path, newline='') as f:
            for row in csv.DictReader(f):
                cs = row.get('callsign', '').strip()
                try:
                    result[cs] = float(row.get('fuel_kg', 0))
                except ValueError:
                    pass
    except Exception:
        pass
    return result


def _orig_fuel_from_rows(rows: list, bada: dict, weather) -> float:
    """Compute ORIG fuel from TMA entry → FAP using actual observed thrust.

    Clips track to FAP (2000 ft), then uses fuel_calc energy equation
    (actual thrust = energy balance clamped to [idle, MCMB]).
    This matches MATLAB's F_burn_TMA = trapz over TMA_time_vector.
    """
    from bluesky.plugins.fuel_calc import _calc_fuel_aircraft
    fap_rows = []
    for r in rows:
        fap_rows.append(r)
        if r['baro_alt_m'] <= _CDO_FAP_ALT_M:
            break
    if len(fap_rows) < 2:
        fap_rows = rows[:2]
    res = _calc_fuel_aircraft(fap_rows, bada, weather)
    return res['fuel_kg']


# ---------------------------------------------------------------------------
# Date extractor (same as fuel_calc.py)
# ---------------------------------------------------------------------------

def _extract_date(stem: str) -> str:
    for p in stem.split('_'):
        if len(p) == 8 and p.isdigit():
            return p
    return ''


# ---------------------------------------------------------------------------
# Save output CSVs
# ---------------------------------------------------------------------------

_STOCKHOLM_TMA_POLY = (
    '60.299444 18.213056 60.266111 18.554722 59.882778 18.847000 '
    '60.035278 19.313611 59.673611 19.830833 59.599444 19.273611 '
    '59.255000 18.968333 59.047500 18.754722 58.832500 18.539444 '
    '58.752500 18.457222 58.583056 17.932778 58.616389 17.456944 '
    '58.966111 17.407778 58.978611 17.223333 59.012500 16.707778 '
    '59.049444 16.267778 59.323889 16.318333 59.749444 16.446667 '
    '60.232778 17.596667'
)


def _save_scn(scn_path: Path, stem: str, csv_path: Path):
    rel_csv = csv_path.relative_to(_REPO_ROOT).as_posix()
    scn_path.parent.mkdir(parents=True, exist_ok=True)
    with open(scn_path, 'w') as f:
        f.write(f'# CDO Scenario — {stem}\n')
        f.write('00:00:00.00>TIME 00:00:00\n')
        f.write('\n# --- Simulation Setup ---\n')
        f.write('00:00:00.00> PAN 59.574, 17.9876\n')
        f.write('00:00:00.00> ZOOM 2.0\n')
        f.write('00:00:00.00> DT 1.0\n')
        f.write('00:00:00.00> TAXI OFF\n')
        f.write('00:00:00.00> SWRAD WPT 0\n')
        f.write('00:00:00.00> SWRAD APT 0\n')
        f.write('00:00:00.00> SWRAD SAT 0\n')
        f.write('\n# --- Visual Elements ---\n')
        f.write(f'00:00:00.00> POLY StockholmTMA {_STOCKHOLM_TMA_POLY}\n')
        f.write('\n# --- Start CDO replay ---\n')
        f.write(f'00:00:00.00> STARTREPLAY {rel_csv}\n')
        f.write('00:00:00.00> OP\n')


def _save_cdo_csv(out_path: Path, rows: list):
    """Save CDO tracks in the same format as *_tracks.csv for STARTREPLAY."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        'icao24', 'callsign', 'est_departure', 'est_arrival',
        'time', 'lat', 'lon', 'baro_alt_m', 'true_track',
        'on_ground', 'velocity_ms', 'vertical_rate_ms',
    ]
    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def _save_summary_csv(out_path: Path, summary: list):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ['callsign', 'actype', 'bada_model',
              'cdo_fuel_kg', 'orig_fuel_kg',
              'fuel_saving_kg', 'fuel_saving_pct',
              'duration_s', 'distance_nm']
    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(summary)
