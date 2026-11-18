"""Place search over OpenStreetMap's Nominatim service, within its usage policy.

Policy: https://operations.osmfoundation.org/policies/nominatim/

* At most one request per second, one at a time, across every grab-mcp process
  on this machine (each Claude session starts its own server process).
* Every request names this app in its User-Agent.
* Answers are cached on disk, so repeated questions do not reach the server.
* When the server says to slow down or refuses the app, searches pause.
* Results carry OpenStreetMap's attribution.

The cache, request spacing and pause live in one SQLite file. A request is made
inside an IMMEDIATE transaction, which holds SQLite's write lock, so a second
process waits for the first to finish.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .countries import Market
from .models import Coordinates, Place

OSM_ATTRIBUTION = "Place data © OpenStreetMap contributors, ODbL (openstreetmap.org/copyright)"
MAX_RESULTS_LIMIT = 10
MAX_QUERY_LENGTH = 200
MIN_REQUEST_INTERVAL_S = 1.0
CACHE_TTL_S = 24 * 60 * 60
CACHE_MAX_ENTRIES = 5000
CACHE_FILE_NAME = "nominatim-cache.sqlite3"
# How long to pause after the server says to slow down (429, 503) or refuses the app (403).
SLOW_DOWN_PAUSE_S = 60
MAX_SLOW_DOWN_PAUSE_S = 24 * 60 * 60
REFUSED_PAUSE_S = 60 * 60
# How long a process waits for another one's request before giving up.
LOCK_WAIT_S = 60
# Coordinates sent for reverse lookups are rounded to about 11 m: enough for an
# address, and it keeps a person's exact position off the wire.
REVERSE_DECIMALS = 4
# Half-width of the box that steers a search towards a point (about 5.5 km at the equator).
NEAR_BOX_DEGREES = 0.05
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, stored_at REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value REAL NOT NULL);
"""


class GeocodingError(Exception):
    """A place search failed in a way worth showing to the person, without internals."""


