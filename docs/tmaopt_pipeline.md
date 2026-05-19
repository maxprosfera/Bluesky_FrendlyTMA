# TMAOpt Pipeline — Technical Reference

## Overview

TMAOpt is a BlueSky simulation plugin (C5 button) that optimises arrival traffic into Stockholm Arlanda (ESSA) TMA using historical OpenSky data. It fetches real aircraft tracks via Trino, filters arriving traffic, computes physics-based CDO descent profiles, solves a Mixed-Integer Program (Gurobi) to assign each aircraft to a merge-tree route, and generates a BlueSky replay scenario showing the optimised CDO trajectories.

---

## Pipeline Architecture

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  TMAOPT  (stack command, C5 button)                                                  │
│  tma_opt.py:tmaopt()                                                                 │
│  Launches _run_tmaopt() in a background daemon thread                                │
└──────────────────────────────┬───────────────────────────────────────────────────────┘
                               │
          ┌────────────────────▼────────────────────┐
          │         PHASE 0 — DATA FETCH             │
          │  _fetch_historical(begin_ts, end_ts)     │
          │  _save_historical_csv()  → *_historical.csv │
          │  _build_aircraft_by_entry()              │
          │  → selected.txt, aircraft.csv            │
          └────────────────────┬────────────────────┘
                               │  aircraft_by_entry dict
          ┌────────────────────▼────────────────────┐
          │        PHASE 1 — CDO PRECOMPUTE          │
          │  cdo_gen._run_cdoprecompute_inline()     │
          │  ProcessPoolExecutor (one job per ac)    │
          │  _precompute_one_aircraft()              │
          │    ├─ _grid_rows_from_nodes()            │
          │    └─ _cdo_for_aircraft()  (BADA 4.2)   │
          │  → u_table[ac_idx][path_idx][edge_idx]  │
          └────────────────────┬────────────────────┘
                               │  u_table (CDO edge travel times)
          ┌────────────────────▼────────────────────┐
          │       PHASE 2 — GUROBI OPTIMISATION     │
          │  _run_optimisation()                     │
          │  Loads graph.pkl + Paths_with_max14edges.pkl │
          │  Builds Gurobi MIP (rho, X_new, tau)    │
          │  Wake turbulence separation constraints  │
          │  → result dict (ac_path, tree_links, …) │
          │  _save_scn() → *_tmaopt.scn + *_tracks.csv │
          └────────────────────┬────────────────────┘
                               │  result dict
          ┌────────────────────▼────────────────────┐
          │    PHASE 3 — CDO ON OPTIMAL ROUTES      │
          │  cdo_gen._run_cdogenopt_inline()         │
          │  Per aircraft: _grid_rows_from_nodes()  │
          │                _cdo_for_aircraft()      │
          │  → per-aircraft *_cdo_<cs>.csv          │
          │  → combined *_cdo_opt.csv               │
          │  → *_cdo_opt.scn  (auto-loaded via IC)  │
          │  → *_fuel_consumption.csv               │
          └─────────────────────────────────────────┘
