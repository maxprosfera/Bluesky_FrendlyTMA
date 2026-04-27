"""Convert OpenSky FlightTrack data to BlueSky .scn scenario files."""
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .fetcher import FlightTrack, Waypoint

log = logging.getLogger(__name__)

_MIN_ALT_M = 914.0   # FL030 — tracks never exceeding this are excluded from replay


class ScenarioConverter:
    """Converts a list of FlightTrack objects into a BlueSky .scn file."""

    def __init__(
        self,
        begin_ts: int,
        airport_label: str = "ESSA",
        lamin: float = 58.5,
        lomin: float = 17.0,
        lamax: float = 60.5,
        lomax: float = 20.5,
        actypedb: Optional[dict] = None,
        output_dir: Optional[Path] = None,
        end_ts: Optional[int] = None,
    ):
        self.begin_ts = begin_ts
        self.end_ts = end_ts
        self.airport_label = airport_label.upper()
        self.lamin = lamin
        self.lomin = lomin
        self.lamax = lamax
        self.lomax = lomax
        self.actypedb = actypedb or {}
        self.output_dir = output_dir or Path("scenario") / "OpenSky"
        self._last_ac_count = 0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def convert_and_save(self, tracks: list) -> Path:
        """Convert flight tracks to a .scn file and return the saved path."""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        start_dt = datetime.fromtimestamp(self.begin_ts, tz=timezone.utc)
        name_ts = self.end_ts if self.end_ts else self.begin_ts
        name_dt = datetime.fromtimestamp(name_ts, tz=timezone.utc)
        stem = f"opensky_{self.airport_label}_{name_dt.strftime('%Y%m%d_%H%M')}"
        out_path = self.output_dir / f"{stem}.scn"
        raw_path = self.output_dir / f"{stem}_tracks.csv"

        lines = self._build_scenario(tracks, start_dt, name_dt)

        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        self._save_raw_tracks(tracks, raw_path, start_dt)

        log.info("Scenario saved to: %s  (%d aircraft)", out_path, self._last_ac_count)
        return out_path

    def _save_raw_tracks(self, tracks: list, path: Path, start_dt: datetime) -> None:
        """Save raw trajectory data as CSV for reference/reuse."""
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "icao24", "callsign", "est_departure", "est_arrival",
                    "time", "lat", "lon", "baro_alt_m", "true_track",
                    "on_ground", "velocity_ms", "vertical_rate_ms",
                ])
                for t in tracks:
                    for wp in t.waypoints:
                        writer.writerow([
                            t.icao24, t.callsign,
                            t.est_departure or "", t.est_arrival or "",
                            wp.time, wp.lat, wp.lon,
                            wp.baro_alt_m if wp.baro_alt_m is not None else "",
                            wp.true_track if wp.true_track is not None else "",
                            int(wp.on_ground),
                            wp.velocity_ms if wp.velocity_ms is not None else "",
                            wp.vertical_rate_ms if wp.vertical_rate_ms is not None else "",
                        ])
            log.info("Raw tracks saved to: %s", path)
        except Exception as exc:
            log.warning("Could not save raw tracks: %s", exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_scenario(self, tracks: list, start_dt: datetime, name_dt: datetime) -> list:
        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        area_desc = (
            f"{self.airport_label} TMA"
            if self.airport_label != "CUSTOM"
            else (
                f"custom bbox "
                f"lat [{self.lamin:.2f},{self.lamax:.2f}] "
                f"lon [{self.lomin:.2f},{self.lomax:.2f}]"
            )
        )

        valid_tracks = [t for t in tracks if self._is_valid(t)]
        high_tracks = [t for t in valid_tracks if self._is_high_altitude(t)]
        low_tracks  = [t for t in valid_tracks if not self._is_high_altitude(t)]
        self._last_ac_count = len(high_tracks)

        csv_filename = (
            f"opensky_{self.airport_label}_{name_dt.strftime('%Y%m%d_%H%M')}_tracks.csv"
        )

        header = [
            "# ==========================================================",
            "# OpenSky Historical Scenario",
            f"# Area:     {area_desc}",
            f"# Datetime: {name_dt.strftime('%Y-%m-%d %H:%M')} UTC",
            f"# Fetched:  {fetched_at}",
            f"# Aircraft: {self._last_ac_count} IFR  ({len(low_tracks)} low-alt excluded)",
            "# Replay:   interpolated via OPENSKY_REPLAY_PLAYER plugin",
            "# ==========================================================",
            "00:00:00.00>TIME 00:00:00",
            "",
            "# --- Simulation Setup ---",
            "00:00:00.00> PAN 59.574, 17.9876",
            "00:00:00.00> ZOOM 2.0",
            "00:00:00.00> DT 1.0",
            "00:00:00.00> TAXI OFF",
            "00:00:00.00> SWRAD WPT 0",
            "00:00:00.00> SWRAD APT 0",
            "00:00:00.00> SWRAD SAT 0",
            "",
            "# --- Visual Elements ---",
            "00:00:00.00> POLY StockholmTMA 60.299444 18.213056 60.266111 18.554722 59.882778 18.847000 60.035278 19.313611 59.673611 19.830833 59.599444 19.273611 59.255000 18.968333 59.047500 18.754722 58.832500 18.539444 58.752500 18.457222 58.583056 17.932778 58.616389 17.456944 58.966111 17.407778 58.978611 17.223333 59.012500 16.707778 59.049444 16.267778 59.323889 16.318333 59.749444 16.446667 60.232778 17.596667",
            "",
            "# --- Start interpolated replay ---",
            f"00:00:00.00> STARTREPLAY scenario/OpenSky/{csv_filename}",
            f"00:00:00.00> LOADTRACES scenario/OpenSky/{csv_filename}",
            "00:00:00.00> OP",
        ]

        return header

    def _is_valid(self, track) -> bool:
        airborne = [
            wp for wp in track.waypoints
            if not wp.on_ground and wp.baro_alt_m is not None
        ]
        if len(airborne) < 2:
            return False
        in_box = [
            wp for wp in airborne
            if self.lamin <= wp.lat <= self.lamax and self.lomin <= wp.lon <= self.lomax
        ]
        return len(in_box) >= 1

    def _is_high_altitude(self, track) -> bool:
        return any(
            wp.baro_alt_m is not None and wp.baro_alt_m > _MIN_ALT_M
            for wp in track.waypoints
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
