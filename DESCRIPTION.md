# FrendlyTMA — System Description

## What it does

FrendlyTMA is a research tool that takes a 1-hour window of real arriving air traffic at Stockholm Arlanda (ESSA), computes fuel-optimal Continuous Descent Operation (CDO) vertical profiles for each aircraft, feeds those profiles into a network-based route optimizer, and visualizes the result.

The full pipeline runs in three phases triggered by a single button click:

```
OpenSky historical data
        ↓
Phase 1 — CDO precompute
  For every aircraft × every possible route in the TMA grid,
  compute the ideal continuous-descent vertical profile using
  BADA 4.2 aerodynamic performance data.
        ↓
Phase 2 — Gurobi route optimisation
  Use the CDO-derived travel times as input to a Mixed-Integer
  Program that selects one collision-free route per aircraft,
  minimising total delay subject to wake-turbulence separation.
        ↓
Phase 3 — CDO on optimal routes
  Generate the final CDO vertical profile for each aircraft
  on its chosen optimal route. Save CSV data and figures.
```

---

## Input

The tool queries the **OpenSky Network Trino API** for historical state vectors (position, altitude, speed, heading) of all aircraft within 200 nm of ESSA during a chosen 1-hour window. No manual data preparation is needed — the user clicks a button and specifies a date/time.

Alternatively, a previously saved `_historical.csv` file can be loaded directly (offline mode).

---

## The TMA grid

The Terminal Manoeuvring Area (TMA) around ESSA is represented as a **directed graph** with:

- **165 nodes** spaced ~0.12° lat × 0.25° lon apart
- **1 133 edges** (directed links between adjacent nodes)
- **3 821 feasible paths** from each of 4 entry nodes to the runway exit node
- **4 entry nodes**: North (node 9), West (node 45), East (node 66), South (node 160)

Each aircraft enters the grid at one of the 4 entry nodes and must reach the runway exit. The optimizer selects which of the 3 821 paths to assign each aircraft.

---

## CDO vertical profiles

For each aircraft on each candidate path, the tool integrates the BADA 4.2 flight performance equations backward from the Final Approach Point (FAP, ~2 500 ft / 762 m) to the aircraft's observed entry altitude. This gives:

- Altitude profile (m) vs. distance flown (nm)
- Calibrated airspeed (CAS) and Mach number at each point
- Fuel flow (kg/s) and cumulative fuel burn (kg)
- Rate of climb/descent (m/s)

The CDO profile represents the theoretically minimum-fuel descent — engine at idle, no level-flight segments.

**Supported aircraft types:** all jets and turbofans in BADA 4.2 (64 ICAO type codes). Turboprops are detected and skipped automatically.

---

## Optimisation model

The optimizer (Gurobi MIP) assigns exactly one path to each aircraft such that:

1. **Wake turbulence separation** is maintained between all pairs of aircraft that share any edge (Heavy/Medium ≥ 96 s, Medium/Light ≥ 120 s, Heavy/Light ≥ 120 s).
2. **Merge point spacing** is enforced where paths converge.
3. **Total delay** (sum of extra edge travel time over minimum CDO time) is minimised.

The model is solved with an epsilon-feasibility relaxation: it tries epsilon = 0 first (strict), then 1 and 2 minutes of slack if needed.

---

## Outputs

All outputs are saved to `scenario/TMAOpt/tmaopt_YYYYMMDD_HHMMSS/`:

| File | Contents |
|---|---|
| `*_historical.csv` | Raw OpenSky state vectors for all arriving aircraft |
| `aircraft.csv` | One row per aircraft: entry node, position, speed, altitude at TMA entry |
| `*.scn` | BlueSky scenario file — loads the optimised routes for replay |
| `*_cdo_opt.csv` | CDO vertical profile for each aircraft on its optimal route |
| `*_cdo_opt_summary.csv` | Per-aircraft fuel saving: CDO vs. original trajectory |
| `*_cdo_opt.scn` | BlueSky scenario file for CDO profile replay |
| `Figures/` | Per-aircraft altitude, speed, and fuel profile plots (fig1/2/3) |
| `result.pkl` | Full optimisation result (Python pickle) |

---

## Figures

Three figures are generated per aircraft:

- **Fig 1** — Altitude (m) vs. distance to runway (nm): CDO profile vs. actual observed trajectory
- **Fig 2** — Speed (CAS kt, Mach) vs. distance: CDO speed schedule
- **Fig 3** — Cumulative fuel burn (kg) vs. distance: CDO savings vs. original

