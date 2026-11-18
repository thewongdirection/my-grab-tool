"""Value types shared by the geocoder, the link builders and the server."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

_EARTH_RADIUS_KM = 6371.0088  # Mean radius (IUGG)


@dataclass(frozen=True)
class Coordinates:
    """A WGS84 point."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError(f"latitude {self.latitude} is outside -90..90")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError(f"longitude {self.longitude} is outside -180..180")

    def distance_km(self, other: Coordinates) -> float:
        """Straight-line (great-circle) distance, not a road distance."""
        lat1, lat2 = math.radians(self.latitude), math.radians(other.latitude)
        d_lat = lat2 - lat1
        d_lon = math.radians(other.longitude - self.longitude)
        a = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
        return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class Place:
    """One place found by the geocoder."""

    name: str
    address: str
    coordinates: Coordinates
    place_id: str | None = None
    distance_m: int | None = None  # Straight-line metres from the search's near point

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
