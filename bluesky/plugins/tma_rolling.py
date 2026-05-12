"""tma_rolling.py — Rolling TMA Optimizer plugin for BlueSky.

Stack command: TMAROLLING <dtstr> <total_duration_min> [same params as TMAOPT]

Fetches a minimum 2-hour window, splits it into 1-hour slots, runs a full
TMAOPT (fetch → CDO precompute → Gurobi → CDO gen) for each slot, then
writes ONE combined SCN file with timed tree-switching commands.

Tree colours by slot generation:
  slot 0  — cyan     0 190 255
  slot 1  — orange   255 140 0
  slot 2  — magenta  255 0 200
  slot 3+ — cycle
"""

import csv
import math
import pickle
import time
from datetime import datetime, timezone
from math import ceil
from pathlib import Path

import bluesky as bs
from bluesky import stack

# ── repo root ─────────────────────────────────────────────────────────────────
_REPO_ROOT    = Path(__file__).resolve().parents[2]
_SCENARIO_DIR = _REPO_ROOT / 'scenario' / 'TMAOpt'

_TREE_COLORS = [
    (0,   190, 255),   # cyan
    (255, 140,   0),   # orange
    (255,   0, 200),   # magenta
    (0,   220,   0),   # green
    (255, 220,   0),   # yellow
]


# ── plugin registration ───────────────────────────────────────────────────────
def init_plugin():
    config = {
        'plugin_name': 'TMA_ROLLING',
        'plugin_type': 'sim',
    }
    stackfunctions = {
        'TMAROLLING': [
            'TMAROLLING dtstr [total_duration_min entries max_ac max_ac_per_entry '
            'max_eps time_limit s1 s2 fetch_radius '
            'cdo_fap_alt cdo_ias_start cdo_ias_restrict cdo_mach '
            'cdo_mlw cdo_kt_per_sec cdo_wind cdo_c_v_min]',
            'txt,[int,txt,int,int,int,int,int,int,int,'
            'int,int,int,float,float,float,int,float]',
            _cmd_tmarolling,
            'Rolling TMA optimiser — builds sequential hour-slot trees over ≥2h window.',
        ],
    }
    return config, stackfunctions


# ── stack command ─────────────────────────────────────────────────────────────
def _cmd_tmarolling(dtstr='', total_duration=120, entries='NESW',
                    max_ac=15, max_ac_per_entry=5, max_eps=3,
                    time_limit_per_eps=120, s1=2, s2=3, fetch_radius=50,
                    cdo_fap_alt=2000, cdo_ias_start=200, cdo_ias_restrict=220,
                    cdo_mach=0.84, cdo_mlw=0.9, cdo_kt_per_sec=1.0,
                    cdo_wind=1, cdo_c_v_min=1.23):

    cdo_params = {
        'fap_alt_ft':      cdo_fap_alt,
        'ias_start_kt':    cdo_ias_start,
        'ias_restrict_kt': cdo_ias_restrict,
        'mach':            cdo_mach,
        'mlw_factor':      cdo_mlw,
        'kt_per_sec':      cdo_kt_per_sec,
        'use_wind':        bool(cdo_wind),
        'c_v_min':         cdo_c_v_min,
    }

    _run_rolling(
        dtstr=dtstr,
        total_duration=max(120, int(total_duration)),
        entries=entries,
        max_ac=int(max_ac),
        max_ac_per_entry=int(max_ac_per_entry),
        max_eps=int(max_eps),
        time_limit_per_eps=int(time_limit_per_eps),
        s1=int(s1),
        s2=int(s2),
        fetch_radius=int(fetch_radius),
        cdo_params=cdo_params,
    )


