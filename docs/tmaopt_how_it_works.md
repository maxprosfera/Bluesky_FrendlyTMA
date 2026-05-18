# How TMAOpt Works — Full Technical Description

---

## Trigger

The user presses **C5** (or types `TMAOPT`). The plugin spawns a background daemon thread running `_run_tmaopt()` so BlueSky remains responsive. The command accepts:

```
TMAOPT [datetime] [duration_min] [entries] [max_ac] [max_ac_per_entry] [max_eps] [TL_s] [s1] [s2] [radius_nm]
```

---

## Step 0 — Time Window

```
begin_ts = end_ts - duration × 60
ref_unix = midpoint of window  (or "now" if no datetime given)
midnight = UTC midnight of ref_unix day
```

All times inside the model are expressed in **minutes since midnight** of that UTC day.

---

## Step 1 — Data Fetch (`_fetch_historical`)

**Input:** bounding box ±`radius_nm` around ESSA (`59.6373°N, 17.9132°E`).

**Query:** OpenSky Trino database via `OpenSkyFetcher.fetch_area_flights_trino(begin_ts, end_ts, bbox)`. Returns full per-second waypoint tracks for every flight seen in the box during the window.

**Per-track processing:**
- Drop on-ground and below 300 m waypoints
- Find the **last waypoint outside the grid bounding box** — used as the aircraft's state vector snapshot (position, speed, heading, altitude at that moment)
- Call `_grid_crossing_time(wps)` — scan consecutive waypoint pairs for the first outside→inside grid transition and **linearly interpolate** the exact Unix timestamp of that boundary crossing → stored as `crossing_time`

**Output:** `raw_ac` (one dict per aircraft, state vector) + `raw_tracks` (full waypoints, saved to `*_historical.csv`)

---

## Step 2 — Arrival Filtering (`_build_aircraft_by_entry`)

**Input:** `raw_ac`, full `track_waypoints`, `entries` mask, caps.

Each aircraft passes **9 sequential filters**:

| # | Filter | Threshold |
|---|--------|-----------|
| 1 | Not on ground | — |
| 2 | Altitude | > 50 m |
| 3 | No duplicate callsign | — |
| 4 | Crosses TMA polygon OR inside it | `_crosses_tma_boundary` or `_point_in_tma` |
| 5 | Not GA/helicopter | `_is_ga_icao24()` via actype cache |
| 6 | Has airborne waypoints | — |
| 7 | Descending in second half of track | altitude drop ≥ 500 m |
| 8 | Heading toward ESSA at midpoint | within ±100° of bearing to ESSA |
| 9 | Max altitude / vertical rate | ≤ 12,000 m, vertical rate ≤ +2.0 m/s |

**Entry assignment:** each surviving aircraft is assigned to the nearest of the 4 entry nodes (N=9, W=45, E=66, S=160) by Haversine distance.

**Capping:** sort by `dist_to_entry_nm`, keep closest `max_ac_per_entry` per node, then global cap to `max_ac` closest overall.

**Entry time:** for each aircraft:
- If `crossing_time` available → convert to minutes since midnight → `ta1`
- Else → estimate ETA from current position + speed to entry node

**Output:** `aircraft_by_entry = {'N': [...], 'W': [...], 'E': [...], 'S': [...]}`

---

## Step 3 — Save `aircraft.csv` + `selected.txt`

Written to `scenario/TMAOpt/tmaopt_<ts>/`:
- `aircraft.csv` — one row per aircraft with all state fields
- `selected.txt` — callsign list used by `TRACETOGGLE` mode 3 to highlight these aircraft

---

## Phase 1 — CDO Precompute (`_run_cdoprecompute_inline`)

**Purpose:** compute physics-accurate edge travel times for every aircraft on every possible grid path (3821 paths). These replace the constant speed estimate in Gurobi.

**Parallelism:** `ProcessPoolExecutor` with `min(cpu_count, n_ac)` workers. Each worker handles one aircraft — `_precompute_one_aircraft(args)`.

### Per aircraft × per path (`_precompute_one_aircraft`):

