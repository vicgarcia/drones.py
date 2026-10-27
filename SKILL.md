---
name: drones
description: FAA airspace for drone pilots, with aviation weather and a live map on request. Use when the user wants to know whether they can fly a drone somewhere (controlled airspace, LAANC ceilings, TFRs, prohibited or special use airspace, stadiums), wants to scout a spot or size up a mission area, or asks to see the airspace on a map. Also covers current conditions and forecasts (METAR/TAF), a go/no-go preflight for a place and time, and a live map with air traffic and weather radar, but only when the user asks for weather or a live view. US only.
compatibility: Requires 'drones.py' script in PATH. No API keys needed. Uses FAA UAS Data Delivery System (ArcGIS), tfr.faa.gov, NOAA Aviation Weather Center, adsb.fi, and OpenStreetMap Nominatim.
---

# drones.py

Airspace lookups for drone (Part 107 and recreational) flights, with weather and live traffic as
opt-ins. Every command takes a place name, an address, or `lat,lng` coordinates.

## Defaults: airspace first

Assume the user is asking about **airspace**. Add the rest only when they ask for it:

| The user asks about... | Add |
|---|---|
| Weather, wind, forecast, "conditions", a specific flying time | `metar`, `taf` or `preflight` |
| A "live" view, traffic, aircraft, radar, "what's flying" | `map --adsb --radar` |
| Seeing it, "show me", a map | `map` (airspace only unless one of the above applies) |

- Don't fetch weather or start a live map just in case. Weather is rarely what the user wants
  when exploring.
- Start the map only when the user asks to see one. It is interactive, served on localhost.
- The search radius defaults to 5 NM. Pass `--radius` only when the user gives an area size.

## Use cases

### Scouting a spot - "can I fly at X?"
```bash
drones.py airspace "Hollywood Beach, FL"
drones.py nearby "Hollywood Beach, FL"            # the airspace around it, if asked
```
Answer with a short verdict card, not the full output:

```
**AUTHORIZATION REQUIRED** · 4406 Fillmore St, West Hollywood, Hollywood, FL
- Class D (Hollywood, part time) · LAANC up to **300 ft AGL**, near-instant via a LAANC app
- TFRs: none here · nearby: public gathering 6.4 NM NE, Oct 28 – Nov 2
- [Google Maps](https://www.google.com/maps?q=26.014228,-80.1877794)
```

- First line: the verdict and the resolved address, shortened, so a wrong geocode is caught.
- Authorization: the surface class and the LAANC ceiling, with how to get approval: a LAANC app,
  FAADroneZone when the ceiling is 0 or the airport has no LAANC, or "no authorization needed"
  in Class G.
- TFRs and restrictions: each one with its status and times, or "none here". Then any TFR
  within 10 NM that misses the point (`nearby_tfrs`), with distance, direction and dates, since
  plans and drift move a flight.
- Add a line only for what applies: stadium within 3 NM, special use or security airspace
  (active by NOTAM), or a LAANC ceiling below the altitude the user mentioned.
- The Google Maps link from the output, then stop. Give airports, LAANC programs, overhead
  airspace and the rest only when asked.

### Sizing up a mission area - mapping or inspection sites
```bash
drones.py airspace "4406 Fillmore St, Hollywood, FL"
drones.py nearby "4406 Fillmore St, Hollywood, FL" --radius 1 --json
drones.py map "4406 Fillmore St, Hollywood, FL" --radius 1   # when they want to see it
```
`airspace` checks only the exact point. A site can straddle a LAANC grid boundary or a
class airspace edge, so for an area use `nearby` with a radius about the site's size:
`laanc_grid.ceilings_ft` gives every ceiling in it (plan to the lowest), and `airspace` and
`restrictions` list what reaches into it. Tell the user when ceilings differ across the site.

### Live awareness - only on request
```bash
drones.py map "Hollywood, FL" --adsb --radar
```
Live air traffic from adsb.fi (polled every 3 s, airborne only) and NOAA radar (every 2 min).
Traffic is situational awareness only, not see-and-avoid: volunteer receivers miss low traffic
and aircraft without ADS-B.

## Commands

### airspace - can I fly here?
```bash
drones.py airspace "Chicago Midway Airport"
drones.py airspace 41.79,-87.74 --json
drones.py airspace "Montrose Beach" --near "Chicago, IL" --within 15
```
Verdict `CLEAR`, `CAUTION`, `AUTHORIZATION REQUIRED` or `DO NOT FLY`, with the surface airspace
class, the LAANC facility map ceiling and the airports/programs behind it, special use and
security airspace, TFRs containing the point, stadiums within 3 NM, and TFRs within 10 NM that
miss the point (listed apart, not part of the verdict). Each TFR shows its
status from the NOTAM text: active (NO-FLY), scheduled (CAUTION, with start and end times),
or "times unavailable - treat as active". Expired TFRs still in the FAA feed are dropped.
`--near`/`--within` pin down an ambiguous place name.

