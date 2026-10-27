# drones.py wiki

Reference notes behind drones.py: the data sources, what their fields mean, how the tool uses
them, and what they do not cover. The README covers usage; this covers the details.

- [FAA airspace (UAS Data Delivery System)](#faa-airspace-uas-data-delivery-system)
- [TFRs (tfr.faa.gov)](#tfrs-tfrfaagov)
- [Aviation weather (NOAA Aviation Weather Center)](#aviation-weather-noaa-aviation-weather-center)
- [Drone preflight reference](#drone-preflight-reference)
- [Gaps and caveats](#gaps-and-caveats)
- [Sources](#sources)

All sources are free and need no API key or account. Geocoding (place name to lat/lng) is
separate and uses OpenStreetMap Nominatim, paced to 1 request a second.

> The FAA states that facility map data is not updated in real time. Relying on it is neither
> an FAA authorization to operate nor evidence of compliance. drones.py prints an advisory to
> that effect on every airspace answer.

---

## FAA airspace (UAS Data Delivery System)

The feature services behind the FAA UAS Facility Map and the
[UDDS open data hub](https://udds-faa.opendata.arcgis.com/), credited to
*FAA/ATO/Aeronautical Information Services*. Verified live 2026-10-19.

- Service directory: <https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services?f=json>
- Every layer drones.py uses is layer `0` of its `FeatureServer`.

### Layers used

| `LAYERS` key | Service | Geometry | Fields read | Used for |
|---|---|---|---|---|
| `laanc` | `FAA_UAS_FacilityMap_Data` | Polygon (grid cell) | `CEILING`, `MAP_EFF`, `APT1..5_{FAAID,ICAO,NAME,LAANC,Enabled}`, `AIRSPACE_1..5` | LAANC ceiling (ft AGL) and which airports and programs cover the cell |
| `class` | `Class_Airspace` | Polygon | `NAME`, `ICAO_ID`, `LOCAL_TYPE`, `LOWER_*`, `UPPER_*`, `WKHR_*` | Class B/C/D/E-surface areas |
| `sua` | `Special_Use_Airspace` | Polygon | `NAME`, `TYPE_CODE`, `LOWER_*`, `UPPER_*`, `TIMESOFUSE` | Restricted, warning, alert and military operations areas |
| `prohibited` | `Prohibited_Areas` | Polygon | same as `sua` | P-areas (e.g. P-56 over the National Mall) |
| `defense` | `National_Defense_Airspace_TFR_Areas` | Polygon | `NAME`, `WKHR_CODE`, `WKHR_RMK` | National defense airspace, mostly "BY NOTAM" |
| `nsufr` | `Part_Time_National_Security_UAS_Flight_Restrictions` | Polygon | `Facility`, `Base`, `Floor`, `Ceiling`, `ALERTYPE` | Part-time national security UAS restrictions |
| `stadiums` | `Stadiums` | Point | `NAME`, `CITY`, `STATE` | 3 NM / 3,000 ft event TFRs |
| `airports` | `US_Airport` | Point | `IDENT`, `ICAO_ID`, `NAME`, `TYPE_CODE`, `PRIVATEUSE`, `ELEVATION` | Nearby airports and heliports |

### Query pattern

Every lookup is a spatial query against `<service>/FeatureServer/0/query`:

```
geometry=<lng>,<lat>            # note: x,y order
geometryType=esriGeometryPoint
inSR=4326 & outSR=4326
spatialRel=esriSpatialRelIntersects
distance=10 & units=esriSRUnit_NauticalMile   # omit both for "at this point"
outFields=<comma list>
returnGeometry=true|false
orderByFields=OBJECTID          # stable paging
f=geojson
```

With `f=geojson` the ArcGIS server does the coordinate conversion and returns standard GeoJSON.
Map queries also send `geometryPrecision=6&maxAllowableOffset=0.0002`, which simplifies shapes by
about 20 m: large polygons stay small without distorting the LAANC grid cells.

**Paging:** a page is capped at the layer's `maxRecordCount` (2,000 for most layers, 1,000 for
`US_Airport`). A truncated page carries `properties.exceededTransferLimit: true`, and the client
re-requests with `resultOffset`. A 10 NM radius around Chicago Midway returns 550 grid cells, so
one page usually covers it, but busier areas can exceed one.

**Throttling:** ArcGIS Online rate limits bursts, either with HTTP 429 or with a 200 whose body
is an error mentioning "Too many requests". drones.py runs at most 4 layer queries at once and
retries after 2, 5 and 10 seconds before giving up. Back-to-back heavy radius queries (a dense
LAANC grid such as downtown DC) can still trip it; waiting a minute clears it.

### Field notes

- **LAANC grid cells** are 30 x 30 arc-second squares (about 0.5 x 0.4 NM). Cells only exist in
  controlled airspace near airports. No cell at a point usually means Class G, but always check
  `Class_Airspace` too.
- `CEILING` is the maximum altitude the FAA will pre-approve through LAANC, in **feet AGL**, in
  multiples of 50 ft. `0` means no automatic approval.
- `APTn_LAANC = 1` means the airport participates in LAANC. `APTn_Enabled` lists the programs:
  - `107-AA`: Part 107 automatic approval
  - `107-FC`: Part 107 further coordination (requests above the ceiling, handled manually)
  - `44809-AA`: recreational (49 USC 44809) automatic approval
- `MAP_EFF` is the effective date of the current facility map publication (e.g. `10/1/2026`).
- **Class airspace** includes types that don't matter for drones. drones.py keeps only `CLASS_B`,
  `CLASS_C`, `CLASS_D`, `CLASS_E2` (surface area) and `CLASS_E3`/`CLASS_E4` (surface
  extensions). It drops Class A, the `MODE C` veil, and the `CLASS_E5`/`CLASS_E6` floors at 700
  or 1,200 ft. Class B and C "shelves" come back as separate polygons with a non-zero
  `LOWER_VAL`; drones.py shows those as overhead, not at the surface.
- **Altitude fields:** `*_VAL` is a number, `*_UOM` is `FT` or `FL`, and `*_CODE` is `SFC`, `MSL`,
  `AGL` or `STD`. `UPPER_VAL = -9998` means "up to Class A". In `Special_Use_Airspace` and
  `Prohibited_Areas` the values are **strings** (`'4500'`).
- **SUA floors** are mostly MSL. Without ground elevation an MSL floor can't be compared to
  400 ft AGL, so only `SFC` floors and AGL floors at or below 400 ft count as reaching drone
  altitudes; the rest are listed as overhead (a military operations area from 9,000 ft MSL is
  overhead).
- `WKHR_CODE = H24` means continuous operation. Any other code, such as a part-time Class D
  tower, carries its schedule in `WKHR_RMK`.
- **Stadiums** carry `LATITUDE`/`LONGITUDE` as DMS strings (`40-26-49.38N`); drones.py uses the point
  geometry instead. The layer has no event schedule, so the 3 NM TFR is only "active during
  qualifying events".

### Other layers in the org, not used

`Digital_Obstacle_File`, `Airspace_Schedule`, `Boundary_Airspace`, `Route_Airspace`, `Runways`,
and the `Pending_*` layers (published changes not yet effective). The `_V5`, `_Test`, `_Dev`,
`_Primary` and `_Secondary` variants of the facility map look like FAA internal copies; stick to
`FAA_UAS_FacilityMap_Data`.

---

## TFRs (tfr.faa.gov)

### Shapes (GeoServer WFS)

```
https://tfr.faa.gov/geoserver/TFR/ows?service=WFS&version=1.1.0&request=GetFeature
    &typeName=TFR:V_TFR_LOC&outputFormat=application/json&srsname=EPSG:4326
```

- Every published TFR nationwide as GeoJSON polygons in `[lng, lat]` order: about 75 features
  and 95 KB in October 2026. drones.py fetches the whole feed and does the point-in-polygon and
  distance tests itself.
- Properties: `NOTAM_KEY` (`6/9169-1-FDC-F`; the NOTAM number is the part before the first `-`),
  `TITLE` (place and date range), `LEGAL` (`SECURITY`, `HAZARDS`, `VIP`, `SPACE OPERATIONS`,
  `AIR SHOWS/SPORTS`, `UAS PUBLIC GATHERING`), `CNS_LOCATION_ID` (ARTCC), `STATE`,
  `LAST_MODIFICATION_DATETIME`.
- The feed includes **scheduled** TFRs, not just active ones, and has **no times or altitudes**.
  A renewed TFR is a new NOTAM on an identical shape, so the same polygon can appear several
  times; the map draws it once and links every NOTAM.

### Lists, text and detail pages

| Endpoint | Returns |
|---|---|
| `https://tfr.faa.gov/tfrapi/exportTfrList` | JSON list: `notam_id`, `type`, `facility`, `state`, `description`, `creation_date` |
| `https://tfr.faa.gov/tfrapi/getWebText?notamId=4/9383` | JSON `{notam_id, text}`, where `text` is the NOTAM's HTML page |
| `https://tfr.faa.gov/tfr3/?page=detail_4_9383` | Human detail page (`/` in the NOTAM id becomes `_`). drones.py links to this. |

### How drones.py reads TFR times

The shape feed can't say whether a TFR is in force, so drones.py fetches `getWebText` for each TFR
that matters (the ones containing the point, or within the radius) and reads the page text:

- `Beginning Date and Time` / `Ending Date and Time` are the overall window across all areas:
  `October 26, 2026 at 1300 UTC`, or `Effective Immediately` and `Permanent` for open ends. A
  multi-area TFR (VIP movements) can give each area a narrower `Effective Date(s)` inside that
  window; drones.py uses the overall one, which is the conservative choice.
- Each area has an `Altitude:` line such as `From the surface up to and including 4999 feet
  AGL`. drones.py reports the lowest floor and highest ceiling across areas.
- Status at the time checked (now, or `preflight --at`): **active** is NO-FLY, **scheduled** is
  CAUTION with its window, **expired** (the feed can lag) is dropped. If the text can't be
  fetched or parsed the TFR is treated as active.

---

## Aviation weather (NOAA Aviation Weather Center)

AWC is the FAA-operated, free, public source that virtually every aviation weather product
downstream is built on. Consumer weather APIs give temperature and general forecasts but not the
primitives that gate a flight: surface wind and gusts, visibility, cloud ceiling, flight
category, convective hazards. `api.weather.gov` (NWS) has observations but no TAFs.

### API basics

| Property | Value |
|---|---|
| Base URL | `https://aviationweather.gov/api/data/` |
| Authentication | None |
| Rate limit | 100 requests a minute globally; about 1 a minute per thread recommended |
| Max results per query | 400 |
| Formats | `raw` (default), `json`, `geojson`, `xml`, `iwxxm`, `decoded` (varies by endpoint) |
| OpenAPI | <https://aviationweather.gov/data/schema/openapi.yaml> |
| Cache files | gzipped XML at `/data/cache/*.cache.xml.gz`, refreshed every 1-10 min, meant for bulk consumers (e.g. `stations.cache.xml.gz`) |

Errors are standard HTTP: `204` no content (an empty result, **not** `[]`), `400` invalid
request, `404`, `429` too many requests, `500-504`. Sustained abuse triggers temporary IP blocks.

### Endpoints

| Endpoint | Purpose | Used by drones.py |
|---|---|---|
| `/metar` | Current observations at airports and AWOS sites | yes - `metar`, `preflight` |
| `/taf` | Terminal Aerodrome Forecast (24-30 h) | yes - `taf`, `preflight` |
| `/stationinfo` | Station metadata, including which reports a station files | yes - nearest-station search |
| `/pirep` | Pilot reports: turbulence, icing, ceilings at low altitude | no |
| `/airsigmet` | SIGMETs (severe weather) and legacy AIRMETs; convective SIGMETs are the drone-relevant ones | no |
| `/gairmet` | Graphical AIRMETs, hazard specific and time stepped, CONUS: surface wind, low-level wind shear, IFR | no |
| `/cwa` | Center Weather Advisories: short-fuse hazards (next 2 h) over an ARTCC region | no |
| `/airport` | Airport info | no |

### Query parameters

**`/metar`** - `ids` (ICAO `KCHO`, comma list `KCHO,KIAD`, or state `@VA`) or `bbox`
(`lat0,lon0,lat1,lon1`), one of them required; `format`; `taf=true` attaches each station's TAF
as raw text (`rawTaf`, not decoded); `hours` how far back (default 1.5); `date` (`yyyymmdd_hhmm`
or ISO, up to 15 days back).

**`/taf`** - `ids` or `bbox` as above; `format` (`raw`, `json`, `geojson`, `xml`, `iwxxm`);
`metar` include the latest observation; `time` = `valid` (forecast in force now, default) or
`issue`; `date`.

**`/stationinfo`** - `ids` or `bbox`; `format=json` gives `icaoId`, `site`, `state`, `lat`,
`lon`, `elev`, `priority` and `siteType` (e.g. `["METAR", "TAF"]`; buoys and other sensors come
back with `[]`).

**`/pirep`** - `id` (airport as centre), `distance` (statute miles), `bbox`, `format`, `age`
(hours back), `level` (flight level +/- 3,000 ft), `inten` (`lgt`, `mod`, `sev`), `date`.

**`/airsigmet`** - `format`, `types` (`sigmet`; `airmet` is deprecated in favour of
`/gairmet`), `hazard` (`conv`, `turb`, `ice`, `ifr`), `level`.

**`/gairmet`** - `product` (`sierra` IFR/mountain obscuration, `tango` turbulence/wind, `zulu`
icing/freezing level), `format`, `hazard` (`turb-hi`, `turb-lo`, `llws`, `sfc_wind`, `ifr`,
`mtn_obs`, `ice`, `fzlvl`), `fore` forecast hour (`0`, `3`, `6`, `9`, `12`), `date`.
`sfc_wind`, `llws` and `ifr` cover the three hazards that ground small drones most often.

**`/cwa`** - `hazard` (`ts`, `turb`, `ice`, `ifr`, `pcpn`, `unk`), `format`, `date`.

AWC has deprecated parameters before (`recent`, `order`, `vfr`, `output`, `filter`, `fields`,
`tempo` on METAR/TAF are gone); re-check the OpenAPI spec before relying on an edge-case field.

### METAR JSON

```jsonc
{
  "icaoId": "KFLL",
  "obsTime": 1792972380,                 // Unix seconds
  "reportTime": "2026-10-26T00:00:00.000Z",
  "temp": 25, "dewp": 20.6,              // deg C
  "wdir": 40,                            // degrees true, or "VRB"
  "wspd": 13,                            // knots, sustained
  "wgst": 22,                            // knots, gust; absent when no gust
  "visib": "10+",                        // statute miles; "10+" unrestricted, also 3, 0.5, "1/2"
  "altim": 1013.6,                       // hPa
  "rawOb": "METAR KFLL 252353Z 04013G22KT 10SM SCT026CB BKN036 BKN090 25/21 A2993 RMK ... CB ALQDS MOV SW ...",
  "lat": 26.0787, "lon": -80.1622, "elev": 1,
  "name": "Ft Lauderdale-Hollywood Intl, FL, US",
  "cover": "BKN",
  "clouds": [ {"cover": "SCT", "base": 2600}, {"cover": "BKN", "base": 3600} ],   // base ft AGL
  "fltCat": "VFR"                        // VFR / MVFR / IFR / LIFR
}
```

- Optional fields (`wgst`, `wxString`, `precip`, ...) are **omitted** when absent, not `null`.
- **The cloud list drops the CB/TCU suffix.** `SCT026CB` arrives as plain `SCT 2600`, and remarks
  such as `CB ALQDS MOV SW` (cumulonimbus all quadrants, moving southwest) only exist in `rawOb`.
  drones.py parses the raw report to restore both, since cumulonimbus is the cloud detail that
  matters most to a drone.
- There is no ceiling field: the ceiling is the lowest `BKN`, `OVC` or obscured (`OVX`/vertical
  visibility) layer.

### TAF JSON

- Top level: `icaoId`, `name`, `issueTime`, `validTimeFrom`/`validTimeTo` (Unix), `rawTAF`, and
  `fcsts`, one entry per period.
- Each period: `timeFrom`/`timeTo` (Unix), `fcstChange` (`null` for the first period, `FM`,
  `TEMPO`, `BECMG`, `PROB`), `probability`, `wdir`/`wspd`/`wgst`, `visib` (`"6+"` = P6SM),
  `wxString`, `vertVis`, `clouds` (with a `type` for CB/TCU, unlike METAR), wind shear fields,
  `icgTurb`, `temp`.
- **`FM` starts a new prevailing period; `TEMPO`, `PROB` and `BECMG` overlay the one in force**
  and list only what changes. An overlay's unchanged visibility comes back as `""`, not absent.
- There is no flight category on TAF periods; drones.py computes it from ceiling and visibility.

### Flight categories

The worse of ceiling and visibility decides:

| Category | Ceiling | Visibility |
|---|---|---|
| VFR | > 3,000 ft | > 5 SM |
| MVFR (marginal VFR) | 1,000-3,000 ft | 3-5 SM |
| IFR | 500-999 ft | 1-<3 SM |
| LIFR (low IFR) | < 500 ft | < 1 SM |

### Stations

- About **2,900** US stations file METARs; about **860** of them issue TAFs. A station's TAF is
  attached to its METAR record, so a station with no METAR can never answer.
- **TAF-capable is not TAF-issuing.** At any moment a few dozen flagged stations have no current
  TAF: part-time fields, military bases overnight and at weekends, `AMD NOT SKED`, stations whose
  METARs have stopped. North Perry (KHWO) is one. Whether a forecast exists is decided by what
  AWC returns, never by the flag alone.
- **Nearest observation is not nearest forecast.** Across the lower 48 the nearest station is
  observation-only at roughly two thirds of locations. Median distance to the nearest METAR
  station is about 17 NM, to the nearest TAF airport about 30 NM, and in open country the nearest
  TAF can be 50-70 NM away while a field a mile off is reporting. In a city both usually land on
  the same airport (Miami: KMIA); elsewhere they split (Boca Raton: observation KBCT, forecast
  KFXE; Broken Bow, NE: observation KBBW, forecast KLBF about 49 NM away).
- A TAF officially covers 5 SM around its airport.

**How drones.py picks a station:** `/stationinfo?bbox=` over rings of 15, 30, 60 and 120 NM until a
qualifying station turns up, sorted by true distance (the bbox result is unsorted and its corners
reach past the circle, so both are fixed client side). The nearest five candidates go into one
`ids=` request, and the first that actually returned a report wins; skipped stations are noted on
stderr. TAFs farther than 10 NM come with a "treat it as regional" warning.

**Update cadence:** METARs roughly hourly (SPECI sooner on significant changes), TAFs every 6
hours, G-AIRMETs on a 3-hour cycle.

---

## Drone preflight reference

### Part 107 weather rules

| Rule | Source field | Threshold for go | In drones.py |
|---|---|---|---|
| 107.51(c): 3 SM visibility from the control station | METAR `visib` | `>= 3` SM or `"10+"` | yes |
| 107.51(d)(1): 500 ft below clouds | cloud bases (ft AGL) and planned altitude | lowest base `>= altitude + 500` | yes (`--altitude`) |
| 107.51(d)(2): 2,000 ft horizontal from clouds | cloud cover | operator judgment | no |
| 107.51(b): below 400 ft AGL | - | not weather driven | - |
| 107.49(a)(2): aircraft capable of safe operation | `wspd`, `wgst` | airframe specific | yes (`--max-wind`) |

drones.py fails broken/overcast layers below altitude + 500 ft and warns on few/scattered ones
(fly clear of them). It fails thunderstorms and warns on cumulonimbus, precipitation and
fog/mist/haze.

### Airframe wind tolerance

Not regulatory; manufacturer wind resistance ratings.

| Airframe | Max sustained wind |
|---|---|
| DJI Mini 4 Pro | about 10.7 m/s (about 21 kt), "Level 5" |
| DJI Air 3 | about 12 m/s (about 23 kt), "Level 6" |
| DJI Mavic 3 | about 12 m/s (about 23 kt), "Level 6" |
| DJI Matrice 350 RTK | about 12 m/s (about 23 kt), industrial |

A common rule of thumb is no-go when gusts exceed 1.5x the airframe's sustained limit even if the
sustained wind is fine, since gust transients are what cause loss of control. drones.py instead
compares the higher of wind and gust to one `--max-wind` and warns within 85% of it.

---

## Gaps and caveats

**Not covered by drones.py:**

- **Filing LAANC authorizations.** Only FAA-approved UAS Service Suppliers (Aloft, Airspace Link
  and others) can submit requests, and their developer APIs are gated. drones.py reports
  eligibility and ceilings and points the pilot to a LAANC app.
- **NOTAMs other than TFRs.** The FAA NOTAM API (`api.faa.gov`) requires a registered key.
- **Real-time activation** of special use, national defense and part-time security airspace.
  These go active by NOTAM, so drones.py flags them as caution rather than deciding.
- **Non-US airspace.** The FAA layers cover the US and its territories; a point elsewhere returns
  no features. Weather works anywhere AWC has a station.

**Not available from AWC:**

| Gap | Note |
|---|---|
| Wind at flight altitude | METARs measure about 10 m above the airport; wind at 400 ft AGL is often quite different. NOAA HRRR model data or a micro-forecast service would be needed. |
| Precipitation radar | Not exposed; `api.weather.gov/radar` has tile overlays. |
| Lightning detection | Commercial only (Vaisala, Earth Networks). |
| Sub-hourly updates | Standard METARs are hourly and SPECIs fire only on threshold changes. |

**Operational notes:**

- **Density altitude** is not in the METAR; compute it from `temp`, `altim` and station `elev`.
  It matters for hot-and-high operations where heavy drones lose payload capacity.
- **Icing** (G-AIRMET `hazard=ice`) matters only near the freezing level; relevant in winter.
- **Outages:** AWC sometimes degrades or goes down. Secondary sources exist (AVWX, CheckWX).
- **Attribution:** NOAA data is public; attribution is courteous, not required.

---

## Sources

- [FAA UAS Data Delivery System](https://udds-faa.opendata.arcgis.com/) and its
  [service directory](https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services?f=json)
- [FAA TFR list](https://tfr.faa.gov/tfr3/)
- [AviationWeather.gov API documentation](https://aviationweather.gov/data/api/) and the
  [OpenAPI spec](https://aviationweather.gov/data/schema/openapi.yaml), the authoritative
  parameter reference
- [Bulk station file](https://aviationweather.gov/data/cache/stations.cache.xml.gz)
- [14 CFR Part 107, small UAS operating rules](https://www.ecfr.gov/current/title-14/chapter-I/subchapter-F/part-107)
