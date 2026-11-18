# grab-mcp

An MCP server that lets Claude plan Grab rides and point you to GrabFood, without
touching your Grab account.

- **Places** come from OpenStreetMap, through its free Nominatim search service.
  No account, API key or cloud setup is needed.
- **Rides and food** are handed off with links that open the Grab app or
  food.grab.com. You confirm the trip, fare and payment in Grab yourself.

Grab offers no public API for booking rides or ordering food as a customer,
and its terms forbid scripts, scraping and reverse engineering of its apps. So
this server does not log in to Grab, quote Grab fares or travel times, book or
order.

## Tools

| Tool | What it does | Network |
|---|---|---|
| `search_places` | Name or address to places with coordinates | OpenStreetMap |
| `describe_location` | Coordinate to nearest address | OpenStreetMap |
| `plan_grab_ride` | Place names to matched places, straight-line distance and a Grab link | OpenStreetMap |
| `grab_ride_link` | Coordinates to a Grab link | None |
| `grab_food_links` | Dish or cuisine to GrabFood search and app links | None |

Supported markets: SGP, MYS, IDN, THA, VNM, PHL, KHM, MMR.

There is no road distance or travel time tool. The free routing service that
could provide one (the public OSRM demo server) is not meant for regular use,
so this server leaves estimates to the Grab app. `plan_grab_ride` gives only a
straight-line distance, which helps spot a wrong match.

## OpenStreetMap's usage policy

Place search uses the public Nominatim server run by the OpenStreetMap
Foundation. Read its usage policy before you use this server:
<https://operations.osmfoundation.org/policies/nominatim/>

In short, and how this server keeps to it:

- **At most one request per second, one at a time.** Searches are queued and
  spaced a second apart, across every Claude session on the machine, since
  each session starts its own copy of this server.
- **Identify the app.** Requests carry the User-Agent `grab-mcp/<version>`.
  The policy prefers a way to reach you, so consider setting your own, such as
  `grab-mcp (you@example.com)`, in `GRAB_MCP_USER_AGENT`.
- **Cache results.** Answers are kept for 24 hours in a small SQLite file in
  your user cache folder, so a repeated search does not reach the server.
- **Back off when asked.** If the server says to slow down, searches pause for
  at least a minute (longer if it asks). If it refuses the app, they pause for
  an hour.
- **Credit OpenStreetMap.** Search results carry an attribution line:
  © OpenStreetMap contributors, ODbL.
- **No autocomplete, bulk or systematic searching.** Use it for the occasional
  trip, not to sweep an area.
- **No personal data.** Search for places and addresses, not people. Reverse
  lookups send your position rounded to about 11 metres.
- **The service can be withdrawn.** Turn search off with
  `GRAB_MCP_NOMINATIM_URL=off` (only the link tools are then offered), or
  point it at another Nominatim server.

The limits apply to the app as a whole. If you share this server with others,
run your own Nominatim server and set `GRAB_MCP_NOMINATIM_URL`.

## Setup

1. Python 3.11 or later, then:

   ```bash
   pip install -e .
   ```

2. Optional settings:

   | Variable | Default | Meaning |
   |---|---|---|
   | `GRAB_MCP_COUNTRY` | `SGP` | Default market |
   | `GRAB_MCP_NOMINATIM_URL` | `https://nominatim.openstreetmap.org` | Nominatim server (https, or http on localhost), or `off` |
   | `GRAB_MCP_USER_AGENT` | `grab-mcp/<version>` | How requests identify this app |
   | `GRAB_MCP_LANGUAGE` | `en` | Preferred language for place names, e.g. `th,en` |
   | `GRAB_MCP_CACHE_DIR` | your user cache folder | Where the search cache file lives: an absolute path on a local disk, not a network drive |

3. Add it to Claude Code:

   ```bash
   claude mcp add grab -- python -m grab_mcp
   ```

## About the links

Grab does not document its app links. This server uses the formats Grab itself
emits (its booking link carries `pickUp*` and `dropOff*` coordinates, and
grab.com wraps app links in an AppsFlyer OneLink). They may change without
notice, and they need testing on your phone:

- `tap_link` opens Grab on a phone, or Grab's website elsewhere.
- `app_link` is the raw `grab://` link, which only works where Grab is installed.

OpenStreetMap places a pin where its mappers put the building or entrance,
which may not be where Grab drivers pick up. Check the pin in Grab before you
confirm.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```

Tests fake every network call, so they need no internet access.