```

---

## Phase 0 — Data Fetch & Aircraft Assignment

### Time Window

The plugin fetches a configurable window (default 60 min) ending at a specified UTC timestamp. `ref_unix` is the midpoint of this window and is used as the scheduling reference throughout.

```
begin_ts = end_ts - duration*60
ref_unix = (begin_ts + end_ts) / 2
```

### OpenSky Trino Fetch — `_fetch_historical()`

Queries the OpenSky Trino database for all flights in a bounding box around ESSA (default ±50 nm). For each track:

1. Finds the last waypoint **outside** the grid bounding box — this is used as the state vector snapshot (current aircraft position).
2. Calls `_grid_crossing_time()` to interpolate the exact Unix timestamp when the track first enters the grid. This is stored as `crossing_time` and is used as the scheduled arrival time `ta1` in Gurobi.

### Arrival Filtering — `_build_aircraft_by_entry()`

Each aircraft in the raw state vectors is accepted only if **all** of the following hold:

| Filter | Threshold |
|--------|-----------|
| Not on ground | — |
| Baro altitude | > 50 m |
| Track enters TMA polygon | boundary crossing or point inside |
| Not GA / helicopter | checked via ICAO 24-bit hex → type code cache |
| Has airborne waypoints | — |
| Descending in second half of track | ≥ 500 m drop |
| Heading toward ESSA at midpoint | within ±100° of bearing to ESSA |
| Maximum altitude | ≤ 12,000 m |
| Vertical rate | ≤ +2.0 m/s (not climbing) |

After filtering, each aircraft is assigned to the nearest of the four TMA entry nodes (N, W, E, S) using Haversine distance.

### Entry Nodes

```
N → grid node  9  (60.2646°N, 18.6409°E)
W → grid node 45  (59.8464°N, 16.7003°E)
E → grid node 66  (59.7419°N, 19.1261°E)
S → grid node 160 (58.8010°N, 17.9132°E)
```

### Output Files

| File | Contents |
|------|----------|
| `*_historical.csv` | All raw waypoints from Trino (all traffic categories) |
| `aircraft.csv` | One row per selected arriving aircraft with entry assignment |
| `selected.txt` | One callsign per line — used by the traces plugin (mode 3) |

---

## Grid

The FrendlyTMA grid covers Stockholm TMA with **165 nodes** in a 15×11 regular lattice:

```
Latitude range:  58.801° – 60.265° N   (15 rows,  ~0.104° spacing ≈ 6.3 NM)
Longitude range: 16.700° – 19.126° E   (11 cols,  ~0.243° spacing ≈ 7.2 NM)

Node numbering: left-to-right, top-to-bottom
  Node 1   = top-left    (60.265°N, 16.700°E)
  Node 11  = top-right   (60.265°N, 19.126°E)
  Node 165 = bottom-right (58.801°N, 19.126°E)

Special nodes:
  Node 72  = ESSA runway threshold (59.637°N, 17.913°E) — N_exit
```

Pre-computed grid topology (edges and all possible routes) is loaded from:
- `FrendlyTMA/Code/may16-2018-graph.pkl` — graph edges, node sets, crossing-pair tables
- `FrendlyTMA/Code/Paths_with_max14edges.pkl` — 3821 possible routes (≤ 14 edges each)

---

## Phase 1 — CDO Precompute

### Purpose

For every aircraft × every possible grid path (up to 3821), compute the **CDO-derived edge travel time** in minutes. These replace the default constant speed estimate in the Gurobi model, giving physics-accurate separation constraints.

### Parallelism

Runs via `concurrent.futures.ProcessPoolExecutor` with `min(cpu_count, n_aircraft)` workers. Each worker handles one aircraft across all 3821 paths.

### Per-aircraft, per-path logic — `_precompute_one_aircraft()`

```
For each path (node_list):
  1. _grid_rows_from_nodes()   → synthetic horizontal track along grid nodes
  2. _cdo_for_aircraft()       → physics-based descent profile (BADA 4.2)
  3. Bin CDO output by cumulative distance into node segments
  4. u_dict[path_idx] = [edge_time_min, edge_time_min, ...]
```

### Output

```python
u_table[ac_idx][path_idx] = [t_edge1_min, t_edge2_min, ...]   # 0-based
fuel_table[ac_idx][path_idx] = total_cdo_fuel_kg
```

Remapped to 1-based IDs before passing to Gurobi:
```python
u_override = {ac_idx + 1: paths for ac_idx, paths in u_cdo.items()}
```

---

## Phase 2 — Gurobi MIP Optimisation

### Decision Variables

| Variable | Type | Size | Meaning |
|----------|------|------|---------|
| `rho[k]` | Binary | `n_paths` | 1 if path `k` is selected in the merge tree |
| `X_new[i,j]` | Binary | `n_links` | 1 if edge `(i,j)` is used in the merge tree |
| `tau[a,k,t]` | Binary | `n_ac × n_paths × epsilon_window` | 1 if aircraft `a` is assigned path `k` with entry offset `t` |

### Objective

```
minimize:  α · Σ X_new[i,j] · length[i,j]
         + (1-α) · Σ |AC[B[i]]| · length[i,j] · rho[k]

