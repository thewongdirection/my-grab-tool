import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

from mcp import Client

import grab_mcp.server as server_module
from grab_mcp.config import Settings
from grab_mcp.geocoding import OSM_ATTRIBUTION, GeocodingError
from grab_mcp.models import Coordinates, Place
from grab_mcp.server import create_server

CHANGI = Place("Changi Airport T3", "65 Airport Blvd", Coordinates(1.3644, 103.9915), "p1")
MBS = Place("Marina Bay Sands", "10 Bayfront Ave", Coordinates(1.2834, 103.8607), "p2")


class FakeGeocoder:
    """Stands in for OsmGeocoder; records calls."""

    def __init__(self, places=None, error=None):
        self.places = places or {}
        self.error = error
        self.calls = []

    def search_places(self, query, market, near=None, max_results=5):
        self.calls.append(("search", query, market.iso3, near, max_results))
        if self.error:
            raise self.error
        match = self.places.get(query)
        return [match] if match else []

    def reverse_geocode(self, point):
        self.calls.append(("reverse", point))
        return self.places.get("reverse")


def _server(geocoder=None):
    return create_server(Settings(), geocoder=geocoder or FakeGeocoder({"changi": CHANGI, "mbs": MBS}))


async def _call(server, tool, args):
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    text = result.content[0].text
    return result.is_error, (text if result.is_error else json.loads(text))


async def test_lists_five_read_only_tools():
    async with Client(_server()) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {
        "search_places",
        "describe_location",
        "plan_grab_ride",
        "grab_ride_link",
        "grab_food_links",
    }
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)


async def test_plan_ride_with_pickup_gives_distance_and_both_ends():
    geocoder = FakeGeocoder({"changi": CHANGI, "mbs": MBS})
    is_error, data = await _call(
        _server(geocoder), "plan_grab_ride", {"dropoff_query": "mbs", "pickup_query": "changi"}
    )
    assert not is_error
    assert data["pickup"]["name"] == "Changi Airport T3"
    assert data["dropoff"]["name"] == "Marina Bay Sands"
    assert data["straight_line_km"] == 17.1
    assert "not a road distance" in data["note"]
    assert data["attribution"] == OSM_ATTRIBUTION
    params = parse_qs(urlsplit(data["app_link"]).query)
    assert params["pickUpLatitude"] == ["1.364400"] and params["dropOffLatitude"] == ["1.283400"]
    assert "nothing is booked" in data["note"]
    # Drop-off search is biased towards the pickup.
    assert geocoder.calls[1] == ("search", "mbs", "SGP", CHANGI.coordinates, 1)


async def test_plan_ride_without_pickup_uses_phone_location():
    geocoder = FakeGeocoder({"mbs": MBS})
    is_error, data = await _call(_server(geocoder), "plan_grab_ride", {"dropoff_query": "mbs"})
    assert not is_error
    assert data["pickup"] == "current location"
    assert data["straight_line_km"] is None
    assert "not a road distance" not in data["note"]
    assert "pickUpLatitude" not in data["app_link"]


async def test_plan_ride_reports_unmatched_place():
    is_error, text = await _call(_server(), "plan_grab_ride", {"dropoff_query": "atlantis"})
    assert is_error and "no place matching 'atlantis'" in text


async def test_search_errors_reach_the_model_as_tool_errors():
    geocoder = FakeGeocoder(error=GeocodingError("Could not reach the place search service"))
    is_error, text = await _call(_server(geocoder), "search_places", {"query": "mbs"})
    assert is_error and "Could not reach" in text


async def test_search_places_with_country_and_bias():
    geocoder = FakeGeocoder({"mbs": MBS})
    is_error, data = await _call(
        _server(geocoder),
        "search_places",
        {"query": "mbs", "country": "sg", "near_latitude": 1.3, "near_longitude": 103.8},
    )
    assert not is_error and data["country"] == "SGP" and data["places"][0]["place_id"] == "p2"
    assert data["attribution"] == OSM_ATTRIBUTION
    assert geocoder.calls[0][3] == Coordinates(1.3, 103.8)


