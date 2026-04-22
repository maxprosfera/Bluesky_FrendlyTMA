# OpenSky Historical Replay – User Guide

This module fetches historical flight data from the [OpenSky Network](https://opensky-network.org) REST API, converts it into a BlueSky scenario file, and loads it directly in the simulator.

---

## One-Time Account Setup

1. Register a free account at <https://opensky-network.org>
2. Log in → go to **Account → API Clients**
3. Click **Create new client** and give it a name
4. Copy the generated `client_id` and `client_secret`
5. Open `settings.cfg` (in the repository root) and fill in:

```ini
opensky_client_id = 'your_client_id'
opensky_client_secret = 'your_client_secret'
```

> The credentials are only used to obtain short-lived Bearer tokens; they are never transmitted anywhere except to the OpenSky authentication server.

---

## Usage – GUI

1. Launch BlueSky normally (`python BlueSky.py`)
2. Click the **📡 OpenSky History** button in the top-right corner of the menu bar
3. Fill in the dialog:
   - **Date / Time (UTC)** – the start of the replay window
   - **Duration** – how many minutes to fetch (1–120, max 2 hours)
   - **Area preset** – select *ESSA Arlanda TMA* or choose *Custom* and enter your own bounding box
4. Click **OK** – the fetch runs in the background; a console message will confirm when the scenario is ready and loaded

> A yellow warning is shown in the dialog if credentials are missing.

---

## Usage – Stack Command

Enable the plugin first (add `opensky_replay` to `enabled_plugins` in `settings.cfg`, or run `PLUGINS OPENSKY_REPLAY`), then:

```
LOADOPENSKY 2024-11-15T14:00
LOADOPENSKY 2024-11-15T14:00 58.5 17.0 60.5 20.5
LOADOPENSKY 2024-11-15T14:00 58.5 17.0 60.5 20.5 60
```

Arguments:
| Argument | Description | Default |
|---|---|---|
| `datetime` | UTC start time (`YYYY-MM-DDTHH:MM`) | required |
| `lamin lomin lamax lomax` | Bounding box (decimal degrees) | from `settings.cfg` |
| `duration` | Fetch window in minutes (max 120) | from `settings.cfg` |

---

## Usage – CLI Script

```bash
python utils/opensky_importer/fetch_opensky.py \
  --datetime "2024-11-15 14:00" \
  --airport ESSA \
  --lamin 58.5 --lomin 17.0 --lamax 60.5 --lomax 20.5 \
  --duration 60 \
  --client-id YOUR_ID \
  --client-secret YOUR_SECRET
```

Use `--run` to also launch BlueSky automatically with the generated scenario.  
Use `--yes` / `-y` to skip the confirmation prompt (useful in scripts).  
Credentials can also be supplied via environment variables `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET`.

---

## Aircraft Type Lookup (3-Tier)

The module resolves each aircraft's ICAO type code as follows:

| Tier | Source | Description |
|---|---|---|
| 1 | **actypedb** | ICAO24 hex → type code (cached from junzisun.com/adb) |
| 2 | **BADA 4.2** | Validates / confirms the type code exists in `Data/BADA/BADA_4.2/release.csv` |
| 3 | **Fallback** | `B738` if neither tier resolves the type |

---

## Scenario File Format

Generated files are saved to `scenario/OpenSky/` and follow the naming pattern:

```
opensky_ESSA_YYYYMMDD_HHMM.scn
```

Each file contains:
- A header comment block with metadata (area, datetime, aircraft count)
- `CRE` commands to create each aircraft at its first airborne waypoint
- `DEST` commands for aircraft with a known destination airport
- `MOVE` commands for subsequent waypoints (with altitude, heading, speed, vertical rate)

---

## Data Limits & Credit Usage

| Constraint | Value |
|---|---|
| `/tracks` endpoint history limit | **30 days** |
| `/flights/all` max interval | **2 hours** |
| Estimated credits per fetch | Shown in console/CLI before fetching |

For data older than 30 days, the OpenSky [Trino interface](https://openskynetwork.github.io/opensky-api/trino.html) must be used (not covered by this module).

Credit quotas reset daily for standard accounts. Active feeders (≥30% uptime/month) receive double the daily allowance.

---

## Default Bounding Box – ESSA Arlanda TMA

| Parameter | Value |
|---|---|
| Lat min | 58.5° N |
| Lon min | 17.0° E |
| Lat max | 60.5° N |
| Lon max | 20.5° E |

All defaults can be overridden in `settings.cfg` via `opensky_default_lamin`, `opensky_default_lomin`, `opensky_default_lamax`, `opensky_default_lomax`, and `opensky_default_duration`.
