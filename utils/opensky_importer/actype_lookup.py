"""Three-tier aircraft type resolution.

Lookup order:
  1. OpenSky metadata API  — icao24 → typecode (ICAO designator), cached locally
  2. BADA 4.2 release.csv  — validate a callsign-derived candidate exists in BADA
  3. Fallback              — 'B738'
"""
import csv
import json
import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)

_FALLBACK_TYPE = "B738"
_API_BASE = "https://opensky-network.org/api"

_METADATA_CACHE_PATH = Path(__file__).parents[2] / "cache" / "opensky" / "actype_cache.json"
_metadata_cache: dict = {}
_metadata_cache_loaded: bool = False

_bada4_icao_set: set = set()
_bada4_loaded: bool = False


def _load_metadata_cache() -> None:
    global _metadata_cache, _metadata_cache_loaded
    if _metadata_cache_loaded:
        return
    _metadata_cache_loaded = True
    if _METADATA_CACHE_PATH.exists():
        try:
            with open(_METADATA_CACHE_PATH, encoding="utf-8") as f:
                _metadata_cache = json.load(f)
            log.debug("Loaded %d entries from actype metadata cache", len(_metadata_cache))
        except Exception as exc:
            log.debug("Could not load actype cache: %s", exc)


def _save_metadata_cache() -> None:
    try:
        _METADATA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_METADATA_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_metadata_cache, f)
    except Exception as exc:
        log.debug("Could not save actype cache: %s", exc)


def _fetch_typecode(icao24: str) -> str:
    """Query OpenSky metadata API for typecode. Returns '' on failure."""
    try:
        url = f"{_API_BASE}/metadata/aircraft/icao/{icao24.lower()}"
        resp = requests.get(url, timeout=(5, 10))
        if resp.status_code == 200:
            return resp.json().get("typecode", "").strip().upper()
    except Exception as exc:
        log.debug("Metadata API error for %s: %s", icao24, exc)
    return ""


def _load_bada4_index() -> None:
    global _bada4_loaded
    if _bada4_loaded:
        return
    _bada4_loaded = True

    csv_path = Path(__file__).parents[2] / "Data" / "BADA" / "BADA_4.2" / "release.csv"
    if not csv_path.exists():
        log.debug("BADA 4.2 release.csv not found at %s — tier-2 lookup disabled", csv_path)
        return

    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                icao = row.get("ICAO", "").strip().upper()
                if icao:
                    _bada4_icao_set.add(icao)
        log.debug("Loaded %d ICAO types from BADA 4.2 release.csv", len(_bada4_icao_set))
    except Exception as exc:
        log.warning("Could not load BADA 4.2 release.csv: %s", exc)


def resolve_actype(icao24: str, callsign: str, actypedb: dict) -> str:
    """Resolve an ICAO aircraft type code using the 3-tier lookup chain."""
    _load_metadata_cache()
    _load_bada4_index()

    key = icao24.lower()

    # --- Tier 1: local metadata cache (populated via OpenSky metadata API) ---
    if key in _metadata_cache:
        cached = _metadata_cache[key]
        if cached:
            log.debug("Cache hit: icao24=%s → %s", key, cached)
            return cached
    else:
        typecode = _fetch_typecode(key)
        _metadata_cache[key] = typecode
        _save_metadata_cache()
        if typecode:
            log.debug("API hit: icao24=%s → %s", key, typecode)
            return typecode

    # --- Tier 2: BADA 4.2 via callsign heuristic ---
    candidate = _guess_type_from_callsign(callsign)
    if candidate and candidate in _bada4_icao_set:
        log.debug("Tier-2 hit: icao24=%s callsign=%s → %s", key, callsign, candidate)
        return candidate

    # --- Tier 3: fallback ---
    log.debug("Unknown type for icao24=%s callsign=%s → %s", key, callsign, _FALLBACK_TYPE)
    return _FALLBACK_TYPE


def _guess_type_from_callsign(callsign: str) -> str:
    import re
    cs = callsign.strip().upper()
    if not cs:
        return ""
    if re.fullmatch(r"[A-Z]{1,2}\d{1,3}[A-Z]?", cs):
        return cs
    return ""