async def test_half_a_bias_point_is_rejected():
    is_error, text = await _call(_server(), "search_places", {"query": "mbs", "near_latitude": 1.3})
    assert is_error and "near_latitude and near_longitude" in text


async def test_unknown_country_is_rejected():
    is_error, text = await _call(_server(), "grab_food_links", {"query": "pizza", "country": "USA"})
    assert is_error and "Unsupported country" in text


async def test_describe_location_with_no_result():
    is_error, text = await _call(_server(), "describe_location", {"latitude": 0, "longitude": 0})
    assert is_error and "no address" in text


async def test_ride_link_needs_no_search_calls():
    geocoder = FakeGeocoder()
    is_error, data = await _call(
        _server(geocoder),
        "grab_ride_link",
        {"dropoff_latitude": 1.2834, "dropoff_longitude": 103.8607, "country": "MYS"},
    )
    assert not is_error and geocoder.calls == []
    assert "www.grab.com%2Fmy%2Ftransport" in data["tap_link"]


async def test_ride_link_with_half_a_pickup_is_rejected():
    args = {"dropoff_latitude": 1.28, "dropoff_longitude": 103.86, "pickup_latitude": 1.3}
    is_error, text = await _call(_server(), "grab_ride_link", args)
    assert is_error and "pickup_latitude and pickup_longitude" in text


async def test_food_links():
    is_error, data = await _call(_server(), "grab_food_links", {"query": "Chicken Rice"})
    assert not is_error
    assert data["web_search"] == "https://food.grab.com/sg/en/restaurants?search=chicken-rice"


async def test_food_links_reject_empty_query():
    is_error, _ = await _call(_server(), "grab_food_links", {"query": "!!!"})
    assert is_error


async def test_describe_location_reports_search_errors():
    class Broken(FakeGeocoder):
        def reverse_geocode(self, point):
            raise GeocodingError("Place search failed (HTTP 500)")

    is_error, text = await _call(
        _server(Broken()), "describe_location", {"latitude": 1.3, "longitude": 103.8}
    )
    assert is_error and "HTTP 500" in text


def test_geocoder_is_created_once_under_concurrent_first_use(monkeypatch):
    created = []

    def counting_client(settings):
        time.sleep(0.05)  # Widen the race window.
        created.append(settings)
        return FakeGeocoder()

    monkeypatch.setattr(server_module, "OsmGeocoder", counting_client)
    lazy = server_module._LazyGeocoder(Settings(), None)
    threads = [threading.Thread(target=lazy.get) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(created) == 1


async def test_search_turned_off_offers_only_the_link_tools():
    server = create_server(Settings(nominatim_url="off"))
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"grab_ride_link", "grab_food_links"}
    is_error, data = await _call(server, "grab_food_links", {"query": "laksa"})
    assert not is_error and data["web_search"].endswith("search=laksa")


async def test_blank_pickup_means_current_location():
    geocoder = FakeGeocoder({"mbs": MBS})
    is_error, data = await _call(
        _server(geocoder), "plan_grab_ride", {"dropoff_query": "mbs", "pickup_query": "  "}
    )
    assert not is_error and data["pickup"] == "current location"
    assert [call[1] for call in geocoder.calls] == ["mbs"]


async def test_out_of_range_coordinates_are_rejected_before_any_search():
    geocoder = FakeGeocoder()
    is_error, text = await _call(_server(geocoder), "describe_location", {"latitude": 95, "longitude": 0})
    assert is_error and "latitude" in text and geocoder.calls == []


async def test_describe_location_includes_attribution():
    geocoder = FakeGeocoder({"reverse": MBS})
    is_error, data = await _call(
        _server(geocoder), "describe_location", {"latitude": 1.28, "longitude": 103.86}
    )
    assert not is_error and data["name"] == "Marina Bay Sands" and data["attribution"] == OSM_ATTRIBUTION


async def test_search_input_errors_reach_the_model_as_tool_errors():
    class Strict(FakeGeocoder):
        def search_places(self, query, market, near=None, max_results=5):
            raise ValueError("Search text is longer than 200 characters")

    is_error, text = await _call(_server(Strict()), "plan_grab_ride", {"dropoff_query": "x" * 300})
    assert is_error and "longer than 200" in text
