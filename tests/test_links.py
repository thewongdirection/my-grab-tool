from urllib.parse import parse_qs, urlsplit

import pytest

from grab_mcp.countries import get_market
from grab_mcp.links import (
    MAX_LABEL_LENGTH,
    ONELINK_BASE,
    build_food_links,
    build_ride_links,
    slugify_food_query,
)
from grab_mcp.models import Coordinates

SG = get_market("SGP")
MBS = Coordinates(1.2834, 103.8607)
CHANGI = Coordinates(1.3644, 103.9915)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


class TestRideLinks:
    def test_dropoff_only_leaves_pickup_to_the_phone(self):
        links = build_ride_links(SG, dropoff=MBS, dropoff_label="Marina Bay Sands")
        params = _query(links["app_link"])
        assert links["app_link"].startswith("grab://open?")
        assert params["screenType"] == ["BOOKING"]
        assert params["dropOffLatitude"] == ["1.283400"]
        assert params["dropOffLongitude"] == ["103.860700"]
        assert params["dropOffAddress"] == ["Marina Bay Sands"]
        assert not any(key.startswith("pickUp") for key in params)

    def test_pickup_is_included_when_given(self):
        links = build_ride_links(SG, dropoff=MBS, pickup=CHANGI, pickup_label="Changi T3")
        params = _query(links["app_link"])
        assert params["pickUpLatitude"] == ["1.364400"]
        assert params["pickUpLongitude"] == ["103.991500"]
        assert params["pickUpAddress"] == ["Changi T3"]

    def test_spaces_are_percent_encoded_not_plus(self):
        links = build_ride_links(SG, dropoff=MBS, dropoff_label="Marina Bay Sands")
        assert "Marina%20Bay%20Sands" in links["app_link"]
        assert "+" not in links["app_link"]

    def test_tap_link_wraps_the_app_link_in_grabs_onelink(self):
        links = build_ride_links(SG, dropoff=MBS)
        assert links["tap_link"].startswith(ONELINK_BASE + "?")
        params = _query(links["tap_link"])
        assert params["af_dp"] == [links["app_link"]]
        assert params["af_force_deeplink"] == ["true"]
        assert params["af_web_dp"] == ["https://www.grab.com/sg/transport/"]

    def test_fallback_page_follows_the_market(self):
        links = build_ride_links(get_market("THA"), dropoff=MBS)
        assert _query(links["tap_link"])["af_web_dp"] == ["https://www.grab.com/th/transport/"]

    def test_label_cannot_inject_extra_parameters(self):
        links = build_ride_links(SG, dropoff=MBS, dropoff_label="Home&screenType=PAYMENT#x")
        params = _query(links["app_link"])
        assert params["screenType"] == ["BOOKING"]
        assert params["dropOffAddress"] == ["Home&screenType=PAYMENT#x"]
        assert "#" not in links["app_link"]

    def test_label_cannot_override_onelink_parameters(self):
        evil = "Home&af_web_dp=https://evil.example&af_force_deeplink=false"
        params = _query(build_ride_links(SG, dropoff=MBS, dropoff_label=evil)["tap_link"])
        assert params["af_web_dp"] == ["https://www.grab.com/sg/transport/"]
        assert params["af_force_deeplink"] == ["true"]
        assert set(params) == {"af_dp", "af_force_deeplink", "af_web_dp"}

    def test_control_and_bidi_characters_are_dropped(self):
        label = "Changi\u202e Airport\x00\u200b T3"
        address = _query(build_ride_links(SG, dropoff=MBS, dropoff_label=label)["app_link"])[
            "dropOffAddress"
        ][0]
        assert address == "Changi Airport T3"

    @pytest.mark.parametrize("label", [None, "", "  \n ", "\u202e"])
    def test_empty_labels_leave_the_address_out(self, label):
        links = build_ride_links(SG, dropoff=MBS, dropoff_label=label, pickup=CHANGI, pickup_label=label)
        params = _query(links["app_link"])
        assert "dropOffAddress" not in params and "pickUpAddress" not in params

    def test_label_is_flattened_and_truncated(self):
        label = "Line one\nline\ttwo " + "x" * 500
        address = _query(build_ride_links(SG, dropoff=MBS, dropoff_label=label)["app_link"])[
            "dropOffAddress"
        ][0]
        assert "\n" not in address and "\t" not in address
        assert address.startswith("Line one line two")
        assert len(address) == MAX_LABEL_LENGTH


class TestFoodLinks:
    @pytest.mark.parametrize(
        ("query", "slug"),
        [
            ("Bubble Tea!", "bubble-tea"),
            ("  Mala-Xiang  Guo ", "mala-xiang-guo"),
            ("phở bò", "pho-bo"),
            ("Café crème", "cafe-creme"),
            ("ข้าวมันไก่", "ข้าวมันไก่"),
            ("chicken_rice", "chicken-rice"),
            ("Bánh Đa Cua", "banh-da-cua"),
        ],
    )
    def test_slugify(self, query, slug):
        assert slugify_food_query(query) == slug

    @pytest.mark.parametrize("query", ["", "   ", "!!! ---"])
    def test_slugify_rejects_empty_queries(self, query):
        with pytest.raises(ValueError):
            slugify_food_query(query)

    def test_web_search_url(self):
        links = build_food_links(SG, "Chicken Rice")
        assert links["web_search"] == "https://food.grab.com/sg/en/restaurants?search=chicken-rice"

    def test_app_link_opens_grabfood_and_falls_back_to_search(self):
        links = build_food_links(get_market("MYS"), "nasi lemak")
        params = _query(links["open_grabfood_app"])
        assert params["af_dp"] == ["grab://open?screenType=GRABFOOD"]
        assert params["af_web_dp"] == ["https://food.grab.com/my/en/restaurants?search=nasi-lemak"]

    def test_non_latin_search_is_percent_encoded(self):
        url = build_food_links(get_market("THA"), "ข้าวมันไก่")["web_search"]
        assert url.isascii()
        assert _query(url)["search"] == ["ข้าวมันไก่"]
