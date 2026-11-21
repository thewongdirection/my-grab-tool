# SPDX-License-Identifier: MIT
"""Runtime settings, read from environment variables only. Nothing here is secret."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import __version__
from .countries import get_market

DEFAULT_COUNTRY = "SGP"
DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org"
# Nominatim's usage policy asks every app to identify itself; stock library agents are refused.
DEFAULT_USER_AGENT = f"grab-mcp/{__version__}"
DEFAULT_LANGUAGE = "en"
# Setting GRAB_MCP_NOMINATIM_URL to this turns place search off without a code change.
SEARCH_OFF = "off"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MAX_USER_AGENT_LENGTH = 200


def _is_printable_ascii(text: str) -> bool:
    return all(" " <= ch <= "~" for ch in text)


def default_cache_dir(env: Mapping[str, str]) -> Path:
    """The usual per-user cache folder for this platform."""
    if sys.platform == "win32" and env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"]) / "grab-mcp"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "grab-mcp"
    xdg = Path(env.get("XDG_CACHE_HOME") or "")
    base = xdg if xdg.is_absolute() else Path.home() / ".cache"  # The XDG spec ignores relative paths.
    return base / "grab-mcp"


def _check_nominatim_url(url: str) -> str:
    if url.strip().lower() == SEARCH_OFF:
        return SEARCH_OFF
    problem = f"GRAB_MCP_NOMINATIM_URL must be an https:// URL (http only for localhost); got {url!r}"
    if not _is_printable_ascii(url) or " " in url:
        raise ValueError(problem)
    parts = urlsplit(url)
    try:
        parts.port  # noqa: B018 - raises ValueError for an out-of-range port
        httpx.URL(url)
    except (ValueError, httpx.InvalidURL) as exc:
        raise ValueError(problem) from exc
    local = parts.hostname in _LOCAL_HOSTS
    if not parts.hostname or parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local):
        raise ValueError(problem)
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("GRAB_MCP_NOMINATIM_URL must not carry a query, fragment or credentials")
    if parts.path.rstrip("/").endswith(("/search", "/reverse")):
        raise ValueError("GRAB_MCP_NOMINATIM_URL is the server's base address, without /search or /reverse")
    return url.rstrip("/")


@dataclass(frozen=True)
class Settings:
    default_country: str = DEFAULT_COUNTRY
    nominatim_url: str = DEFAULT_NOMINATIM_URL
    user_agent: str = DEFAULT_USER_AGENT
    language: str = DEFAULT_LANGUAGE
    cache_dir: Path = field(default_factory=lambda: default_cache_dir(os.environ))

    def __post_init__(self) -> None:
        get_market(self.default_country)  # Fail fast on a mistyped country.
        # Frozen dataclass: normalised values are set through object.__setattr__.
        object.__setattr__(self, "nominatim_url", _check_nominatim_url(self.nominatim_url))
        if not 0 < len(self.user_agent) <= MAX_USER_AGENT_LENGTH or not _is_printable_ascii(self.user_agent):
            raise ValueError(
                f"GRAB_MCP_USER_AGENT must be 1-{MAX_USER_AGENT_LENGTH} printable ASCII characters"
            )
        object.__setattr__(self, "cache_dir", Path(self.cache_dir))  # Accept a str too.
        if not self.cache_dir.is_absolute():
            # Each Claude session starts in its own folder, so a relative path would split the cache.
            raise ValueError(f"GRAB_MCP_CACHE_DIR must be an absolute path; got {str(self.cache_dir)!r}")
        if not self.language or not all(
            ch.isascii() and (ch.isalnum() or ch in "-,;=. ") for ch in self.language
        ):
            raise ValueError("GRAB_MCP_LANGUAGE must be language codes such as 'en' or 'th,en'")

    @property
    def search_enabled(self) -> bool:
        return self.nominatim_url != SEARCH_OFF

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env

        def read(name: str, default: str) -> str:
            return (source.get(name) or "").strip() or default

        cache_dir = read("GRAB_MCP_CACHE_DIR", "")
        return cls(
            default_country=read("GRAB_MCP_COUNTRY", DEFAULT_COUNTRY),
            nominatim_url=read("GRAB_MCP_NOMINATIM_URL", DEFAULT_NOMINATIM_URL),
            user_agent=read("GRAB_MCP_USER_AGENT", DEFAULT_USER_AGENT),
            language=read("GRAB_MCP_LANGUAGE", DEFAULT_LANGUAGE),
            cache_dir=Path(cache_dir).expanduser() if cache_dir else default_cache_dir(source),
        )
