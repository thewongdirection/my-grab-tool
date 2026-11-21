# SPDX-License-Identifier: MIT
"""MCP server exposing OpenStreetMap place search and Grab app hand-off links.

Nothing here books, orders or pays. Ride and food tools return links; the
person opens them and confirms in the Grab app.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .config import Settings
from .countries import Market, get_market
from .geocoding import OSM_ATTRIBUTION, GeocodingError, OsmGeocoder
from .links import build_food_links, build_ride_links
from .models import Coordinates, Place

SEARCH_INSTRUCTIONS = """\
Tools for getting around and eating in Southeast Asia with Grab.
- Use search_places to turn names or addresses into coordinates (OpenStreetMap data).
- Use plan_grab_ride to resolve a trip and get a link that opens the Grab app with it filled in.
- Use grab_food_links to send the person to GrabFood for a dish or cuisine.
Search with place names or addresses only, never people's names or other personal details:
searches go to a public OpenStreetMap server that allows one request a second, so do not
search in bulk or repeat a search that failed."""
LINKS_ONLY_INSTRUCTIONS = """\
Tools for getting around and eating in Southeast Asia with Grab. Place search is turned off.
- Use grab_ride_link with coordinates the person gives you.
- Use grab_food_links to send the person to GrabFood for a dish or cuisine."""
LIMITS = """
These tools cannot book rides, quote Grab fares or travel times, place orders or pay. Say so,
and tell the person to check the trip, price and payment in the Grab app."""

HAND_OFF_NOTE = (
    "Open tap_link on the phone. Grab shows the fare and asks you to confirm; nothing is booked until you do."
)
LINK_CAVEAT = "Grab does not document these links, so they may stop working."
STRAIGHT_LINE_NOTE = "straight_line_km is as the crow flies, not a road distance; Grab shows the real trip."

_READ_ONLY_REMOTE = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)
_READ_ONLY_LOCAL = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)


@contextmanager
def _as_tool_error() -> Iterator[None]:
    """Show expected failures (bad input, search problems) to the model as tool errors."""
    try:
        yield
    except (GeocodingError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def _point(latitude: float, longitude: float) -> Coordinates:
    with _as_tool_error():
        return Coordinates(latitude=latitude, longitude=longitude)


def _optional_point(latitude: float | None, longitude: float | None, what: str) -> Coordinates | None:
    if latitude is None and longitude is None:
        return None
    if latitude is None or longitude is None:
        raise ToolError(f"Give both {what}_latitude and {what}_longitude, or neither")
    return _point(latitude, longitude)


class _LazyGeocoder:
    """Creates the geocoder on first use, once, even when tools run in parallel.

    Lazy so the link-only tools work even when place search is turned off.
    """

    def __init__(self, settings: Settings, geocoder: OsmGeocoder | None):
        self._settings = settings
        self._geocoder = geocoder
        self._lock = threading.Lock()

    def get(self) -> OsmGeocoder:
        with self._lock:
            if self._geocoder is None:
                self._geocoder = OsmGeocoder(self._settings)
            return self._geocoder


MarketResolver = Callable[[str | None], Market]


def _add_search_tools(
    server: MCPServer, resolve_market: MarketResolver, lazy_geocoder: _LazyGeocoder
) -> None:
    def first_match(query: str, market: Market, near: Coordinates | None = None) -> Place:
        with _as_tool_error():
            places = lazy_geocoder.get().search_places(query, market, near=near, max_results=1)
        if not places:
            raise ToolError(f"OpenStreetMap found no place matching {query!r} in {market.iso3}")
        return places[0]

    @server.tool(annotations=_READ_ONLY_REMOTE)
    def search_places(
        query: str,
        country: str | None = None,
        near_latitude: float | None = None,
        near_longitude: float | None = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        """Find places by name or address with OpenStreetMap.

        country is an ISO alpha-3 code (SGP, MYS, IDN, THA, VNM, PHL, KHM, MMR) or
        a two-letter code; it defaults to the server's home country. Give
        near_latitude and near_longitude to prefer results close to a point; each
        result then carries its straight-line distance_m from that point.
        """
        market = resolve_market(country)
        near = _optional_point(near_latitude, near_longitude, "near")
        with _as_tool_error():
            places = lazy_geocoder.get().search_places(query, market, near=near, max_results=max_results)
        return {
            "country": market.iso3,
            "places": [p.to_dict() for p in places],
            "attribution": OSM_ATTRIBUTION,
        }

    @server.tool(annotations=_READ_ONLY_REMOTE)
    def describe_location(latitude: float, longitude: float) -> dict[str, Any]:
        """Return the nearest address for a coordinate (reverse geocoding with OpenStreetMap)."""
        point = _point(latitude, longitude)
        with _as_tool_error():
            place = lazy_geocoder.get().reverse_geocode(point)
        if place is None:
            raise ToolError("OpenStreetMap has no address near that point")
        return {**place.to_dict(), "attribution": OSM_ATTRIBUTION}

    @server.tool(annotations=_READ_ONLY_REMOTE)
    def plan_grab_ride(
        dropoff_query: str, pickup_query: str | None = None, country: str | None = None
    ) -> dict[str, Any]:
        """Plan a Grab ride from place names and return a link that opens Grab with it filled in.

        Resolves each name to its best OpenStreetMap match. Without pickup_query,
        Grab uses the phone's location. Always show the person the matched places
        so they can catch a wrong match.
        """
        market = resolve_market(country)
        pickup_query = (pickup_query or "").strip()
        pickup = first_match(pickup_query, market) if pickup_query else None
        dropoff = first_match(dropoff_query, market, near=pickup.coordinates if pickup else None)

        result: dict[str, Any] = {
            "pickup": pickup.to_dict() if pickup else "current location",
            "dropoff": dropoff.to_dict(),
            "straight_line_km": (
                round(pickup.coordinates.distance_km(dropoff.coordinates), 1) if pickup else None
            ),
        }
        result.update(
            build_ride_links(
                market,
                dropoff=dropoff.coordinates,
                dropoff_label=dropoff.name or dropoff.address,
                pickup=pickup.coordinates if pickup else None,
                pickup_label=(pickup.name or pickup.address) if pickup else None,
            )
        )
        notes = [HAND_OFF_NOTE, LINK_CAVEAT] + ([STRAIGHT_LINE_NOTE] if pickup else [])
        result["note"] = " ".join(notes)
        result["attribution"] = OSM_ATTRIBUTION
        return result


def _add_link_tools(server: MCPServer, resolve_market: MarketResolver) -> None:
    @server.tool(annotations=_READ_ONLY_LOCAL)
    def grab_ride_link(
        dropoff_latitude: float,
        dropoff_longitude: float,
        dropoff_label: str | None = None,
        pickup_latitude: float | None = None,
        pickup_longitude: float | None = None,
        pickup_label: str | None = None,
        country: str | None = None,
    ) -> dict[str, Any]:
        """Build a link that opens the Grab app's ride booking screen for known coordinates.

        Leave out the pickup to use the phone's location. Labels are display text
        only; the coordinates decide where the car goes, so take them from
        search_places rather than guessing. Does not call any service.
        """
        market = resolve_market(country)
        links = build_ride_links(
            market,
            dropoff=_point(dropoff_latitude, dropoff_longitude),
            dropoff_label=dropoff_label,
            pickup=_optional_point(pickup_latitude, pickup_longitude, "pickup"),
            pickup_label=pickup_label,
        )
        return {**links, "note": f"{HAND_OFF_NOTE} {LINK_CAVEAT}"}

    @server.tool(annotations=_READ_ONLY_LOCAL)
    def grab_food_links(query: str, country: str | None = None) -> dict[str, Any]:
        """Build a GrabFood website search link for a dish or cuisine, and a link that opens GrabFood.

        Menus, prices and checkout stay on GrabFood; this tool reads nothing from it.
        """
        market = resolve_market(country)
        with _as_tool_error():
            links = build_food_links(market, query)
        return {**links, "note": LINK_CAVEAT}


def create_server(settings: Settings | None = None, geocoder: OsmGeocoder | None = None) -> MCPServer:
    """Build the server. ``geocoder`` is injectable for tests; otherwise it is created on first use.

    With place search turned off, only the link tools are offered, since the others could only fail.
    """
    settings = settings or Settings.from_env()

    def resolve_market(country: str | None) -> Market:
        with _as_tool_error():
            return get_market(country or settings.default_country)

    instructions = (SEARCH_INSTRUCTIONS if settings.search_enabled else LINKS_ONLY_INSTRUCTIONS) + LIMITS
    server = MCPServer(name="grab", instructions=instructions, version=__version__)
    if settings.search_enabled:
        _add_search_tools(server, resolve_market, _LazyGeocoder(settings, geocoder))
    _add_link_tools(server, resolve_market)
    return server


def main() -> None:
    create_server().run()  # stdio transport
