---
name: faa
description: FAA airspace and aviation weather for drone pilots. Use when the user wants to know whether they can fly a drone somewhere (controlled airspace, LAANC ceilings, TFRs, prohibited or special use airspace, stadiums), what the current conditions or forecast are at an airport (METAR and TAF decoded to plain language), which weather stations are nearby, or a single go/no-go preflight for a place and time. Can also show the airspace on an interactive map. US only.
compatibility: Requires 'faa.py' script in PATH. No API keys needed. Uses FAA UAS Data Delivery System (ArcGIS), tfr.faa.gov, NOAA Aviation Weather Center, and OpenStreetMap Nominatim.
---

# faa.py

Airspace and weather lookups for drone (Part 107 and recreational) flights. Every command
takes a place name, an address, or `lat,lng` coordinates.

## Commands

### preflight - the one-call answer
Airspace at the point plus the weather for when they plan to fly, as one verdict:
```bash
faa.py preflight "Hollywood, FL"
faa.py preflight "Hollywood Beach, FL" --at "tomorrow 7am" --max-wind 15
faa.py preflight 41.79,-87.74 --at 2pm --altitude 200 --json
```

- `--at`: `now` (default), `2pm`, `14:30`, `tomorrow 7am`, or ISO. A clock time already
  passed today means tomorrow. Up to 30 hours out.
- Within the hour it uses the nearest **METAR**; later, the **TAF** period in force at that
  time plus any TEMPO/PROB overlays.
- Verdict is `GO`, `CAUTION` or `NO-GO`, with `reasons`. A TFR, prohibited area or national
  defense airspace is NO-GO regardless of weather. A weather minimum failed in the
  prevailing forecast is NO-GO; failed only in a TEMPO/PROB overlay it is CAUTION
  ("possible: ...").
- LAANC appears under `requirements`, not as a verdict - including a warning when the LAANC
  ceiling is below the planned `--altitude`.
- **Exit code 3 on NO-GO**, 0 otherwise, 1 on errors.

### airspace - can I fly here?
```bash
faa.py airspace "Chicago Midway Airport"
faa.py airspace 41.79,-87.74 --json
faa.py airspace "Montrose Beach" --near "Chicago, IL" --within 15
```
Verdict `CLEAR`, `CAUTION`, `AUTHORIZATION REQUIRED` or `DO NOT FLY`, with surface airspace
class, LAANC facility map ceiling and the airports/programs behind it, special use and
security airspace, TFRs containing the point, and stadiums within 3 NM.

### nearby - everything within a radius
```bash
faa.py nearby "Navy Pier, Chicago"
faa.py nearby 38.8895,-77.0353 --radius 5
```
Public airports, controlled airspace, LAANC grid ceilings, restrictions and stadiums within
`--radius` NM (default 10, max 50).

### metar - current conditions, decoded
```bash
faa.py metar "Hollywood, FL"
faa.py metar KFLL,KMIA --max-wind 15
faa.py metar KFLL --raw
```
Flight category, wind (kt and mph, gusts), visibility, weather, clouds, ceiling,
temperature, hazards from the remarks (cumulonimbus, lightning, virga), then the drone
checks and the raw report.

### taf - forecast timeline, decoded
```bash
faa.py taf "Hollywood, FL"
faa.py taf KMIA --raw
```
One row per period in local time with a computed flight category; `↳ temporarily`,
`↳ 30% chance` and `↳ becoming` rows are overlays on the period above them, not replacements.

### stations - who reports weather nearby
```bash
faa.py stations "Hollywood, FL" --taf-only
```
METAR stations within `--radius` NM (default 25), nearest first, marking `METAR + TAF`.

### map - airspace on an interactive map
```bash
faa.py map "Chicago Midway Airport"
faa.py map "Denver, CO" --geojson > denver.geojson
faa.py map "Navy Pier, Chicago" --radius 2 --png navy-pier.png
```
**Blocks until interrupted** - run it in the background and report the URL it prints. Falls
back to a free port if 8765 is taken, so read the printed URL. `--png` saves a screenshot
instead (needs Chrome, Edge or Chromium, or set `FAA_BROWSER`); `--geojson` prints the
layers instead.

## Which station?

A location is either station IDs (`KFLL`, or `KFLL,KMIA`, kept in that order) or a place.
For a place, **METAR and TAF pick their stations independently**:

- `metar` uses the nearest station that files METARs (about 2,900 in the US).
- `taf` uses the nearest airport that issues TAFs (about 860), which can be farther away.
  Part-time TAF airports with no current forecast are skipped with a note on stderr.
- A TAF officially covers 5 SM around its airport; past 10 NM a warning says to treat it
  as regional. **Always tell the user which station and how far** - it is in the output.

A four-letter place name that is not a station (Rome, Iowa) falls back to geocoding.

## Drone checks

- Wind: `--max-wind KT` (default 20, or env `FAA_MAX_WIND`). Over the limit ✗, within 85%
  of it ⚠. Gusts count.
- Visibility: Part 107 minimum 3 SM.
- Clouds: 500 ft below any cloud, so a flight to `--altitude` (default 400 ft AGL) needs bases
  at altitude + 500. Broken/overcast below that ✗; few/scattered ⚠ (fly clear of them).
- Thunderstorms ✗; cumulonimbus, precipitation, fog/mist/haze ⚠.

✓ ok · ⚠ watch · ✗ fails. In `--json` every check is `[status, text]` with status
`ok`, `warn` or `fail`.

## Output and errors

- `--json` on every command except `map` (which has `--geojson`). Times in JSON are ISO
  UTC (`observed_utc`, `from_utc`); text output shows local time with Zulu in parentheses.
- Errors print `Error: ...` to stderr and exit 1; argument errors exit 2; warnings and
  notes go to stderr so stdout stays clean for pipes.
- Distances are nautical miles; visibility statute miles; heights feet.

## Limits and caveats

- **Advisory only.** FAA facility map data is not real time and is not an authorization.
  Special use, defense and part-time security airspace activate by NOTAM, so they show as
  CAUTION. Always remind the user to confirm with B4UFLY or a LAANC app and check NOTAMs.
- No NOTAMs other than TFRs (the FAA NOTAM API needs a key). No LAANC filing - the tool
  reports eligibility and points to a LAANC app.
- Rate limits: Nominatim 1 request/second (automatic within a process); AWC about 100
  requests/minute. Pass coordinates instead of place names in loops to skip geocoding.
- US airspace only; weather works worldwide where AWC has the station.

## Tips for Agents

1. For "can I fly at X?" start with `preflight` - it answers airspace and weather together.
2. Echo the resolved address and the station used; a wrong geocode or a far TAF changes the answer.
3. Ask for the aircraft's wind limit if the user has not given one; 20 kt is only a default.
4. Share the Google Maps link from the output as a clickable markdown link.
