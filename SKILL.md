---
name: drones
description: FAA airspace and drone rules for US drone pilots, with aviation weather and a live map on request. Use when the user wants to know whether they can fly a drone somewhere (controlled airspace, LAANC ceilings, TFRs, prohibited or special use airspace, stadiums), wants to scout a spot or size up a mission area, asks to see the airspace on a map, or asks how the drone rules work (Part 107, recreational flying, registration, Remote ID, night or over-people operations, waivers, authorizations, accident reporting). Also covers current conditions and forecasts (METAR/TAF), a go/no-go preflight for a place and time, and a live map with air traffic and weather radar, but only when the user asks for weather or a live view. US only.
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

## UAS pilot expert - the rules

Answer rules questions as an experienced Part 107 remote pilot would: name the rule set that
applies, give the plain answer, then cite the section and link it. Use only rules in effect now.
Proposed rules (pending rulemakings) are not law; don't present them as requirements.

**Which rules apply.** Flying purely for fun is recreational, under 49 USC 44809. Anything else is
Part 107, including work, a client, a nonprofit, or "goodwill". Getting paid is not what decides
it, and when in doubt the FAA says to assume Part 107. A drone registered as recreational can't be
flown under Part 107.

**Part 107 at a glance** (the regulation is the authority; the FAA rules reference below links each section):
- Remote pilot certificate (knowledge test, age 16+), carried when flying; free online recurrent
  training every 24 calendar months
- Under 55 lb; registered ($5 per drone for 3 years), marked, with Remote ID (built in, a broadcast
  module, or inside a FRIA)
- Visual line of sight (yours or a visual observer's); give way to manned aircraft
- 400 ft AGL, or within 400 ft of a structure and no higher than 400 ft above its top; 87 kt
  (100 mph) max; 3 SM visibility; 500 ft below and 2,000 ft horizontally from clouds
- Class B, C, D and surface Class E need ATC authorization: LAANC up to the facility map
  ceiling, DroneZone otherwise
- Night and civil twilight need anti-collision lighting visible 3 SM
- Over people only within the Subpart D categories, or over participants or people under cover
- No flying from a moving vehicle except over sparsely populated areas
- Report serious injury, or property damage over $500, to the FAA within 10 days
- Most limits can be waived (§ 107.205) through DroneZone

**Recreational at a glance:** fly only for fun; pass TRUST and carry proof; follow an FAA-recognized
community-based organization's safety guidelines; stay within visual line of sight; give way to
other aircraft; 400 ft in Class G; LAANC or DroneZone authorization in controlled airspace.
Register if 250 g or more ($5, 3 years, covers all your drones), mark it, and use Remote ID.

**Always true for both:** TFRs, prohibited areas and national security restrictions apply to every
drone; check NOTAMs before flying. National Parks close launching and landing in most parks. State
and local laws (privacy, trespass, where you take off) vary, so point to them and don't guess.

**Going deeper.** The FAA rules reference at the end of this skill lists the exact eCFR sections,
FAA guidance (AC 107-2A, FAQs, AIM), and the registration, Remote ID, waiver and authorization pages. Read the linked page before
answering anything beyond the summary above. Say when a question needs the FAA's own answer (a
waiver, an unusual operation, enforcement) or a lawyer, and never tell the user a flight is
authorized: the tool reports eligibility, LAANC grants authorization.

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

## FAA rules reference

Where to read the actual rule behind a drone question. Each link was checked on 2026-10-26.
Regulations on eCFR are the law and always current; FAA web pages and advisory circulars
explain it. Quote the regulation when the answer turns on exact wording.

### Regulations (the law)

