# drones.py

A Python CLI for drone pilots: FAA airspace and aviation weather for any place in the US, with a single go/no-go preflight.

## Features

- **Single-file executable** - Uses `uv run --script` with inline dependencies (PEP 723)
- **Preflight** - Airspace plus the weather for when you plan to fly, as one GO / CAUTION / NO-GO verdict
- **Airspace** - Controlled airspace, LAANC ceilings, special use and security airspace, TFRs and stadiums at a point or within a radius
- **METAR and TAF, decoded** - Current conditions and forecasts in plain language, with Part 107 and wind-limit checks
- **Nearest station logic** - Current conditions from the closest METAR station, forecasts from the closest airport that issues TAFs
- **Interactive map** - The airspace around a point on a local Leaflet map, as GeoJSON, or as a PNG
- **No API keys** - FAA UAS Data Delivery System, tfr.faa.gov, NOAA Aviation Weather Center, OpenStreetMap

## Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) package manager

## Installation

```bash
# Clone and make executable
git clone https://github.com/vicgarcia/drones.py.git
cd drones.py
chmod +x drones.py

# Run (dependencies install on first run)
./drones.py --help

# Install
cp drones.py ~/.local/bin
chmod +x ~/.local/bin/drones.py
```

## Configuration

```bash
# Your aircraft's wind limit in knots (default 20); --max-wind overrides it
export DRONES_MAX_WIND=18

# Browser for map --png, when Chrome/Edge/Chromium is not found automatically
export DRONES_BROWSER="/path/to/chrome"
```

## Commands

| Command | What it answers |
|---------|-----------------|
| `preflight` | Can I fly here at this time? Airspace plus weather as one verdict |
| `airspace` | What airspace and restrictions apply at this point? |
| `nearby` | What airports, airspace and restrictions are within a radius? |
| `metar` | What are conditions right now? |
| `taf` | What is the forecast? |
| `stations` | Which weather stations report near here? |
| `map` | Show me the airspace on a map |

Every command takes a place name, an address, or `lat,lng` coordinates. `metar` and `taf`
also take station IDs (`KFLL`, or `KFLL,KMIA`).

## Usage

### Preflight

```bash
drones.py preflight "Hollywood, FL"
drones.py preflight "Hollywood Beach, FL" --at "tomorrow 7am" --max-wind 15
drones.py preflight 41.79,-87.74 --at 2pm --altitude 200 --json
```

```
  Preflight: Young Circle, Hollywood, FL
  ==========================================================
  When:       Sun 9:32 PM (400 ft AGL, 20 kt wind limit)
  Verdict:    CAUTION
              - gusts 19 kt near your 20 kt limit
              - cumulonimbus nearby
              Requires: LAANC available: near-instant approval up to 400 ft
              AGL through an FAA-approved LAANC app (Aloft, Autel, DJI
              FlightHub, etc.).

  Airspace
    AUTHORIZATION REQUIRED: Class C FORT LAUDERDALE CLASS C
    LAANC ceiling 400 ft AGL

  Weather
    Ft Lauderdale-Hollywood Intl (KFLL) · 4.2 NM NNW of Young Circle, Hollywood, FL
    Observed 8:53 PM EDT (00:53Z): VFR, wind ENE 60° at 10 kt (12 mph),
    gusting 19 kt (22 mph), visibility 10+ statute miles, clouds scattered
    2,600 ft (cumulonimbus)
      ⚠ gusts 19 kt near your 20 kt limit
      ✓ visibility meets 3 SM
      ✓ clouds clear of 400 ft + 500 ft
      ⚠ cumulonimbus nearby
```

Within the hour the nearest METAR decides; later times use the TAF period in force plus any
TEMPO/PROB overlays, whose failures only raise CAUTION. An active TFR, prohibited area or
national defense airspace is NO-GO regardless of weather. Exits 3 on NO-GO.

