"""OpenSky Network REST API client with OAuth2 authentication.

Fetching strategy (per official docs):
  - /states/all?time=T&lamin=..&lomin=..&lamax=..&lomax=..
      Authenticated users: up to 1 hour in the past (400 if older).
      Returns state vectors for all aircraft in the bbox at time T.
      Cost: 1-4 credits per call depending on bbox area.
      Time resolution: 5 seconds.

  - /flights/all?begin=T1&end=T2
      Any time up to 30 days in the past. Max interval 2 hours.
      Returns flight metadata (no positions). Cost: 4-960 credits.

  - /tracks?icao24=X&time=T   (experimental, NOT /tracks/all)
      Any time within 30 days. time=0 for live track.
      Returns waypoints for a single aircraft. Rate-limited separately.

Workflow implemented here:
  1. Sample /states/all at _SNAPSHOT_INTERVAL second intervals across
     the requested window — no per-aircraft limit.
  2. Save raw API responses to cache/opensky/<timestamp>.json so the
     data can be reused without re-fetching.
  3. On next call for same timestamp, load from cache if it exists.
"""
import json
import os
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

_API_BASE = "https://opensky-network.org/api"
_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
_TOKEN_REFRESH_MARGIN = 30  # s before expiry to proactively refresh

_SNAPSHOT_INTERVAL = 60      # seconds between state snapshots
_MAX_STATE_AGE_S   = 3590   # authenticated users: max 1 hour (with small margin)

# Cache directory — relative to repo root
_CACHE_DIR = Path(__file__).parents[2] / "cache" / "opensky"


class ConfigurationError(Exception):
    pass


class OpenSkyAPIError(Exception):
    def __init__(self, status_code: int, reason: str):
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"OpenSky API error {status_code}: {reason}")


@dataclass
class Waypoint:
    time: int
    lat: float
    lon: float
    baro_alt_m: Optional[float]
    true_track: Optional[float]
    on_ground: bool
    velocity_ms: Optional[float] = None
    vertical_rate_ms: Optional[float] = None


@dataclass
class FlightTrack:
    icao24: str
    callsign: str
    est_departure: Optional[str]
    est_arrival: Optional[str]
    waypoints: list = field(default_factory=list)


class TokenManager:
    """OAuth2 client_credentials token manager for the OpenSky API."""

    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise ConfigurationError(
                "OpenSky credentials not set. "
                "Register at opensky-network.org, go to Account > API Clients, "
                "create a client, then add opensky_client_id and "
                "opensky_client_secret to settings.cfg"
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: Optional[str] = None
        self._expires_at: Optional[datetime] = None

    def get_token(self) -> str:
        if (
            self._token is not None
            and self._expires_at is not None
            and datetime.now(timezone.utc) < self._expires_at
        ):
            return self._token
        return self._refresh()

    def _refresh(self) -> str:
        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=(5, 10),
        )
        if resp.status_code != 200:
            raise ConfigurationError(
                f"Failed to obtain OpenSky token: {resp.status_code} {resp.reason}"
            )
        data = resp.json()
        self._token = data["access_token"]
        expires_in = data.get("expires_in", 1800)
        self._expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in - _TOKEN_REFRESH_MARGIN
        )
        log.debug("OpenSky token refreshed, expires in %ds", expires_in)
        return self._token

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get_token()}"}