| Topic | Source |
|---|---|
| Part 107, the small UAS rule (whole part) | [14 CFR Part 107](https://www.ecfr.gov/current/title-14/chapter-I/subchapter-F/part-107) |
| Accident reporting: serious injury, or over $500 of damage, within 10 days | [§ 107.9](https://www.ecfr.gov/current/title-14/section-107.9) |
| Flying from a moving vehicle | [§ 107.25](https://www.ecfr.gov/current/title-14/section-107.25) |
| Night and civil twilight: anti-collision lighting visible 3 SM | [§ 107.29](https://www.ecfr.gov/current/title-14/section-107.29) |
| Visual line of sight and visual observers | [§ 107.31](https://www.ecfr.gov/current/title-14/section-107.31) |
| Right of way to manned aircraft | [§ 107.37](https://www.ecfr.gov/current/title-14/section-107.37) |
| Controlled airspace needs ATC authorization (LAANC or DroneZone) | [§ 107.41](https://www.ecfr.gov/current/title-14/section-107.41) |
| Operating near airports | [§ 107.43](https://www.ecfr.gov/current/title-14/section-107.43) |
| Areas restricted by NOTAM (TFRs, security areas) | [§ 107.47](https://www.ecfr.gov/current/title-14/section-107.47) |
| Preflight familiarization and inspection | [§ 107.49](https://www.ecfr.gov/current/title-14/section-107.49) |
| Limits: 400 ft AGL, 87 kt, 3 SM visibility, cloud clearance | [§ 107.51](https://www.ecfr.gov/current/title-14/section-107.51) |
| Recurrent training every 24 calendar months | [§ 107.65](https://www.ecfr.gov/current/title-14/section-107.65) |
| Certification and the knowledge test | [Subpart C](https://www.ecfr.gov/current/title-14/part-107/subpart-C) |
| Operations over people and moving vehicles (Categories 1-4) | [Subpart D](https://www.ecfr.gov/current/title-14/part-107/subpart-D) |
| Which rules can be waived | [§ 107.205](https://www.ecfr.gov/current/title-14/section-107.205) |
| Remote ID | [14 CFR Part 89](https://www.ecfr.gov/current/title-14/chapter-I/subchapter-F/part-89) |
| Registration | [14 CFR Part 48](https://www.ecfr.gov/current/title-14/chapter-I/subchapter-C/part-48) |
| Recreational flying: the statutory exception | [49 USC 44809](https://www.law.cornell.edu/uscode/text/49/44809) |
| Disaster, VIP and space operations TFRs | [14 CFR 91.137](https://www.ecfr.gov/current/title-14/section-91.137) through [91.145](https://www.ecfr.gov/current/title-14/section-91.145) |
| Special security instructions (national defense airspace) | [14 CFR 99.7](https://www.ecfr.gov/current/title-14/section-99.7) |
| National Parks: most parks close launching and landing by superintendent order | [36 CFR 1.5](https://www.ecfr.gov/current/title-36/section-1.5) (see each park's superintendent's compendium) |

### FAA guidance (how the FAA reads the rules)

- [AC 107-2A, Small Unmanned Aircraft Systems](https://www.faa.gov/documentLibrary/media/Advisory_Circular/AC_107-2A.pdf): the FAA's explanation of Part 107, with examples. Not binding, but it is how inspectors read the rule. [Status page](https://www.faa.gov/regulations_policies/advisory_circulars/index.cfm/go/document.information/documentID/1038977)
- [UAS FAQs](https://www.faa.gov/uas/resources/faqs): short official answers to common questions
- [Policy library](https://www.faa.gov/uas/resources/policy_library): rules, ACs, orders and legal interpretations in one place
- [AIM Chapter 11, Unmanned Aircraft Systems](https://www.faa.gov/air_traffic/publications/atpubs/aim_html/chap11_section_1.html) and [Chapter 3, Controlled Airspace](https://www.faa.gov/air_traffic/publications/atpubs/aim_html/chap3_section_2.html)
- [Remote Pilot Study Guide (FAA-G-8082-22)](https://www.faa.gov/sites/faa.gov/files/regulations_policies/handbooks_manuals/aviation/remote_pilot_study_guide.pdf) and the [UAS Airman Certification Standards](https://www.faa.gov/training_testing/testing/acs/uas_acs.pdf)
- [Pilot's Handbook of Aeronautical Knowledge](https://www.faa.gov/regulations_policies/handbooks_manuals/aviation/phak) and the [Aviation Weather Handbook (FAA-H-8083-28)](https://www.faa.gov/regulationspolicies/handbooksmanuals/aviation/faa-h-8083-28-aviation-weather-handbook)

### Which rules apply

- [Recreational flyers](https://www.faa.gov/uas/recreational_flyers): the 44809 conditions, TRUST, and what counts as recreational. Compensation alone does not decide it; when in doubt the FAA says assume Part 107
- [User identification tool](https://www.faa.gov/uas/getting_started/user_identification_tool): the FAA's "which kind of flyer am I" helper
- [Commercial operators (Part 107)](https://www.faa.gov/uas/commercial_operators)
- [Public safety and government](https://www.faa.gov/uas/public_safety_gov) and [emergency SGI waivers](https://www.faa.gov/uas/advanced_operations/emergency_situations)
- [Foreign operators](https://www.faa.gov/uas/resources/foreign_operators)

### Certificates, registration and Remote ID

- [Become a drone pilot](https://www.faa.gov/uas/commercial_operators/become_a_drone_pilot): eligibility, the UAG knowledge test, IACRA, and the free online recurrent training
- [TRUST for recreational flyers](https://www.faa.gov/uas/recreational_flyers/knowledge_test_updates)
- [Register a drone](https://www.faa.gov/uas/getting_started/register_drone): $5 for 3 years; per drone under Part 107, one registration for all of a recreational flyer's drones
- [Remote ID](https://www.faa.gov/uas/getting_started/remote_id): standard Remote ID, broadcast module, or fly in a [FRIA](https://www.faa.gov/uas/getting_started/remote_id/fria)
- [FAADroneZone](https://faadronezone-access.faa.gov/): registration, waivers, authorizations and accident reports

### Airspace, authorizations and restrictions

- [Where can I fly](https://www.faa.gov/uas/getting_started/where_can_i_fly), [Airspace 101](https://www.faa.gov/uas/recreational_fliers/where_can_i_fly/airspace_101) and [airspace restrictions](https://www.faa.gov/uas/recreational_fliers/where_can_i_fly/airspace_restrictions)
- [LAANC](https://www.faa.gov/uas/getting_started/laanc): near-real-time authorization up to the facility map ceiling, or "further coordination" above it. Approved apps are on the [UAS Data Exchange](https://www.faa.gov/uas/programs_partnerships/data_exchange) page
- [Part 107 airspace authorizations](https://www.faa.gov/uas/commercial_operators/part_107_airspace_authorizations) (DroneZone, where LAANC is not available) and [recreational authorizations](https://www.faa.gov/uas/recreational_flyers/authorization)
- [UAS facility maps](https://www.faa.gov/uas/commercial_operators/uas_facility_maps)
- [Temporary flight restrictions](https://www.faa.gov/uas/getting_started/temporary_flight_restrictions), the [TFR list](https://tfr.faa.gov/) and [NOTAM search](https://notams.aim.faa.gov/notamSearch/)
- [Critical infrastructure and public venues](https://www.faa.gov/uas/critical_infrastructure): standing restrictions over military bases, landmarks and some critical infrastructure, surface to 400 ft
- [No Drone Zone](https://www.faa.gov/uas/resources/community_engagement/no_drone_zone)
- [B4UFLY](https://www.faa.gov/uas/getting_started/b4ufly)

### Waivers and beyond the basic rules

- [Part 107 waivers](https://www.faa.gov/uas/commercial_operators/part_107_waivers): what can be waived and how to apply
- [Section 927 waivers](https://www.faa.gov/uas/commercial_operators/section_927_waivers): a 2024 Reauthorization Act waiver path for relief that would otherwise need an exemption
- [Operations over people overview](https://www.faa.gov/uas/commercial_operators/operations_over_people)
- [Advanced operations](https://www.faa.gov/uas/advanced_operations): Part 91 waivers, package delivery, chemical dispensing

### Charts and data

- [VFR sectional charts](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/vfr/), the [Chart Users' Guide](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/aero_guide/) and the [Chart Supplement](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dafd/)
- [UAS Data Delivery System](https://udds-faa.opendata.arcgis.com/): the airspace layers drones.py reads

### Safety reporting

- [Aviation Safety Reporting (ASRP) for drone pilots](https://www.faa.gov/uas/getting_started/asrp)
- [Report a UAS sighting](https://www.faa.gov/uas/contact_us/report_uas_sighting)
- [FAA Safety Team (FAASTeam)](https://www.faasafety.gov/): recurrent training courses

### State and local

The FAA governs the airspace; states and cities can still regulate privacy, trespass, takeoff
and landing on their land, and use of drones by police. These vary by place:

- [NCSL state drone law landscape](https://www.ncsl.org/transportation/current-unmanned-aircraft-state-law-landscape)
