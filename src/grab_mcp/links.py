# SPDX-License-Identifier: MIT
"""Builders for links that open the Grab app or GrabFood website.

These never contact Grab. They only format URLs; the person still reviews the
trip or order and confirms it in the Grab app.

Formats, as observed in November 2026 (Grab does not document them publicly):

* ``grab://open?screenType=BOOKING`` with ``pickUp*`` and ``dropOff*``
  parameters is the shape Grab's own fare service returns as a
  ``directDeepLink``.
* ``https://grab.onelink.me/2695613898`` with ``af_dp`` (app link),
  ``af_force_deeplink`` and ``af_web_dp`` (fallback page) is the AppsFlyer
  OneLink that grab.com itself uses for "Order Now" and download buttons.
* ``https://food.grab.com/<cc>/en/restaurants?search=<slug>`` is the search page
  linked from food.grab.com's "Frequently Searched" list.

Because these are undocumented, they may change without notice.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote, urlencode

from .countries import Market
from .models import Coordinates

ONELINK_BASE = "https://grab.onelink.me/2695613898"
APP_SCHEME = "grab://open"
FOOD_WEB_BASE = "https://food.grab.com"

MAX_LABEL_LENGTH = 120
MAX_FOOD_QUERY_LENGTH = 80

_SLUG_SEPARATORS = re.compile(r"[\s-]+")


def _encode(params: dict[str, str]) -> str:
    """Percent-encode a query string, with spaces as %20 (apps may not read '+' as a space)."""
    return urlencode(params, quote_via=quote)


def _format_coordinate(value: float) -> str:
    return f"{value:.6f}"


def _clean_label(label: str | None) -> str:
    """Make a free-text place label short and display-safe.

    Folds whitespace and drops control and formatting characters (such as
    right-to-left overrides) that could make a label read differently from
    what it says. The label is display text only; the coordinates decide
    where the car goes.
    """
    if not label:
        return ""
    visible = "".join(ch for ch in label if ch.isspace() or unicodedata.category(ch) not in ("Cc", "Cf"))
    return " ".join(visible.split())[:MAX_LABEL_LENGTH]


def _wrap_in_onelink(app_url: str, web_fallback: str) -> str:
    query = _encode({"af_dp": app_url, "af_force_deeplink": "true", "af_web_dp": web_fallback})
    return f"{ONELINK_BASE}?{query}"


def _end_params(prefix: str, point: Coordinates, label: str | None) -> dict[str, str]:
    """Parameters for one end of the trip. An empty address is left out so Grab can fill it in."""
    params = {
        f"{prefix}Latitude": _format_coordinate(point.latitude),
        f"{prefix}Longitude": _format_coordinate(point.longitude),
    }
    address = _clean_label(label)
    if address:
        params[f"{prefix}Address"] = address
    return params


def build_ride_links(
    market: Market,
    dropoff: Coordinates,
    dropoff_label: str | None = None,
    pickup: Coordinates | None = None,
    pickup_label: str | None = None,
) -> dict[str, str]:
    """Return links that open Grab's ride booking screen with the trip filled in.

    With no pickup, Grab uses the phone's current location.
    """
    params: dict[str, str] = {"screenType": "BOOKING"}
    if pickup is not None:
        params.update(_end_params("pickUp", pickup, pickup_label))
    params.update(_end_params("dropOff", dropoff, dropoff_label))

    app_url = f"{APP_SCHEME}?{_encode(params)}"
    web_fallback = f"https://www.grab.com/{market.grab_code}/transport/"
    return {"tap_link": _wrap_in_onelink(app_url, web_fallback), "app_link": app_url}


# Latin letters that carry no decomposable accent, so NFD alone would leave them.
_LATIN_TRANSLITERATIONS = str.maketrans({"đ": "d", "ø": "o", "ł": "l", "ß": "ss", "æ": "ae", "œ": "oe"})


def _strip_latin_accents(text: str) -> str:
    """Drop accents on Latin letters (pho bo, not ph-b) but keep marks other scripts need, e.g. Thai."""
    kept: list[str] = []
    for ch in unicodedata.normalize("NFD", text.translate(_LATIN_TRANSLITERATIONS)):
        if unicodedata.combining(ch) and kept and kept[-1].isascii():
            continue
        kept.append(ch)
    return unicodedata.normalize("NFC", "".join(kept))


def slugify_food_query(query: str) -> str:
    """Turn free text into the hyphenated form food.grab.com uses, e.g. 'bubble-tea'."""
    text = _strip_latin_accents(query.strip().lower()[:MAX_FOOD_QUERY_LENGTH])
    # Keep letters, marks and digits; anything else separates words.
    words_only = "".join(ch if unicodedata.category(ch)[0] in "LMN" else " " for ch in text)
    slug = _SLUG_SEPARATORS.sub("-", words_only).strip("-")
    if not slug:
        raise ValueError("Food search needs at least one letter or number")
    return slug


def build_food_links(market: Market, query: str) -> dict[str, str]:
    """Return a GrabFood web search link and a link that opens GrabFood in the app.

    The app link opens GrabFood's home screen; Grab has no known app link that
    carries a search term.
    """
    web_url = (
        f"{FOOD_WEB_BASE}/{market.grab_code}/en/restaurants?{_encode({'search': slugify_food_query(query)})}"
    )
    app_url = f"{APP_SCHEME}?{_encode({'screenType': 'GRABFOOD'})}"
    return {"web_search": web_url, "open_grabfood_app": _wrap_in_onelink(app_url, web_url)}