A separate **tree figure** (`*_tree.png`) shows the full TMA grid in grey with the optimal tree (selected routes for all aircraft) overlaid in black on a basemap.

---

## Requirements

| Dependency | Purpose |
|---|---|
| Python 3.10–3.14 | Runtime |
| Gurobi 11+ with licence | MIP solver (free academic licence available) |
| BADA 4.2 data | Aircraft performance (EUROCONTROL licence required) |
| OpenSky Network credentials | Historical data API (free registration) |
| FrendlyTMA grid code | TMA graph definition and path enumeration |

Python packages: `numpy`, `scipy`, `matplotlib`, `pandas`, `PyQt6`, `gurobipy`, `requests`, `shapely`, `contextily`, `pyproj`.

---

## How to run

1. Start BlueSky: `python3 BlueSky.py`
2. Click **TMAOpt** in the toolbar, enter a date/time (UTC), click OK.
3. The pipeline runs automatically (~2–5 min for a typical 5-aircraft scenario).
4. Results appear in `scenario/TMAOpt/tmaopt_<datetime>/`.
5. Load the scenario in BlueSky: `IC scenario/TMAOpt/tmaopt_.../tmaopt_...cdo_opt.scn`

### Running from an existing saved scenario (offline mode)

If you already have a `_historical.csv` from a previous TMAOpt run or an OSHist recording, you can re-run the full pipeline without fetching from OpenSky. This is useful for reproducing results, testing with a known scenario, or sharing data with a colleague.

**Step 1 — Locate the historical CSV**

Every TMAOpt run saves a file named `tmaopt_YYYYMMDD_HHMMSS_historical.csv` inside its output folder:

```
scenario/TMAOpt/tmaopt_20260512_083800/tmaopt_20260512_083800_historical.csv
```

OSHist recordings saved from the OSHist button are stored in:

```
scenario/OpenSky/<stem>_tracks.csv
```

**Step 2 — Type the command in the BlueSky console**

In the BlueSky console (the text input at the bottom of the screen), type:

```
TMAOPTFILE tmaopt_20260512_083800_historical.csv
```

The filename can be given in three ways — the tool searches automatically:

| What you type | Where it looks |
|---|---|
| Just the filename | `scenario/OpenSky/` then `scenario/TMAOpt/` |
| A stem without extension | All subdirectories of `scenario/TMAOpt/`, tries `_historical.csv`, `_tracks.csv`, `.csv` |
| A full absolute path | Used directly |

**Step 3 — Wait for the pipeline to complete**

The same three-phase pipeline runs as in live mode (CDO precompute → Gurobi → CDO on optimal routes). Progress messages appear in the BlueSky console. For a 5-aircraft scenario this typically takes 2–5 minutes.

**Step 4 — Inspect the results**

A new timestamped output folder is created alongside the input file:

```
scenario/TMAOpt/tmaopt_20260512_083800/
    tmaopt_20260512_083800_cdo_opt.csv        ← CDO profiles on optimal routes
    tmaopt_20260512_083800_cdo_opt_summary.csv ← fuel savings per aircraft
    tmaopt_20260512_083800_cdo_opt.scn        ← BlueSky scenario for replay
    Figures/
        tmaopt_20260512_083800_NSZ8GH_B38M_fig1.png
        ...
```

To replay the optimised CDO scenario in BlueSky:

```
IC scenario/TMAOpt/tmaopt_20260512_083800/tmaopt_20260512_083800_cdo_opt.scn
```

**Optional parameters**

The command accepts optional arguments to control which entry points and how many aircraft are considered:

```
TMAOPTFILE <filename> [entries] [max_ac] [max_ac_per_entry] [max_eps] [time_limit_s] [s1] [s2]
```

| Parameter | Default | Description |
|---|---|---|
| `entries` | `NESW` | Which cardinal entry points to use (any combination of N, E, S, W) |
| `max_ac` | `15` | Maximum total aircraft to include in the optimisation |
| `max_ac_per_entry` | `5` | Maximum aircraft per entry node |
| `max_eps` | `3` | Maximum delay slack in minutes before declaring infeasible |
| `time_limit_s` | `300` | Gurobi solver time limit per epsilon attempt (seconds) |
| `s1` | `2` | Minimum separation multiplier for Medium/Medium pairs |
| `s2` | `3` | Minimum separation multiplier for Heavy/Light pairs |

Example — run with only North and South entries, up to 4 aircraft total:

```
TMAOPTFILE tmaopt_20260512_083800_historical.csv NS 4
```