### nearby - everything within a radius
```bash
drones.py nearby "Navy Pier, Chicago"
drones.py nearby 38.8895,-77.0353 --radius 2 --json
```
Public airports, controlled airspace, LAANC grid ceilings, restrictions and stadiums within
`--radius` NM (default 5, max 30), nearest first. TFRs are always listed out to 30 NM whatever
the radius. Distances are to a feature's nearest edge, 0 when inside it.

### map - airspace on an interactive map
```bash
drones.py map "Chicago Midway Airport"
drones.py map "Navy Pier, Chicago" --radius 2 --tiles satellite
drones.py map "Chicago Midway Airport" --adsb --radar      # live view, on request
drones.py map "Denver, CO" --geojson > denver.geojson
```
- **Blocks until stopped.** Run it in the background and give the user the URL it prints, which
  falls back to a free port when 8765 is taken. Before starting another, stop the old one
  (`pkill -f "drones.py map"`), and stop it when the user is done.
- Airspace only by default: LAANC grid, class, special use, defense and security airspace,
  TFRs, stadiums and airports, each toggled in the layer panel. TFRs are drawn out to 30 NM, and
  the LAANC grid and TFR toggles are listed even when there are none. The search center and
  radius ring are always shown.
- Basemaps: `street` (default), `satellite`, `light` and `dark`, all switchable on the map.
- Opt-ins for a live view: `--adsb` air traffic, `--radar` NOAA weather radar. The map never
  shows station weather; use `metar`/`taf` for that.
- `--geojson` prints the airspace layers instead of serving; `--png FILE` saves a screenshot
  (needs Chrome, Edge or Chromium, or `DRONES_BROWSER`) - only when the user asks for an image.

### preflight - airspace and weather for a time (on request)
```bash
drones.py preflight "Hollywood Beach, FL" --at "tomorrow 7am" --max-wind 15
drones.py preflight 41.79,-87.74 --at 2pm --altitude 200 --json
```
Use when the user asks about flying at a particular time or about the weather. Verdict `GO`,
`CAUTION` or `NO-GO`:
- `--at`: `now` (default), `2pm`, `14:30`, `tomorrow 7am`, or ISO, up to 30 hours out.
  A clock time already passed today means tomorrow.
- Within the hour the nearest **METAR** decides; later, the **TAF** period in force plus any
  TEMPO/PROB overlays, whose failures only raise CAUTION.
- An active TFR, prohibited area or national defense airspace is NO-GO regardless of weather.
  LAANC goes under `requirements`, with a warning when the ceiling is below `--altitude`.
- **Exit code 3 on NO-GO**, 0 otherwise, 1 on errors.

### metar, taf, stations - weather (on request)
```bash
drones.py metar "Hollywood, FL"
drones.py metar KFLL,KMIA --max-wind 15
drones.py taf KMIA
drones.py stations "Hollywood, FL" --taf-only
```
`metar` decodes current conditions with the drone checks; `taf` gives one row per forecast
period (`↳` rows are overlays on the period above, not replacements); `stations` lists who
reports within `--radius` NM (default 25). `--raw` prints only the report.

For a place, **METAR and TAF pick their stations independently**: the nearest METAR station,
and the nearest airport that issues TAFs, which can be farther away. A TAF covers 5 SM around
its airport; past 10 NM a warning says to treat it as regional. **Tell the user which station
and how far** - it is in the output.

Drone checks: wind and gusts against `--max-wind` (default 20 kt, or env `DRONES_MAX_WIND`;
⚠ within 85% of it), Part 107's 3 SM visibility, 500 ft below clouds at `--altitude` (default
400 ft AGL), plus thunderstorms, cumulonimbus, precipitation and fog. ✓ ok · ⚠ watch · ✗ fails.
Ask for the aircraft's wind limit when weather matters; 20 kt is only a default.

## Output and errors

- `--json` on every command except `map` (which has `--geojson`). Times in JSON are ISO
  UTC; text output shows local time with Zulu in parentheses.
- Errors print `Error: ...` to stderr and exit 1; argument errors exit 2; warnings and
  notes go to stderr so stdout stays clean for pipes.
- Distances are nautical miles; visibility statute miles; heights feet.
- Share the Google Maps link from the output as a clickable markdown link.

## Limits and caveats

- **Advisory only.** FAA facility map data is not real time and is not an authorization.
  Special use, defense and part-time security airspace activate by NOTAM, so they show as
  CAUTION. Remind the user to confirm with B4UFLY or a LAANC app and check NOTAMs.
- No NOTAMs other than TFRs (the FAA NOTAM API needs a key). No LAANC filing - the tool
  reports eligibility and points to a LAANC app.
- Rate limits: Nominatim 1 request/second (automatic within a process); AWC about 100
  requests/minute. Pass coordinates instead of place names in loops to skip geocoding.
- US airspace only; weather works worldwide where AWC has the station.