# ── core rolling function ─────────────────────────────────────────────────────
def _run_rolling(dtstr, total_duration, entries, max_ac, max_ac_per_entry,
                 max_eps, time_limit_per_eps, s1, s2, fetch_radius, cdo_params):

    from bluesky.plugins.tma_opt import (
        _parse_dtstr, _fetch_historical, _save_historical_csv,
        _run_optimisation, _get_all_paths, _build_aircraft_by_entry,
        _node_to_latlon, _STOCKHOLM_TMA_POLY, _write_grid, _ENTRY_NODES,
    )
    from bluesky.plugins.cdo_gen import (
        _run_cdoprecompute_inline, _run_cdogenopt_inline,
    )

    n_slots = ceil(total_duration / 60)

    if dtstr:
        base_end_ts = _parse_dtstr(dtstr)
        if base_end_ts is None:
            stack.stack(f'ECHO TMAROLLING: Cannot parse datetime "{dtstr}". Use YYYY-MM-DDTHH:MM.')
            return
        base_end_ts = int(base_end_ts)
    else:
        base_end_ts = int(time.time()) - 1800

    base_ts_label = datetime.fromtimestamp(
        base_end_ts - (n_slots * 3600), tz=timezone.utc
    ).strftime('%Y%m%d_%H%M%S')

    stem     = f'tmarolling_{base_ts_label}'
    out_dir  = _SCENARIO_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    stack.stack(f'ECHO TMAROLLING: Starting {n_slots}-slot rolling optimisation ...')
    stack.stack(f'ECHO TMAROLLING: Output → {out_dir.relative_to(_REPO_ROOT)}')

    slot_results = []

    for slot_idx in range(n_slots):
        slot_end   = base_end_ts - (n_slots - 1 - slot_idx) * 3600
        slot_start = slot_end - 3600
        slot_label = datetime.fromtimestamp(slot_end, tz=timezone.utc).strftime('%Y%m%d_%H%M%S')
        slot_stem  = f'slot{slot_idx}_{slot_label}'

        stack.stack(
            f'ECHO TMAROLLING: ── Slot {slot_idx+1}/{n_slots} '
            f'({datetime.fromtimestamp(slot_start, tz=timezone.utc).strftime("%H:%M")} – '
            f'{datetime.fromtimestamp(slot_end, tz=timezone.utc).strftime("%H:%M")} UTC) ──'
        )

        raw_ac, tracks = _fetch_historical(slot_start, slot_end, fetch_radius)
        if not raw_ac:
            stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: no data, skipping.')
            slot_results.append(None)
            continue

        _save_historical_csv(out_dir, tracks, slot_stem)

        # Build track_waypoints dict for arrival filtering
        track_waypoints = {}
        for t in tracks:
            cs = t.callsign.strip().upper() or t.icao24.upper()
            track_waypoints[cs] = [wp for wp in t.waypoints if not wp.on_ground]

        ref_unix = slot_end
        aircraft_by_entry, n_arriving = _build_aircraft_by_entry(
            raw_ac, track_waypoints, entries, max_ac, max_ac_per_entry, ref_unix)
        stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: {n_arriving} ac crossed/inside TMA.')

        if not any(aircraft_by_entry.values()):
            stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: no arrivals found, skipping.')
            slot_results.append(None)
            continue

        n_ac = sum(len(v) for v in aircraft_by_entry.values())

        # ── Phase 1: CDO precompute ──────────────────────────────────────────
        all_paths  = _get_all_paths()
        base_result = {
            'all_paths':         all_paths,
            'cdo_params':        cdo_params,
            'aircraft_by_entry': aircraft_by_entry,
            'ref_unix':          ref_unix,
        }

        stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: CDO precompute ({n_ac} ac) ...')
        t0 = time.time()
        try:
            u_cdo, _fuel_cdo = _run_cdoprecompute_inline(base_result)
        except Exception as _e:
            stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: CDO precompute error: {_e}')
            u_cdo = {}
        stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: CDO precompute done in {time.time()-t0:.1f}s')

        u_override = {ac_idx + 1: paths for ac_idx, paths in u_cdo.items()} if u_cdo else None

        # ── Phase 2: Gurobi ──────────────────────────────────────────────────
        stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: Gurobi optimisation ...')
        _base = [0, 2]
        eps_seq = sorted(set(_base + list(range(3, max_eps + 1))))
        eps_seq = [e for e in eps_seq if e <= max_eps]

        result = None
        for eps in eps_seq:
            t_eps = time.time()
            result = _run_optimisation(
                aircraft_by_entry, ref_unix,
                epsilon=eps, time_limit_override=time_limit_per_eps,
                s1=s1, s2=s2, u_override=u_override,
            )
            elapsed = time.time() - t_eps
            if result is None:
                stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: optimisation failed.')
                break
            if result['feasible']:
                stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: feasible eps={eps} {elapsed:.1f}s')
                break
            stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: eps={eps} infeasible {elapsed:.1f}s')

        if result is None or not result['feasible']:
            stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: no feasible solution.')
            slot_results.append(None)
            continue

        result['cdo_params']        = cdo_params
        result['aircraft_by_entry'] = aircraft_by_entry
        result['ref_unix']          = ref_unix

        with open(out_dir / f'{slot_stem}_result.pkl', 'wb') as f:
            pickle.dump(result, f)

        # ── Phase 3: CDO on optimal routes ───────────────────────────────────
        stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: CDO on optimal routes ...')
        try:
            _run_cdogenopt_inline(result, out_dir, stem_override=slot_stem)
        except Exception as _e:
            stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: CDO gen error: {_e}')

        slot_results.append({
            'slot_idx':   slot_idx,
            'slot_end':   slot_end,
            'slot_stem':  slot_stem,
            'result':     result,
        })
        stack.stack(f'ECHO TMAROLLING: Slot {slot_idx}: done.')

    valid_slots = [s for s in slot_results if s is not None]
    if not valid_slots:
        stack.stack('ECHO TMAROLLING: No feasible slots — aborting SCN generation.')
        return

    # ── Write combined rolling SCN ────────────────────────────────────────────
    combined_scn = out_dir / f'{stem}_combined.scn'
    _write_rolling_scn(combined_scn, valid_slots, out_dir, _node_to_latlon,
                       _STOCKHOLM_TMA_POLY, _write_grid)

    stack.stack(f'ECHO TMAROLLING: Combined SCN → {combined_scn.name}')
    stack.stack(f'IC {combined_scn.resolve()}')


