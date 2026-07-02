# FrendlyTMA — BlueSky Plugin Suite

Research tool for TMA arrival optimisation, CDO fuel savings analysis, and OpenSky historical replay, built on top of the [BlueSky ATC Simulator](https://github.com/TUDelft-CNS-ATM/bluesky).

Targets ESSA Stockholm Arlanda. Requires a Gurobi licence and BADA 4.2 data.

---

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10 – 3.14 | 3.14 recommended |
| Gurobi | 11+ | Academic licence available free at gurobi.com |
| BADA 4.2 | — | Licence required from EUROCONTROL; XML files go in `Data/BADA/BADA_4.2/` |
| FrendlyTMA code | — | Sibling repo at `../FrendlyTMA/Code` |
| OpenSky credentials | — | OAuth2 client id + secret from opensky-network.org |

---

## Installation — macOS

### 1. Clone the repository

```bash
git clone https://github.com/your-org/Bluesky_FrendlyTMA.git
cd Bluesky_FrendlyTMA
```

### 2. Clone the FrendlyTMA optimisation code (sibling directory)

The plugin expects this at `../FrendlyTMA/Code` relative to the repo root.

```bash
cd ..
git clone https://github.com/your-org/FrendlyTMA.git
cd Bluesky_FrendlyTMA
```

### 3. Create and activate a virtual environment

```bash
python3 -m venv bluesky_env
source bluesky_env/bin/activate
```

### 4. Install Python dependencies

```bash
pip install --upgrade pip
pip install numpy scipy matplotlib pandas
pip install pyzmq msgpack
pip install PyQt6 PyQt6-WebEngine pyopengl
pip install openap bluesky-navdata bluesky-guidata
pip install requests shapely geopandas
pip install gurobipy
```

Verify Gurobi licence:

```bash
python3 -c "import gurobipy; gurobipy.Model()"
```

### 5. Compile the BlueSky C extensions (optional but recommended)

```bash
python3 setup.py build_ext --inplace
```

### 6. Place BADA 4.2 data

Copy the BADA 4.2 aircraft folders into:

```
Data/BADA/BADA_4.2/<ModelName>/<ModelName>.xml
```

Example: `Data/BADA/BADA_4.2/B738W26/B738W26.xml`

The `Data/BADA/BADA_4.2/release.csv` index file must also be present.

If the `Data/BADA/BADA_4.2/` folder does not exist inside the repo, the plugin automatically falls back to `../Fuel_CDO_calculator/Data/BADA/BADA_4.2/` (sibling project).

### 7. Configure settings

Edit `bluesky/resources/settings.cfg` (create it if it doesn't exist — copy from `settings.cfg` at the repo root):

```bash
cp settings.cfg bluesky/resources/settings.cfg
```

Then open `bluesky/resources/settings.cfg` and fill in your credentials:

```ini
opensky_client_id     = 'your-client-id'
opensky_client_secret = 'your-client-secret'
```

### 8. Run

```bash
python3 BlueSky.py
```

---

## Installation — Windows

### 1. Install Python 3.10–3.14

Download from [python.org](https://www.python.org/downloads/windows/). During installation, check **"Add Python to PATH"**.

### 2. Clone repositories

Open **PowerShell** or **Git Bash**:

```powershell
git clone https://github.com/your-org/Bluesky_FrendlyTMA.git
cd Bluesky_FrendlyTMA
cd ..
git clone https://github.com/your-org/FrendlyTMA.git
cd Bluesky_FrendlyTMA
```

### 3. Create and activate a virtual environment

```powershell
python -m venv bluesky_env
bluesky_env\Scripts\activate
```

### 4. Install Python dependencies

```powershell
pip install --upgrade pip
pip install numpy scipy matplotlib pandas
pip install pyzmq msgpack
pip install PyQt6 PyQt6-WebEngine pyopengl
pip install openap bluesky-navdata bluesky-guidata
pip install requests shapely geopandas
pip install gurobipy
```

Verify Gurobi:

```powershell
python -c "import gurobipy; gurobipy.Model()"
```

### 5. Compile C extensions (optional)

Requires **Microsoft C++ Build Tools** (install from [visualstudio.microsoft.com](https://visualstudio.microsoft.com/visual-cpp-build-tools/)):

```powershell
python setup.py build_ext --inplace
```

### 6. Place BADA 4.2 data

Same structure as macOS — copy BADA 4.2 folders into `Data\BADA\BADA_4.2\`.

### 7. Configure settings

```powershell
copy settings.cfg bluesky\resources\settings.cfg
```

Open `bluesky\resources\settings.cfg` in a text editor and fill in your OpenSky credentials.

### 8. Run

Use the provided batch script:

```powershell
run-qtgl.bat
```

Or directly:

```powershell
python BlueSky.py
```

---

## Project structure

```
Bluesky_FrendlyTMA/
├── BlueSky.py                  # Entry point
├── settings.cfg                # Reference settings (do not edit at runtime — see below)
├── bluesky/
│   ├── resources/
│   │   └── settings.cfg        # Active settings file read at runtime
│   └── plugins/
│       ├── tma_opt.py          # TMA arrival optimisation (Gurobi)
│       ├── cdo_gen.py          # CDO profile generation and CDOGENOPT
│       ├── cdo_plots.py        # CDO figure generation (altitude/speed/fuel profiles)
│       ├── fuel_calc.py        # BADA 4.2 fuel burn model
│       ├── opensky_replay.py   # OpenSky historical scenario loader (OSHist button)
│       ├── opensky_replay_player.py  # CDO-aware replay with CAS/TAS correction
│       └── opensky_traces.py   # OpenSky live/historical track fetcher
├── Data/
│   ├── BADA/
│   │   ├── BADA_4.2/           # BADA 4.2 XML performance files (not included in repo)
│   │   └── BADA_3.16/          # BADA 3.16 aircraft list CSVs (metadata only)
│   └── Weather/                # ECMWF/GFS wind data
└── scenario/
    ├── OpenSky/                # Historical OpenSky track scenarios
    │   └── <stem>_tracks.csv
    └── TMAOpt/                 # TMAOpt results, CDO profiles, figures
        └── tmaopt_YYYYMMDD_HHMMSS/
            ├── aircraft.csv
            ├── *_historical.csv
            ├── *_cdo_opt.csv
            ├── *_cdo_opt_summary.csv
            ├── tmaopt_YYYYMMDD_HHMMSS.scn
            ├── *_cdo_opt.scn
            └── CDO_YYYYMMDD_HHMMSS/
                ├── *_cdo_<callsign>.csv
                ├── *_cdo_summary.csv
                └── Figures/
```

---

## Workflow 1 — Load and replay historical OpenSky data (OSHist)

### Via the UI dialog

1. Start BlueSky: `python3 BlueSky.py`
2. Click the **OSHist** button in the toolbar.
3. In the dialog:
   - **Date / End Time (UTC):** set the end of the window you want to fetch (e.g. `2026-06-16`, `08:17`)
   - **Duration:** length of the window in minutes (e.g. `60`)
   - **Preset:** select `ESSA Arlanda TMA` — this fills the bounding box automatically
   - Click **OK**
4. The plugin fetches historical state vectors from OpenSky Trino, saves a `_tracks.csv` and a `.scn` file to `scenario/OpenSky/`, and loads the scenario automatically.
5. Press **OP** in the console to start the replay.

### From a previously saved tracks file

If you already have a `_tracks.csv` from a previous OSHist run or TMAOpt run, load it directly:

```
IC scenario/OpenSky/myfile_tracks.csv
```

or for a TMAOpt historical file:

```
IC scenario/TMAOpt/tmaopt_20260512_083800/tmaopt_20260512_083800_historical.csv
```

The replay player reads the CSV and animates all aircraft in real time.

### Controlling the replay

| Command | Description |
|---|---|
| `OP` | Start / resume |
| `HOLD` | Pause |
| `FF` | Fast-forward |
| `DT 1` | 1-second time steps (normal speed) |
| `DT 5` | 5-second time steps (faster) |
| `RESET` | Stop and unload |

---

## Workflow 2 — Run TMAOpt with live OpenSky data

1. Click **TMAOpt** in the toolbar.
2. The plugin:
   - Fetches arriving aircraft for the last 60 minutes via OpenSky Trino
   - Saves `*_historical.csv` and `aircraft.csv` to `scenario/TMAOpt/tmaopt_YYYYMMDD_HHMMSS/`
   - Runs CDO precompute across all aircraft × all grid paths (Phase 1)
   - Solves Gurobi optimisation with CDO travel times (Phase 2)
   - Generates CDO profiles on the optimal routes and saves figures (Phase 3)
   - Auto-loads `*_cdo_opt.scn`

Progress is shown in the BlueSky console. The full pipeline takes 1–3 minutes depending on traffic count.

---

## Workflow 3 — Run TMAOpt from a saved historical file

Use this to re-run the optimisation on a previously saved `_historical.csv` without fetching new data from OpenSky.

### Via the console command

```
TMAOPTFILE scenario/TMAOpt/tmaopt_20260512_083800/tmaopt_20260512_083800_historical.csv
```

Or with an OpenSky tracks file from `scenario/OpenSky/`:

```
TMAOPTFILE scenario/OpenSky/myfile_tracks.csv
```

The plugin reads the aircraft tracks from the CSV, reconstructs TMA entry times and positions, then runs the same 3-phase pipeline (CDO precompute → Gurobi → CDOGENOPT) as a live run. Results are saved to a new timestamped subfolder under `scenario/TMAOpt/`.

### Optional parameters

```
TMAOPTFILE <csv_path> [entries=NESW] [max_ac=2] [eps=3] [tl=120] [s1=2] [s2=3]
```

| Parameter | Default | Description |
|---|---|---|
| `entries` | `NESW` | Which entry nodes to use (any combination of N, E, S, W) |
| `max_ac` | `2` | Maximum total aircraft in the optimisation |
| `eps` | `3` | Maximum epsilon relaxation steps |
| `tl` | `120` | Gurobi time limit per solve (seconds) |
| `s1` | `2` | Minimum separation at merge points (minutes) |
| `s2` | `3` | Minimum separation on shared segments (minutes) |

---

## Workflow 4 — Generate CDO profiles from a saved scenario (CDOGEN)

Use this to compute CDO fuel savings for any previously recorded historical track.

1. First load the scenario so it is the active scene (optional — the command works without loading):

   ```
   IC scenario/TMAOpt/tmaopt_20260512_083800/tmaopt_20260512_083800_historical.csv
   ```

2. Run the CDOGEN command in the console:

   ```
   CDOGEN tmaopt_20260512_083800_historical
   ```

   Or for an OpenSky tracks file:

   ```
   CDOGEN myfile_tracks
   ```

   The argument is just the file stem (without path or extension). The plugin auto-detects whether the file is in `scenario/TMAOpt/<stem>/` or `scenario/OpenSky/`.

3. Results are saved to a timestamped subfolder:

   ```
   scenario/TMAOpt/tmaopt_20260512_083800/CDO_YYYYMMDD_HHMMSS/
     *_cdo.csv              # CDO profile for all aircraft combined
     *_cdo_<callsign>.csv   # Per-aircraft CDO profile
     *_cdo_summary.csv      # Fuel savings summary table
     *_cdo.scn              # Loadable CDO replay scenario
     Figures/
       *_<callsign>_fig1.png   # Altitude + speed profile
       *_<callsign>_fig2.png   # Fuel burn profile
       *_<callsign>_fig3.png   # Horizontal track map
   ```

4. The CDO scenario loads automatically. Press **OP** to replay.

---

## Workflow 5 — Review saved TMAOpt results

To reload and replay any previously completed TMAOpt run:

```
IC scenario/TMAOpt/tmaopt_20260512_083800/tmaopt_20260512_083800_cdo_opt.scn
```

| Scenario file | What it shows |
|---|---|
| `tmaopt_YYYYMMDD_HHMMSS.scn` | Gurobi-optimal routes on the grid (coloured per entry) |
| `*_cdo_opt.scn` | CDO replay on the Gurobi-optimal routes |

---

## Settings reference

The active settings file is `bluesky/resources/settings.cfg`.

> **Important:** Always edit `bluesky/resources/settings.cfg`, not the root `settings.cfg`. The root file is a reference copy only. On a fresh install, copy it with `cp settings.cfg bluesky/resources/settings.cfg`.

| Key | Description |
|---|---|
| `enabled_plugins` | Plugins loaded on startup |
| `opensky_client_id` | OpenSky Network OAuth2 client ID |
| `opensky_client_secret` | OpenSky Network OAuth2 client secret |
| `opensky_default_lamin/lomin/lamax/lomax` | Default bounding box for ESSA TMA queries |
| `opensky_default_duration` | Default fetch window in minutes |
| `start_location` | Airport shown on startup (ESSA) |
| `simdt` | Simulation time step in seconds |

### Settings file locations by installation type

| Installation | Settings file location |
|---|---|
| Source (this repo) | `bluesky/resources/settings.cfg` |
| pip package (`bluesky-simulator`) | `~/bluesky/settings.cfg` (macOS/Linux) or `%USERPROFILE%\bluesky\settings.cfg` (Windows) |

If you run both the source install and a pip-installed BlueSky on the same machine, each needs its own credentials added to its own settings file.

---

## FrendlyTMA code path

`tma_opt.py` expects the FrendlyTMA optimisation code at the **hardcoded path**:

```
/Users/maximmoroz/liuprojects/FrendlyTMA/Code
```

On a new machine, update the `_FRENDLY_CODE` constant at the top of `bluesky/plugins/tma_opt.py`:

```python
_FRENDLY_CODE = Path('/your/path/to/FrendlyTMA/Code')
```

---

## Gurobi licence

A free academic licence can be obtained at [gurobi.com/academia](https://www.gurobi.com/academia/academic-program-and-licenses/).
After activating, run `grbgetkey <your-key>` once to install the licence file.

| Platform | Licence file location |
|---|---|
| macOS / Linux | `~/gurobi.lic` |
| Windows | `C:\Users\<username>\gurobi.lic` |

---

## Troubleshooting

**`OpenSky credentials not configured` warning in OSHist or TMAOpt dialog**

The credentials must be in the settings file that the running BlueSky instance reads. There are two common locations depending on how BlueSky was started:

- Source install: `bluesky/resources/settings.cfg` inside this repo
- pip install: `~/bluesky/settings.cfg`

Add these lines to whichever file is missing them:

```ini
opensky_client_id     = 'your-client-id'
opensky_client_secret = 'your-client-secret'
```

**`AttributeError: 'Constant' object has no attribute 's'`**

Python 3.14 removed the deprecated `ast.Str.s` attribute. Apply this patch to `bluesky/core/plugin.py`:

```python
# Replace all occurrences of .s and .n on ast.Constant nodes with .value
# Affects lines ~130–151
```

**`FileNotFoundError: Paths_with_max14edges.pkl`**

Check that `_FRENDLY_CODE` in `tma_opt.py` points to the correct FrendlyTMA/Code directory.

**`_pickle.PicklingError: Can't pickle local object _WP`**

The `_WP` waypoint class must be defined at module level in `tma_opt.py`, not inside a function. This has been fixed — clear stale bytecode and restart:

```bash
find bluesky/plugins/__pycache__ -name "*.pyc" -delete
```

**`BADA load failed` for all aircraft**

The BADA 4.2 XML files are not found. Check that `Data/BADA/BADA_4.2/` exists and contains the model subfolders (e.g. `B738W26/B738W26.xml`). If the folder is absent, the plugin falls back to `../Fuel_CDO_calculator/Data/BADA/BADA_4.2/` automatically.

**`TMAOPT takes N arguments, but M were given`**

Stale `.pyc` bytecode. Clear it and restart:

```bash
find bluesky/plugins/__pycache__ -name "*.pyc" -delete
```

**Gurobi `Model()` fails with no licence**

Run `grbgetkey <key>` and ensure the `gurobi.lic` file exists at the location shown in the table above.
