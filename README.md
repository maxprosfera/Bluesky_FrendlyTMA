# FrendlyTMA — BlueSky Plugin Suite

Research tool for TMA arrival optimisation, CDO fuel savings analysis, and OpenSky historical replay, built on top of the [BlueSky ATC Simulator](https://github.com/TUDelft-CNS-ATM/bluesky).

Targets ESSA Stockholm Arlanda. Requires a Gurobi licence and BADA 4.2 data.

---

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10 – 3.14 | 3.14 recommended |
| Gurobi | 11+ | Academic licence available free at gurobi.com |
| BADA 4.2 | — | Licence required from EUROCONTROL |
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
python3 setup.py build_ext --inplace   # or: pip install -e .
```

### 6. Place BADA 4.2 data

Copy the BADA 4.2 aircraft folders into:

```
Data/BADA/BADA_4.2/<ModelName>/<ModelName>.xml
```

Example: `Data/BADA/BADA_4.2/B738W26/B738W26.xml`

The `Data/BADA/BADA_4.2/release.csv` index file must also be present.

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
│       ├── opensky_replay.py   # OpenSky historical scenario loader
│       ├── opensky_replay_player.py  # CDO-aware replay with CAS/TAS correction
│       └── opensky_traces.py   # OpenSky live/historical track fetcher
├── Data/
│   ├── BADA/
│   │   ├── BADA_4.2/           # BADA 4.2 XML performance files (not included)
│   │   └── BADA_3.16/          # BADA 3.16 aircraft list CSVs (metadata only)
│   └── Weather/                # ECMWF/GFS wind data
└── scenario/
    ├── OpenSky/                # Historical OpenSky track scenarios
    └── TMAOpt/                 # TMAOpt results, CDO profiles, figures
        └── tmaopt_YYYYMMDD_HHMMSS/
            ├── aircraft.csv
            ├── *_historical.csv
            ├── *_cdo_opt.csv
            ├── *_cdo_opt.scn
            ├── *_cdo_opt_summary.csv
            └── CDO_YYYYMMDD_HHMMSS/
                └── Figures/
```

---

## Plugins and UI buttons

| Button | Plugin | Description |
|---|---|---|
| **TMAOpt** | `tma_opt.py` | Fetches arriving traffic via OpenSky Trino, runs Gurobi route optimisation, generates CDO profiles on optimal routes, saves scenario + figures |
| **OSHist** | `opensky_replay.py` | Loads a historical OpenSky scenario from disk or fetches from the API |
| **CDOGEN** | `cdo_gen.py` | Computes CDO profile from an existing `_historical.csv` track; saves results into a timestamped `CDO_YYYYMMDD_HHMMSS/` subfolder with figures |

---

## Settings reference (`bluesky/resources/settings.cfg`)

| Key | Description |
|---|---|
| `enabled_plugins` | List of plugins to load on startup |
| `opensky_client_id` | OpenSky Network OAuth2 client ID |
| `opensky_client_secret` | OpenSky Network OAuth2 client secret |
| `opensky_default_lamin/lomin/lamax/lomax` | Default bounding box for OpenSky queries (ESSA TMA) |
| `opensky_default_duration` | Default replay duration in minutes |
| `start_location` | ICAO code of the airport shown on startup |
| `simdt` | Simulation time step (seconds) |

> **Important:** Always edit `bluesky/resources/settings.cfg`, not the root `settings.cfg`. The root file is a reference copy only.

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
After activating, run `grbgetkey <your-key>` once to install the licence file (`gurobi.lic`).

On macOS the licence file goes to `~/gurobi.lic`.  
On Windows it goes to `C:\Users\<username>\gurobi.lic`.

---

## Troubleshooting

**`AttributeError: 'Constant' object has no attribute 's'`**  
Python 3.14 removed the deprecated `ast.Str.s` attribute. The fix has already been applied to `bluesky/core/plugin.py`. If you see this on a fresh install, apply the patch:
```python
# In bluesky/core/plugin.py replace all .s → .value and .n → .value
# on ast.Constant nodes (lines ~130–151)
```

**`OpenSky credentials not configured` warning**  
The credentials in `settings.cfg` at the repo root are not read at runtime. Copy the file:
```bash
cp settings.cfg bluesky/resources/settings.cfg   # macOS
copy settings.cfg bluesky\resources\settings.cfg  # Windows
```

**`FileNotFoundError: Paths_with_max14edges.pkl`**  
The grid path file must be present in the FrendlyTMA code directory. Check that `_FRENDLY_CODE` in `tma_opt.py` points to the correct location.

**`TMAOPT takes N arguments, but M were given`**  
Stale `.pyc` bytecode. Clear it and restart:
```bash
find bluesky/plugins/__pycache__ -name "*.pyc" -delete
```

**Gurobi `Model()` fails with no licence**  
Run `grbgetkey <key>` and ensure `~/gurobi.lic` (macOS) or `C:\Users\<user>\gurobi.lic` (Windows) exists.