class OpenSkyFetcher:
    """Fetches historical/live flight data from the OpenSky Network REST API.

    Uses repeated /states/all snapshots at bbox to build per-aircraft tracks.
    Raw API responses are saved to cache/opensky/ as JSON for reuse.
    """

    def __init__(self, client_id: str = "", client_secret: str = ""):
        if not client_id:
            client_id = os.environ.get("OPENSKY_CLIENT_ID", "")
        if not client_secret:
            client_secret = os.environ.get("OPENSKY_CLIENT_SECRET", "")
        self._tokens = None
        if client_id and client_secret:
            self._tokens = TokenManager(client_id, client_secret)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self, begin_ts: int, end_ts: int) -> list:
        """Return list of warning/error strings. ERRORs block the fetch."""
        warnings = []
        now = int(time.time())
        age_s = now - begin_ts

        if age_s > _MAX_STATE_AGE_S:
            warnings.append(
                f"INFO: Requested start time is {age_s // 60} minutes ago — using OpenSky Trino API for historical data."
            )
        return warnings

    def estimate_credits(
        self, begin_ts: int, end_ts: int,
        lamin: float, lomin: float, lamax: float, lomax: float,
    ) -> str:
        """Return a human-readable credit cost estimate (no leading slashes)."""
        area = (lamax - lamin) * (lomax - lomin)
        if area <= 25:
            cost_per = 1
        elif area <= 100:
            cost_per = 2
        elif area <= 400:
            cost_per = 3
        else:
            cost_per = 4

        duration_s = end_ts - begin_ts
        n = max(1, duration_s // _SNAPSHOT_INTERVAL)
        total = n * cost_per
        return (
            f"Estimated credit cost: {n} snapshots x {cost_per} credits "
            f"= ~{total} credits from states bucket "
            f"(area={area:.1f} sq deg, duration={duration_s // 60} min)"
        )

    def fetch_area_flights_trino(
        self,
        begin_ts: int,
        end_ts: int,
        lamin: float,
        lomin: float,
        lamax: float,
        lomax: float,
    ) -> list:
        """
        Fetch historical flight tracks via pyopensky Trino (handles OAuth automatically).
        Returns list of FlightTrack objects.
        """
        try:
            from pyopensky.trino import Trino
            import pandas as pd
        except ImportError:
            log.error("pyopensky not installed. Run: pip3 install pyopensky")
            return []

        import datetime as dt

        start_dt = dt.datetime.fromtimestamp(begin_ts, tz=dt.timezone.utc)
        stop_dt  = dt.datetime.fromtimestamp(end_ts,   tz=dt.timezone.utc)
        bounds   = (lomin, lamin, lomax, lamax)  # pyopensky: (west, south, east, north)

        try:
            trino = Trino()
            log.info("Querying pyopensky Trino: %s → %s bbox=%s", start_dt, stop_dt, bounds)
            df = trino.history(start_dt, stop_dt, bounds=bounds)
        except Exception as e:
            log.error("pyopensky Trino error: %s", e)
            return []

        if df is None or (hasattr(df, '__len__') and len(df) == 0):
            log.warning("pyopensky Trino returned no data.")
            return []

        # Group by icao24 and build FlightTrack objects
        aircraft: dict = {}
        for _, row in df.iterrows():
            import pandas as pd
            icao24   = '' if pd.isna(row.get('icao24'))   else str(row.get('icao24', '')).strip().lower()
            callsign = '' if pd.isna(row.get('callsign')) else str(row.get('callsign', '')).strip()
            if not icao24:
                continue

            try:
                ts_raw = row['time']
                ts = int(ts_raw.timestamp()) if hasattr(ts_raw, 'timestamp') else int(ts_raw)
                lat      = float(row['lat'])
                lon      = float(row['lon'])
                velocity = float(row.get('velocity') or 0.0)
                heading  = float(row.get('heading')  or 0.0)
                vertrate = float(row.get('vertrate')  or 0.0)
                baro_alt = float(row.get('baroaltitude') or 0.0)
                og_val = row.get('onground')
                on_ground = False if (og_val is None or (hasattr(og_val, '__bool__') == False)) else bool(og_val) if not pd.isna(og_val) else False
            except (TypeError, ValueError):
                continue

            wp = Waypoint(
                time=ts,
                lat=lat,
                lon=lon,
                baro_alt_m=baro_alt,
                true_track=heading,
                on_ground=on_ground,
                velocity_ms=velocity,
                vertical_rate_ms=vertrate,
            )

            if icao24 not in aircraft:
                aircraft[icao24] = {'callsign': callsign, 'waypoints': []}
            aircraft[icao24]['waypoints'].append(wp)

        tracks = []
        for icao24, info in aircraft.items():
            airborne = [wp for wp in info['waypoints'] if not wp.on_ground]
            if len(airborne) < 2:
                continue
            tracks.append(FlightTrack(
                icao24=icao24,
                callsign=info['callsign'] or icao24,
                est_departure=None,
                est_arrival=None,
                waypoints=airborne,
            ))

        log.info("pyopensky Trino built %d tracks.", len(tracks))
        return tracks

    def fetch_area_flights(
        self,
        begin_ts: int,
        end_ts: int,
        lamin: float,
        lomin: float,
        lamax: float,
        lomax: float,
        use_cache: bool = True,
    ) -> list:
        """
        Sample /states/all across the time window. Save each snapshot to
        cache/opensky/ as JSON (keyed by timestamp + bbox). Build and return
        per-aircraft FlightTrack objects from the accumulated snapshots.
        """
        now = int(time.time())

        # Route to Trino for data older than 1 hour (beyond REST API range)
        if begin_ts < now - _MAX_STATE_AGE_S:
            log.info("Request is historical (>1h ago) — using Trino API.")
            return self.fetch_area_flights_trino(begin_ts, end_ts, lamin, lomin, lamax, lomax)

        # Clamp to accessible range (authenticated: last 3590 s)
        effective_begin = max(begin_ts, now - _MAX_STATE_AGE_S)
        effective_end   = min(end_ts,   now - 5)  # never request "future"

        if effective_end <= effective_begin:
            log.warning("Requested window is entirely outside the accessible range.")
            return []

        duration_s = effective_end - effective_begin
        n = max(1, duration_s // _SNAPSHOT_INTERVAL)
        timestamps = [effective_begin + i * _SNAPSHOT_INTERVAL for i in range(n + 1)]

        log.info(
            "Fetching %d snapshots over %d min in ESSA bbox [%.1f,%.1f,%.1f,%.1f]",
            len(timestamps), duration_s // 60, lamin, lomin, lamax, lomax,
        )

        aircraft: dict = {}  # icao24 -> {callsign, waypoints}
        consecutive_fails = 0
        deadline = time.time() + duration_s + 120  # hard wall-clock limit

        for idx, ts in enumerate(timestamps):
            if time.time() > deadline:
                log.warning("Fetch deadline exceeded — stopping early after %d snapshots.", idx)
                break
            dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
            log.info("[%d/%d] Snapshot at %s UTC", idx + 1, len(timestamps), dt_str)

            # Try cache first
            states = None
            cache_path = _cache_path(ts, lamin, lomin, lamax, lomax)
            if use_cache and cache_path.exists():
                states = _load_cache(cache_path)
                log.debug("  loaded from cache: %d states", len(states) if states else 0)

            if states is None:
                states = self._get_states(ts, lamin, lomin, lamax, lomax)
                if states is None:
                    consecutive_fails += 1
                    if consecutive_fails >= 3:
                        log.error("3 consecutive API failures — aborting fetch.")
                        break
                    continue
                # Save raw response to cache
                _save_cache(cache_path, states)

            consecutive_fails = 0

            for sv in states:
                wp = _parse_state_vector(sv, ts)
                if wp is None:
                    continue
                icao24   = (sv[0] or "").strip().lower()
                callsign = (sv[1] or "").strip()
                if not icao24:
                    continue
                if icao24 not in aircraft:
                    aircraft[icao24] = {"callsign": callsign, "waypoints": []}
                aircraft[icao24]["waypoints"].append(wp)

            if idx < len(timestamps) - 1:
                time.sleep(0.3)  # polite pacing between snapshots

        # Build FlightTrack list — require at least 2 airborne waypoints
        tracks = []
        for icao24, info in aircraft.items():
            airborne = [wp for wp in info["waypoints"] if not wp.on_ground]
            if len(airborne) < 2:
                continue
            tracks.append(FlightTrack(
                icao24=icao24,
                callsign=info["callsign"],
                est_departure=None,
                est_arrival=None,
                waypoints=airborne,
            ))

        log.info("Built %d aircraft tracks from %d snapshots.", len(tracks), len(timestamps))
        return tracks

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_states(
        self, ts: int,
        lamin: float, lomin: float, lamax: float, lomax: float,
    ) -> Optional[list]:
        """Call /states/all and return the states list, or None on failure."""
        now = int(time.time())
        params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
        if ts < now - 10:           # only add time param for historical requests
            params["time"] = ts

        data = self._request(f"{_API_BASE}/states/all", params)
        if data is None:
            return None
        states = data.get("states")
        return states if states else []

    def _request(self, url: str, params: dict = None, retry: bool = True):
        if self._tokens is None:
            return []
        headers = self._tokens.headers()
        resp = requests.get(url, headers=headers, params=params, timeout=(5, 10))

        if resp.status_code == 401 and retry:
            log.debug("Token expired, refreshing...")
            self._tokens._token = None
            return self._request(url, params, retry=False)

        if resp.status_code == 429:
            raw = resp.headers.get("X-Rate-Limit-Retry-After-Seconds", "15")
            try:
                wait = min(int(raw), 60)
            except (ValueError, OverflowError):
                wait = 15
            log.warning("Rate limited — waiting %ds...", wait)
            time.sleep(wait)
            if retry:
                return self._request(url, params, retry=False)
            return None

        if resp.status_code == 400:
            log.error("HTTP 400: %s — likely time > 1 hour ago for /states/all", resp.text[:120])
            return None

        if resp.status_code in (403, 404):
            log.warning("HTTP %d: %s", resp.status_code, resp.text[:120])
            return None

        if resp.status_code != 200:
            raise OpenSkyAPIError(resp.status_code, resp.reason)

        return resp.json()


# ------------------------------------------------------------------
# Cache helpers
# ------------------------------------------------------------------

def _cache_path(ts: int, lamin: float, lomin: float, lamax: float, lomax: float) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = f"{ts}_{lamin:.2f}_{lomin:.2f}_{lamax:.2f}_{lomax:.2f}"
    return _CACHE_DIR / f"states_{key}.json"


def _save_cache(path: Path, states: list) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(states, f)
        log.debug("Cached %d states to %s", len(states), path.name)
    except Exception as exc:
        log.debug("Could not write cache: %s", exc)


def _load_cache(path: Path) -> Optional[list]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception as exc:
        log.debug("Could not read cache %s: %s", path.name, exc)
    return None


# ------------------------------------------------------------------
# State vector parsing
# ------------------------------------------------------------------

# Field indices per OpenSky docs
_I_ICAO24         = 0
_I_CALLSIGN       = 1
_I_LON            = 5
_I_LAT            = 6
_I_BARO_ALT       = 7
_I_ON_GROUND      = 8
_I_VELOCITY       = 9
_I_TRUE_TRACK     = 10
_I_VERTICAL_RATE  = 11


def _parse_state_vector(sv: list, ts: int) -> Optional[Waypoint]:
    if len(sv) < 12:
        return None
    lat = sv[_I_LAT]
    lon = sv[_I_LON]
    if lat is None or lon is None:
        return None
    return Waypoint(
        time             = ts,
        lat              = float(lat),
        lon              = float(lon),
        baro_alt_m       = float(sv[_I_BARO_ALT])      if sv[_I_BARO_ALT]      is not None else None,
        true_track       = float(sv[_I_TRUE_TRACK])     if sv[_I_TRUE_TRACK]     is not None else None,
        on_ground        = bool(sv[_I_ON_GROUND]),
        velocity_ms      = float(sv[_I_VELOCITY])       if sv[_I_VELOCITY]       is not None else None,
        vertical_rate_ms = float(sv[_I_VERTICAL_RATE])  if sv[_I_VERTICAL_RATE]  is not None else None,
    )
