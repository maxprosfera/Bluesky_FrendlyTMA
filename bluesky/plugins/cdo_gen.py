"""BlueSky plugin: CDO (Continuous Descent Operations) profile generator.

Stack commands:
    CDOGEN [csv_path]                      — generate CDO for all arrivals in
                                             the most recent (or given) *_tracks.csv
    CDOGEN <callsign> <begin_ts> <end_ts>  — fetch one aircraft trajectory from
                                             OpenSky Trino, clip to TMA entry,
                                             run CDO on the full TMA track
    CDOGENSCN [stem]                       — merge all *_cdo.csv files in
                                             scenario/OpenSky/ into one combined
                                             CSV + SCN for replay of all CDO aircraft

Bound to C4 button in the Cust layout.

Algorithm: CDO_profile_predictor() from fuel_burn_v3.m translated to Python.
Physics:   BADA 4.2 aerodynamics via fuel_calc.py (imported directly).
Output:    scenario/OpenSky/<stem>_cdo.csv        — per-second CDO tracks
           scenario/OpenSky/<stem>_cdo_summary.csv — per-aircraft summary
"""

import csv
import math
import multiprocessing
import os
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
_CDO_FAP_ALT_M   = 2500.0 * _FT_TO_M    # 762.0 m  — FAF/FAP for ESSA ILS RWY 01L (SSA DME 7.6)
_CDO_END_ALT_M   = 0.0                   # ground level

# Worker count for CDO parallelism
_N_CDO_WORKERS = max(1, (os.cpu_count() or 4))

# Worker-global shared state — set once per pool via initializer
_W_ALL_PATHS = None
_W_WEATHER   = None

def _pool_init(all_paths, weather):
    global _W_ALL_PATHS, _W_WEATHER
    _W_ALL_PATHS = all_paths
    _W_WEATHER   = weather

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
    """Generate CDO profiles.
    Syntax:
      CDOGEN [csv_path]                     — all arrivals in *_tracks.csv
      CDOGEN <callsign> <begin_ts> <end_ts> — fetch from Trino and run CDO
    """
    parts = csv_path.strip().split()
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        callsign  = parts[0].upper()
        begin_ts  = int(parts[1])
        end_ts    = int(parts[2])
        stack.stack(f'ECHO CDOGEN: Fetching {callsign} from Trino ({begin_ts}–{end_ts}) ...')
        t = threading.Thread(target=_run_cdo_trino,
                             args=(callsign, begin_ts, end_ts), daemon=True)
        t.start()
        return

    # --- CSV mode (original behaviour) ---
    if not csv_path or len(parts) != 1:
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


@stack.command
def cdogenscn(stem: 'txt' = ''):
    """Merge all *_cdo.csv files into one combined CSV + SCN for replay.
    Syntax: CDOGENSCN [stem]
      stem — optional output name (default: cdo_all)"""
    t = threading.Thread(target=_run_merge_scn, args=(stem.strip(),), daemon=True)
    t.start()


@stack.command
def cdoprecompute(result_pkl: 'txt' = ''):
    """Pre-compute CDO profiles for ALL grid routes for each aircraft in a TMAOpt result.
    Derives per-aircraft per-edge travel times u[a,p,k] used by the Gurobi optimiser.
    Syntax: CDOPRECOMPUTE [result_pkl_path]
      result_pkl — path to TMAOpt result.pkl (default: most recent in scenario/TMAOpt/)"""
    t = threading.Thread(target=_run_cdoprecompute, args=(result_pkl.strip(),), daemon=True)
    t.start()


@stack.command
def cdogenopt(result_pkl: 'txt' = ''):
    """Generate CDO profiles on the Gurobi-optimal route for each aircraft.
    Reads a TMAOpt result.pkl, uses the assigned node-path as horizontal trajectory,
    runs CDO physics on it, and outputs fuel savings + combined SCN.
    Syntax: CDOGENOPT [result_pkl_path]
      result_pkl — path to TMAOpt result.pkl (default: most recent in scenario/TMAOpt/)"""
    t = threading.Thread(target=_run_cdogenopt, args=(result_pkl.strip(),), daemon=True)
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
# Merge all CDO CSVs into one combined replay SCN
# ---------------------------------------------------------------------------