TFRs are judged at the time you ask about. The FAA's shape feed lists scheduled TFRs alongside
active ones and carries no times or altitudes, so drones.py reads each nearby TFR's NOTAM text:
active means NO-GO, scheduled means CAUTION with its window (e.g. "scheduled Tue Oct 27
10:30 AM EDT to 4:00 PM EDT"), expired is dropped, and one whose text can't be fetched is
treated as active.

### Airspace

```bash
drones.py airspace "Chicago Midway Airport"
drones.py airspace 41.79,-87.74 --json
drones.py airspace "Montrose Beach" --near "Chicago, IL" --within 15
drones.py nearby "Navy Pier, Chicago"
drones.py nearby 38.8895,-77.0353 --radius 5
```

### Weather

```bash
drones.py metar "Hollywood, FL"
drones.py metar KFLL,KMIA --max-wind 15
drones.py taf "Hollywood, FL"
drones.py taf KMIA --raw
drones.py stations "Hollywood, FL" --taf-only
```

```
  TAF Ft Lauderdale-Hollywood Intl (KFLL) · 4.2 NM NNW of Young Circle, Hollywood, FL
  Issued 7:20 PM EDT (23:20Z) · valid Sun 8 PM - Tue 2 AM
  ==========================================================
  Sun 8 PM - Mon 12 AM   VFR  · NE 15 G25 kt · 6+ SM · showers in the vicinity · scattered 2,000 ft, broken 6,000 ft
                         ✗ gusts 25 kt over your 20 kt limit
  Mon 12 AM - Mon 12 PM  VFR  · NE 12 G22 kt · 6+ SM · showers in the vicinity · scattered 2,000 ft, scattered 6,000 ft
                         ✗ gusts 22 kt over your 20 kt limit
```

METAR and TAF pick their stations independently: the nearest METAR station for conditions
(about 2,900 in the US), the nearest TAF airport for forecasts (about 860), skipping
part-time TAF airports that have no current forecast. A TAF covers 5 SM around its airport,
so one more than 10 NM away comes with a warning.

Decoding goes beyond AWC's JSON where it falls short: cumulonimbus and towering cumulus are
recovered from the raw report (the JSON drops the `CB` tag), remark hazards such as
`CB ALQDS MOV SW` and lightning are spelled out, and TAF periods get a computed flight
category.

**Drone checks:** wind and gusts against `--max-wind` (warning within 85% of it), Part 107's
3 SM visibility, and 500 ft below clouds at your `--altitude` (default 400 ft AGL), plus
thunderstorms, cumulonimbus, precipitation and fog.

### Map

```bash
drones.py map "Chicago Midway Airport"
drones.py map 41.79,-87.74 --radius 5 --tiles satellite
drones.py map "Denver, CO" --geojson > denver.geojson
drones.py map "Navy Pier, Chicago" --radius 2 --png navy-pier.png
drones.py map 41.79,-87.74 --png midway.png --tiles satellite --size 1920x1080
```

Serves on `127.0.0.1`, falls back to a free port if 8765 is taken, and runs until Ctrl+C.

## Data Sources

| Data | Source |
|------|--------|
| LAANC grid, class airspace, special use, prohibited and defense airspace, national security UAS restrictions, stadiums, airports | FAA UAS Data Delivery System - the public ArcGIS layers behind the FAA UAS Facility Map |
| TFR shapes, NOTAM text (effective times, altitudes) and links | tfr.faa.gov |
| METAR, TAF and the station index | NOAA Aviation Weather Center (aviationweather.gov) |
| Geocoding | OpenStreetMap Nominatim |

All free with no key. Nominatim allows 1 request a second (paced automatically), AWC about 100
a minute. Not covered: NOTAMs other than TFRs (the FAA NOTAM API needs a key), LAANC filing
(only approved suppliers can submit), real-time activation of NOTAM-activated airspace (shown
as CAUTION), and airspace outside the US.

> Advisory only - FAA data is not real time and is not an authorization to fly. Confirm with
> B4UFLY or a LAANC app and check NOTAMs before you fly.

[WIKI.md](WIKI.md) has the reference details: every FAA layer and field, how TFR times are read,
the AWC endpoints and JSON quirks, how stations are chosen, and Part 107 weather rules.

## Development

Run the offline regression suite (service calls are mocked, network access is refused):

```bash
uv run --with 'geopy>=2.4.0' --with 'requests>=2.31.0' python -m unittest discover -v
```

## Agent Skill

This project includes a `SKILL.md` file for use with AI coding agents (Claude Code, etc.). The skill enables natural language airspace checks, aviation weather, and preflight decisions.

### Installation

```bash
# Create skills directory
mkdir -p /path/to/agent/skills/drones

# Copy SKILL.md
cp /path/to/drones.py/SKILL.md /path/to/agent/skills/drones/SKILL.md

# Ensure drones.py is executable and in PATH
chmod +x ~/.local/bin/drones.py
```

### Usage with Claude Code

Add the skills directory to your agent configuration, then interact naturally:

> "Can I fly my drone at Hollywood Beach tomorrow morning?"
> "What's the weather at KFLL right now?"
> "Is there a TFR near Navy Pier today?"
> "Show me the airspace around Midway on a map"
