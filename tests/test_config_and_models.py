from pathlib import Path

import pytest

from grab_mcp.config import DEFAULT_NOMINATIM_URL, Settings, default_cache_dir
from grab_mcp.countries import MARKETS, get_market
from grab_mcp.models import Coordinates


class TestCoordinates:
    @pytest.mark.parametrize(("lat", "lon"), [(91, 0), (-91, 0), (0, 181), (0, -181)])
    def test_out_of_range_is_rejected(self, lat, lon):
        with pytest.raises(ValueError):
            Coordinates(lat, lon)

    def test_straight_line_distance(self):
        changi, mbs = Coordinates(1.3644, 103.9915), Coordinates(1.2834, 103.8607)
        assert changi.distance_km(mbs) == pytest.approx(17.1, abs=0.1)
        assert changi.distance_km(changi) == 0

    def test_distance_between_antipodes_does_not_fail(self):
        assert Coordinates(0, 0).distance_km(Coordinates(0, 180)) == pytest.approx(20015, rel=1e-3)


class TestMarkets:
    @pytest.mark.parametrize("code", ["SGP", "sgp", "sg", " SG "])
    def test_lookup_by_iso3_or_two_letter_code(self, code):
        assert get_market(code).iso3 == "SGP"

    def test_unknown_country(self):
        with pytest.raises(ValueError, match="Unsupported country"):
            get_market("USA")

    def test_codes_have_the_right_shape(self):
        for market in MARKETS.values():
            assert len(market.iso3) == 3 and len(market.iso2) == 2 and len(market.grab_code) == 2
            assert market.iso2 == market.iso2.lower()  # Nominatim's countrycodes are lower case.


class TestSettings:
    def test_defaults(self):
        settings = Settings.from_env({})
        assert settings.default_country == "SGP"
        assert settings.nominatim_url == DEFAULT_NOMINATIM_URL
        assert settings.user_agent.startswith("grab-mcp/")
        assert settings.language == "en"
        assert settings.search_enabled

    def test_reads_environment(self):
        settings = Settings.from_env(
            {
                "GRAB_MCP_COUNTRY": "MYS",
                "GRAB_MCP_NOMINATIM_URL": " https://geo.example.org/ ",
                "GRAB_MCP_USER_AGENT": "my-copy/1.0",
                "GRAB_MCP_LANGUAGE": "ms,en",
                "GRAB_MCP_CACHE_DIR": "/var/cache/grab",
            }
        )
        assert settings == Settings(
            "MYS", "https://geo.example.org", "my-copy/1.0", "ms,en", Path("/var/cache/grab")
        )

    def test_blank_values_fall_back_to_defaults(self):
        blank = Settings.from_env({"GRAB_MCP_USER_AGENT": "  ", "GRAB_MCP_COUNTRY": ""})
        assert blank == Settings(cache_dir=default_cache_dir({}))

    @pytest.mark.parametrize("value", ["off", "OFF", " Off "])
    def test_search_can_be_turned_off(self, value):
        assert not Settings.from_env({"GRAB_MCP_NOMINATIM_URL": value}).search_enabled

    def test_local_http_server_is_allowed(self):
        assert Settings(nominatim_url="http://localhost:8080").nominatim_url == "http://localhost:8080"

    @pytest.mark.parametrize(
        "url",
        [
            "http://geo.example.org",
            "ftp://geo.example.org",
            "geo.example.org",
            "https://",
            "https://geo.example.org/?key=1",
            "https://geo.example.org/#x",
            "https://user:pw@geo.example.org",
            "https://geo.example.org\n/x",
            "https://geo.example.org:99999",
            "https://geo .example.org",
            "https://geo.example.org/search",
            "https://geo.example.org/reverse/",
        ],
    )
    def test_unsafe_server_urls_are_rejected(self, url):
        with pytest.raises(ValueError, match="GRAB_MCP_NOMINATIM_URL"):
            Settings(nominatim_url=url)

    @pytest.mark.parametrize("agent", ["", "bad\r\nX-Injected: 1", "é-agent", "x" * 201])
    def test_bad_user_agents_are_rejected(self, agent):
        with pytest.raises(ValueError, match="GRAB_MCP_USER_AGENT"):
            Settings(user_agent=agent)

    @pytest.mark.parametrize("language", ["", "en\r\nX: 1", "é"])
    def test_bad_languages_are_rejected(self, language):
        with pytest.raises(ValueError, match="GRAB_MCP_LANGUAGE"):
            Settings(language=language)

    def test_unknown_default_country_is_rejected(self):
        with pytest.raises(ValueError):
            Settings(default_country="XX")

    def test_cache_folder_follows_xdg_cache_home(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        assert default_cache_dir({"XDG_CACHE_HOME": "/x"}) == Path("/x/grab-mcp")
        assert default_cache_dir({}) == Path.home() / ".cache" / "grab-mcp"

    def test_cache_folder_on_windows_and_mac(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        assert default_cache_dir({"LOCALAPPDATA": "C:/Users/x/AppData/Local"}) == Path(
            "C:/Users/x/AppData/Local/grab-mcp"
        )
        monkeypatch.setattr("sys.platform", "darwin")
        assert default_cache_dir({}) == Path.home() / "Library" / "Caches" / "grab-mcp"

    def test_cache_folder_expands_home(self):
        settings = Settings.from_env({"GRAB_MCP_CACHE_DIR": "~/grab-cache"})
        assert settings.cache_dir == Path.home() / "grab-cache"

    def test_relative_cache_folder_is_rejected(self):
        with pytest.raises(ValueError, match="absolute"):
            Settings(cache_dir=Path("cache"))

    def test_relative_xdg_cache_home_is_ignored(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        assert default_cache_dir({"XDG_CACHE_HOME": "relative"}) == Path.home() / ".cache" / "grab-mcp"