α = 0.1
```

Minimises weighted tree edge length (infrastructure cost, 10%) plus traffic-weighted path length (route efficiency, 90%).

### Constraints

| # | Constraint | Purpose |
|---|------------|---------|
| 1 | `rho[k] ≤ X_new[i,j]` ∀ edge (i,j) on path k | Active paths must use tree edges |
| 2 | In-degree ≤ 2 per node | At most one merge point per node |
| 3 | Out-degree ≤ 1 per node | Tree structure (no divergence) |
| 4 | `Σ rho[k] = 1` per entry | Each entry has exactly one active path |
| 5 | No path crossings | 4 groups of crossing-pair constraints on `X_new` |
| 6 | `Σ_t tau[a,k,t] = rho[k]` | Aircraft assigned to path only if path is used |
| 7 | `Σ_{k,t} tau[a,k,t] = 1` | Each aircraft gets exactly one (path, entry time) |
| 8 | Wake turbulence separation (4 types) | H→H: s1 min, M→M: s1 min, H→M: s1 min, M→H: s2 min |

### Wake Separation Parameters

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `s1` | 2 min | Heavy/Medium–Heavy/Medium separation |
| `s2` | 3 min | Heavy/Medium–Light and Light–Light separation |

Heavy types include: B744, B748, B77W, B788, B789, A332, A333, A359, A388, A124, C5, AN12, and others.

### Epsilon (Flexibility Window)

Each aircraft's entry time `ta1` (from `crossing_time`) has a flexibility window `±epsilon` minutes. The optimizer tries `epsilon = [0, 2, 3, ..., max_eps]` in sequence and stops at the first feasible solution.

### Gurobi Parameters

| Parameter | Value |
|-----------|-------|
| `Threads` | `min(8, cpu_count)` |
| `MIPGap` | 0.01 (1%) |
| `MIPFocus` | 1 (feasibility focus) |
| `Heuristics` | 0.3 |
| `TimeLimit` | `time_limit_per_eps` per epsilon (default 120 s) |

### Result Dict Keys

| Key | Type | Contents |
|-----|------|----------|
| `feasible` | bool | Whether a feasible solution was found |
| `objective` | float | Objective function value |
| `ac_path` | dict | `ac_id → (node_list, entry_time_min)` |
| `callsign_map` | dict | `ac_id → callsign string` |
| `tree_links` | list | `[(i,j), ...]` active merge-tree edges |
| `merge_points` | list | Node IDs where two paths merge |
| `u` | dict | `(ac_id, path_len, step) → edge_time_min` |
| `ta1` | dict | `(node, ac_id) → scheduled entry time (min from midnight)` |

### Output Files

| File | Contents |
|------|----------|
| `result.pkl` | Full result dict |
| `*_tracks.csv` | Per-node waypoints for each aircraft on its optimal path |
| `*_tmaopt.scn` | BlueSky scenario: grid + TMA polygon + tree (cyan) + merge circles (yellow) + `STARTREPLAY` |

---

## Phase 3 — CDO Profile Generation on Optimal Routes

### Purpose

For each aircraft, generate a physics-accurate CDO vertical profile along its Gurobi-assigned route, preserving the exact node arrival times from the optimiser schedule.

### Node Timestamp Anchoring

Gurobi output `u[ac_id, path_len, step]` gives the scheduled edge travel time in minutes. These are converted to per-node Unix timestamps:

```
node_unix_times[0] = entry_unix  (from entry_time_min + ref_midnight)
node_unix_times[i] = node_unix_times[i-1] + u[ac_id, path_len, i] * 60
```

These timestamps are passed to `_grid_rows_from_nodes()` so that the CDO CSV replay exactly preserves the separations enforced by Gurobi.

### Horizontal Track — `_grid_rows_from_nodes()`

Converts a sequence of grid node IDs into a list of track row dicts:

- **Timestamps**: from `node_unix_times` if provided (Gurobi schedule), otherwise derived from constant `speed_ms`
- **Altitude**: linearly interpolated from `entry_alt_m` (from `alt_m` in aircraft.csv) at node 0 down to `_CDO_FAP_ALT_M = 609.6 m` (2000 ft) at the final path node
- **Runway node** (node 72) appended at ground level (`baro_alt_m = 0`, `on_ground = True`)

### CDO Backward Integration — `_cdo_for_aircraft()` (BADA 4.2)

Implements the MATLAB `CDO_profile_predictor` algorithm:

```
Starting conditions: FAP altitude (609.6 m), aircraft mass = MLW × mlw_factor