def _describe_http_error(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (429, 503):
            return "OpenStreetMap's place search asked this app to slow down. Searches are paused for now."
        if status == 403:
            return (
                "OpenStreetMap's place search refused this app, so searches are paused for an hour. "
                "Check GRAB_MCP_USER_AGENT, or point GRAB_MCP_NOMINATIM_URL at another Nominatim server."
            )
        return f"Place search failed (HTTP {status})"
    if isinstance(exc, httpx.TimeoutException):
        return "Place search timed out. Try again shortly."
    return f"Could not reach the place search service ({type(exc).__name__})"


def _pause_after(response: httpx.Response) -> float:
    """Seconds to stop sending after an error reply; 0 when no pause is needed."""
    if response.status_code == 403:
        return REFUSED_PAUSE_S
    if response.status_code not in (429, 503):
        return 0
    try:
        asked = float(response.headers.get("Retry-After", ""))
    except ValueError:  # Missing, or an HTTP date; fall back to the default.
        asked = 0
    if not math.isfinite(asked):
        asked = 0
    return min(max(asked, SLOW_DOWN_PAUSE_S), MAX_SLOW_DOWN_PAUSE_S)


def _place_name(item: dict[str, Any], display_name: str) -> str:
    """The place's own name, else its street address, else the first part of its full address."""
    if item.get("name"):
        return str(item["name"])
    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    street = " ".join(str(address[k]) for k in ("house_number", "road") if address.get(k))
    return street or display_name.split(",", 1)[0].strip()


def _parse_place(item: dict[str, Any], near: Coordinates | None) -> Place:
    point = Coordinates(latitude=float(item["lat"]), longitude=float(item["lon"]))
    display_name = str(item.get("display_name") or "")
    osm_type, osm_id = item.get("osm_type"), item.get("osm_id")
    return Place(
        name=_place_name(item, display_name),
        address=display_name,
        coordinates=point,
        place_id=f"{osm_type}/{osm_id}" if osm_type and osm_id is not None else None,
        distance_m=round(near.distance_km(point) * 1000) if near else None,
    )


def _parse_places(items: list[Any], near: Coordinates | None) -> list[Place]:
    """Parse every usable result; one malformed result does not sink the others."""
    places = []
    for item in items:
        try:
            places.append(_parse_place(item, near))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return places


def _near_box(point: Coordinates) -> str:
    """A viewbox (lon1,lat1,lon2,lat2) around a point, clamped to valid coordinates."""
    d = NEAR_BOX_DEGREES
    left, right = max(point.longitude - d, -180.0), min(point.longitude + d, 180.0)
    top, bottom = min(point.latitude + d, 90.0), max(point.latitude - d, -90.0)
    return f"{left:.4f},{top:.4f},{right:.4f},{bottom:.4f}"


class _SharedStore:
    """The SQLite file that holds the cache and the request timing for all processes.

    Keep it on a local disk: SQLite's locking is unreliable on network drives.
    """

    def __init__(self, path: Path, now: float):
        self._path = path
        try:
            # Searches can say where someone is going, so keep them private to this user.
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with closing(self._connect()) as db:
                db.executescript(_SCHEMA)
                db.execute("DELETE FROM cache WHERE stored_at < ?", (now - CACHE_TTL_S,))
            if os.name == "posix":
                path.chmod(0o600)
        except (OSError, sqlite3.Error) as exc:
            raise GeocodingError(self._describe(exc)) from exc

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=LOCK_WAIT_S, isolation_level=None)

    def _describe(self, exc: Exception) -> str:
        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
            return "Place search is busy with other requests. Try again shortly."
        return (
            f"Could not use the search cache at {self._path} ({type(exc).__name__}). Set GRAB_MCP_CACHE_DIR."
        )

    def cached(self, key: str, now: float) -> Any | None:
        """A fresh cached answer, read without the write lock so hits never queue behind a request."""
        try:
            with closing(self._connect()) as db:
                row = db.execute("SELECT stored_at, body FROM cache WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error as exc:
            raise GeocodingError(self._describe(exc)) from exc
        if row and 0 <= now - row[0] <= CACHE_TTL_S:
            return json.loads(row[1])
        return None

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        """Hold the write lock for the block, and keep whatever it wrote, even if it raises."""
        db = None
        try:
            db = self._connect()
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            if db is not None:
                db.close()
            raise GeocodingError(self._describe(exc)) from exc
        failed = False
        try:
            yield db
        except sqlite3.Error as exc:
            failed = True
            raise GeocodingError(self._describe(exc)) from exc
        except BaseException:
            failed = True
            raise
        finally:
            try:
                if db.in_transaction:
                    db.execute("COMMIT")
            except sqlite3.Error as exc:
                if not failed:  # Never hide the error that is already on its way out.
                    raise GeocodingError(self._describe(exc)) from exc
            finally:
                db.close()


def _read_state(db: sqlite3.Connection, name: str) -> float:
    row = db.execute("SELECT value FROM state WHERE name = ?", (name,)).fetchone()
    return row[0] if row else 0.0


def _write_state(db: sqlite3.Connection, name: str, value: float) -> None:
    db.execute("INSERT OR REPLACE INTO state (name, value) VALUES (?, ?)", (name, value))


class OsmGeocoder:
    """Forward and reverse geocoding with Nominatim. Every call is read-only."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not settings.search_enabled:
            raise GeocodingError("Place search is turned off (GRAB_MCP_NOMINATIM_URL=off).")
        self._settings = settings
        self._clock = clock  # Wall-clock time, because other processes read the stored timestamps.
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=settings.nominatim_url,
            headers={"User-Agent": settings.user_agent, "Accept-Language": settings.language},
            timeout=_TIMEOUT,
            transport=transport,
            follow_redirects=False,
        )
        self._store = _SharedStore(settings.cache_dir / CACHE_FILE_NAME, clock())
        # Threads in this process queue here first, rather than polling SQLite's lock.
        self._lock = threading.Lock()

    def _get(self, path: str, params: dict[str, str], expected: type) -> Any:
        server = self._settings.nominatim_url
        key = json.dumps([server, path, sorted(params.items())], ensure_ascii=False)
        cached = self._store.cached(key, self._clock())
        if cached is not None:
            return cached
        with self._lock, self._store.locked() as db:
            now = self._clock()
            # Another process may have fetched this while we waited for the lock.
            row = db.execute("SELECT stored_at, body FROM cache WHERE key = ?", (key,)).fetchone()
            if row and 0 <= now - row[0] <= CACHE_TTL_S:
                return json.loads(row[1])

            # Stored times come from the wall clock, which can step backwards (clock sync,
            # sleep and resume), so never wait or pause longer than the limits allow.
            paused_until = _read_state(db, f"paused_until {server}")
            if now < paused_until <= now + MAX_SLOW_DOWN_PAUSE_S:
                minutes = max(1, round((paused_until - now) / 60))
                raise GeocodingError(
                    f"Place search is paused for about {minutes} more minute(s) because "
                    "OpenStreetMap's server asked this app to slow down or refused it."
                )
            wait = _read_state(db, f"last_request {server}") + MIN_REQUEST_INTERVAL_S - now
            if wait > 0:
                self._sleep(min(wait, MIN_REQUEST_INTERVAL_S))
            _write_state(db, f"last_request {server}", self._clock())

            try:
                response = self._http.get(path, params=params)
                response.raise_for_status()
                value = response.json()
            except httpx.HTTPStatusError as exc:
                pause = _pause_after(exc.response)
                if pause:
                    _write_state(db, f"paused_until {server}", self._clock() + pause)
                raise GeocodingError(_describe_http_error(exc)) from exc
            except httpx.HTTPError as exc:
                raise GeocodingError(_describe_http_error(exc)) from exc
            except ValueError as exc:  # Body was not JSON.
                raise GeocodingError("Place search sent back an unreadable reply") from exc
            if not isinstance(value, expected):
                raise GeocodingError("Place search sent back an unexpected reply")

            db.execute(
                "INSERT OR REPLACE INTO cache (key, stored_at, body) VALUES (?, ?, ?)",
                (key, now, json.dumps(value)),
            )
            db.execute("DELETE FROM cache WHERE stored_at < ?", (now - CACHE_TTL_S,))
            db.execute(
                "DELETE FROM cache WHERE key NOT IN (SELECT key FROM cache ORDER BY stored_at DESC LIMIT ?)",
                (CACHE_MAX_ENTRIES,),
            )
            return value

    def search_places(
        self, query: str, market: Market, near: Coordinates | None = None, max_results: int = 5
    ) -> list[Place]:
        # Nominatim ignores case, so folding it lets the cache serve "Changi" and "changi" alike.
        query = " ".join(query.split()).casefold()
        if not query:
            raise ValueError("Search text is empty")
        try:
            query.encode("utf-8")
        except UnicodeEncodeError as exc:  # A lone surrogate, which no server can read.
            raise ValueError("Search text has characters that cannot be sent") from exc
        if len(query) > MAX_QUERY_LENGTH:
            raise ValueError(f"Search text is longer than {MAX_QUERY_LENGTH} characters")
        params = {
            "q": query,
            "format": "jsonv2",
            "addressdetails": "1",
            "countrycodes": market.iso2,
            "limit": str(max(1, min(max_results, MAX_RESULTS_LIMIT))),
        }
        if near is not None:
            params["viewbox"] = _near_box(near)  # Preference only; bounded stays 0.
        return _parse_places(self._get("/search", params, list), near)

    def reverse_geocode(self, point: Coordinates) -> Place | None:
        params = {
            "lat": f"{point.latitude:.{REVERSE_DECIMALS}f}",
            "lon": f"{point.longitude:.{REVERSE_DECIMALS}f}",
            "format": "jsonv2",
            "addressdetails": "1",
        }
        data = self._get("/reverse", params, dict)
        if "error" in data or "lat" not in data or "lon" not in data:
            return None  # Nominatim answers {"error": "Unable to geocode"} where nothing is near.
        places = _parse_places([data], None)
        if not places:
            raise GeocodingError("Place search sent back a place without valid coordinates")
        return places[0]
