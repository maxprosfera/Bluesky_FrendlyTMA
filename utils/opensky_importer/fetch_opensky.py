"""Standalone CLI script: fetch historical OpenSky data and generate a BlueSky scenario.

Usage:
    python utils/opensky_importer/fetch_opensky.py --datetime "2024-11-15 14:00"
    python utils/opensky_importer/fetch_opensky.py --datetime "2024-11-15 14:00" \\
        --airport ESSA --lamin 58.5 --lomin 17.0 --lamax 60.5 --lomax 20.5 \\
        --duration 60 --output scenario/OpenSky/ --run
"""
import argparse
import logging
import os
import subprocess
import sys
from calendar import timegm
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repository root
_REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from utils.opensky_importer.fetcher import OpenSkyFetcher, ConfigurationError, OpenSkyAPIError
from utils.opensky_importer.converter import ScenarioConverter

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser(
        description="Fetch historical OpenSky flights and generate a BlueSky scenario file."
    )
    p.add_argument(
        "--datetime",
        required=True,
        metavar="DATETIME",
        help='Start datetime in UTC, e.g. "2024-11-15 14:00" or "2024-11-15T14:00"',
    )
    p.add_argument("--airport", default="ESSA", help="ICAO label for the area (default: ESSA)")
    p.add_argument("--lamin", type=float, default=58.5, help="Bounding box lat min (default: 58.5)")
    p.add_argument("--lomin", type=float, default=17.0, help="Bounding box lon min (default: 17.0)")
    p.add_argument("--lamax", type=float, default=60.5, help="Bounding box lat max (default: 60.5)")
    p.add_argument("--lomax", type=float, default=20.5, help="Bounding box lon max (default: 20.5)")
    p.add_argument(
        "--duration",
        type=int,
        default=60,
        metavar="MINUTES",
        help="Fetch window in minutes, max 120 (default: 60)",
    )
    p.add_argument(
        "--output",
        default=str(_REPO_ROOT / "scenario" / "OpenSky"),
        help="Output directory for .scn files",
    )
    p.add_argument("--client-id", default="", help="OpenSky OAuth2 client_id")
    p.add_argument("--client-secret", default="", help="OpenSky OAuth2 client_secret")
    p.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    p.add_argument(
        "--run", action="store_true", help="Launch BlueSky with the generated scenario"
    )
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    return p.parse_args()


def parse_datetime_utc(dt_str: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(
        f"Cannot parse datetime '{dt_str}'. "
        "Use format: '2024-11-15 14:00' or '2024-11-15T14:00'"
    )


def main():
    args = parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # --- Parse datetime ---
    try:
        start_dt = parse_datetime_utc(args.datetime)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    duration_min = min(max(1, args.duration), 120)
    begin_ts = int(timegm(start_dt.utctimetuple()))
    end_ts = begin_ts + duration_min * 60

    # --- Credentials ---
    client_id = args.client_id or os.environ.get("OPENSKY_CLIENT_ID", "")
    client_secret = args.client_secret or os.environ.get("OPENSKY_CLIENT_SECRET", "")

    # --- Validation ---
    print(f"\nOpenSky Historical Scenario Fetcher")
    print(f"  Area:     {args.airport}  [{args.lamin},{args.lomin}] → [{args.lamax},{args.lomax}]")
    print(f"  Datetime: {start_dt.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Duration: {duration_min} min")

    try:
        fetcher = OpenSkyFetcher(client_id=client_id, client_secret=client_secret)
    except ConfigurationError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    warnings = fetcher.validate(begin_ts, end_ts)
    for w in warnings:
        print(f"\n  {w}")
        if w.startswith("ERROR:"):
            sys.exit(1)

    credit_info = fetcher.estimate_credits(begin_ts, end_ts, args.lamin, args.lomin, args.lamax, args.lomax)
    print(f"\n  {credit_info}")

    # --- Confirmation ---
    if not args.yes:
        try:
            answer = input("\nProceed? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
        if answer and answer not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    # --- Fetch ---
    print("\nFetching flight data from OpenSky Network...")
    try:
        tracks = fetcher.fetch_area_flights(
            begin_ts, end_ts,
            args.lamin, args.lomin, args.lamax, args.lomax,
        )
    except ConfigurationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except OpenSkyAPIError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not tracks:
        print("No flights found for the specified area and time window.")
        sys.exit(0)

    print(f"Retrieved {len(tracks)} flight tracks.")

    # --- Load actypedb cache (optional, best-effort) ---
    actypedb = _try_load_actypedb()

    # --- Convert ---
    output_dir = Path(args.output)
    converter = ScenarioConverter(
        begin_ts=begin_ts,
        airport_label=args.airport,
        lamin=args.lamin,
        lomin=args.lomin,
        lamax=args.lamax,
        lomax=args.lomax,
        actypedb=actypedb,
        output_dir=output_dir,
    )

    print("Converting to BlueSky scenario...")
    scn_path = converter.convert_and_save(tracks)
    print(f"\nScenario saved to: {scn_path}")

    # --- Optionally launch BlueSky ---
    if args.run:
        bluesky_main = _REPO_ROOT / "BlueSky.py"
        if not bluesky_main.exists():
            print("WARNING: BlueSky.py not found; cannot launch.", file=sys.stderr)
        else:
            print(f"\nLaunching BlueSky with scenario: {scn_path}")
            subprocess.Popen([sys.executable, str(bluesky_main), "--scenfile", str(scn_path)])


def _try_load_actypedb() -> dict:
    """Best-effort load of the actypedb cache used by the opensky plugin."""
    try:
        import pickle
        cache_path = _REPO_ROOT / "cache" / "actypedb.p"
        if cache_path.exists():
            with open(cache_path, "rb") as f:
                data = pickle.load(f)
            if isinstance(data, dict):
                log.debug("Loaded actypedb from cache: %d entries", len(data))
                return data
    except Exception as exc:
        log.debug("Could not load actypedb cache: %s", exc)
    return {}


if __name__ == "__main__":
    main()