Speed schedule (7 bands, anchored at FAP Vstall):
  Band 1–4: Vmin = C_V_MIN × Vstall + VD_DES[i]    (low-altitude stall margin bands)
  Band 5:   min(CAS_start, 220 kt)                   (below FL060)
  Band 6:   min(CAS_start, 250 kt)                   (below FL100)
  Band 7:   CAS_start                                (high-altitude CAS hold)
  Above:    Mach hold at M_descent

Backward integration (1-second steps, max 7200 steps):
  ├─ ISA atmospheric conditions at (alt, lat, lon) + ERA5 wind if available
  ├─ _cdo_speed_step() → CAS target for current altitude band
  ├─ BADA 4.2: CL, CD, Drag (parabolic polar)
  ├─ CT_idle → Thr_idle → CF_idle → fuel flow
  ├─ ROCD (energy-share method)
  ├─ alt += |ROCD| × dt     (climbing backwards = descending forward)
  └─ cum_dist += GS × dt    (stop when cum_dist ≥ TMA_dist_nm)

Reversal: backward sequence is flipped to produce forward-time profile
Lat/lon: interpolated along the original horizontal grid track
```

Output: per-second rows with `lat, lon, baro_alt_m, cas_ms, mach, velocity_ms, vertical_rate_ms, fuel_flow_kg_s, cum_fuel_kg`.

### Output Files

| File | Contents |
|------|----------|
| `*_cdo_<callsign>.csv` | Per-aircraft CDO trajectory (per-second, full columns) |
| `*_cdo_opt.csv` | All aircraft combined, sorted by time — fed to `STARTREPLAY` |
| `*_cdo_opt_summary.csv` | Per-aircraft fuel saving summary |
| `*_fuel_consumption.csv` | `callsign, orig_fuel_kg, cdo_fuel_kg, saving_kg, saving_pct, duration_s, distance_nm` |
| `*_cdo_opt.scn` | BlueSky scenario: TMA polygon + grid + tree + `STARTREPLAY` pointing to combined CSV |

The `_cdo_opt.scn` is automatically loaded via `IC <absolute_path>` at the end of Phase 3.

---

## Replay & Display

### `STARTREPLAY` — opensky_replay_player.py

Loads `*_cdo_opt.csv` into memory. On every simulation step (`dt=0`), interpolates each aircraft's position between 1-second CDO snapshots and injects directly into `bs.traf` arrays (bypasses autopilot). Aircraft are created via `bs.traf.cre()` when they first appear, deleted 30 s after their last snapshot.

### `TRACETOGGLE` (C2) — opensky_traces.py

Draws historical POLYLINE traces from `*_historical.csv`. Cycles through 5 modes:

| Mode | Displayed | Colour |
|------|-----------|--------|
| 0 | All traffic (arrivals + departures + enroute) | green/yellow |
| 1 | Arrivals only | green |
| 2 | Departures only | yellow |
| 3 | Optimised aircraft only (from `selected.txt`) | cyan |
| 4 | No traces, aircraft symbols visible | — |

Classification uses arrival/departure heuristics based on proximity to ESSA and altitude profile of the track.

---

## Output Directory Structure

```
scenario/TMAOpt/tmaopt_<YYYYMMDD_HHMMSS>/
├── tmaopt_<ts>_historical.csv         ← all raw tracks from Trino
├── aircraft.csv                        ← selected arrivals with entry assignment
├── selected.txt                        ← callsign list for traces plugin
├── result.pkl                          ← full Gurobi result dict
├── tmaopt_<ts>.scn                     ← Gurobi tree scenario
├── tmaopt_<ts>_tracks.csv              ← per-node waypoints (for tree replay)
├── tmaopt_<ts>_cdo_opt.scn             ← CDO scenario (auto-loaded)
├── tmaopt_<ts>_cdo_opt.csv             ← combined CDO trajectories
├── tmaopt_<ts>_cdo_opt_summary.csv     ← fuel summary table
├── tmaopt_<ts>_fuel_consumption.csv    ← fuel saving CSV
└── tmaopt_<ts>_cdo_<callsign>.csv      ← per-aircraft CDO profile (one per ac)
```

---

## Key Parameters Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `dtstr` | now | UTC end of fetch window (`YYYY-MM-DDTHH:MM`) |
| `duration` | 60 | Fetch window length in minutes |
| `entries` | `NESW` | Active entry nodes |
| `max_ac` | 15 | Global aircraft cap |
| `max_ac_per_entry` | 5 | Per-entry cap |
| `max_eps` | 3 | Maximum flexibility window (minutes) |
| `time_limit_per_eps` | 120 | Gurobi time limit per epsilon (seconds) |
| `s1` | 2 | H–M and H–L wake separation (minutes) |
| `s2` | 3 | M–L wake separation (minutes) |
| `fetch_radius` | 50 | Bounding box half-size (nm) |
| `cdo_fap_alt` | 2000 | Final Approach Point altitude (ft) |
| `cdo_ias_start` | 200 | Initial CAS at TMA entry (kt) |
| `cdo_ias_restrict` | 220 | Speed restriction altitude band (kt) |
| `cdo_mach` | 0.84 | Mach hold value above crossover altitude |
| `cdo_mlw` | 0.9 | Mass factor (fraction of MLW) |
| `cdo_kt_per_sec` | 1.0 | Deceleration rate (kt/s) |
| `cdo_wind` | 1 | Use ERA5 wind data (1=yes, 0=no) |
| `cdo_c_v_min` | 1.23 | Stall margin factor |

---

## Inter-module Dependencies

```
tma_opt.py
  └── uses cdo_gen._run_cdoprecompute_inline()    (Phase 1)
  └── uses cdo_gen._run_cdogenopt_inline()         (Phase 3)
  └── exports _build_aircraft_by_entry()           (used by tma_rolling.py)
  └── exports _run_optimisation()                  (used by tma_rolling.py)
  └── exports _get_all_paths()                     (used by tma_rolling.py)
  └── exports _write_grid()                        (used by cdo_gen.py, tma_rolling.py)

cdo_gen.py
  └── imports _GRID_COORDS, _haversine_nm, _bearing from tma_opt  (at runtime)
  └── imports _write_grid from tma_opt              (for SCN writing)

opensky_traces.py
  └── reads selected.txt written by tma_opt._run_tmaopt()
  └── reads *_historical.csv written by tma_opt._save_historical_csv()

opensky_replay_player.py
  └── reads *_cdo_opt.csv written by cdo_gen._run_cdogenopt_inline()

tma_rolling.py
  └── imports _build_aircraft_by_entry, _run_optimisation,
              _get_all_paths, _write_grid from tma_opt
  └── imports _run_cdoprecompute_inline, _run_cdogenopt_inline from cdo_gen
```
