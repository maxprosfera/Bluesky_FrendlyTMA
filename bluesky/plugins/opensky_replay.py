"""BlueSky plugin: load historical OpenSky traffic as a scenario.

Stack command:
    LOADOPENSKY <datetime> [lamin lomin lamax lomax [duration]]

Examples:
    LOADOPENSKY 2024-11-15T14:00
    LOADOPENSKY 2024-11-15T14:00 58.5 17.0 60.5 20.5
    LOADOPENSKY 2024-11-15T14:00 58.5 17.0 60.5 20.5 60
"""
import sys
import threading
from calendar import timegm
from datetime import datetime, timezone
from pathlib import Path

from bluesky import settings, stack

_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.opensky_importer.fetcher import OpenSkyFetcher, ConfigurationError
from utils.opensky_importer.converter import ScenarioConverter

settings.set_variable_defaults(
    opensky_client_id='',
    opensky_client_secret='',
    opensky_default_lamin=58.5,
    opensky_default_lomin=17.0,
    opensky_default_lamax=60.5,
    opensky_default_lomax=20.5,
    opensky_default_duration=30,
)

_fetch_active: bool = False


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def init_plugin():
    config = {
        'plugin_name': 'OPENSKY_REPLAY',
        'plugin_type': 'sim',
    }

    stackfunctions = {
        'LOADOPENSKY': [
            'LOADOPENSKY datetime [lamin lomin lamax lomax [duration]]',
            'txt,[float,float,float,float,float]',
            loadopensky,
            'Fetch historical OpenSky traffic and save it as a BlueSky scenario.',
        ]
    }

    return config, stackfunctions


# ---------------------------------------------------------------------------
# Stack command handler
# ---------------------------------------------------------------------------

def loadopensky(dtstr, lamin=None, lomin=None, lamax=None, lomax=None, duration=None):
    global _fetch_active

    if _fetch_active:
        stack.stack('ECHO OpenSky: a fetch is already in progress, please wait.')
        return False, 'Fetch already in progress.'

    if lamin is None:
        lamin = settings.opensky_default_lamin
    if lomin is None:
        lomin = settings.opensky_default_lomin
    if lamax is None:
        lamax = settings.opensky_default_lamax
    if lomax is None:
        lomax = settings.opensky_default_lomax
    if duration is None:
        duration = settings.opensky_default_duration

    duration = min(max(1, int(float(duration))), 59)

    try:
        start_dt = _parse_datetime_utc(dtstr)
    except ValueError as exc:
        return False, str(exc)

    begin_ts = int(timegm(start_dt.utctimetuple()))
    end_ts = begin_ts + duration * 60

    client_id = settings.opensky_client_id
    client_secret = settings.opensky_client_secret

    try:
        fetcher = OpenSkyFetcher(client_id=client_id, client_secret=client_secret)
    except ConfigurationError as exc:
        return False, str(exc)

    warnings = fetcher.validate(begin_ts, end_ts)
    for w in warnings:
        stack.stack(f'ECHO {w}')
        if w.startswith('ERROR:'):
            return False, w

    scn_root = _REPO_ROOT / 'scenario' / 'OpenSky'
    expected_scn = scn_root / f"opensky_ESSA_{start_dt.strftime('%Y%m%d_%H%M')}.scn"

    if expected_scn.exists():
        rel = str(expected_scn.relative_to(_REPO_ROOT))
        stack.stack(f'ECHO Scenario already saved: {rel}')
        return True, 'Already saved.'

    credit_info = fetcher.estimate_credits(begin_ts, end_ts, lamin, lomin, lamax, lomax)
    stack.stack(f'ECHO {credit_info}')
    stack.stack('ECHO Fetching OpenSky data in background...')

    _fetch_active = True

    t = threading.Thread(
        target=_fetch_and_convert,
        args=(fetcher, begin_ts, end_ts, lamin, lomin, lamax, lomax, 'ESSA', scn_root),
        daemon=True,
    )
    t.start()

    return True, 'OpenSky fetch started.'


# ---------------------------------------------------------------------------
# Background worker — calls stack.stack() directly (GIL-safe list.append)
# so results appear in INIT mode without needing the update() hook.
# ---------------------------------------------------------------------------

def _fetch_and_convert(fetcher, begin_ts, end_ts, lamin, lomin, lamax, lomax,
                       airport_label, scn_root):
    global _fetch_active
    try:
        tracks = fetcher.fetch_area_flights(
            begin_ts, end_ts, lamin, lomin, lamax, lomax, use_cache=True
        )

        if not tracks:
            stack.stack('ECHO OpenSky: no flights found for the specified area and time window.')
            return

        converter = ScenarioConverter(
            begin_ts=begin_ts,
            airport_label=airport_label,
            lamin=lamin, lomin=lomin, lamax=lamax, lomax=lomax,
            actypedb={},
            output_dir=scn_root,
        )
        scn_path = converter.convert_and_save(tracks)
        rel = str(Path(scn_path).relative_to(_REPO_ROOT))
        stack.stack(f'ECHO Scenario saved: {rel}')

    except BaseException as exc:
        stack.stack(f'ECHO OpenSky error: {exc}')
    finally:
        _fetch_active = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_datetime_utc(dt_str: str) -> datetime:
    for fmt in (
        '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M',
        '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
    ):
        try:
            return datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(
        f"Cannot parse datetime '{dt_str}'. "
        "Use format: '2024-11-15T14:00' or '2024-11-15 14:00'"
    )
