import pytest


@pytest.fixture(autouse=True)
def _private_cache_dir(tmp_path, monkeypatch):
    """Keep every test's search cache out of the real user cache folder."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # The macOS and default folders sit under it.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("GRAB_MCP_CACHE_DIR", raising=False)