# ── SCN writer ────────────────────────────────────────────────────────────────
def _merge_slot_csvs(valid_slots, out_dir, merged_path: Path):
    """Concatenate all slot CDO CSVs into one file for a single STARTREPLAY."""
    header_written = False
    with open(merged_path, 'w', newline='') as out_f:
        writer = None
        for slot_info in valid_slots:
            slot_stem = slot_info['slot_stem']
            cdo_csv = out_dir / f'{slot_stem}_cdo_opt.csv'
            if not cdo_csv.exists():
                continue
            with open(cdo_csv, newline='') as in_f:
                reader = csv.DictReader(in_f)
                if not header_written:
                    writer = csv.DictWriter(out_f, fieldnames=reader.fieldnames)
                    writer.writeheader()
                    header_written = True
                for row in reader:
                    writer.writerow(row)
    return merged_path if header_written else None


def _write_rolling_scn(scn_path, valid_slots, out_dir,
                        _node_to_latlon, _STOCKHOLM_TMA_POLY, _write_grid):

    slot0 = valid_slots[0]
    result0 = slot0['result']

    # Determine t=0 in simulation: earliest entry_min of slot 0
    ac_path0   = result0.get('ac_path', {})
    all_entry0 = [em for _, (_, em) in ac_path0.items()] if ac_path0 else [0]
    t0_min     = min(all_entry0) if all_entry0 else 0

    def _min_to_scn_time(abs_min):
        delta_s = max(0, int((abs_min - t0_min) * 60))
        h = delta_s // 3600
        m = (delta_s % 3600) // 60
        s = delta_s % 60
        return f'{h:02d}:{m:02d}:{s:02d}.00'

    # Merge all slot CDO CSVs into one so STARTREPLAY sees all aircraft
    stem = scn_path.stem.replace('_combined', '')
    merged_csv = out_dir / f'{stem}_all_slots_cdo_opt.csv'
    merged_csv_path = _merge_slot_csvs(valid_slots, out_dir, merged_csv)

    with open(scn_path, 'w') as f:
        f.write(f'# TMAROLLING Combined Scenario — {slot0["slot_stem"]}\n')
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

        B      = result0.get('B', [])
        N_exit = result0.get('N_exit', 72)
        _write_grid(f, result0.get('LINKS', []), B, N_exit)

        # Single STARTREPLAY with all slots' aircraft merged
        if merged_csv_path is not None:
            f.write(f'00:00:00.00> STARTREPLAY {merged_csv_path}\n')

        prev_tree_names = []
        prev_mp_names   = []
        prev_callsigns_end_times = {}

        for slot_info in valid_slots:
            slot_idx  = slot_info['slot_idx']
            result    = slot_info['result']
            slot_stem = slot_info['slot_stem']
            color     = _TREE_COLORS[slot_idx % len(_TREE_COLORS)]
            cr, cg, cb = color

            ac_path   = result.get('ac_path', {})
            tree_links = result.get('tree_links', [])
            merge_pts  = result.get('merge_points', [])
            u          = result.get('u', {})

            # Earliest entry min in this slot → switch time
            all_entry = [em for _, (_, em) in ac_path.items()] if ac_path else [t0_min]
            slot_earliest_min = min(all_entry) if all_entry else t0_min
            switch_ts = _min_to_scn_time(slot_earliest_min)

            # Compute last landing time per callsign (entry_min + total travel u)
            callsign_map = result.get('callsign_map', {})
            for ac_id, (node_list, entry_min) in ac_path.items():
                cs = callsign_map.get(ac_id, f'AC{ac_id}')
                pl = len(node_list) - 1
                total_travel = sum(u.get((ac_id, pl, step), 2) for step in range(1, pl + 1))
                prev_callsigns_end_times[cs] = entry_min + total_travel

            f.write(f'\n# ── Slot {slot_idx} ({slot_stem}) at sim-time {switch_ts} ──\n')

            # Delete tree lines from previous slots whose aircraft have all landed
            if slot_idx > 0:
                still_active_names = set()
                for name, end_min in prev_callsigns_end_times.items():
                    if end_min > slot_earliest_min:
                        still_active_names.add(name)

                for name in prev_tree_names:
                    if not still_active_names:
                        f.write(f'{switch_ts}> DEL {name}\n')
                for name in prev_mp_names:
                    if not still_active_names:
                        f.write(f'{switch_ts}> DEL {name}\n')

            # Draw new tree
            this_tree_names = []
            this_mp_names   = []

            if tree_links:
                f.write(f'\n# Tree S{slot_idx}\n')
                for idx, (i, j) in enumerate(tree_links):
                    lat1, lon1 = _node_to_latlon(i)
                    lat2, lon2 = _node_to_latlon(j)
                    name = f'RTREE{slot_idx}_{idx:04d}'
                    f.write(f'{switch_ts}> POLYLINE {name} {lat1:.6f} {lon1:.6f} {lat2:.6f} {lon2:.6f}\n')
                    f.write(f'{switch_ts}> COLOR {name} {cr} {cg} {cb}\n')
                    this_tree_names.append(name)

            if merge_pts:
                f.write(f'\n# Merge points S{slot_idx}\n')
                for mp in merge_pts:
                    lat, lon = _node_to_latlon(mp)
                    name = f'RMP{slot_idx}_{mp}'
                    f.write(f'{switch_ts}> CIRCLE {name} {lat:.6f} {lon:.6f} 1\n')
                    f.write(f'{switch_ts}> COLOR {name} 255 255 0\n')
                    this_mp_names.append(name)

            prev_tree_names = this_tree_names
            prev_mp_names   = this_mp_names

        f.write('\n')