1. **`_grid_rows_from_nodes(node_list, entry_unix, speed_ms, entry_alt_m)`**
   - Builds a synthetic horizontal track along the grid nodes
   - Altitude: linearly interpolated from `entry_alt_m` (aircraft's observed altitude) at node 0 down to `_CDO_FAP_ALT_M = 762 m` (2500 ft) at the last node
   - Timestamps: speed-based (`entry_unix + cumulative distance / speed_ms`)
   - `entry_unix` = aircraft's real `crossing_time` (Unix) so ERA5 wind is sampled at the correct absolute time
   - Appends node 72 (FAF, `59.5114°N, 17.9132°E`) at 762 m as the final waypoint

2. **`_cdo_for_aircraft(rows, bada, weather)`** — BADA 4.2 backward integration (see below)

3. **Bin CDO output by cumulative distance into node segments:**
   - For each grid edge `[node_i-1 → node_i]`: find CDO points within that distance range, compute `edge_time_min = round((t_last - t_first) / 60)`
   - Minimum edge time clamped to `s1` minutes

**Output:** `u_table[ac_idx][path_idx] = [edge_time_min, ...]`

---

## CDO Integration — `_cdo_for_aircraft` (BADA 4.2)

This is the core physics engine, a translation of MATLAB's `CDO_profile_predictor`.

### Speed schedule (7 bands, anchored at FAP stall speed)

```
Band 1: C_V_MIN × Vstall + 5 kt   ← lowest, near FAP
Band 2: C_V_MIN × Vstall + 10 kt
Band 3: C_V_MIN × Vstall + 10 kt
Band 4: C_V_MIN × Vstall + 10 kt
Band 5: min(CAS_entry, 220 kt)    ← below FL060
Band 6: min(CAS_entry, 250 kt)    ← below FL100
Band 7: CAS_entry                 ← high-altitude CAS hold
Above:  Mach_descent hold         ← above crossover altitude
```

### Backward integration (1-second steps, max 7200 steps)

Starting from FAP altitude (762 m) and going **backwards in time** (= climbing in altitude):

```
For each second:
  1. ISA conditions at current altitude + ERA5 wind if available
  2. _cdo_speed_step() → target CAS for current altitude band
  3. BADA 4.2 aerodynamics: CL, CD, Drag (parabolic polar)
  4. CT_idle → Thr_idle → CF_idle → fuel flow (FF kg/s)
  5. ROCD = f(Thr_idle, Drag, TAS, mass, Mach)  [negative = descending]
  6. alt  -= rocd × 1s   (backward: alt increases)
  7. cum_dist += GS × 1s × M_TO_NM
  8. Stop when cum_dist ≥ TMA_dist_nm
```

### Reversal

Backward sequence is flipped → forward CDO profile. Lat/lon interpolated along the original horizontal track by cumulative distance.

**Output:** per-second rows with `lat, lon, baro_alt_m, CAS_ms, Mach, GS_ms, ROCD_ms, fuel_flow_kg_s, cum_fuel_kg, cum_dist_nm`

---

## Phase 2 — Gurobi MIP (`_run_optimisation`)

### Inputs

- `aircraft_by_entry` — selected aircraft with `ta1` (entry time, min since midnight)
- `u_table` — CDO edge travel times from Phase 1
- `epsilon`, `s1`, `s2` — flexibility and separation parameters
- Grid pkl files: 165 nodes, 1133 directed edges, 3821 paths

### Decision Variables

| Variable | Type | Size | Meaning |
|----------|------|------|---------|
| `rho[k]` | Binary | 3821 | 1 if path `k` is in the merge tree |
| `X_new[i,j]` | Binary | 1133 | 1 if edge `(i,j)` is used in the merge tree |
| `tau[a,k,t]` | Binary | n_ac × paths × (2ε+1) | 1 if aircraft `a` uses path `k` entering at time `t` |

### Objective (minimise)

```
0.1 × Σ X_new[i,j] × length[i,j]                     ← tree edge length (infrastructure cost)
+ 0.9 × Σ |AC[entry]| × length[i,j] × rho[k]         ← traffic-weighted path length
```

### Key Constraints

1. **Tree structure:** active paths use only tree edges; in-degree ≤ 2 per node; out-degree ≤ 1 (except N_exit)
2. **Exactly one path per entry node:** `Σ rho[k] = 1` per entry with aircraft
3. **No crossing paths:** 4 groups of crossing-pair constraints on `X_new`
4. **Each aircraft gets exactly one (path, time):** `Σ_{k,t} tau[a,k,t] = 1`
5. **Wake turbulence separation** at every node, every time step:

| Pair | Min separation |
|------|---------------|
| Heavy → Medium | s1 min |
| Medium → Light | s2 min |
| Heavy → Light  | s1 min |
| Medium → Medium | s1 min |

### Epsilon Loop

The solver tries `epsilon = [0, 2, 3, ..., max_eps]` in order, stopping at the first feasible solution. With `epsilon=0` aircraft must enter at exactly `ta1`; with larger values they may shift ±epsilon minutes.

### Gurobi Parameters

| Parameter | Value |
|-----------|-------|
| Threads | `min(8, cpu_count)` |
| MIPGap | 0.01 (1%) |
| MIPFocus | 1 (feasibility) |
| Heuristics | 0.3 |
| TimeLimit | `time_limit_per_eps` per epsilon |

### Output

- `ac_path[ac_id] = (node_list, actual_entry_time_min)` — optimal route + assigned time
- `tree_links` — active merge tree edges for display
- `merge_points` — nodes where two paths merge
- `u[ac_id, path_len, step]` — edge times used in the model

---

## Phase 3 — CDO on Optimal Routes (`_run_cdogenopt_inline`)

For each aircraft in `ac_path`:

1. **Compute `entry_unix`** from the Gurobi-assigned entry time:
   ```
   entry_unix = midnight + entry_min × 60
   ```
   This is the only timestamp anchor passed to CDO. All subsequent node arrival times are determined by BADA physics + ERA5 wind, not by the Gurobi schedule.

2. **`_grid_rows_from_nodes(node_list, entry_unix, speed_ms, entry_alt_m)`** — horizontal track starting at `entry_unix`, timestamps speed-based (same as Phase 1). ERA5 wind sampled at correct absolute times, consistent with Phase 1.

3. **`_cdo_for_aircraft(rows, bada, weather)`** — pure physics integration. Timestamps in the output CSV come from BADA integration, not from Gurobi `u` values.

4. Save per-aircraft `*_cdo_<callsign>.csv`

5. Merge all → `*_cdo_opt.csv` (sorted by time)

6. Compute fuel savings: `orig_fuel` (conventional approach) vs `cdo_fuel`

7. Write `*_fuel_consumption.csv` and `*_cdo_opt.scn`

8. **Auto-load** the CDO scenario via `IC <absolute_path>`

---

## Output Files

```
scenario/TMAOpt/tmaopt_<YYYYMMDD_HHMMSS>/
├── tmaopt_<ts>_historical.csv       ← raw Trino tracks (all traffic)
├── aircraft.csv                      ← selected arrivals with entry assignment
├── selected.txt                      ← callsigns for TRACETOGGLE mode 3
├── result.pkl                        ← full Gurobi result dict
├── tmaopt_<ts>.scn                   ← tree scenario (Gurobi output, grid + tree)
├── tmaopt_<ts>_tracks.csv            ← per-node waypoints for tree replay
├── tmaopt_<ts>_cdo_opt.scn           ← CDO scenario (auto-loaded)
├── tmaopt_<ts>_cdo_opt.csv           ← all CDO trajectories combined
├── tmaopt_<ts>_cdo_opt_summary.csv   ← per-aircraft fuel summary
├── tmaopt_<ts>_fuel_consumption.csv  ← callsign, orig_kg, cdo_kg, saving_kg, pct
└── tmaopt_<ts>_cdo_<callsign>.csv    ← per-aircraft CDO profile (one per ac)
```

---

## Complete Data Flow Diagram

```
OpenSky Trino
    │  full waypoint tracks (begin_ts → end_ts)
    ▼
_fetch_historical()
    │  raw_ac: state vectors (lat, lon, alt, spd, hdg)
    │  crossing_time: interpolated grid boundary crossing Unix timestamp
    ▼
_build_aircraft_by_entry()
    │  9 arrival filters
    │  nearest entry node assignment (N/W/E/S)
    │  ta1 = crossing_time → minutes since midnight
    │  cap: max_ac_per_entry, max_ac
    ▼
aircraft_by_entry dict
    │
    ├─────────────────────────────────────────────┐
    │                                             │
    ▼  (Phase 1, parallel workers)                ▼  (Phase 2)
_precompute_one_aircraft()                   _run_optimisation()
    │  for each ac × 3821 paths:                  │  loads graph.pkl + paths.pkl
    │    ref_time = ac.crossing_time              │  builds ta1, u, wake cats
    │    _grid_rows_from_nodes(ref_time)           │  Gurobi MIP:
    │    _cdo_for_aircraft() BADA 4.2             │    rho, X_new, tau
    │    → edge_times [min/edge]                  │    objective: tree length
    │                                             │    constraints: tree structure,
    ▼                                             │    no crossings, wake sep
u_table[ac_idx][path_idx] = [times]              │
    │                                             │
    └────────────────────────────────────────►  Gurobi.optimize()
                                                  │
                                                  ▼
                                             result dict:
                                               ac_path[id] = (node_list, entry_min)
                                               tree_links, merge_points, u
                                                  │
                                                  ▼  (Phase 3)
                                        _run_cdogenopt_inline()
                                             for each ac:
                                               entry_unix = midnight + entry_min*60
                                               _grid_rows_from_nodes(entry_unix)
                                               _cdo_for_aircraft() BADA 4.2 (pure physics)
                                               → per-second CDO profile (physics timestamps)
                                                  │
                                                  ▼
                                        *_cdo_opt.csv  (all ac, sorted by time)
                                        *_cdo_opt.scn  (auto-loaded via IC)
                                        *_fuel_consumption.csv
```