def _run_merge_scn(stem: str):
    """Collect every *_cdo.csv in scenario/OpenSky/, merge rows, write combined
    CSV + summary CSV + SCN so all CDO aircraft play back together."""
    cdo_files = sorted(_SCENARIO_DIR.glob('*_cdo.csv'))
    # Exclude any file that is itself a combined output (contains "cdo_all")
    cdo_files = [f for f in cdo_files if 'cdo_all' not in f.name]

    if not cdo_files:
        stack.stack('ECHO CDOGENSCN: No *_cdo.csv files found in scenario/OpenSky/')
        return

    out_stem = stem if stem else 'cdo_all'
    out_path = _SCENARIO_DIR / f'{out_stem}.csv'
    scn_path = _SCENARIO_DIR / f'{out_stem}.scn'
    sum_path = _SCENARIO_DIR / f'{out_stem}_summary.csv'

    fields = [
        'icao24', 'callsign', 'est_departure', 'est_arrival',
        'time', 'lat', 'lon', 'baro_alt_m', 'true_track',
        'on_ground', 'velocity_ms', 'vertical_rate_ms', 'cas_ms',
    ]

    all_rows   = []
    seen_cs    = set()
    sum_rows   = []
    errors     = []

    for f in cdo_files:
        try:
            rows = _read_tracks_csv(f)
        except Exception as e:
            errors.append(f'{f.name}: read error: {e}')
            continue
        if not rows:
            continue
        cs = rows[0].get('callsign', f.stem)
        if cs in seen_cs:
            continue
        seen_cs.add(cs)
        all_rows.extend(rows)

        # Try to load matching summary file
        sum_file = f.with_name(f.stem + '_summary.csv') if '_cdo' in f.stem \
                   else f.parent / (f.stem.replace('_cdo', '') + '_cdo_summary.csv')
        # Also check sibling *_summary.csv patterns
        for candidate in [
            f.with_suffix('').with_suffix('') ,   # strip double suffix attempt
            _SCENARIO_DIR / f.name.replace('_cdo.csv', '_cdo_summary.csv'),
        ]:
            if candidate.is_file():
                try:
                    with open(candidate, newline='') as sf:
                        for row in csv.DictReader(sf):
                            if row.get('callsign', '') == cs:
                                sum_rows.append(row)
                except Exception:
                    pass
                break

    if not all_rows:
        stack.stack('ECHO CDOGENSCN: No rows found across CDO CSV files.')
        for err in errors:
            stack.stack(f'ECHO WARN: {err}')
        return

    # Sort combined rows by time so STARTREPLAY gets a chronological stream
    all_rows.sort(key=lambda r: float(r.get('time', 0)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(all_rows)

    # Write combined summary if we collected any rows
    if sum_rows:
        sum_fields = ['callsign', 'actype', 'bada_model',
                      'cdo_fuel_kg', 'orig_fuel_kg',
                      'fuel_saving_kg', 'fuel_saving_pct',
                      'duration_s', 'distance_nm']
        with open(sum_path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=sum_fields, extrasaction='ignore')
            w.writeheader()
            w.writerows(sum_rows)

    # Write SCN
    rel_csv = out_path.relative_to(_REPO_ROOT).as_posix()
    scn_path.parent.mkdir(parents=True, exist_ok=True)
    with open(scn_path, 'w') as f:
        f.write(f'# CDO Combined Scenario — {out_stem}\n')
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
        f.write('\n# --- Aircraft list ---\n')
        for cs in sorted(seen_cs):
            f.write(f'# {cs}\n')
        f.write('\n# --- Start CDO replay ---\n')
        f.write(f'00:00:00.00> STARTREPLAY {rel_csv}\n')
        f.write('00:00:00.00> OP\n')

    n = len(seen_cs)
    stack.stack(f'ECHO CDOGENSCN: merged {n} aircraft from {len(cdo_files)} CDO files.')
    stack.stack(f'ECHO   Combined CSV: {out_path.name}')
    stack.stack(f'ECHO   SCN        : {scn_path.name}')
    for err in errors:
        stack.stack(f'ECHO WARN: {err}')


# ---------------------------------------------------------------------------
# Helpers shared by CDOPRECOMPUTE and CDOGENOPT
# ---------------------------------------------------------------------------

def _find_latest_result_pkl(pkl_arg: str):
    """Return Path to result.pkl — from arg or most recent TMAOpt subdirectory."""
    if pkl_arg:
        p = Path(pkl_arg)
        if p.is_file():
            return p
    tmaopt_dir = _REPO_ROOT / 'scenario' / 'TMAOpt'
    dirs = sorted(tmaopt_dir.glob('tmaopt_*'), key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs:
        candidate = d / 'result.pkl'
        if candidate.is_file():
            return candidate
    return None


def _grid_rows_from_nodes(node_list, ref_time_unix, speed_ms=200.0,
                          entry_alt_m=None, node_unix_times=None):
    """Build a list of row-dicts along a grid node path.

    node_unix_times: optional list of per-node Unix timestamps (same length as node_list).
    When provided, uses Gurobi-schedule timestamps so the CDO replay respects the
    exact temporal separation the optimizer enforced. When None, timestamps are derived
    from speed-based propagation.
    """
    import sys as _sys
    _REPO_ROOT_STR = str(_REPO_ROOT)
    if _REPO_ROOT_STR not in _sys.path:
        _sys.path.insert(0, _REPO_ROOT_STR)
    from bluesky.plugins.tma_opt import _GRID_COORDS, _haversine_nm, _bearing

    _DEFAULT_ENTRY_ALT_M = 6096.0   # FL200 — sensible TMA entry altitude
    alt_entry = float(entry_alt_m) if entry_alt_m and entry_alt_m > 0 else _DEFAULT_ENTRY_ALT_M

    rows = []
    t    = float(ref_time_unix)
    n    = len(node_list)
    for i, node_id in enumerate(node_list):
        lat, lon = _GRID_COORDS[node_id]
        if i > 0:
            prev_lat, prev_lon = _GRID_COORDS[node_list[i - 1]]
            trk = _bearing((prev_lat, prev_lon), (lat, lon))
            if node_unix_times is not None and i < len(node_unix_times):
                t = float(node_unix_times[i])
            else:
                dist_nm = _haversine_nm((prev_lat, prev_lon), (lat, lon))
                t      += dist_nm * 1852.0 / max(speed_ms, 1.0)
        else:
            trk = 0.0
            if node_unix_times is not None and len(node_unix_times) > 0:
                t = float(node_unix_times[0])
        # Linearly interpolate altitude from entry (alt_entry) down to FAP (762.0 m / 2500 ft)
        # so _cdo_for_aircraft sees a realistic descending track, not a flat 3000 m sheet.
        frac     = i / max(n - 1, 1)
        node_alt = alt_entry + frac * (_CDO_FAP_ALT_M - alt_entry)
        rows.append({
            'icao24':           '',
            'callsign':         '',
            'est_departure':    '',
            'est_arrival':      '',
            'time':             t,
            'lat':              lat,
            'lon':              lon,
            'baro_alt_m':       node_alt,
            'true_track':       trk,
            'on_ground':        False,
            'velocity_ms':      speed_ms,
            'vertical_rate_ms': -5.0,
        })

    # Append the FAF node (N_exit = 72, repositioned to ESSA ILS RWY 01L FAF
    # at SSA DME 7.6 NM: 59.5114°N 17.9393°E, 2500 ft) so the CDO ends at the FAF.
    _N_EXIT = 72
    rwy_lat, rwy_lon = _GRID_COORDS[_N_EXIT]
    if rows and (rows[-1]['lat'] != rwy_lat or rows[-1]['lon'] != rwy_lon):
        last_lat, last_lon = rows[-1]['lat'], rows[-1]['lon']
        dist_nm = _haversine_nm((last_lat, last_lon), (rwy_lat, rwy_lon))
        dt      = dist_nm * 1852.0 / max(speed_ms, 1.0)
        t      += dt
        trk     = _bearing((last_lat, last_lon), (rwy_lat, rwy_lon))
        rows.append({
            'icao24':           '',
            'callsign':         '',
            'est_departure':    '',
            'est_arrival':      '',
            'time':             t,
            'lat':              rwy_lat,
            'lon':              rwy_lon,
            'baro_alt_m':       _CDO_FAP_ALT_M,
            'true_track':       trk,
            'on_ground':        False,
            'velocity_ms':      speed_ms * 0.5,
            'vertical_rate_ms': -5.0,
        })

    return rows


def _apply_cdo_params(cdo_params: dict):
    """Temporarily apply CDO params dict to module-level CDO constants.
    Returns a context dict with old values for restoration."""
    import bluesky.plugins.cdo_gen as _me
    old = {
        '_CDO_FAP_ALT_M': _me._CDO_FAP_ALT_M,
        '_C_V_MIN':       _me._C_V_MIN,
        '_KT_PER_SEC':    _me._KT_PER_SEC,
    }
    if 'fap_alt_ft' in cdo_params:
        _me._CDO_FAP_ALT_M = cdo_params['fap_alt_ft'] * _me._FT_TO_M
    if 'c_v_min' in cdo_params:
        _me._C_V_MIN = cdo_params['c_v_min']
    if 'kt_per_sec' in cdo_params:
        _me._KT_PER_SEC = cdo_params['kt_per_sec']
    return old


def _restore_cdo_params(old: dict):
    import bluesky.plugins.cdo_gen as _me
    for k, v in old.items():
        setattr(_me, k, v)


# ---------------------------------------------------------------------------
# Phase 1 — CDOPRECOMPUTE
# ---------------------------------------------------------------------------

def _run_cdoprecompute(pkl_arg: str):
    """For every aircraft in a TMAOpt result, run CDO on EVERY pre-computed
    grid path and extract per-edge travel times u[ac_id, path_len, edge_idx].
    Saves cdo_u_<stem>.pkl next to result.pkl.
    """
    import pickle as _pk

    pkl_path = _find_latest_result_pkl(pkl_arg)
    if pkl_path is None:
        stack.stack('ECHO CDOPRECOMPUTE: No result.pkl found.')
        return

    stack.stack(f'ECHO CDOPRECOMPUTE: Loading {pkl_path.name} ...')
    with open(pkl_path, 'rb') as f:
        result = _pk.load(f)

    all_paths  = result.get('all_paths', [])
    ac_by_ent  = result.get('aircraft_by_entry', {})
    ref_unix   = result.get('ref_unix', 0)
    cdo_params = result.get('cdo_params', {})

    if not all_paths:
        stack.stack('ECHO CDOPRECOMPUTE: result.pkl has no all_paths.')
        return

    # Flatten aircraft list with stable ac_id order
    all_ac = []
    for d in ('N', 'E', 'S', 'W'):
        for ac in ac_by_ent.get(d, []):
            all_ac.append(ac)

    if not all_ac:
        stack.stack('ECHO CDOPRECOMPUTE: No aircraft in result.')
        return

    n_ac    = len(all_ac)
    n_paths = len(all_paths)
    stack.stack(f'ECHO CDOPRECOMPUTE: {n_ac} aircraft × {n_paths} paths ...')

    old_params = _apply_cdo_params(cdo_params)
    date_str   = __import__('datetime').datetime.fromtimestamp(
        ref_unix, tz=__import__('datetime').timezone.utc).strftime('%Y%m%d')
    weather    = _get_era5(date_str)

    # u[ac_id][path_idx] = list of edge travel times in minutes
    u_table   = {}
    fuel_table = {}

    for ac_idx, ac in enumerate(all_ac):
        cs       = ac.get('callsign', f'AC{ac_idx}')
        icao24   = ac.get('icao24', '')
        speed_ms = ac.get('velocity_ms', 200.0)

        actype    = _resolve_actype(icao24, cs)
        bada_name = _resolve_bada_model(actype)
        try:
            bada = _load_bada(bada_name)
        except Exception:
            try:
                bada = _load_bada('B738W26')
            except Exception:
                stack.stack(f'ECHO CDOPRECOMPUTE: BADA load failed for {cs}, skipping.')
                continue

        # Override mass from mlw_factor
        mlw_factor = cdo_params.get('mlw_factor', 0.9)
        bada_work  = dict(bada)
        if 'mlw' in bada_work:
            bada_work['mass'] = bada_work['mlw'] * mlw_factor

        u_table[ac_idx]    = {}
        fuel_table[ac_idx] = {}

        entry_alt_m = ac.get('alt_m') or ac.get('baro_alt_m')
        for path_idx, node_list in enumerate(all_paths):
            rows = _grid_rows_from_nodes(node_list, ref_unix, speed_ms=speed_ms,
                                         entry_alt_m=entry_alt_m)
            if not rows:
                continue
            rows[0]['icao24']   = icao24
            rows[0]['callsign'] = cs

            try:
                cdo_pts = _cdo_for_aircraft(rows, bada_work, weather)
            except Exception:
                cdo_pts = []

            if not cdo_pts:
                # fallback: estimate from speed
                from bluesky.plugins.tma_opt import _GRID_COORDS, _haversine_nm
                edges = []
                for i in range(1, len(node_list)):
                    d_nm = _haversine_nm(_GRID_COORDS[node_list[i-1]], _GRID_COORDS[node_list[i]])
                    edges.append(round(d_nm / (speed_ms * 0.000539957) / 60.0))
                u_table[ac_idx][path_idx]    = edges
                fuel_table[ac_idx][path_idx] = 0.0
                continue

            # Extract edge travel times from CDO output (seconds → minutes, rounded)
            from bluesky.plugins.tma_opt import _GRID_COORDS, _haversine_nm
            cum_dist = [pt['cum_dist_nm'] for pt in cdo_pts]
            edge_times = []
            node_dists = [0.0]
            for i in range(1, len(node_list)):
                d = _haversine_nm(_GRID_COORDS[node_list[i-1]], _GRID_COORDS[node_list[i]])
                node_dists.append(node_dists[-1] + d)

            for i in range(1, len(node_dists)):
                d0, d1 = node_dists[i-1], node_dists[i]
                # Find CDO points within this edge
                pts_in = [pt for pt in cdo_pts if d0 <= pt['cum_dist_nm'] <= d1]
                if len(pts_in) >= 2:
                    dt_s   = pts_in[-1]['time'] - pts_in[0]['time']
                    dt_min = max(1, round(dt_s / 60.0))
                else:
                    seg_nm  = d1 - d0
                    avg_gs  = (sum(pt['velocity_ms'] for pt in cdo_pts) / len(cdo_pts)) * 0.000539957 * 3600
                    dt_min  = max(1, round(seg_nm / avg_gs * 60.0))
                edge_times.append(dt_min)

            u_table[ac_idx][path_idx]    = edge_times
            fuel_table[ac_idx][path_idx] = cdo_pts[-1]['cum_fuel_kg'] if cdo_pts else 0.0

        if (ac_idx + 1) % 5 == 0 or ac_idx == n_ac - 1:
            stack.stack(f'ECHO CDOPRECOMPUTE: {ac_idx+1}/{n_ac} done ...')

    _restore_cdo_params(old_params)

    # Save
    out = {
        'u':         u_table,
        'fuel':      fuel_table,
        'callsigns': [ac.get('callsign', '') for ac in all_ac],
        'n_paths':   n_paths,
        'ref_unix':  ref_unix,
    }
    out_path = pkl_path.parent / f'cdo_u_{pkl_path.parent.name}.pkl'
    with open(out_path, 'wb') as f:
        _pk.dump(out, f)

    stack.stack(f'ECHO CDOPRECOMPUTE: Done. Saved → {out_path.name}')
    stack.stack(f'ECHO CDOPRECOMPUTE: {n_ac} aircraft × {n_paths} paths → u table ready.')
    stack.stack('ECHO CDOPRECOMPUTE: Re-run TMAOPT with CDOOPT to use CDO-derived u values.')


# ---------------------------------------------------------------------------
# Phase 2 — CDOGENOPT
# ---------------------------------------------------------------------------

def _run_cdogenopt(pkl_arg: str):
    """Run CDO on the Gurobi-optimal route for each aircraft.
    Reads result.pkl → ac_path (node list + entry time) → builds grid-path rows
    → runs _cdo_for_aircraft → saves per-aircraft CDO CSV + combined SCN.
    """
    import pickle as _pk
    from datetime import datetime, timezone

    pkl_path = _find_latest_result_pkl(pkl_arg)
    if pkl_path is None:
        stack.stack('ECHO CDOGENOPT: No result.pkl found.')
        return

    with open(pkl_path, 'rb') as f:
        result = _pk.load(f)

    if not result.get('feasible'):
        stack.stack('ECHO CDOGENOPT: result is not feasible — no optimal paths to process.')
        return

    ac_path    = result.get('ac_path', {})
    ac_by_ent  = result.get('aircraft_by_entry', {})
    ref_unix   = result.get('ref_unix', 0)
    cdo_params = result.get('cdo_params', {})
    callsign_map = result.get('callsign_map', {})

    if not ac_path:
        stack.stack('ECHO CDOGENOPT: No ac_path in result.')
        return

    # Build ac_id → aircraft snapshot mapping
    all_ac_flat = {}
    ac_counter  = 1  # 1-based to match ac_id from _run_optimisation
    for d in ('N', 'E', 'S', 'W'):
        for ac in ac_by_ent.get(d, []):
            all_ac_flat[ac_counter] = ac
            ac_counter += 1

    old_params = _apply_cdo_params(cdo_params)
    date_str   = datetime.fromtimestamp(ref_unix, tz=timezone.utc).strftime('%Y%m%d')
    weather    = _get_era5(date_str)

    stem       = pkl_path.parent.name
    out_dir    = pkl_path.parent
    all_cdo_rows = []
    summary      = []
    errors       = []

    stack.stack(f'ECHO CDOGENOPT: Processing {len(ac_path)} aircraft on optimal routes ...')

    for ac_id, (node_list, entry_min) in ac_path.items():
        ac      = all_ac_flat.get(ac_id, {})
        cs      = callsign_map.get(ac_id, ac.get('callsign', f'AC{ac_id}'))
        icao24  = ac.get('icao24', '')
        speed_ms = ac.get('velocity_ms', 200.0)

        # Entry time in Unix seconds
        midnight = datetime.fromtimestamp(ref_unix, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        entry_unix = midnight.timestamp() + entry_min * 60.0

        entry_alt_m = ac.get('alt_m') or ac.get('baro_alt_m')
        rows = _grid_rows_from_nodes(node_list, entry_unix, speed_ms=speed_ms,
                                     entry_alt_m=entry_alt_m)
        if not rows:
            errors.append(f'{cs}: empty path rows')
            continue
        rows[0]['icao24']   = icao24
        rows[0]['callsign'] = cs

        actype    = _resolve_actype(icao24, cs)
        bada_name = _resolve_bada_model(actype)
        try:
            bada = _load_bada(bada_name)
        except Exception:
            try:
                bada      = _load_bada('B738W26')
                bada_name = 'B738W26'
            except Exception:
                errors.append(f'{cs}: BADA load failed')
                continue

        mlw_factor = cdo_params.get('mlw_factor', 0.9)
        bada_work  = dict(bada)
        if 'mlw' in bada_work:
            bada_work['mass'] = bada_work['mlw'] * mlw_factor

        try:
            cdo_pts = _cdo_for_aircraft(rows, bada_work, weather)
        except Exception as e:
            errors.append(f'{cs}: CDO error: {e}')
            continue

        if not cdo_pts:
            errors.append(f'{cs}: CDO produced no points')
            continue

        for pt in cdo_pts:
            pt['icao24']   = icao24
            pt['callsign'] = cs

        orig_f    = _orig_fuel_from_rows(rows, bada_work, weather)
        cdo_fuel  = cdo_pts[-1]['cum_fuel_kg']
        saving    = orig_f - cdo_fuel
        saving_pc = 100.0 * saving / orig_f if orig_f > 0 else 0.0

        # Save individual aircraft CDO CSV
        ac_csv = out_dir / f'{stem}_cdo_{cs}.csv'
        _save_cdo_csv(ac_csv, cdo_pts)

        all_cdo_rows.extend(cdo_pts)
        summary.append({
            'callsign':        cs,
            'actype':          actype,
            'bada_model':      bada_name,
            'cdo_fuel_kg':     round(cdo_fuel, 2),
            'orig_fuel_kg':    round(orig_f,   2),
            'fuel_saving_kg':  round(saving,   2),
            'fuel_saving_pct': round(saving_pc, 1),
            'duration_s':      len(cdo_pts),
            'distance_nm':     round(cdo_pts[-1]['cum_dist_nm'], 1),
            'n_nodes':         len(node_list),
            'entry_min':       entry_min,
        })
        stack.stack(f'ECHO CDOGENOPT: {cs} ({actype}) — CDO fuel={cdo_fuel:.1f}kg  saving={saving:.1f}kg ({saving_pc:.1f}%)')

    _restore_cdo_params(old_params)

    if not all_cdo_rows:
        stack.stack('ECHO CDOGENOPT: No CDO output produced.')
        for e in errors:
            stack.stack(f'ECHO WARN: {e}')
        return

    # Save combined CSV + summary + SCN
    all_cdo_rows.sort(key=lambda r: float(r.get('time', 0)))
    combined_csv = out_dir / f'{stem}_cdo_opt.csv'
    sum_csv      = out_dir / f'{stem}_cdo_opt_summary.csv'
    scn_path     = out_dir / f'{stem}_cdo_opt.scn'

    _save_cdo_csv(combined_csv, all_cdo_rows)
    _save_summary_csv(sum_csv, summary)

    fuel_csv = out_dir / f'{stem}_fuel_consumption.csv'
    with open(fuel_csv, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['callsign', 'orig_fuel_kg', 'cdo_fuel_kg',
                    'saving_kg', 'saving_pct', 'duration_s', 'distance_nm'])
        for s in summary:
            w.writerow([
                s['callsign'],
                s['orig_fuel_kg'],
                s['cdo_fuel_kg'],
                s['fuel_saving_kg'],
                s['fuel_saving_pct'],
                s['duration_s'],
                s['distance_nm'],
            ])

    # Write SCN: TMA polygon + TMAOpt tree links + CDO replay
    rel_csv = combined_csv.relative_to(_REPO_ROOT).as_posix()
    tree_links  = result.get('tree_links', [])
    merge_pts   = result.get('merge_points', [])

    from bluesky.plugins.tma_opt import _GRID_COORDS as _GC

    with open(scn_path, 'w') as f:
        f.write(f'# CDO-Optimal Scenario — {stem}\n')
        f.write('00:00:00.00>TIME 00:00:00\n')
        f.write('00:00:00.00> PAN 59.574, 17.9876\n')
        f.write('00:00:00.00> ZOOM 2.0\n')
        f.write('00:00:00.00> DT 1.0\n')
        f.write('00:00:00.00> TAXI OFF\n')
        f.write('00:00:00.00> SWRAD WPT 0\n')
        f.write('00:00:00.00> SWRAD APT 0\n')
        f.write('00:00:00.00> SWRAD SAT 0\n')
        f.write(f'00:00:00.00> POLY StockholmTMA {_STOCKHOLM_TMA_POLY}\n')
        for (i, j) in tree_links:
            if i in _GC and j in _GC:
                f.write(f'00:00:00.00> POLYLINE OPT_{i}_{j} '
                        f'{_GC[i][0]:.5f} {_GC[i][1]:.5f} '
                        f'{_GC[j][0]:.5f} {_GC[j][1]:.5f}\n')
                f.write(f'00:00:00.00> COLOR OPT_{i}_{j} 0 255 255\n')
        for node in merge_pts:
            if node in _GC:
                f.write(f'00:00:00.00> CIRCLE MERGE_{node} '
                        f'{_GC[node][0]:.5f} {_GC[node][1]:.5f} 1\n')
                f.write(f'00:00:00.00> COLOR MERGE_{node} 255 255 0\n')
        f.write(f'00:00:00.00> STARTREPLAY {rel_csv}\n')
        f.write('00:00:00.00> OP\n')

    total_saving = sum(s['fuel_saving_kg'] for s in summary)
    stack.stack(f'ECHO CDOGENOPT: ── Summary ──────────────────────')
    stack.stack(f'ECHO CDOGENOPT: {len(summary)} aircraft processed')
    stack.stack(f'ECHO CDOGENOPT: Total CDO fuel saving: {total_saving:.1f} kg')
    stack.stack(f'ECHO CDOGENOPT: Combined CSV : {combined_csv.name}')
    stack.stack(f'ECHO CDOGENOPT: SCN          : {scn_path.name}')
    for e in errors:
        stack.stack(f'ECHO WARN: {e}')


# ---------------------------------------------------------------------------
# Inline versions called directly from _run_tmaopt (no threading, no pkl I/O)
# ---------------------------------------------------------------------------

def _precompute_one_aircraft(args):
    """Top-level worker: compute u/fuel for one aircraft across all paths.
    all_paths and weather are taken from worker globals set by _pool_init.
    Returns (ac_idx, u_dict, fuel_dict).
    """
    (ac_idx, ac, ref_unix, mlw_factor) = args

    import sys as _sys
    _REPO_ROOT_STR = str(_REPO_ROOT)
    if _REPO_ROOT_STR not in _sys.path:
        _sys.path.insert(0, _REPO_ROOT_STR)
    from bluesky.plugins.tma_opt import _GRID_COORDS, _haversine_nm

    all_paths = _W_ALL_PATHS
    weather   = _W_WEATHER

    cs       = ac.get('callsign', f'AC{ac_idx}')
    icao24   = ac.get('icao24', '')
    speed_ms = ac.get('velocity_ms', 200.0)
    entry_alt_m = ac.get('alt_m') or ac.get('baro_alt_m')

    actype    = _resolve_actype(icao24, cs)
    bada_name = _resolve_bada_model(actype)
    try:
        bada = _load_bada(bada_name)
    except Exception:
        try:
            bada = _load_bada('B738W26')
        except Exception:
            return ac_idx, {}, {}

    bada_work = dict(bada)
    if 'mlw' in bada_work:
        bada_work['mass'] = bada_work['mlw'] * mlw_factor

    u_dict    = {}
    fuel_dict = {}

    for path_idx, node_list in enumerate(all_paths):
        rows = _grid_rows_from_nodes(node_list, ref_unix, speed_ms=speed_ms,
                                     entry_alt_m=entry_alt_m)
        if not rows:
            continue
        rows[0]['icao24']   = icao24
        rows[0]['callsign'] = cs

        try:
            cdo_pts = _cdo_for_aircraft(rows, bada_work, weather)
        except Exception:
            cdo_pts = []

        node_dists = [0.0]
        for i in range(1, len(node_list)):
            d = _haversine_nm(_GRID_COORDS[node_list[i-1]], _GRID_COORDS[node_list[i]])
            node_dists.append(node_dists[-1] + d)

        if not cdo_pts:
            edges = []
            for i in range(1, len(node_dists)):
                seg_nm = node_dists[i] - node_dists[i-1]
                edges.append(max(1, round(seg_nm / (speed_ms * 0.000539957) / 60.0)))
            u_dict[path_idx]    = edges
            fuel_dict[path_idx] = 0.0
            continue

        edge_times = []
        for i in range(1, len(node_dists)):
            d0, d1 = node_dists[i-1], node_dists[i]
            pts_in = [pt for pt in cdo_pts if d0 <= pt['cum_dist_nm'] <= d1]
            if len(pts_in) >= 2:
                dt_min = max(1, round((pts_in[-1]['time'] - pts_in[0]['time']) / 60.0))
            else:
                seg_nm = d1 - d0
                avg_gs = (sum(pt['velocity_ms'] for pt in cdo_pts) / len(cdo_pts)) * 0.000539957 * 3600
                dt_min = max(1, round(seg_nm / avg_gs * 60.0))
            edge_times.append(dt_min)

        u_dict[path_idx]    = edge_times
        fuel_dict[path_idx] = cdo_pts[-1]['cum_fuel_kg']

    return ac_idx, u_dict, fuel_dict


def _run_cdoprecompute_inline(result: dict):
    """CDO precompute called inline from _run_tmaopt.
    Returns (u_table, fuel_table) dicts keyed by 0-based ac_idx → path_idx → data.
    """
    from datetime import datetime, timezone as _tz

    all_paths  = result.get('all_paths', [])
    ac_by_ent  = result.get('aircraft_by_entry', {})
    ref_unix   = result.get('ref_unix', 0)
    cdo_params = result.get('cdo_params', {})

    if not all_paths:
        stack.stack('ECHO TMAOPT: CDO precompute — no all_paths in result.')
        return {}, {}

    all_ac = []
    for d in ('N', 'E', 'S', 'W'):
        for ac in ac_by_ent.get(d, []):
            all_ac.append(ac)

    if not all_ac:
        return {}, {}

    n_ac    = len(all_ac)
    n_paths = len(all_paths)
    stack.stack(f'ECHO TMAOPT: CDO precompute — {n_ac} aircraft × {n_paths} paths ...')

    old_params = _apply_cdo_params(cdo_params)
    date_str   = datetime.fromtimestamp(ref_unix, tz=_tz.utc).strftime('%Y%m%d')
    weather    = _get_era5(date_str)
    mlw_factor = cdo_params.get('mlw_factor', 0.9)

    u_table    = {}
    fuel_table = {}

    from bluesky.plugins.tma_opt import _GRID_COORDS, _haversine_nm

    args_list = [
        (ac_idx, ac, ac.get('crossing_time') or ref_unix, mlw_factor)
        for ac_idx, ac in enumerate(all_ac)
    ]

    completed = 0
    with multiprocessing.Pool(
        processes=_N_CDO_WORKERS,
        initializer=_pool_init,
        initargs=(all_paths, weather),
    ) as pool:
        for result in pool.imap_unordered(_precompute_one_aircraft, args_list):
            try:
                ac_idx, u_dict, fuel_dict = result
            except Exception as _e:
                ac_idx = completed
                u_dict, fuel_dict = {}, {}
            u_table[ac_idx]    = u_dict
            fuel_table[ac_idx] = fuel_dict
            completed += 1
            if completed % 3 == 0 or completed == n_ac:
                stack.stack(f'ECHO TMAOPT: CDO precompute — {completed}/{n_ac} done ...')

    _restore_cdo_params(old_params)
    return u_table, fuel_table


def _run_cdogenopt_inline(result: dict, out_dir: Path, stem_override: str = None):
    """CDO on optimal routes, called inline from _run_tmaopt after Phase 2."""
    from datetime import datetime, timezone as _tz

    ac_path      = result.get('ac_path', {})
    ac_by_ent    = result.get('aircraft_by_entry', {})
    ref_unix     = result.get('ref_unix', 0)
    cdo_params   = result.get('cdo_params', {})
    callsign_map = result.get('callsign_map', {})
    stem         = stem_override if stem_override else out_dir.name

    if not ac_path:
        stack.stack('ECHO TMAOPT: CDOGENOPT — no ac_path in result, skipping.')
        return

    all_ac_flat = {}
    ac_counter  = 1  # 1-based to match ac_id from _run_optimisation
    for d in ('N', 'E', 'S', 'W'):
        for ac in ac_by_ent.get(d, []):
            all_ac_flat[ac_counter] = ac
            ac_counter += 1

    old_params = _apply_cdo_params(cdo_params)
    date_str   = datetime.fromtimestamp(ref_unix, tz=_tz.utc).strftime('%Y%m%d')
    weather    = _get_era5(date_str)
    mlw_factor = cdo_params.get('mlw_factor', 0.9)
    midnight   = datetime.fromtimestamp(ref_unix, tz=_tz.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)

    all_cdo_rows = []
    summary      = []
    errors       = []

    for ac_id, (node_list, entry_min) in ac_path.items():
        ac       = all_ac_flat.get(ac_id, {})
        cs       = callsign_map.get(ac_id, ac.get('callsign', f'AC{ac_id}'))
        icao24   = ac.get('icao24', '')
        speed_ms = ac.get('velocity_ms', 200.0)

        # Anchor CDO to the entry time Gurobi assigned (entry_min from midnight).
        # Let CDO physics determine timestamps at each node naturally — no override.
        # This ensures the CDO profile (speed, altitude, fuel) and its timing are
        # internally consistent with BADA physics and ERA5 wind.
        entry_unix  = midnight.timestamp() + entry_min * 60.0
        entry_alt_m = ac.get('alt_m') or ac.get('baro_alt_m')
        rows = _grid_rows_from_nodes(node_list, entry_unix, speed_ms=speed_ms,
                                     entry_alt_m=entry_alt_m)
        if not rows:
            errors.append(f'{cs}: empty path rows')
            continue
        rows[0]['icao24']   = icao24
        rows[0]['callsign'] = cs

        actype    = _resolve_actype(icao24, cs)
        bada_name = _resolve_bada_model(actype)
        try:
            bada = _load_bada(bada_name)
        except Exception:
            try:
                bada      = _load_bada('B738W26')
                bada_name = 'B738W26'
            except Exception:
                errors.append(f'{cs}: BADA load failed')
                continue

        bada_work = dict(bada)
        if 'mlw' in bada_work:
            bada_work['mass'] = bada_work['mlw'] * mlw_factor

        try:
            cdo_pts = _cdo_for_aircraft(rows, bada_work, weather)
        except Exception as e:
            errors.append(f'{cs}: CDO error: {e}')
            continue

        if not cdo_pts:
            errors.append(f'{cs}: CDO produced no points')
            continue

        for pt in cdo_pts:
            pt['icao24']   = icao24
            pt['callsign'] = cs

        orig_f    = _orig_fuel_from_rows(rows, bada_work, weather)
        cdo_fuel  = cdo_pts[-1]['cum_fuel_kg']
        saving    = orig_f - cdo_fuel
        saving_pc = 100.0 * saving / orig_f if orig_f > 0 else 0.0

        ac_csv = out_dir / f'{stem}_cdo_{cs}.csv'
        _save_cdo_csv(ac_csv, cdo_pts)

        all_cdo_rows.extend(cdo_pts)
        summary.append({
            'callsign': cs, 'actype': actype, 'bada_model': bada_name,
            'cdo_fuel_kg': round(cdo_fuel, 2), 'orig_fuel_kg': round(orig_f, 2),
            'fuel_saving_kg': round(saving, 2), 'fuel_saving_pct': round(saving_pc, 1),
            'duration_s': len(cdo_pts), 'distance_nm': round(cdo_pts[-1]['cum_dist_nm'], 1),
            'n_nodes': len(node_list), 'entry_min': entry_min,
        })
        stack.stack(f'ECHO TMAOPT: CDO {cs} ({actype}) — {cdo_fuel:.1f}kg  saving={saving:.1f}kg ({saving_pc:.1f}%)')

    _restore_cdo_params(old_params)

    if not all_cdo_rows:
        stack.stack('ECHO TMAOPT: CDOGENOPT — no CDO output.')
        for e in errors:
            stack.stack(f'ECHO WARN: {e}')
        return

    all_cdo_rows.sort(key=lambda r: float(r.get('time', 0)))
    combined_csv = out_dir / f'{stem}_cdo_opt.csv'
    sum_csv      = out_dir / f'{stem}_cdo_opt_summary.csv'
    scn_path_out = out_dir / f'{stem}_cdo_opt.scn'

    _save_cdo_csv(combined_csv, all_cdo_rows)
    _save_summary_csv(sum_csv, summary)

    fuel_csv = out_dir / f'{stem}_fuel_consumption.csv'
    with open(fuel_csv, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['callsign', 'orig_fuel_kg', 'cdo_fuel_kg',
                    'saving_kg', 'saving_pct', 'duration_s', 'distance_nm'])
        for s in summary:
            w.writerow([
                s['callsign'],
                s['orig_fuel_kg'],
                s['cdo_fuel_kg'],
                s['fuel_saving_kg'],
                s['fuel_saving_pct'],
                s['duration_s'],
                s['distance_nm'],
            ])

    rel_csv    = combined_csv.relative_to(_REPO_ROOT).as_posix()
    tree_links = result.get('tree_links', [])
    merge_pts  = result.get('merge_points', [])

    from bluesky.plugins.tma_opt import _GRID_COORDS as _GC, _write_grid

    with open(scn_path_out, 'w') as f:
        f.write(f'# CDO-Optimal Scenario — {stem}\n')
        f.write('00:00:00.00>TIME 00:00:00\n')
        f.write('00:00:00.00> PAN 59.574, 17.9876\n')
        f.write('00:00:00.00> ZOOM 2.0\n')
        f.write('00:00:00.00> DT 1.0\n')
        f.write('00:00:00.00> TAXI OFF\n')
        f.write('00:00:00.00> SWRAD WPT 0\n')
        f.write('00:00:00.00> SWRAD APT 0\n')
        f.write('00:00:00.00> SWRAD SAT 0\n')
        f.write(f'00:00:00.00> POLY StockholmTMA {_STOCKHOLM_TMA_POLY}\n')
        _write_grid(f, result.get('LINKS', []), result.get('B', []), result.get('N_exit', 72))
        for (i, j) in tree_links:
            if i in _GC and j in _GC:
                f.write(f'00:00:00.00> POLYLINE OPT_{i}_{j} '
                        f'{_GC[i][0]:.5f} {_GC[i][1]:.5f} '
                        f'{_GC[j][0]:.5f} {_GC[j][1]:.5f}\n')
                f.write(f'00:00:00.00> COLOR OPT_{i}_{j} 0 255 255\n')
        for node in merge_pts:
            if node in _GC:
                f.write(f'00:00:00.00> CIRCLE MERGE_{node} '
                        f'{_GC[node][0]:.5f} {_GC[node][1]:.5f} 1\n')
                f.write(f'00:00:00.00> COLOR MERGE_{node} 255 255 0\n')
        f.write(f'00:00:00.00> STARTREPLAY {rel_csv}\n')
        f.write('00:00:00.00> OP\n')

    total_saving = sum(s['fuel_saving_kg'] for s in summary)
    stack.stack(f'ECHO TMAOPT: CDO optimal — {len(summary)} ac, total saving: {total_saving:.1f} kg')
    stack.stack(f'ECHO TMAOPT: CDO SCN: {scn_path_out.name}')
    for e in errors:
        stack.stack(f'ECHO WARN: {e}')

    # Auto-load the CDO optimal scenario (use absolute path to avoid resolution ambiguity)
    stack.stack(f'IC {scn_path_out.resolve()}')


def _parse_tma_poly_cdo():
    nums = [float(x) for x in _STOCKHOLM_TMA_POLY.split()]
    return [(nums[i], nums[i+1]) for i in range(0, len(nums), 2)]


_TMA_VERTICES_CDO = None


def _tma_vertices_cdo():
    global _TMA_VERTICES_CDO
    if _TMA_VERTICES_CDO is None:
        _TMA_VERTICES_CDO = _parse_tma_poly_cdo()
    return _TMA_VERTICES_CDO


def _point_in_tma_cdo(lat, lon):
    verts = _tma_vertices_cdo()
    n = len(verts)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = verts[i][1], verts[i][0]
        xj, yj = verts[j][1], verts[j][0]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------------------
# Trino fetch for a single callsign
# ---------------------------------------------------------------------------

def _fetch_trino_track(callsign: str, begin_ts: int, end_ts: int):
    """Fetch full trajectory for one callsign from OpenSky Trino.

    Returns list of row-dicts (same format as _read_tracks_csv) ordered by time,
    clipped to start at the first waypoint inside the TMA, or None on failure.
    """
    import sys
    _REPO_ROOT_STR = str(_REPO_ROOT)
    if _REPO_ROOT_STR not in sys.path:
        sys.path.insert(0, _REPO_ROOT_STR)

    try:
        from utils.opensky_importer.fetcher import OpenSkyFetcher
    except ImportError as e:
        stack.stack(f'ECHO CDOGEN: Cannot import OpenSkyFetcher: {e}')
        return None

    lat_margin = 200.0 / 60.0
    lon_margin = 200.0 / (60.0 * math.cos(math.radians(_ESSA_LAT)))
    lamin = _ESSA_LAT - lat_margin
    lamax = _ESSA_LAT + lat_margin
    lomin = _ESSA_LON - lon_margin
    lomax = _ESSA_LON + lon_margin

    fetcher = OpenSkyFetcher()
    try:
        tracks = fetcher.fetch_area_flights_trino(begin_ts, end_ts, lamin, lomin, lamax, lomax)
    except Exception as e:
        stack.stack(f'ECHO CDOGEN: Trino fetch error: {e}')
        return None

    target = callsign.upper()
    track  = next((t for t in tracks if (t.callsign or '').upper() == target), None)
    if track is None:
        stack.stack(f'ECHO CDOGEN: Callsign {callsign} not found in Trino results.')
        return None

    wps = [wp for wp in track.waypoints
           if not wp.on_ground and wp.baro_alt_m and wp.baro_alt_m > 300]
    if not wps:
        stack.stack(f'ECHO CDOGEN: {callsign} — no airborne waypoints.')
        return None

    # Clip track to first waypoint inside TMA
    tma_idx = next((i for i, wp in enumerate(wps) if _point_in_tma_cdo(wp.lat, wp.lon)), None)
    if tma_idx is None:
        stack.stack(f'ECHO CDOGEN: {callsign} — track never enters TMA.')
        return None
    wps = wps[tma_idx:]

    rows = [{
        'icao24':           track.icao24 or '',
        'callsign':         track.callsign or callsign,
        'est_departure':    track.est_departure or '',
        'est_arrival':      track.est_arrival or '',
        'time':             wp.time,
        'lat':              wp.lat,
        'lon':              wp.lon,
        'baro_alt_m':       wp.baro_alt_m,
        'true_track':       wp.true_track or 0.0,
        'on_ground':        False,
        'velocity_ms':      wp.velocity_ms or 150.0,
        'vertical_rate_ms': wp.vertical_rate_ms or 0.0,
    } for wp in wps]

    return rows


# ---------------------------------------------------------------------------
# CDO runner — Trino single-aircraft mode
# ---------------------------------------------------------------------------

def _run_cdo_trino(callsign: str, begin_ts: int, end_ts: int):
    rows = _fetch_trino_track(callsign, begin_ts, end_ts)
    if not rows:
        return

    icao24    = rows[0].get('icao24', '')
    actype    = _resolve_actype(icao24, callsign)
    if _is_ga(actype):
        stack.stack(f'ECHO CDOGEN: {callsign} is GA/helicopter, skipping.')
        return
    bada_name = _resolve_bada_model(actype)

    try:
        bada = _load_bada(bada_name)
    except Exception as e:
        stack.stack(f'ECHO CDOGEN: BADA load failed ({bada_name}): {e}')
        try:
            bada      = _load_bada('B738W26')
            bada_name = 'B738W26'
        except Exception:
            stack.stack(f'ECHO CDOGEN: Fallback BADA also failed, aborting.')
            return

    from datetime import datetime, timezone
    date_str = datetime.fromtimestamp(begin_ts, tz=timezone.utc).strftime('%Y%m%d')
    weather  = _get_era5(date_str)

    try:
        cdo_pts = _cdo_for_aircraft(rows, bada, weather)
    except Exception as e:
        stack.stack(f'ECHO CDOGEN: CDO calc error for {callsign}: {e}')
        return

    if not cdo_pts:
        stack.stack(f'ECHO CDOGEN: {callsign} — CDO produced no points.')
        return

    orig_f    = _orig_fuel_from_rows(rows, bada, weather)
    cdo_fuel  = cdo_pts[-1]['cum_fuel_kg']
    saving    = orig_f - cdo_fuel
    saving_pc = 100.0 * saving / orig_f if orig_f > 0 else 0.0
    dist_nm   = cdo_pts[-1]['cum_dist_nm']

    for pt in cdo_pts:
        pt['actype']     = actype
        pt['bada_model'] = bada_name

    stem         = f'cdo_{callsign}_{begin_ts}'
    out_path     = _SCENARIO_DIR / f'{stem}_cdo.csv'
    summary_path = _SCENARIO_DIR / f'{stem}_cdo_summary.csv'
    scn_path     = _SCENARIO_DIR / f'{stem}_cdo.scn'

    _save_cdo_csv(out_path, cdo_pts)
    _save_summary_csv(summary_path, [{
        'callsign':       callsign,
        'actype':         actype,
        'bada_model':     bada_name,
        'cdo_fuel_kg':    round(cdo_fuel, 2),
        'orig_fuel_kg':   round(orig_f,   2),
        'fuel_saving_kg': round(saving,   2),
        'fuel_saving_pct':round(saving_pc, 1),
        'duration_s':     len(cdo_pts),
        'distance_nm':    round(dist_nm,  1),
    }])
    _save_scn(scn_path, stem, out_path)

    stack.stack(f'ECHO --- CDO: {callsign} ({actype} / {bada_name}) ---')
    stack.stack(f'ECHO   CDO fuel : {cdo_fuel:.1f} kg')
    stack.stack(f'ECHO   ORIG fuel: {orig_f:.1f} kg')
    stack.stack(f'ECHO   Saving  : {saving:.1f} kg ({saving_pc:.1f}%)')
    stack.stack(f'ECHO   Distance: {dist_nm:.1f} nm  Duration: {len(cdo_pts)} s')
    stack.stack(f'ECHO   SCN: {scn_path.name}')



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

    Follows the paper methodology (Section 3.4, Fig. 3.3):
      1. Build the speed schedule anchored at FAP altitude (2000 ft).
      2. Integrate BACKWARD in time from FAP up to the entry altitude,
         accumulating distance, to find altitude/speed/fuel at each second.
      3. Reverse the backward sequence and output it forward in time,
         interpolating lat/lon from the original horizontal track.

    The horizontal path is kept identical to the observed track.
    Only the vertical profile (alt, ROCD, speed, fuel) is replaced.
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

    # Use the full track from TMA entry to runway — matching MATLAB CDO_profile_predictor
    # which feeds the complete reversed track and stops only when TMA_dist is covered.
    if len(rows) < 2:
        return []

    cum_d, lats, lons = _build_cum_dist_nm(rows)
    TMA_dist_nm = float(cum_d[-1])
    if TMA_dist_nm < 0.1:
        return []

    r0      = rows[0]
    fap_alt = _CDO_FAP_ALT_M

    # -----------------------------------------------------------------------
    # Build speed schedule anchored at FAP (paper: speed schedule fixed,
    # stall margin based on conditions at FAP altitude).
    # -----------------------------------------------------------------------
    T_fap, p_fap, delta_fap, _ = _isa(fap_alt)
    rho_fap      = p_fap / (_R * T_fap)
    cas_stall_ms = _cas_stall_approx(bada, mass, delta_fap)

    # Entry speed: use observed GS at first track point as CAS approximation.
    T_isa0, p0_Pa, _, _ = _isa(r0['baro_alt_m'])
    rho0          = p0_Pa / (_R * T_isa0)
    gs0           = max(float(r0['velocity_ms']), 30.0)
    TAS0          = _tas_from_cas(gs0, p0_Pa, rho0)
    a0_ms         = math.sqrt(_k * _R * T_isa0)
    M_descent     = min(Mmax, max(Mmin, TAS0 / a0_ms))
    CAS_start_ms  = _cas_from_tas(TAS0, p0_Pa, rho0)

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

    # -----------------------------------------------------------------------
    # Backward integration: start at FAP, integrate upward until we have
    # covered TMA_dist_nm or exceeded entry altitude.
    # Each step represents 1 second going backwards in time.
    # ROCD sign is inverted (climbing backward = positive altitude change).
    # -----------------------------------------------------------------------
    alt        = fap_alt
    CAS_ms     = reduce_ms[0]   # speed at FAP: lowest band
    T_isa, p_Pa, _, _ = _isa(alt)
    rho        = p_Pa / (_R * T_isa)
    TAS        = _tas_from_cas(CAS_ms, p_Pa, rho)
    M          = _mach_from_tas(TAS, math.sqrt(_k * _R * T_isa))
    M          = max(Mmin, min(Mmax, M))
    exit_flags = [False] * 6
    cum_dist   = 0.0
    # For backward integration we use the FAP lat/lon as starting horizontal pos.
    lat_b, lon_b = float(lats[-1]), float(lons[-1])

    backward = []   # list of (alt, CAS_ms, M, TAS, GS, FF, rocd) per backward step
    MAX_ITER = 7200

    _ALT_CAP_M = 15000.0   # hard cap — above this BADA idle coefficients are unreliable

    for _ in range(MAX_ITER):
        alt = min(alt, _ALT_CAP_M)
        T_isa, p_Pa, delta, theta = _isa(alt)
        delta = max(delta, 1e-4)
        theta = max(theta, 1e-4)
        rho = max(p_Pa / (_R * T_isa), 1e-6)
        a   = math.sqrt(_k * _R * T_isa)

        T_era5, u_era5, v_era5 = _era5_at(weather, alt, lat_b, lon_b)
        if T_era5 and T_era5 > 100.0:
            T_act = max(T_isa + (T_era5 - T_isa), 1.0)
            delta = max((p_Pa / _p0) * (_T0 / T_act), 1e-4)
            theta = max(T_act / _T0, 1e-4)
            a     = math.sqrt(_k * _R * T_act)
            rho   = max(p_Pa / (_R * T_act), 1e-6)
        T_sound = T_era5 if (T_era5 and T_era5 > 100) else T_isa

        # Use reversed horizontal position for wind lookup (travel backward along track)
        back_dist = TMA_dist_nm - cum_dist
        lat_b, lon_b = _interp_latlon(cum_d, lats, lons, max(0.0, back_dist))

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

        trk_r      = math.radians(float(r0['true_track']))
        wind_along = (u_era5 * math.sin(trk_r) + v_era5 * math.cos(trk_r)) if u_era5 else 0.0
        GS         = max(_gs_from_tas(TAS, wind_along), 10.0)

        CL_v = _cl_max(M, bf)
        CL   = _cl(mass, delta, S, M, CL_v)
        CD   = _cd(M, CL, d, CD_scalar)
        D    = _drag(delta, S, M, CD)

        try:
            CT_idle    = _ct_idle_jet(delta, M, ti)
            Thr_idle_N = _thr_idle(delta, mass, CT_idle)
            CF_idle    = _cf_idle_jet(fi, delta, M, theta)
            FF         = _fuel_flow(delta, theta, mass, LHV, CF_idle)
            rocd       = _rocd(Thr_idle_N, D, TAS, mass, M)
        except (OverflowError, ZeroDivisionError, ValueError):
            CT_idle    = 0.0
            Thr_idle_N = 0.0
            CF_idle    = 0.0
            FF         = 0.0
            rocd       = -6.0

        backward.append((alt, CAS_ms, M, TAS, GS, FF, rocd))

        # Step backward: altitude climbs (reverse of descent)
        alt      = alt - rocd * 1.0   # rocd is negative during descent, so alt increases
        cum_dist += GS * _M_TO_NM

        if cum_dist >= TMA_dist_nm:
            break

    if not backward:
        return []

    # -----------------------------------------------------------------------
    # Reverse the backward sequence → forward CDO profile.
    # Assign lat/lon by cumulative distance forward along the original track.
    # -----------------------------------------------------------------------
    backward.reverse()

    t0_abs   = int(float(r0['time']))
    output   = []
    fwd_dist = 0.0
    prev_lat = float(lats[0])
    prev_lon = float(lons[0])
    cum_fuel = 0.0

    for step, (alt_s, CAS_s, M_s, TAS_s, GS_s, FF_s, rocd_s) in enumerate(backward):
        fwd_dist += GS_s * _M_TO_NM
        lat, lon  = _interp_latlon(cum_d, lats, lons, fwd_dist)
        trk       = _bearing(prev_lat, prev_lon, lat, lon) if step > 0 else float(r0['true_track'])
        cum_fuel += FF_s

        output.append({
            'icao24':           r0.get('icao24', ''),
            'callsign':         r0['callsign'],
            'est_departure':    '',
            'est_arrival':      '',
            'time':             t0_abs + step,
            'lat':              round(lat, 6),
            'lon':              round(lon, 6),
            'baro_alt_m':       round(alt_s, 1),
            'true_track':       round(trk, 1),
            'on_ground':        False,
            'velocity_ms':      round(GS_s, 2),
            'vertical_rate_ms': round(rocd_s, 3),
            'cas_ms':           round(CAS_s, 2),
            'mach':             round(M_s, 4),
            'fuel_flow_kg_s':   round(FF_s, 5),
            'cum_fuel_kg':      round(cum_fuel, 3),
            'cum_dist_nm':      round(fwd_dist, 3),
        })

        prev_lat, prev_lon = lat, lon

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
        'on_ground', 'velocity_ms', 'vertical_rate_ms', 'cas_ms',
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
