import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import grab_mcp.geocoding as geocoding_module
from grab_mcp.config import Settings
from grab_mcp.countries import get_market
from grab_mcp.geocoding import (
    CACHE_FILE_NAME,
    CACHE_TTL_S,
    MAX_QUERY_LENGTH,
    MIN_REQUEST_INTERVAL_S,
    REFUSED_PAUSE_S,
    SLOW_DOWN_PAUSE_S,
    GeocodingError,
    OsmGeocoder,
)
from grab_mcp.models import Coordinates

SG = get_market("SGP")
MBS_ITEM = {
    "place_id": 123,
    "osm_type": "way",
    "osm_id": 456,
    "lat": "1.2834",
    "lon": "103.8607",
    "name": "Marina Bay Sands",
    "display_name": "Marina Bay Sands, 10, Bayfront Avenue, Downtown Core, Singapore, 018956, Singapore",
    "address": {"house_number": "10", "road": "Bayfront Avenue", "country_code": "sg"},
}


class FakeClock:
    def __init__(self):
        self.now = 1_800_000_000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Recorder:
    """An httpx transport that answers from a function and records each request."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.answer(request)

    def params(self, index=-1):
        return {k: v[0] for k, v in parse_qs(urlsplit(str(self.requests[index].url)).query).items()}


def _json(payload, status=200, headers=None):
    return lambda request: httpx.Response(status, content=json.dumps(payload).encode(), headers=headers)


@pytest.fixture
def make(tmp_path):
    """Build a geocoder with a fake server and clock; geocoders built together share a cache folder."""

    def build(answer, clock=None, **settings):
        recorder = Recorder(answer)
        clock = clock or FakeClock()
        geocoder = OsmGeocoder(
            Settings(cache_dir=tmp_path / "cache", **settings),
            transport=httpx.MockTransport(recorder),
            clock=clock,
            sleep=clock.sleep,
        )
        return geocoder, recorder, clock

    return build


class TestSearch:
    def test_sends_policy_headers_and_country_filter(self, make):
        geocoder, recorder, _ = make(_json([MBS_ITEM]))
        places = geocoder.search_places("  Marina   Bay Sands ", SG)
        request = recorder.requests[0]
        assert request.url.host == "nominatim.openstreetmap.org" and request.url.path == "/search"
        assert request.headers["User-Agent"].startswith("grab-mcp/")
        assert request.headers["Accept-Language"] == "en"
        assert recorder.params() == {
            "q": "marina bay sands",
            "format": "jsonv2",
            "addressdetails": "1",
            "countrycodes": "sg",
            "limit": "5",
        }
        assert places[0].name == "Marina Bay Sands"
        assert places[0].coordinates == Coordinates(1.2834, 103.8607)
        assert places[0].place_id == "way/456"
        assert places[0].distance_m is None

    def test_near_point_adds_a_preference_box_and_distances(self, make):
        geocoder, recorder, _ = make(_json([MBS_ITEM]))
        near = Coordinates(1.3, 103.8)
        place = geocoder.search_places("mbs", SG, near=near)[0]
        params = recorder.params()
        assert params["viewbox"] == "103.7500,1.3500,103.8500,1.2500"
        assert "bounded" not in params  # A preference, not a filter.
        assert place.distance_m == round(near.distance_km(place.coordinates) * 1000)

    def test_near_box_is_clamped_at_the_antimeridian(self, make):
        geocoder, recorder, _ = make(_json([]))
        geocoder.search_places("x", SG, near=Coordinates(89.99, 179.99))
        left, top, right, bottom = map(float, recorder.params()["viewbox"].split(","))
        assert right == 180.0 and top == 90.0 and left < right and bottom < top

    @pytest.mark.parametrize(("asked", "sent"), [(0, "1"), (3, "3"), (50, "10")])
    def test_result_count_is_clamped(self, make, asked, sent):
        geocoder, recorder, _ = make(_json([]))
        geocoder.search_places("mbs", SG, max_results=asked)
        assert recorder.params()["limit"] == sent

    def test_unnamed_place_is_named_by_its_street_address(self, make):
        geocoder, _, _ = make(_json([{**MBS_ITEM, "name": ""}]))
        assert geocoder.search_places("10 bayfront", SG)[0].name == "10 Bayfront Avenue"

    def test_unnamed_place_without_address_parts_uses_its_full_address(self, make):
        item = {**MBS_ITEM, "name": "", "address": {}, "display_name": "Bayfront, Singapore"}
        geocoder, _, _ = make(_json([item]))
        assert geocoder.search_places("bayfront", SG)[0].name == "Bayfront"

    @pytest.mark.parametrize(
        "bad",
        [{"display_name": "nowhere"}, {**MBS_ITEM, "lat": "123"}, {**MBS_ITEM, "lat": None}, "junk"],
    )
    def test_malformed_results_are_skipped_not_fatal(self, make, bad):
        geocoder, _, _ = make(_json([bad, MBS_ITEM]))
        assert [p.place_id for p in geocoder.search_places("mbs", SG)] == ["way/456"]

    @pytest.mark.parametrize("query", ["", "   ", "x" * (MAX_QUERY_LENGTH + 1), "cafe \ud800"])
    def test_bad_queries_are_rejected_before_any_request(self, make, query):
        geocoder, recorder, _ = make(_json([]))
        with pytest.raises(ValueError):
            geocoder.search_places(query, SG)
        assert recorder.requests == []

    def test_custom_server_and_agent_are_used(self, make):
        geocoder, recorder, _ = make(
            _json([]),
            nominatim_url="https://geo.example.org/nominatim/",
            user_agent="my-copy/1.0",
            language="th,en",
        )
        geocoder.search_places("mbs", SG)
        request = recorder.requests[0]
        assert str(request.url).startswith("https://geo.example.org/nominatim/search?")
        assert request.headers["User-Agent"] == "my-copy/1.0"
        assert request.headers["Accept-Language"] == "th,en"


class TestReverse:
    def test_returns_the_nearest_address_from_a_rounded_point(self, make):
        geocoder, recorder, _ = make(_json(MBS_ITEM))
        place = geocoder.reverse_geocode(Coordinates(1.28341234, 103.86071234))
        assert recorder.requests[0].url.path == "/reverse"
        assert recorder.params() == {
            "lat": "1.2834",
            "lon": "103.8607",
            "format": "jsonv2",
            "addressdetails": "1",
        }
        assert place.name == "Marina Bay Sands"

    def test_points_metres_apart_share_one_request(self, make):
        geocoder, recorder, _ = make(_json(MBS_ITEM))
        geocoder.reverse_geocode(Coordinates(1.2834001, 103.8607001))
        geocoder.reverse_geocode(Coordinates(1.2834002, 103.8607002))
        assert len(recorder.requests) == 1

    def test_nothing_near_gives_none(self, make):
        geocoder, _, _ = make(_json({"error": "Unable to geocode"}))
        assert geocoder.reverse_geocode(Coordinates(0, 0)) is None

    def test_place_with_bad_coordinates_is_an_error(self, make):
        geocoder, _, _ = make(_json({**MBS_ITEM, "lat": "123"}))
        with pytest.raises(GeocodingError, match="valid coordinates"):
            geocoder.reverse_geocode(Coordinates(1.28, 103.86))


class TestRequestSpacing:
    def test_requests_are_spaced_at_least_a_second_apart(self, make):
        geocoder, recorder, clock = make(_json([]))
        geocoder.search_places("a", SG)
        clock.now += 0.25
        geocoder.search_places("b", SG)
        assert len(recorder.requests) == 2
        assert clock.slept == [pytest.approx(MIN_REQUEST_INTERVAL_S - 0.25)]

    def test_no_wait_once_a_second_has_passed(self, make):
        geocoder, _, clock = make(_json([]))
        geocoder.search_places("a", SG)
        clock.now += 5
        geocoder.search_places("b", SG)
        assert clock.slept == []

    def test_failed_requests_still_count(self, make):
        geocoder, _, clock = make(_json({}, status=500))
        for _ in range(2):
            with pytest.raises(GeocodingError):
                geocoder.search_places("a", SG)
        assert clock.slept == [pytest.approx(MIN_REQUEST_INTERVAL_S)]

    def test_spacing_is_shared_by_separate_processes(self, make):
        clock = FakeClock()
        first, _, _ = make(_json([]), clock=clock)
        second, _, _ = make(_json([]), clock=clock)  # Same cache folder, as two sessions would have.
        first.search_places("a", SG)
        second.search_places("b", SG)
        assert clock.slept == [pytest.approx(MIN_REQUEST_INTERVAL_S)]

    def test_a_clock_that_steps_back_never_means_a_long_wait(self, make):
        geocoder, recorder, clock = make(_json([]))
        geocoder.search_places("a", SG)
        clock.now -= 3600  # Clock sync, or sleep and resume.
        geocoder.search_places("b", SG)
        assert clock.slept == [MIN_REQUEST_INTERVAL_S] and len(recorder.requests) == 2

    def test_only_one_request_is_in_flight_at_a_time(self, tmp_path):
        in_flight, most = 0, 0
        guard = threading.Lock()

        def slow(request):
            nonlocal in_flight, most
            with guard:
                in_flight += 1
                most = max(most, in_flight)
            time.sleep(0.02)
            with guard:
                in_flight -= 1
            return httpx.Response(200, json=[])

        clock = FakeClock()
        geocoders = [
            OsmGeocoder(
                Settings(cache_dir=tmp_path),
                transport=httpx.MockTransport(slow),
                clock=clock,
                sleep=clock.sleep,
            )
            for _ in range(2)
        ]
        threads = [
            threading.Thread(target=geocoders[i % 2].search_places, args=(f"q{i}", SG)) for i in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert most == 1


class TestCache:
    def test_repeated_questions_are_answered_from_the_cache(self, make):
        geocoder, recorder, clock = make(_json([MBS_ITEM]))
        first = geocoder.search_places("Changi Airport", SG)
        second = geocoder.search_places(" changi   AIRPORT ", SG)
        assert first == second and len(recorder.requests) == 1 and clock.slept == []

    def test_cache_outlives_the_process(self, make):
        clock = FakeClock()
        make(_json([MBS_ITEM]), clock=clock)[0].search_places("mbs", SG)
        later, recorder, _ = make(_json([]), clock=clock)
        assert later.search_places("mbs", SG)[0].name == "Marina Bay Sands"
        assert recorder.requests == []

    def test_cache_entries_expire(self, make):
        geocoder, recorder, clock = make(_json([MBS_ITEM]))
        geocoder.search_places("mbs", SG)
        clock.now += CACHE_TTL_S + 1
        geocoder.search_places("mbs", SG)
        assert len(recorder.requests) == 2

    def test_cache_is_kept_per_server(self, make):
        clock = FakeClock()
        make(_json([MBS_ITEM]), clock=clock)[0].search_places("mbs", SG)
        other, recorder, _ = make(_json([]), clock=clock, nominatim_url="https://geo.example.org")
        assert other.search_places("mbs", SG) == [] and len(recorder.requests) == 1

    def test_errors_are_not_cached(self, make):
        answers = iter([httpx.Response(500), httpx.Response(200, json=[MBS_ITEM])])
        geocoder, recorder, clock = make(lambda request: next(answers))
        with pytest.raises(GeocodingError):
            geocoder.search_places("mbs", SG)
        clock.now += 2
        assert geocoder.search_places("mbs", SG)[0].name == "Marina Bay Sands"
        assert len(recorder.requests) == 2

    def test_replies_of_the_wrong_shape_are_not_cached(self, make):
        answers = iter([httpx.Response(200, json={"error": "busy"}), httpx.Response(200, json=[MBS_ITEM])])
        geocoder, recorder, _ = make(lambda request: next(answers))
        with pytest.raises(GeocodingError, match="unexpected"):
            geocoder.search_places("mbs", SG)
        assert geocoder.search_places("mbs", SG)[0].name == "Marina Bay Sands"
        assert len(recorder.requests) == 2

    def test_cache_file_lives_in_the_cache_folder(self, make, tmp_path):
        make(_json([]))[0].search_places("mbs", SG)
        assert (tmp_path / "cache" / CACHE_FILE_NAME).is_file()

    def test_unusable_cache_folder_is_a_clear_error(self, tmp_path):
        blocker = tmp_path / "a-file"
        blocker.write_text("not a folder")
        with pytest.raises(GeocodingError, match="GRAB_MCP_CACHE_DIR"):
            OsmGeocoder(Settings(cache_dir=blocker / "cache"))


class TestPauses:
    @pytest.mark.parametrize(
        ("status", "headers", "pause"),
        [
            (429, None, SLOW_DOWN_PAUSE_S),
            (429, {"Retry-After": "300"}, 300),
            (429, {"Retry-After": "5"}, SLOW_DOWN_PAUSE_S),
            (503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, SLOW_DOWN_PAUSE_S),
            (429, {"Retry-After": "nan"}, SLOW_DOWN_PAUSE_S),
            (429, {"Retry-After": "inf"}, SLOW_DOWN_PAUSE_S),
            (403, None, REFUSED_PAUSE_S),
        ],
    )
    def test_server_pushback_pauses_searches(self, make, status, headers, pause):
        geocoder, recorder, clock = make(_json({}, status=status, headers=headers))
        with pytest.raises(GeocodingError):
            geocoder.search_places("a", SG)
        clock.now += pause - 1
        with pytest.raises(GeocodingError, match="paused"):
            geocoder.search_places("b", SG)
        assert len(recorder.requests) == 1  # The paused search never reached the server.
        clock.now += 2
        with pytest.raises(GeocodingError):
            geocoder.search_places("c", SG)
        assert len(recorder.requests) == 2

    def test_pause_is_shared_by_separate_processes(self, make):
        clock = FakeClock()
        with pytest.raises(GeocodingError):
            make(_json({}, status=429), clock=clock)[0].search_places("a", SG)
        other, recorder, _ = make(_json([]), clock=clock)
        with pytest.raises(GeocodingError, match="paused"):
            other.search_places("b", SG)
        assert recorder.requests == []

    def test_cached_answers_still_work_while_paused(self, make):
        answers = iter([httpx.Response(200, json=[MBS_ITEM]), httpx.Response(429)])
        geocoder, _, clock = make(lambda request: next(answers))
        geocoder.search_places("mbs", SG)
        clock.now += 2
        with pytest.raises(GeocodingError):
            geocoder.search_places("other", SG)
        assert geocoder.search_places("mbs", SG)[0].name == "Marina Bay Sands"

    def test_other_errors_do_not_pause(self, make):
        geocoder, recorder, clock = make(_json({}, status=500))
        with pytest.raises(GeocodingError, match="HTTP 500"):
            geocoder.search_places("a", SG)
        clock.now += 2
        with pytest.raises(GeocodingError, match="HTTP 500"):
            geocoder.search_places("a", SG)
        assert len(recorder.requests) == 2


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "message"),
        [(429, "slow down"), (503, "slow down"), (403, "GRAB_MCP_USER_AGENT"), (500, "HTTP 500")],
    )
    def test_http_errors_say_what_to_do(self, make, status, message):
        geocoder, _, _ = make(_json({}, status=status))
        with pytest.raises(GeocodingError, match=message):
            geocoder.search_places("mbs", SG)

    def test_redirects_are_not_followed(self, make):
        redirect = httpx.Response(302, headers={"Location": "https://elsewhere.example/"})
        geocoder, recorder, _ = make(lambda request: redirect)
        with pytest.raises(GeocodingError, match="HTTP 302"):
            geocoder.search_places("mbs", SG)
        assert len(recorder.requests) == 1

    def test_timeout(self, make):
        def time_out(request):
            raise httpx.ReadTimeout("slow", request=request)

        geocoder, _, _ = make(time_out)
        with pytest.raises(GeocodingError, match="timed out"):
            geocoder.search_places("mbs", SG)

    def test_unreachable(self, make):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        geocoder, _, _ = make(refuse)
        with pytest.raises(GeocodingError, match="Could not reach"):
            geocoder.search_places("mbs", SG)

    def test_non_json_reply(self, make):
        geocoder, _, _ = make(lambda request: httpx.Response(200, content=b"<html>busy</html>"))
        with pytest.raises(GeocodingError, match="unreadable"):
            geocoder.search_places("mbs", SG)

    @pytest.mark.parametrize("payload", [{"unexpected": True}, "text"])
    def test_wrong_shape_for_search(self, make, payload):
        geocoder, _, _ = make(_json(payload))
        with pytest.raises(GeocodingError, match="unexpected"):
            geocoder.search_places("mbs", SG)

    def test_wrong_shape_for_reverse(self, make):
        geocoder, _, _ = make(_json([MBS_ITEM]))
        with pytest.raises(GeocodingError, match="unexpected"):
            geocoder.reverse_geocode(Coordinates(1.28, 103.86))

    def test_search_can_be_turned_off(self):
        with pytest.raises(GeocodingError, match="turned off"):
            OsmGeocoder(Settings(nominatim_url="off"))


class TestSharedStore:
    def test_cache_hits_do_not_wait_for_another_process(self, make, tmp_path, monkeypatch):
        monkeypatch.setattr(geocoding_module, "LOCK_WAIT_S", 0.2)
        geocoder, _, _ = make(_json([MBS_ITEM]))
        geocoder.search_places("mbs", SG)
        with sqlite3.connect(tmp_path / "cache" / CACHE_FILE_NAME, isolation_level=None) as other:
            other.execute("BEGIN IMMEDIATE")  # Another process is mid-request.
            assert geocoder.search_places("mbs", SG)[0].name == "Marina Bay Sands"
            with pytest.raises(GeocodingError, match="busy"):
                geocoder.search_places("something new", SG)
            other.execute("ROLLBACK")

    def test_a_pause_from_a_clock_far_ahead_is_ignored(self, make):
        geocoder, recorder, clock = make(_json({}, status=429))
        with pytest.raises(GeocodingError):
            geocoder.search_places("a", SG)
        clock.now -= 10 * 24 * 3600  # The pause now looks ten days long, which cannot be right.
        with pytest.raises(GeocodingError, match="slow down"):
            geocoder.search_places("b", SG)
        assert len(recorder.requests) == 2

    def test_expired_entries_are_dropped_when_the_store_opens(self, make, tmp_path):
        clock = FakeClock()
        make(_json([MBS_ITEM]), clock=clock)[0].search_places("mbs", SG)
        clock.now += CACHE_TTL_S + 1
        make(_json([]), clock=clock)
        with sqlite3.connect(tmp_path / "cache" / CACHE_FILE_NAME) as db:
            assert db.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
    def test_cache_is_private_to_the_user(self, make, tmp_path):
        make(_json([]))
        assert (tmp_path / "cache").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "cache" / CACHE_FILE_NAME).stat().st_mode & 0o777 == 0o600

    def test_separate_processes_take_turns_and_share_the_cache(self, tmp_path):
        script = textwrap.dedent(
            """
            import sys, time, httpx
            from grab_mcp.config import Settings
            from grab_mcp.countries import get_market
            from grab_mcp.geocoding import OsmGeocoder

            def answer(request):
                start = time.time()
                time.sleep(0.3)
                print(f"request {start:.3f} {time.time():.3f}", flush=True)
                return httpx.Response(200, json=[])

            geocoder = OsmGeocoder(Settings(cache_dir=sys.argv[1]), transport=httpx.MockTransport(answer))
            geocoder.search_places(sys.argv[2], get_market("SGP"))
            geocoder.search_places("shared", get_market("SGP"))
            """
        )
        runs = [
            subprocess.Popen(  # noqa: S603 - our own interpreter and script
                [sys.executable, "-c", script, str(tmp_path), name], stdout=subprocess.PIPE, text=True
            )
            for name in ("first", "second")
        ]
        outputs = [run.communicate(timeout=60)[0] for run in runs]
        assert all(run.returncode == 0 for run in runs)
        spans = sorted(
            (float(start), float(end))
            for line in "".join(outputs).splitlines()
            for _, start, end in [line.split()]
        )
        assert len(spans) == 3  # "shared" was fetched once and then served from the cache.
        for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
            assert start >= end  # Never two requests at once.
            assert start - end >= MIN_REQUEST_INTERVAL_S - 0.75  # Spaced by the policy interval.
