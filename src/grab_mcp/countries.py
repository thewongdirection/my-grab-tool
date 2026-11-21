# SPDX-License-Identifier: MIT
"""Markets where Grab's consumer app runs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    iso3: str  # ISO 3166-1 alpha-3; how markets are named in tool input and output
    iso2: str  # ISO 3166-1 alpha-2, which OpenStreetMap's countrycodes filter takes
    grab_code: str  # Path segment Grab uses on grab.com and food.grab.com


MARKETS: dict[str, Market] = {
    m.iso3: m
    for m in (
        Market("SGP", "sg", "sg"),
        Market("MYS", "my", "my"),
        Market("IDN", "id", "id"),
        Market("THA", "th", "th"),
        Market("VNM", "vn", "vn"),
        Market("PHL", "ph", "ph"),
        Market("KHM", "kh", "kh"),
        Market("MMR", "mm", "mm"),
    )
}


def get_market(country: str) -> Market:
    """Look up a market by ISO alpha-3, ISO alpha-2 or Grab's code, case-insensitively."""
    key = country.strip().upper()
    if key in MARKETS:
        return MARKETS[key]
    for market in MARKETS.values():
        if key in (market.iso2.upper(), market.grab_code.upper()):
            return market
    supported = ", ".join(sorted(MARKETS))
    raise ValueError(f"Unsupported country {country!r}; use one of {supported}")
