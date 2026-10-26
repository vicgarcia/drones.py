#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "geopy>=2.4.0",
#     "requests>=2.31.0",
# ]
# ///
"""
faa.py - FAA airspace and aviation weather for drone pilots.

Answers "can I fly here?" for a place name or lat/lng: controlled airspace,
LAANC grid ceilings, special use airspace, national security restrictions,
stadiums and TFRs. Can also render the airspace around a point on a local map.

Airspace comes from the FAA's UAS Data Delivery System (the public ArcGIS
layers behind the FAA UAS Facility Map) and tfr.faa.gov. No API key required.
"""

import argparse
import http.server
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests
from geopy.distance import geodesic
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
from geopy.point import Point

# User agent for Nominatim (required) and the FAA services
USER_AGENT = "faa.py-cli/1.0"

# How many geocoding candidates to pull back so ambiguity is detectable. Same
# single request either way, so this costs nothing against the rate limit.
CANDIDATE_LIMIT = 5

# Nominatim scores every match with an "importance". When the runner-up scores
# this close to the winner the query is a genuine toss-up, not a clear hit.
AMBIGUITY_RATIO = 0.85

# Public FAA Aeronautical Information Services org on ArcGIS Online
FAA_SERVICES = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/ArcGIS/rest/services"
TFR_SHAPES_URL = "https://tfr.faa.gov/geoserver/TFR/ows"
TFR_DETAIL_URL = "https://tfr.faa.gov/tfr3/?page=detail_{}"

# Seconds to wait between retries when a service throttles us
RETRY_DELAYS = (2, 5, 10)

# Concurrent layer queries; ArcGIS Online throttles bigger bursts
MAX_PARALLEL_QUERIES = 4

DEFAULT_RADIUS_NM = 10.0
STADIUM_TFR_NM = 3.0

# Page time a headless screenshot allows for tiles and data to load
SCREENSHOT_WAIT_MS = 20000
# Square, since the map frames the circular search radius
DEFAULT_PNG_SIZE = (1000, 1000)
KM_PER_NM = 1.852

ADVISORY = ("Advisory only - FAA data is not real time. Confirm with B4UFLY or a "
            "LAANC app and check NOTAMs before you fly.")

CLI_EPILOG = """\
Examples:
  faa.py airspace "Chicago Midway Airport"
  faa.py airspace 41.79,-87.74
  faa.py airspace "Montrose Beach" --near "Chicago, IL" --within 15
  faa.py airspace "-33.86,151.21" --json

  faa.py nearby "Navy Pier, Chicago"
  faa.py nearby 38.8895,-77.0353 --radius 5

  faa.py map "Chicago Midway Airport"
  faa.py map 41.79,-87.74 --radius 5 --tiles satellite
  faa.py map "Denver, CO" --geojson > denver.geojson
  faa.py map "Navy Pier, Chicago" --radius 2 --png navy-pier.png
  faa.py map 41.79,-87.74 --png midway.png --tiles satellite --size 1920x1080

Location:
  Every command takes a place name, address or raw coordinates:
    "41.79,-87.74"         Raw lat,lng (comma or space separated)
    "Chicago Midway"       Place or address (geocoded with OpenStreetMap)
  --near/--within constrain geocoding to an area when a name is ambiguous.

Data:
  FAA UAS Facility Map (LAANC grid), class airspace, special use airspace,
  prohibited areas, national defense airspace, part-time national security
  UAS restrictions, stadiums, recreational flyer fixed sites, airports, and
  TFR shapes from tfr.faa.gov. Distances are nautical miles (NM).
"""


# Keyless raster tile providers. Each is free to use under its own fair-use
# policy - keep traffic light and leave the attribution intact.
TILE_LAYERS = {
    "street": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        "max_zoom": 19,
    },
    "satellite": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution": 'Tiles &copy; <a href="https://www.esri.com/">Esri</a>, Maxar, Earthstar Geographics',
        "max_zoom": 19,
    },
    "dark": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        "attribution": 'Tiles &copy; <a href="https://www.esri.com/">Esri</a> | &copy; OpenStreetMap contributors',
        "max_zoom": 16,
    },
}


# Fields shared by the special use and prohibited airspace layers
SUA_FIELDS = ["NAME", "TYPE_CODE", "LOWER_VAL", "LOWER_UOM", "LOWER_CODE",
              "UPPER_VAL", "UPPER_UOM", "UPPER_CODE", "TIMESOFUSE", "CITY", "STATE"]

# Class airspace that reaches the surface or caps it. Class A, the Mode C veil
# and the 700/1200 ft Class E floors all sit above where drones fly.
DRONE_CLASSES = ("CLASS_B", "CLASS_C", "CLASS_D", "CLASS_E2", "CLASS_E3", "CLASS_E4")

LAYERS = {
    "laanc": {
        "service": "FAA_UAS_FacilityMap_Data",
        "fields": ["CEILING", "UNIT", "MAP_EFF"]
                  + [f"APT{n}_{field}" for n in range(1, 6)
                     for field in ("FAAID", "ICAO", "NAME", "LAANC", "Enabled")]
                  + [f"AIRSPACE_{n}" for n in range(1, 6)],
    },
    "class": {
        "service": "Class_Airspace",
        "where": "LOCAL_TYPE IN ({})".format(",".join(f"'{c}'" for c in DRONE_CLASSES)),
        "fields": ["NAME", "IDENT", "ICAO_ID", "CLASS", "LOCAL_TYPE",
                   "LOWER_VAL", "LOWER_UOM", "LOWER_CODE",
                   "UPPER_VAL", "UPPER_UOM", "UPPER_CODE", "WKHR_CODE", "WKHR_RMK"],
    },
    "sua": {"service": "Special_Use_Airspace", "fields": SUA_FIELDS},
    "prohibited": {"service": "Prohibited_Areas", "fields": SUA_FIELDS},
    "defense": {
        "service": "National_Defense_Airspace_TFR_Areas",
        "fields": ["NAME", "WKHR_CODE", "WKHR_RMK", "CITY", "STATE"],
    },
    "nsufr": {
        "service": "Part_Time_National_Security_UAS_Flight_Restrictions",
        "fields": ["Facility", "Base", "Reason", "Floor", "Ceiling", "ALERTYPE", "State"],
    },
    "stadiums": {"service": "Stadiums", "fields": ["NAME", "CITY", "STATE", "STATUS_CODE"]},
    "recreational": {
        "service": "Recreational_Flyer_Fixed_Sites",
        "fields": ["SITE_NAME", "CEILING", "UNIT", "CITY", "STATE"],
    },
    "airports": {
        "service": "US_Airport",
        "where": "OPERSTATUS = 'OPERATIONAL'",
        "fields": ["IDENT", "ICAO_ID", "NAME", "TYPE_CODE", "PRIVATEUSE",
                   "ELEVATION", "SERVCITY", "STATE"],
    },
}

CLASS_LABELS = {
    "CLASS_B": "Class B",
    "CLASS_C": "Class C",
    "CLASS_D": "Class D",
    "CLASS_E2": "Class E surface",
    "CLASS_E3": "Class E extension",
    "CLASS_E4": "Class E extension",
}

SUA_TYPES = {
    "P": "Prohibited area",
    "R": "Restricted area",
    "W": "Warning area",
    "MOA": "Military operations area",
    "A": "Alert area",
    "D": "Danger area",
}

LAANC_PROGRAMS = {
    "107-AA": "Part 107 auto approval",
    "107-FC": "Part 107 further coordination",
    "44809-AA": "Recreational auto approval",
}

AIRPORT_TYPES = {
    "AD": "airport",
    "HP": "heliport",
    "SP": "seaplane base",
    "UL": "ultralight",
    "GL": "gliderport",
    "BP": "balloonport",
}

# LAANC grid colours, red at 0 ft through green at 400 ft
CEILING_COLORS = [
    (0, "#d7301f"), (50, "#f46d43"), (100, "#fdae61"),
    (200, "#fee08b"), (300, "#a6d96a"), (400, "#1a9850"),
]

# Sectional chart convention: B solid blue, C solid magenta, D dashed blue,
# Class E surface areas dashed magenta
CLASS_STYLES = {
    "CLASS_B": {"stroke": "#1f4e9c"},
    "CLASS_C": {"stroke": "#a6266b"},
    "CLASS_D": {"stroke": "#1f4e9c", "stroke-dasharray": "8 6"},
    "CLASS_E2": {"stroke": "#a6266b", "stroke-dasharray": "8 6"},
    "CLASS_E3": {"stroke": "#a6266b", "stroke-dasharray": "3 6"},
    "CLASS_E4": {"stroke": "#a6266b", "stroke-dasharray": "3 6"},
}

# Map overlays in draw order, bottom to top. A filled shape swallows clicks on
# everything beneath it, so broad areas sit low and the small, specific
# features a pilot clicks on (grid cells, TFRs, airports) sit on top.
MAP_OVERLAYS = [
    ("radius", "Search radius"),
    ("sua", "Special use airspace"),
    ("nsufr", "National security UAS restrictions"),
    ("defense", "National defense airspace"),
    ("class", "Class airspace"),
    ("stadiums", "Stadiums (3 NM)"),
    ("laanc", "LAANC grid"),
    ("recreational", "Recreational flyer sites"),
    ("prohibited", "Prohibited areas"),
    ("tfrs", "TFRs"),
    ("airports", "Airports"),
    ("location", "Location"),
]

# Overlays drawn for reference only, never intercepting clicks
PASSIVE_OVERLAYS = ["radius"]

MAP_PAGE = """\
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>faa.py - __TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body, #map { height: 100%; margin: 0; }
  .leaflet-popup-content { font: 13px/1.45 system-ui, sans-serif; }
  .leaflet-popup-content dt { font-weight: 600; margin-top: 4px; }
  .leaflet-popup-content dd { margin: 0; }
  .legend {
    background: #fff; color: #222; padding: 6px 8px; border-radius: 4px;
    font: 12px/1.6 system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,.3);
  }
  .legend i {
    display: inline-block; width: 12px; height: 12px; margin-right: 6px;
    vertical-align: -2px; opacity: .8;
  }
  .ceiling {
    display: none; background: none; border: none; box-shadow: none; padding: 0;
    font: 600 11px system-ui, sans-serif; color: #222;
    text-shadow: 0 0 2px #fff, 0 0 2px #fff, 0 0 2px #fff;
  }
  .show-ceilings .ceiling { display: block; }
__SCREENSHOT_CSS__
</style>
</head>
<body>
<div id="map"></div>
<script>
const CENTER = __CENTER__;
const OVERLAYS = __OVERLAYS__;
const BASEMAPS = __BASEMAPS__;
const DEFAULT_BASEMAP = __DEFAULT_BASEMAP__;
const CEILING_COLORS = __CEILING_COLORS__;
const PASSIVE = __PASSIVE__;
// LAANC ceiling labels only once the grid cells are big enough to read
const LABEL_ZOOM = 14;

// Fractional zoom lets fitBounds fill the view with the radius ring instead
// of snapping out to the next whole level, which can leave half the frame empty
const map = L.map('map', { preferCanvas: true, zoomSnap: 0.1 });
const bases = {};
for (const [name, tiles] of Object.entries(BASEMAPS)) {
  // Upscale past a provider's last zoom level rather than going blank
  bases[name] = L.tileLayer(tiles.url, {
    maxNativeZoom: tiles.max_zoom, maxZoom: 19, attribution: tiles.attribution
  });
}
bases[DEFAULT_BASEMAP].addTo(map);
const control = L.control.layers(bases, {}, { collapsed: false }).addTo(map);
const layers = {};

function esc(value) {
  return String(value).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// simplestyle-spec: https://github.com/mapbox/simplestyle-spec
function styleFor(feature) {
  const p = feature.properties || {};
  const geometry = feature.geometry || {};
  if (geometry.type && geometry.type.indexOf('Point') !== -1 && p['marker-color']) {
    return { color: '#fff', weight: 2, fillColor: p['marker-color'], fillOpacity: 1 };
  }
  return {
    color: p['stroke'] || '#3388ff',
    weight: p['stroke-width'] !== undefined ? p['stroke-width'] : 2,
    opacity: p['stroke-opacity'] !== undefined ? p['stroke-opacity'] : 0.9,
    dashArray: p['stroke-dasharray'] || null,
    fillColor: p['fill'] || p['stroke'] || '#3388ff',
    fillOpacity: p['fill-opacity'] !== undefined ? p['fill-opacity'] : 0.1
  };
}

function markerFor(feature, latlng) {
  const p = feature.properties || {};
  const sizes = { small: 5, medium: 7, large: 10 };
  if (!p['marker-color']) return L.marker(latlng);
  return L.circleMarker(latlng, {
    radius: sizes[p['marker-size']] || 7,
    color: '#fff', weight: 2, fillColor: p['marker-color'], fillOpacity: 1
  });
}

const HIDDEN_KEYS = /^(stroke|fill|marker-|_)/;

function popupFor(feature, layer) {
  const p = feature.properties || {};
  const parts = [];
  if (p.title) parts.push('<strong>' + esc(p.title) + '</strong>');
  if (p.description) parts.push(esc(p.description));
  const extra = Object.keys(p)
    .filter(k => k !== 'title' && k !== 'description' && !HIDDEN_KEYS.test(k));
  if (extra.length) {
    parts.push('<dl>' + extra
      .map(k => '<dt>' + esc(k) + '</dt><dd>' + esc(p[k]) + '</dd>').join('') + '</dl>');
  }
  const links = p._links || (p.url ? [['Details', p.url]] : []);
  for (const [label, url] of links) {
    parts.push('<a href="' + esc(url) + '" target="_blank">' + esc(label) + '</a>');
  }
  if (parts.length) layer.bindPopup(parts.join('<br>'));
  if (p._ceiling !== undefined) {
    layer.bindTooltip(String(p._ceiling), {
      permanent: true, direction: 'center', className: 'ceiling'
    });
  }
}

function toggleCeilings() {
  map.getContainer().classList.toggle('show-ceilings', map.getZoom() >= LABEL_ZOOM);
}
map.on('zoomend', toggleCeilings);

const legend = L.control({ position: 'bottomright' });
legend.onAdd = () => {
  const div = L.DomUtil.create('div', 'legend');
  div.innerHTML = '<strong>LAANC ceiling (ft AGL)</strong><br>' + CEILING_COLORS
    .map(([ft, color]) => '<i style="background:' + color + '"></i>' + ft).join('<br>');
  return div;
};
legend.addTo(map);

fetch('data.json')
  .then(r => r.json())
  .then(data => {
    for (const [key, label] of OVERLAYS) {
      const features = data.features.filter(f => (f.properties || {})._layer === key);
      if (!features.length) continue;
      const passive = PASSIVE.includes(key);
      layers[key] = L.geoJSON({ type: 'FeatureCollection', features }, {
        style: styleFor, pointToLayer: markerFor,
        onEachFeature: passive ? null : popupFor, interactive: !passive
      }).addTo(map);
      // Merged TFR shapes count once per NOTAM they carry
      const total = features.reduce((n, f) => n + ((f.properties._links || []).length || 1), 0);
      const count = (key === 'radius' || key === 'location') ? '' : ' (' + total + ')';
      control.addOverlay(layers[key], esc(label) + count);
    }
    const bounds = layers.radius && layers.radius.getBounds();
    if (bounds && bounds.isValid()) {
      map.fitBounds(bounds, { padding: [20, 20] });
    } else {
      map.setView(CENTER, 12);
    }
    toggleCeilings();
  })
  .catch(err => console.error('faa.py: failed to load data.json', err));
</script>
</body>
</html>
"""


class FAAError(Exception):
    """Base exception for faa.py errors."""
    pass


# ============================================================================
# Locating: coordinates or geocoded place names
# ============================================================================

class Locator:
    """Resolve a location string to coordinates using OpenStreetMap Nominatim."""

    COORDINATE_PATTERN = re.compile(
        r'^([-+]?\d+\.?\d*)\s*[,\s]\s*([-+]?\d+\.?\d*)$'
    )

    def __init__(self):
        self.geolocator = Nominatim(user_agent=USER_AGENT, timeout=10)
        # Share one pacing clock between forward and reverse lookups.
        self._lookup = RateLimiter(
            self._nominatim_lookup, min_delay_seconds=1.1,
            max_retries=0, swallow_exceptions=False,
        )

    def _nominatim_lookup(self, operation: str, query: str, **kwargs):
        return getattr(self.geolocator, operation)(query, **kwargs)

    @classmethod
    def parse_coordinates(cls, location: str) -> Optional[tuple[float, float]]:
        """Return (lat, lng) when the string is raw coordinates, else None."""
        match = cls.COORDINATE_PATTERN.match(location.strip())
        if not match:
            return None

        lat, lng = float(match.group(1)), float(match.group(2))
        errors = []
        if not math.isfinite(lat) or lat < -90 or lat > 90:
            errors.append(f"Latitude {lat} out of range [-90, 90]")
        if not math.isfinite(lng) or lng < -180 or lng > 180:
            errors.append(f"Longitude {lng} out of range [-180, 180]")
        if errors:
            raise FAAError(f"Invalid coordinates: {', '.join(errors)}")
        return lat, lng

    def locate(
        self,
        location: str,
        near: Optional[str] = None,
        within_miles: float = 25.0,
        lookup_address: bool = True,
    ) -> dict:
        """
        Resolve coordinates or a place name.

        Coordinates are used as given, with a best-effort reverse lookup for a
        readable name. Place names are geocoded, optionally inside a box around
        --near, and ambiguous matches are flagged on stderr.
        """
        coordinates = self.parse_coordinates(location)
        if coordinates:
            lat, lng = coordinates
            address = None
            if lookup_address:
                try:
                    address = self.reverse(lat, lng)
                except FAAError:
                    pass  # a name is nice to have, the coordinates are what matter
            return {"query": location, "latitude": lat, "longitude": lng, "address": address}

        viewbox = None
        if near:
            anchor = self.locate(near, lookup_address=False)
            viewbox = viewbox_around(anchor["latitude"], anchor["longitude"],
                                     within_miles * 1.60934)

        result = self.geocode(location, viewbox=viewbox)
        if not result:
            if near:
                raise FAAError(f"No results for '{location}' within "
                                    f"{within_miles} miles of {near}")
            raise FAAError(f"Could not geocode: {location}")

        _warn_if_ambiguous(result, location)
        return {"query": location, "latitude": result["latitude"],
                "longitude": result["longitude"], "address": result["address"]}

    def geocode(self, address: str, viewbox: Optional[list] = None) -> Optional[dict]:
        """
        Geocode a place name. Runners-up come back under "alternatives" so the
        caller can tell whether the query was ambiguous.
        """
        try:
            kwargs = {"addressdetails": True, "exactly_one": False, "limit": CANDIDATE_LIMIT}
            if viewbox:
                kwargs["viewbox"] = viewbox
                kwargs["bounded"] = True

            found = list(self._lookup("geocode", address, **kwargs) or [])
            if not found:
                return None

            location, runners_up = found[0], found[1:]
            return {
                "latitude": location.latitude,
                "longitude": location.longitude,
                "address": location.address,
                "alternatives": [
                    {
                        "latitude": alt.latitude,
                        "longitude": alt.longitude,
                        "address": alt.address,
                        "type": alt.raw.get("type"),
                        "importance": float(alt.raw.get("importance") or 0),
                    }
                    for alt in runners_up
                ],
                "raw": location.raw,
            }
        except Exception as e:
            raise FAAError(f"Geocoding failed: {e}")

    def reverse(self, lat: float, lng: float) -> Optional[str]:
        """Reverse geocode coordinates to an address string."""
        try:
            location = self._lookup("reverse", f"{lat}, {lng}")
            return location.address if location else None
        except Exception as e:
            raise FAAError(f"Reverse geocoding failed: {e}")


def viewbox_around(lat: float, lng: float, radius_km: float) -> list:
    """
    Build a Nominatim viewbox around a point.

    Free-form search will happily match a same-named street in the wrong town;
    constraining the search area is the reliable fix.
    """
    if not math.isfinite(radius_km) or radius_km <= 0:
        raise FAAError("Search radius must be a positive finite distance")
    center = Point(lat, lng)
    distance = geodesic(kilometers=radius_km)
    north = distance.destination(center, 0).latitude
    south = distance.destination(center, 180).latitude
    east = distance.destination(center, 90).longitude
    west = distance.destination(center, 270).longitude
    return [(south, west), (north, east)]


def _ambiguous_alternative(result: dict) -> Optional[dict]:
    """
    Return the runner-up when it is close enough to be a real toss-up.

    Nominatim happily returns the same place twice, so identical addresses are
    skipped rather than reported as competing answers.
    """
    top = float((result.get("raw") or {}).get("importance") or 0)
    if not top:
        return None

    for alt in result.get("alternatives") or []:
        if alt["address"] == result["address"]:
            continue
        return alt if alt["importance"] / top >= AMBIGUITY_RATIO else None

    return None


def _warn_if_ambiguous(result: dict, query: str) -> None:
    """Warn on stderr when a query had a near-equal runner-up. Keeps stdout pipeable."""
    alt = _ambiguous_alternative(result)
    if not alt:
        return

    print(f"Warning: '{query}' is ambiguous - two close matches", file=sys.stderr)
    print(f"  using:   {result['address']}", file=sys.stderr)
    print(f"  also:    {alt['address']} ({alt['type']})", file=sys.stderr)
    print("  narrow the query, or use --near to constrain it", file=sys.stderr)


# ============================================================================
# FAA data
# ============================================================================

class FAAClient:
    """Read-only client for the FAA UAS data layers and TFR shapes."""

    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })

    def _get_json(self, url: str, params: dict) -> dict:
        """
        GET JSON, backing off when throttled.

        ArcGIS Online rate limits bursts either with HTTP 429 or with a 200
        whose body is an error mentioning "Too many requests".
        """
        for delay in RETRY_DELAYS + (None,):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                throttled = response.status_code == 429
                if not throttled:
                    response.raise_for_status()
                    data = response.json()
                    error = data.get("error") if isinstance(data, dict) else None
                    throttled = "too many requests" in json.dumps(error or "").lower()
                    if not throttled:
                        return data
            except requests.RequestException as e:
                raise FAAError(f"Request to {url} failed: {e}") from e
            except ValueError as e:
                raise FAAError(f"{url} did not return JSON") from e

            if delay is None:
                raise FAAError(f"{url} is rate limiting requests; try again in a minute")
            time.sleep(delay)

    def query(
        self,
        layer: str,
        lat: float,
        lng: float,
        radius_nm: float = 0.0,
        geometry: bool = False,
    ) -> list:
        """
        Features of one FAA layer at a point, or within radius_nm of it.

        Returns GeoJSON features; geometry is only fetched when asked for.
        Pages through results past the service's record limit.
        """
        spec = LAYERS[layer]
        url = f"{FAA_SERVICES}/{spec['service']}/FeatureServer/0/query"
        params = {
            "where": spec.get("where", "1=1"),
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "outSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": ",".join(spec["fields"]),
            "returnGeometry": "true" if geometry else "false",
            "orderByFields": "OBJECTID",
            "f": "geojson",
        }
        if radius_nm > 0:
            params["distance"] = radius_nm
            params["units"] = "esriSRUnit_NauticalMile"
        if geometry:
            # ~20 m of generalisation keeps big polygons light without
            # distorting the 30 arc-second LAANC grid cells
            params["geometryPrecision"] = 6
            params["maxAllowableOffset"] = 0.0002

        features = []
        while True:
            page = self._get_json(url, {**params, "resultOffset": len(features)})
            if "error" in page:
                error = page["error"]
                message = error.get("message") if isinstance(error, dict) else error
                raise FAAError(f"FAA {spec['service']} query failed: {message}")

            batch = page.get("features") or []
            features.extend(batch)
            if not batch or not (page.get("properties") or {}).get("exceededTransferLimit"):
                break

        for feature in features:
            feature["properties"] = feature.get("properties") or {}
            feature.setdefault("geometry", None)
        return features

    def tfrs(self) -> list:
        """Every published TFR shape nationwide (active and scheduled), as GeoJSON."""
        data = self._get_json(TFR_SHAPES_URL, {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": "TFR:V_TFR_LOC",
            "outputFormat": "application/json",
            "srsname": "EPSG:4326",
        })

        shapes = []
        for feature in data.get("features") or []:
            props = feature.get("properties") or {}
            notam = (props.get("NOTAM_KEY") or "").split("-")[0]
            shapes.append({
                "type": "Feature",
                "geometry": feature.get("geometry"),
                "properties": {
                    "notam": notam or None,
                    "type": props.get("LEGAL"),
                    "title": props.get("TITLE"),
                    "facility": props.get("CNS_LOCATION_ID"),
                    "state": props.get("STATE"),
                    "url": TFR_DETAIL_URL.format(notam.replace("/", "_")) if notam else None,
                },
            })
        return shapes


def gather(faa: FAAClient, lat: float, lng: float, requests_by_layer: dict) -> dict:
    """
    Run several layer lookups concurrently.

    requests_by_layer maps a LAYERS key to query() keyword arguments; the
    special key "tfrs" fetches the nationwide TFR shapes.
    """
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_QUERIES) as pool:
        jobs = {
            name: pool.submit(faa.tfrs) if name == "tfrs"
            else pool.submit(faa.query, name, lat, lng, **(kwargs or {}))
            for name, kwargs in requests_by_layer.items()
        }
        return {name: job.result() for name, job in jobs.items()}


# ============================================================================
# Geometry
# ============================================================================

def _polygons(geometry: Optional[dict]) -> list:
    """Polygon and MultiPolygon coordinates as a list of polygons (lists of rings)."""
    kind = (geometry or {}).get("type")
    if kind == "Polygon":
        return [geometry["coordinates"]]
    if kind == "MultiPolygon":
        return geometry["coordinates"]
    return []


def _ring_contains(ring: list, lat: float, lng: float) -> bool:
    """Ray casting point-in-ring test. Ring positions are [lng, lat]."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def contains(geometry: Optional[dict], lat: float, lng: float) -> bool:
    """True when the point falls inside a Polygon/MultiPolygon, honouring holes."""
    for rings in _polygons(geometry):
        if (rings and _ring_contains(rings[0], lat, lng)
                and not any(_ring_contains(hole, lat, lng) for hole in rings[1:])):
            return True
    return False


def _segment_distance(ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from the origin to segment a-b, in the segment's own units."""
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    t = 0.0 if span == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / span))
    return math.hypot(ax + t * dx, ay + t * dy)


def distance_nm(geometry: Optional[dict], lat: float, lng: float) -> Optional[float]:
    """
    Nautical miles from the point to a feature: 0 inside a polygon, else to
    its nearest edge. Edges use a local flat projection, which is well within
    a few metres at the tens-of-miles scale this tool works at.
    """
    if not geometry:
        return None

    if geometry.get("type") == "Point":
        plng, plat = geometry["coordinates"][:2]
        return geodesic((lat, lng), (plat, plng)).km / KM_PER_NM

    if contains(geometry, lat, lng):
        return 0.0

    # One arc-minute of latitude is one nautical mile
    scale = math.cos(math.radians(lat))
    best = None
    for rings in _polygons(geometry):
        for ring in rings:
            points = [((p[0] - lng) * 60 * scale, (p[1] - lat) * 60) for p in ring]
            for (ax, ay), (bx, by) in zip(points, points[1:]):
                d = _segment_distance(ax, ay, bx, by)
                best = d if best is None else min(best, d)
    return best


def calculate_bearing(from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> float:
    """Initial bearing from point 1 to point 2 in degrees true."""
    lat1 = math.radians(from_lat)
    lat2 = math.radians(to_lat)
    diff_lng = math.radians(to_lng - from_lng)

    x = math.sin(diff_lng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(diff_lng)

    return round((math.degrees(math.atan2(x, y)) + 360) % 360, 2)


def bearing_to_cardinal(bearing: float) -> str:
    """Convert bearing in degrees to a 16-point compass direction."""
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[round(bearing / 22.5) % 16]


def circle_ring(lat: float, lng: float, radius_nm: float, points: int = 72) -> list:
    """A closed geodesic circle as a GeoJSON ring of [lng, lat] positions."""
    start = Point(lat, lng)
    distance = geodesic(kilometers=radius_nm * KM_PER_NM)
    ring = []
    for i in range(points):
        p = distance.destination(start, i * 360 / points)
        ring.append([round(p.longitude, 6), round(p.latitude, 6)])
    ring.append(ring[0])
    return ring


# ============================================================================
# Interpreting FAA records
# ============================================================================

def format_altitude(value, uom: Optional[str], code: Optional[str]) -> str:
    """Render an FAA floor/ceiling: SFC, 3,600 ft MSL, FL600, or Class A."""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return str(value) if value not in (None, "") else "?"

    if code == "SFC" or (number == 0 and code in (None, "", "AGL")):
        return "SFC"
    if number == -9998:
        return "Class A"
    if uom == "FL":
        return f"FL{number}"
    return f"{number:,} ft {code or ''}".strip()


def ceiling_color(ceiling) -> str:
    """LAANC grid colour for a ceiling in feet."""
    color = CEILING_COLORS[0][1]
    for threshold, candidate in CEILING_COLORS:
        if ceiling is not None and ceiling >= threshold:
            color = candidate
    return color


def class_entry(props: dict) -> dict:
    floor = format_altitude(props.get("LOWER_VAL"), props.get("LOWER_UOM"), props.get("LOWER_CODE"))
    hours = None
    if props.get("WKHR_CODE") and props.get("WKHR_CODE") != "H24":
        hours = props.get("WKHR_RMK") or props.get("WKHR_CODE")
    return {
        "class": CLASS_LABELS.get(props.get("LOCAL_TYPE"), props.get("CLASS") or "?"),
        "type": props.get("LOCAL_TYPE"),
        "name": props.get("NAME"),
        "airport": props.get("ICAO_ID") or props.get("IDENT"),
        "floor": floor,
        "ceiling": format_altitude(props.get("UPPER_VAL"), props.get("UPPER_UOM"), props.get("UPPER_CODE")),
        "surface": floor == "SFC",
        "hours": hours,
    }


def laanc_entry(props: dict) -> dict:
    airports = []
    for n in range(1, 6):
        name = props.get(f"APT{n}_NAME")
        if not name:
            continue
        codes = [c.strip() for c in (props.get(f"APT{n}_Enabled") or "").split(",") if c.strip()]
        airports.append({
            "ident": props.get(f"APT{n}_ICAO") or props.get(f"APT{n}_FAAID"),
            "name": name,
            "laanc": props.get(f"APT{n}_LAANC") == 1,
            "programs": [LAANC_PROGRAMS.get(code, code) for code in codes],
        })
    return {
        "ceiling_ft": props.get("CEILING"),
        "effective": props.get("MAP_EFF"),
        "airports": airports,
        "airspace": [props[f"AIRSPACE_{n}"] for n in range(1, 6) if props.get(f"AIRSPACE_{n}")],
    }


def _reaches_drones(value, code: Optional[str]) -> bool:
    """
    Whether an airspace floor comes down to where drones fly.

    MSL floors can't be compared without ground elevation, so only SFC and
    AGL floors at or below 400 ft count; a MOA from 9,000 ft MSL does not.
    """
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return True  # unknown floor, err on the side of caution
    return code == "SFC" or number <= 0 or (code == "AGL" and number <= 400)


def sua_entry(props: dict) -> dict:
    code = props.get("TYPE_CODE")
    if code == "P":
        severity = "no-fly"
    elif _reaches_drones(props.get("LOWER_VAL"), props.get("LOWER_CODE")):
        severity = "caution"
    else:
        severity = "overhead"
    return {
        "severity": severity,
        "kind": SUA_TYPES.get(code, code or "Special use airspace"),
        "name": props.get("NAME"),
        "floor": format_altitude(props.get("LOWER_VAL"), props.get("LOWER_UOM"), props.get("LOWER_CODE")),
        "ceiling": format_altitude(props.get("UPPER_VAL"), props.get("UPPER_UOM"), props.get("UPPER_CODE")),
        "hours": props.get("TIMESOFUSE"),
    }


def defense_entry(props: dict) -> dict:
    hours = props.get("WKHR_RMK") or props.get("WKHR_CODE")
    # Most of these only switch on when a NOTAM activates them
    by_notam = "NOTAM" in (hours or "").upper()
    return {
        "severity": "caution" if by_notam else "no-fly",
        "kind": "National defense airspace",
        "name": props.get("NAME"),
        "hours": hours,
    }


def nsufr_entry(props: dict) -> dict:
    return {
        "severity": "caution",
        "kind": "National security UAS restriction (part time)",
        "name": props.get("Facility") or props.get("Base"),
        "floor": props.get("Floor"),
        "ceiling": props.get("Ceiling"),
        "hours": "activated by NOTAM" if (props.get("ALERTYPE") or "").lower() == "intermittent"
                 else props.get("ALERTYPE"),
    }


def tfr_entry(props: dict) -> dict:
    return {
        "severity": "no-fly",
        "kind": f"TFR ({(props.get('type') or 'unknown').lower()})",
        "name": props.get("title"),
        "notam": props.get("notam"),
        "url": props.get("url"),
    }


def stadium_entry(feature: dict, lat: float, lng: float) -> dict:
    props = feature["properties"]
    plng, plat = feature["geometry"]["coordinates"][:2]
    bearing = calculate_bearing(lat, lng, plat, plng)
    return {
        "name": props.get("NAME"),
        "city": props.get("CITY"),
        "state": props.get("STATE"),
        "distance_nm": round(distance_nm(feature["geometry"], lat, lng), 2),
        "bearing": bearing,
        "direction": bearing_to_cardinal(bearing),
    }


def recreational_entry(props: dict) -> dict:
    return {
        "name": props.get("SITE_NAME"),
        "ceiling_ft": props.get("CEILING"),
        "city": props.get("CITY"),
        "state": props.get("STATE"),
    }


def airport_entry(feature: dict, lat: float, lng: float) -> dict:
    props = feature["properties"]
    plng, plat = feature["geometry"]["coordinates"][:2]
    bearing = calculate_bearing(lat, lng, plat, plng)
    return {
        "ident": props.get("ICAO_ID") or props.get("IDENT"),
        "name": props.get("NAME"),
        "type": AIRPORT_TYPES.get(props.get("TYPE_CODE"), props.get("TYPE_CODE")),
        "public": props.get("PRIVATEUSE") == 0,
        "elevation_ft": props.get("ELEVATION"),
        "city": props.get("SERVCITY"),
        "distance_nm": round(distance_nm(feature["geometry"], lat, lng), 2),
        "bearing": bearing,
        "direction": bearing_to_cardinal(bearing),
    }


def _dedupe(entries: list, keys: tuple) -> list:
    """Drop repeats of the same named area, keeping the first (closest) one."""
    seen, unique = set(), []
    for entry in entries:
        key = tuple(entry.get(k) for k in keys)
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def authorization_note(laanc: Optional[dict]) -> str:
    """What a pilot has to do to fly in controlled airspace at this point."""
    if not laanc:
        return ("Controlled airspace with no UAS facility map grid here: request "
                "authorization through FAADroneZone.")

    ceiling = laanc["ceiling_ft"]
    if not any(a["laanc"] for a in laanc["airports"]):
        return (f"Facility map ceiling is {ceiling} ft AGL but the airport is not on "
                "LAANC: request authorization through FAADroneZone.")
    if ceiling:
        return (f"LAANC available: near-instant approval up to {ceiling} ft AGL "
                "through an FAA-approved LAANC app (Aloft, Autel, DJI FlightHub, etc.).")
    return ("Ceiling here is 0 ft: no automatic LAANC approval. Part 107 pilots can "
            "request further coordination through LAANC, or apply via FAADroneZone.")


def assess(controlled: list, laanc: Optional[dict], restrictions: list,
           stadiums: list, recreational: list) -> tuple[str, list]:
    """Boil a point's airspace down to a verdict and the notes behind it."""
    notes = []
    if any(r["severity"] == "no-fly" for r in restrictions):
        verdict = "DO NOT FLY"
        notes.append("Inside a prohibited area, national defense airspace or TFR - "
                     "see restrictions below and check the dates and times.")
    elif controlled or laanc:
        verdict = "AUTHORIZATION REQUIRED"
    elif stadiums or any(r["severity"] == "caution" for r in restrictions):
        verdict = "CAUTION"
    else:
        verdict = "CLEAR"

    if controlled or laanc:
        notes.append(authorization_note(laanc))
    else:
        notes.append("Uncontrolled airspace (Class G) at the surface: no authorization "
                     "needed up to 400 ft AGL.")

    if any(r["severity"] == "caution" for r in restrictions):
        notes.append("Special use or security airspace overlaps this point - check "
                     "whether it is active before flying.")
    if stadiums:
        notes.append("Within 3 NM of a stadium: flight is prohibited below 3,000 ft AGL "
                     "from 1 hour before to 1 hour after major sporting events.")
    if recreational:
        notes.append("Inside an FAA-recognized recreational flyer fixed site.")

    return verdict, notes


def check_point(faa: FAAClient, lat: float, lng: float) -> dict:
    """Everything that applies at one point."""
    data = gather(faa, lat, lng, {
        "laanc": {},
        "class": {},
        "sua": {},
        "prohibited": {},
        "defense": {},
        "nsufr": {},
        "recreational": {},
        "stadiums": {"radius_nm": STADIUM_TFR_NM, "geometry": True},
        "tfrs": None,
    })

    airspace = _dedupe([class_entry(f["properties"]) for f in data["class"]],
                       ("type", "name", "floor", "ceiling"))
    controlled = [a for a in airspace if a["surface"]]
    laanc = laanc_entry(data["laanc"][0]["properties"]) if data["laanc"] else None

    restrictions = _dedupe(
        [sua_entry(f["properties"]) for f in data["prohibited"] + data["sua"]]
        + [defense_entry(f["properties"]) for f in data["defense"]]
        + [nsufr_entry(f["properties"]) for f in data["nsufr"]]
        + [tfr_entry(f["properties"]) for f in data["tfrs"]
           if contains(f["geometry"], lat, lng)],
        ("kind", "name"),
    )
    stadiums = sorted((stadium_entry(f, lat, lng) for f in data["stadiums"]),
                      key=lambda s: s["distance_nm"])
    recreational = [recreational_entry(f["properties"]) for f in data["recreational"]]

    verdict, notes = assess(controlled, laanc, restrictions, stadiums, recreational)
    return {
        "verdict": verdict,
        "notes": notes,
        "airspace": airspace,
        "laanc": laanc,
        "restrictions": restrictions,
        "stadiums": stadiums,
        "recreational_sites": recreational,
        "tfrs_checked": len(data["tfrs"]),
        "advisory": ADVISORY,
    }


def survey(faa: FAAClient, lat: float, lng: float, radius_nm: float) -> dict:
    """
    Every feature within radius_nm, with geometry, keyed by layer.

    Stadiums are searched a further 3 NM out since their event TFRs reach in.
    """
    wanted = {name: {"radius_nm": radius_nm, "geometry": True} for name in LAYERS}
    wanted["stadiums"]["radius_nm"] = radius_nm + STADIUM_TFR_NM
    wanted["tfrs"] = None
    data = gather(faa, lat, lng, wanted)

    for features in data.values():
        for feature in features:
            d = distance_nm(feature["geometry"], lat, lng)
            feature["properties"]["_distance_nm"] = None if d is None else round(d, 2)

    data["tfrs"] = [f for f in data["tfrs"]
                    if f["properties"]["_distance_nm"] is not None
                    and f["properties"]["_distance_nm"] <= radius_nm]
    return data


def summarize_survey(data: dict, lat: float, lng: float) -> dict:
    """Readable, geometry-free summary of a survey()."""
    def by_distance(features):
        return sorted(features, key=lambda f: f["properties"].get("_distance_nm") or 0)

    def with_distance(entry, feature):
        entry["distance_nm"] = feature["properties"].get("_distance_nm")
        return entry

    ceilings = Counter(f["properties"].get("CEILING") for f in data["laanc"])
    grid_airports = sorted({f["properties"].get("APT1_ICAO") or f["properties"].get("APT1_FAAID")
                            for f in data["laanc"]} - {None, ""})
    effective = next((f["properties"].get("MAP_EFF") for f in data["laanc"]), None)

    return {
        "airports": [airport_entry(f, lat, lng) for f in by_distance(data["airports"])],
        "airspace": _dedupe(
            [with_distance(class_entry(f["properties"]), f) for f in by_distance(data["class"])],
            ("type", "name", "floor", "ceiling")),
        "laanc_grid": {
            "cells": len(data["laanc"]),
            "ceilings_ft": {str(k): v for k, v in sorted(ceilings.items(), key=lambda kv: kv[0] or 0)},
            "airports": grid_airports,
            "effective": effective,
        },
        "restrictions": _dedupe(
            [with_distance(sua_entry(f["properties"]), f)
             for f in by_distance(data["prohibited"] + data["sua"])]
            + [with_distance(defense_entry(f["properties"]), f) for f in by_distance(data["defense"])]
            + [with_distance(nsufr_entry(f["properties"]), f) for f in by_distance(data["nsufr"])]
            + [with_distance(tfr_entry(f["properties"]), f) for f in by_distance(data["tfrs"])],
            ("kind", "name")),
        "stadiums": [stadium_entry(f, lat, lng) for f in by_distance(data["stadiums"])],
        "recreational_sites": [with_distance(recreational_entry(f["properties"]), f)
                               for f in by_distance(data["recreational"])],
    }


# ============================================================================
# Map
# ============================================================================

def _feature(geometry: Optional[dict], properties: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {k: v for k, v in properties.items() if v not in (None, "")},
    }


def _altitudes(entry: dict) -> Optional[str]:
    if entry.get("floor") and entry.get("ceiling"):
        return f"{entry['floor']} to {entry['ceiling']}"
    return None


def map_features(location: dict, data: dict, radius_nm: float) -> list:
    """Styled GeoJSON features for the map, tagged with their overlay in _layer."""
    lat, lng = location["latitude"], location["longitude"]
    features = []

    for f in data["laanc"]:
        p = f["properties"]
        entry = laanc_entry(p)
        ceiling = entry["ceiling_ft"]
        color = ceiling_color(ceiling)
        airports = ", ".join(f"{a['ident']} {a['name']}" for a in entry["airports"])
        features.append(_feature(f["geometry"], {
            "_layer": "laanc", "_ceiling": ceiling,
            "title": f"LAANC ceiling {ceiling} ft AGL",
            "description": airports,
            "Programs": "; ".join(sorted({p for a in entry["airports"] for p in a["programs"]})),
            "Effective": entry["effective"],
            "stroke": color, "stroke-width": 0.5, "stroke-opacity": 0.6,
            "fill": color, "fill-opacity": 0.3,
        }))

    for f in data["class"]:
        entry = class_entry(f["properties"])
        style = CLASS_STYLES.get(entry["type"], {"stroke": "#555"})
        features.append(_feature(f["geometry"], {
            "_layer": "class",
            "title": f"{entry['class']} - {entry['name']}",
            "Altitudes": _altitudes(entry),
            "Hours": entry["hours"],
            "stroke-width": 2.5, "fill-opacity": 0.04, **style,
        }))

    def restriction(layer, entry, color, fill_opacity=0.15):
        return {
            "_layer": layer,
            "title": f"{entry['kind']} - {entry['name']}",
            "Altitudes": _altitudes(entry),
            "Hours": entry.get("hours"),
            "url": entry.get("url"),
            "stroke": color, "stroke-width": 2, "fill-opacity": fill_opacity,
        }

    for f in data["sua"]:
        features.append(_feature(f["geometry"], restriction("sua", sua_entry(f["properties"]), "#e08214")))
    for f in data["prohibited"]:
        features.append(_feature(f["geometry"], restriction(
            "prohibited", sua_entry(f["properties"]), "#b2182b", 0.3)))
    for f in data["defense"]:
        features.append(_feature(f["geometry"], restriction(
            "defense", defense_entry(f["properties"]), "#b2182b", 0.2)))
    for f in data["nsufr"]:
        features.append(_feature(f["geometry"], restriction("nsufr", nsufr_entry(f["properties"]), "#762a83")))
    # A renewed TFR is a new NOTAM on the identical shape; stacked, only the top
    # one is clickable, so draw each shape once and list every NOTAM on it
    shapes = {}
    for f in data["tfrs"]:
        key = json.dumps(f["geometry"], sort_keys=True)
        shapes.setdefault(key, (f["geometry"], []))[1].append(tfr_entry(f["properties"]))
    for geometry, entries in shapes.values():
        props = restriction("tfrs", entries[0], "#d7191c", 0.25)
        if len(entries) > 1:
            props["title"] = f"{entries[0]['kind']} - {len(entries)} NOTAMs on this area"
            for entry in entries:
                props[f"NOTAM {entry['notam']}"] = entry["name"]
        props.pop("url", None)
        props["_links"] = [[f"NOTAM {e['notam']}", e["url"]] for e in entries if e.get("url")]
        features.append(_feature(geometry, props))

    for f in data["stadiums"]:
        entry = stadium_entry(f, lat, lng)
        plng, plat = f["geometry"]["coordinates"][:2]
        features.append(_feature(
            {"type": "Polygon", "coordinates": [circle_ring(plat, plng, STADIUM_TFR_NM)]},
            {"_layer": "stadiums",
             "title": f"Stadium - {entry['name']}",
             "description": "3 NM / 3,000 ft AGL TFR during major sporting events",
             "City": ", ".join(x for x in (entry["city"], entry["state"]) if x),
             "stroke": "#8c510a", "stroke-width": 1.5, "stroke-dasharray": "4 4",
             "fill-opacity": 0.08}))

    for f in data["recreational"]:
        entry = recreational_entry(f["properties"])
        features.append(_feature(f["geometry"], {
            "_layer": "recreational",
            "title": f"Recreational flyer site - {entry['name']}",
            "Ceiling": f"{entry['ceiling_ft']} ft AGL" if entry["ceiling_ft"] is not None else None,
            "stroke": "#1a9850", "stroke-width": 2, "fill-opacity": 0.2,
        }))

    for f in data["airports"]:
        entry = airport_entry(f, lat, lng)
        features.append(_feature(f["geometry"], {
            "_layer": "airports",
            "title": f"{entry['ident']} - {entry['name']}",
            "description": f"{'Public' if entry['public'] else 'Private'} {entry['type']}",
            "Elevation": f"{entry['elevation_ft']} ft MSL" if entry["elevation_ft"] is not None else None,
            "Distance": f"{entry['distance_nm']} NM {entry['direction']}",
            "marker-color": "#252525" if entry["public"] else "#969696",
            "marker-size": "medium" if entry["public"] else "small",
        }))

    features.append(_feature(
        {"type": "Polygon", "coordinates": [circle_ring(lat, lng, radius_nm)]},
        {"_layer": "radius", "title": f"{radius_nm:g} NM radius",
         "stroke": "#333333", "stroke-width": 2, "stroke-dasharray": "6 6",
         "fill-opacity": 0}))
    features.append(_feature(
        {"type": "Point", "coordinates": [lng, lat]},
        {"_layer": "location", "title": location["query"],
         "description": location.get("address"),
         "Coordinates": f"{lat:.5f}, {lng:.5f}",
         "marker-color": "#e6550d", "marker-size": "large"}))

    return features


def map_page(location: dict, tiles: str, size: Optional[tuple[int, int]] = None) -> str:
    """
    Fill the Leaflet page template.

    With a size, the map is pinned to exactly that many pixels for a
    screenshot. Headless Chromium lays the page out in a viewport smaller than
    --window-size and only grows it at capture time, too late for the tiles
    the resize uncovers; a fixed-size map loads every tile up front.
    """
    screenshot_css = ""
    if size:
        width, height = size
        screenshot_css = (f"  #map {{ width: {width}px; height: {height}px; }}\n"
                          "  html, body { overflow: hidden; }")
    return (MAP_PAGE
            .replace("__SCREENSHOT_CSS__", screenshot_css)
            .replace("__TITLE__", _html_escape(location["query"]))
            .replace("__CENTER__", json.dumps([location["latitude"], location["longitude"]]))
            .replace("__OVERLAYS__", json.dumps(MAP_OVERLAYS))
            .replace("__BASEMAPS__", json.dumps(TILE_LAYERS))
            .replace("__DEFAULT_BASEMAP__", json.dumps(tiles))
            .replace("__CEILING_COLORS__", json.dumps(CEILING_COLORS))
            .replace("__PASSIVE__", json.dumps(PASSIVE_OVERLAYS)))


def _html_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class _MapServer(http.server.ThreadingHTTPServer):
    """
    Refuse ports that are already taken, so the free-port fallback runs.

    HTTPServer turns on SO_REUSEADDR, which on Windows lets a second server bind a
    port another process is serving - both listen and the older one keeps the
    traffic. Windows gets an exclusive bind instead.
    """

    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _map_server(page: str, data: dict, port: int) -> http.server.ThreadingHTTPServer:
    """Bind a localhost server for the Leaflet page and its data."""
    routes = {
        "/": ("text/html; charset=utf-8", page.encode()),
        "/data.json": ("application/json", json.dumps(data).encode()),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path not in routes:
                self.send_error(404)
                return
            content_type, body = routes[path]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep the terminal readable

    try:
        return _MapServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if port == 0:
            raise FAAError(f"Could not start server: {e}")
        print(f"  Port {port} unavailable ({e.strerror}), using a free port instead")
        return _MapServer(("127.0.0.1", 0), Handler)


def serve_map(page: str, data: dict, port: int, open_browser: bool) -> int:
    """Serve the Leaflet page on localhost until interrupted."""
    server = _map_server(page, data, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    feature_count = len(data.get("features") or [])
    print(f"\n  Serving map at {url}")
    print(f"  {feature_count} feature{'s' if feature_count != 1 else ''} loaded")
    print("  Press Ctrl+C to stop\n", flush=True)

    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("  Stopped")
    finally:
        server.server_close()

    return 0


# Chromium-family browsers that can screenshot headless, checked in order
BROWSER_COMMANDS = [
    "chrome", "google-chrome", "google-chrome-stable",
    "chromium", "chromium-browser", "msedge", "microsoft-edge",
]


def _browser_paths() -> list:
    """Usual install locations on Windows and macOS, which are rarely on PATH."""
    paths = []
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if root:
            paths += [
                os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(root, "Chromium", "Application", "chrome.exe"),
            ]
    paths += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    return paths


def find_browser() -> Optional[str]:
    """Locate Chrome, Edge or Chromium. FAA_BROWSER overrides the search."""
    override = os.environ.get("FAA_BROWSER")
    if override:
        return override
    for command in BROWSER_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    return next((path for path in _browser_paths() if os.path.isfile(path)), None)


def screenshot_map(page: str, data: dict, path: str, size: tuple[int, int],
                   wait_ms: int = SCREENSHOT_WAIT_MS) -> str:
    """
    Render the map to a PNG with a headless Chromium browser.

    The page is served on a free localhost port for the duration. Headless
    screenshots fire on a virtual clock, so wait_ms is the page time allowed
    for the basemap tiles and data to land before the capture.
    """
    browser = find_browser()
    if not browser:
        raise FAAError("No Chrome, Edge or Chromium found for --png; "
                            "install one or set FAA_BROWSER to its path")

    output = os.path.abspath(path)
    if os.path.exists(output):
        os.remove(output)  # so a stale file can't pass for a fresh capture

    server = _map_server(page, data, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    width, height = size
    # A throwaway profile keeps headless runs clear of an open browser session
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        command = [
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            f"--virtual-time-budget={wait_ms}",
            f"--screenshot={output}",
            url,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=wait_ms / 1000 + 90)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise FAAError(f"Browser screenshot failed: {e}") from e
        finally:
            server.shutdown()
            server.server_close()

    if not os.path.isfile(output):
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise FAAError("Browser did not write a screenshot"
                            + (": " + " | ".join(detail) if detail else ""))
    return output


def parse_port(value: str) -> int:
    """argparse type for a TCP port, so a bad value is a usage error, not a traceback."""
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 0-65535, got {port}")
    return port


def parse_size(value: str) -> tuple[int, int]:
    """argparse type for WIDTHxHEIGHT image sizes."""
    match = re.match(r"^(\d{2,5})x(\d{2,5})$", value.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT like 1400x1000, got '{value}'")
    return int(match.group(1)), int(match.group(2))


# ============================================================================
# Output
# ============================================================================

def _maps_url(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps?q={lat},{lng}"


def _wrap(text: str, indent: str) -> str:
    return textwrap.fill(text, width=78, initial_indent=indent, subsequent_indent=indent)


def _header(title: str, location: dict) -> None:
    print(f"\n  {title}: {location['query']}")
    print("  " + "=" * 58)
    print(f"  Location:   {location['latitude']:.5f}, {location['longitude']:.5f}")
    if location.get("address"):
        print(_wrap(location["address"], "              ").replace("              ", "  Address:    ", 1))


def _restriction_line(r: dict) -> list:
    tag = {"no-fly": "NO-FLY ", "caution": "CAUTION"}.get(r["severity"], "ABOVE  ")
    lines = [f"    [{tag}] {r['kind']}: {r['name']}"]
    details = [x for x in (_altitudes(r), r.get("hours")) if x]
    if r.get("distance_nm") is not None:
        details.insert(0, "here" if r["distance_nm"] == 0 else f"{r['distance_nm']} NM")
    if details:
        lines.append("              " + " | ".join(details))
    if r.get("url"):
        lines.append(f"              {r['url']}")
    return lines


def print_check(location: dict, report: dict) -> None:
    _header("Airspace check", location)
    print(f"  Verdict:    {report['verdict']}")
    for note in report["notes"]:
        print(_wrap(note, "              "))

    print("\n  Airspace at the surface")
    controlled = [a for a in report["airspace"] if a["surface"]]
    for a in controlled:
        print(f"    {a['class']:<18} {a['name']}  ({a['floor']} to {a['ceiling']})")
        if a["hours"]:
            print(f"    {'':<18} part time: {a['hours']}")
    if not controlled:
        print("    Class G (uncontrolled)")
    for a in report["airspace"]:
        if not a["surface"]:
            print(f"    Overhead:          {a['class']} {a['name']} from {a['floor']}")

    laanc = report["laanc"]
    if laanc:
        print(f"\n  LAANC facility map (effective {laanc['effective']})")
        print(f"    Ceiling:    {laanc['ceiling_ft']} ft AGL")
        for airport in laanc["airports"]:
            print(f"    Airport:    {airport['ident']} {airport['name']}"
                  + ("" if airport["laanc"] else "  (not on LAANC)"))
            if airport["programs"]:
                print(f"                {', '.join(airport['programs'])}")

    print("\n  Restrictions")
    for r in report["restrictions"]:
        for line in _restriction_line(r):
            print(line)
    if not report["restrictions"]:
        print(f"    None - including {report['tfrs_checked']} published TFRs checked")

    if report["stadiums"]:
        print("\n  Stadiums within 3 NM")
        for s in report["stadiums"]:
            print(f"    {s['name']} ({s['city']}, {s['state']})  {s['distance_nm']} NM {s['direction']}")

    if report["recreational_sites"]:
        print("\n  Recreational flyer fixed sites")
        for site in report["recreational_sites"]:
            print(f"    {site['name']}  ceiling {site['ceiling_ft']} ft AGL")

    print(f"\n  Map:  {_maps_url(location['latitude'], location['longitude'])}")
    print(_wrap(ADVISORY, "  "))
    print()


def print_nearby(location: dict, summary: dict, radius_nm: float) -> None:
    _header(f"Airspace within {radius_nm:g} NM", location)

    public = [a for a in summary["airports"] if a["public"]]
    private = len(summary["airports"]) - len(public)
    print(f"\n  Public airports ({len(public)})")
    for a in public:
        print(f"    {a['ident'] or '?':<6} {a['name'][:38]:<38} {a['distance_nm']:>5.1f} NM {a['direction']:<3}  {a['type']}")
    if private:
        print(f"    plus {private} private-use airports and heliports (on the map)")

    print(f"\n  Controlled airspace ({len(summary['airspace'])})")
    for a in summary["airspace"]:
        where = "here" if a["distance_nm"] == 0 else f"{a['distance_nm']:.1f} NM"
        print(f"    {a['class']:<18} {a['name'][:30]:<30} {a['floor']} to {a['ceiling']}  [{where}]")
    if not summary["airspace"]:
        print("    None - Class G throughout")

    grid = summary["laanc_grid"]
    if grid["cells"]:
        ceilings = ", ".join(f"{ft} ft x{count}" for ft, count in grid["ceilings_ft"].items())
        print(f"\n  LAANC grid ({grid['cells']} cells, effective {grid['effective']})")
        print(_wrap(f"Ceilings: {ceilings}", "    "))
        print(_wrap(f"Airports: {', '.join(grid['airports'])}", "    "))

    print(f"\n  Restrictions ({len(summary['restrictions'])})")
    for r in summary["restrictions"]:
        for line in _restriction_line(r):
            print(line)
    if not summary["restrictions"]:
        print("    None")

    if summary["stadiums"]:
        print("\n  Stadiums (3 NM event TFRs)")
        for s in summary["stadiums"]:
            print(f"    {s['name']} ({s['city']}, {s['state']})  {s['distance_nm']} NM {s['direction']}")

    if summary["recreational_sites"]:
        print("\n  Recreational flyer fixed sites")
        for site in summary["recreational_sites"]:
            print(f"    {site['name']}  ceiling {site['ceiling_ft']} ft AGL  {site['distance_nm']} NM")

    print()
    print(_wrap(ADVISORY, "  "))
    print()


# ============================================================================
# Command handlers
# ============================================================================

def _fail(message: str) -> int:
    """Keep diagnostics off stdout, especially for JSON/GeoJSON pipelines."""
    print(f"Error: {message}", file=sys.stderr)
    return 1


def _check_radius(radius_nm: float) -> None:
    if not math.isfinite(radius_nm) or radius_nm <= 0 or radius_nm > 50:
        raise FAAError("--radius must be between 0 and 50 NM")


def _locate(args: argparse.Namespace, lookup_address: bool = True) -> dict:
    return Locator().locate(args.location, near=args.near, within_miles=args.within,
                            lookup_address=lookup_address)


def cmd_airspace(args: argparse.Namespace) -> int:
    try:
        location = _locate(args, lookup_address=not args.no_address)
        report = check_point(FAAClient(), location["latitude"], location["longitude"])
        if args.json:
            print(json.dumps({"location": location, **report}, indent=2))
        else:
            print_check(location, report)
        return 0
    except FAAError as e:
        return _fail(str(e))


def cmd_nearby(args: argparse.Namespace) -> int:
    try:
        _check_radius(args.radius)
        location = _locate(args, lookup_address=not args.no_address)
        lat, lng = location["latitude"], location["longitude"]
        summary = summarize_survey(survey(FAAClient(), lat, lng, args.radius), lat, lng)
        if args.json:
            print(json.dumps({"location": location, "radius_nm": args.radius,
                              **summary, "advisory": ADVISORY}, indent=2))
        else:
            print_nearby(location, summary, args.radius)
        return 0
    except FAAError as e:
        return _fail(str(e))


def cmd_map(args: argparse.Namespace) -> int:
    try:
        _check_radius(args.radius)
        location = _locate(args, lookup_address=not args.no_address)
        lat, lng = location["latitude"], location["longitude"]
        data = survey(FAAClient(), lat, lng, args.radius)
        collection = {"type": "FeatureCollection",
                      "features": map_features(location, data, args.radius)}
        if args.geojson:
            print(json.dumps(collection, indent=2))
            return 0
        if args.png:
            page = map_page(location, args.tiles, size=args.size)
            saved = screenshot_map(page, collection, args.png, args.size)
            print(f"\n  Saved map to {saved}\n")
            return 0
        return serve_map(map_page(location, args.tiles), collection,
                         args.port, not args.no_open)
    except FAAError as e:
        return _fail(str(e))


def _value_option_strings(parser: argparse.ArgumentParser) -> set:
    """Collect every option string that takes a value, subcommands included."""
    names = set()

    for action in parser._actions:
        if action.option_strings and action.nargs != 0:
            names.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for subparser in action.choices.values():
                names |= _value_option_strings(subparser)

    return names


def _normalize_coordinate_args(argv: list, value_options: set) -> list:
    """
    Let southern hemisphere coordinates survive argparse.

    argparse reads any token starting with '-' as an option, so '-33.87,151.21'
    is rejected wherever a location is expected. Attach such a value to the flag
    it belongs to with '=', and push a bare positional behind a '--' guard.
    """
    normalized = []
    positionals = []

    for token in argv:
        if token.startswith("-") and Locator.COORDINATE_PATTERN.match(token):
            previous = normalized[-1] if normalized else ""
            if previous in value_options:
                normalized[-1] = f"{previous}={token}"
            else:
                positionals.append(token)
            continue
        normalized.append(token)

    if positionals:
        normalized.append("--")
        normalized.extend(positionals)

    return normalized


def _add_location_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "location",
        help="Place, address or 'lat,lng' coordinates"
    )
    parser.add_argument(
        "--near", "-n",
        help="Constrain geocoding to the area around this location"
    )
    parser.add_argument(
        "--within", "-w",
        type=float,
        default=25.0,
        help="Radius in miles used by --near (default: 25)"
    )
    parser.add_argument(
        "--no-address",
        action="store_true",
        help="Skip the reverse lookup that names raw coordinates"
    )


def _add_radius_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--radius", "-r",
        type=float,
        default=DEFAULT_RADIUS_NM,
        help=f"Search radius in nautical miles, up to 50 (default: {DEFAULT_RADIUS_NM:g})"
    )


def utf8_output():
    """Windows pipes default to the ANSI code page; read and write UTF-8 so any name prints."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="faa.py - FAA airspace and aviation weather for drone pilots",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=CLI_EPILOG,
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    check_parser = subparsers.add_parser(
        "airspace",
        help="What airspace and restrictions apply at a point",
        description="Airspace class, LAANC ceiling, restrictions and TFRs at one point."
    )
    _add_location_options(check_parser)
    check_parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")

    nearby_parser = subparsers.add_parser(
        "nearby",
        help="Airspace, airports and restrictions within a radius",
        description="Everything within a radius (default 10 NM) of a point."
    )
    _add_location_options(nearby_parser)
    _add_radius_option(nearby_parser)
    nearby_parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")

    map_parser = subparsers.add_parser(
        "map",
        help="Serve an interactive airspace map on localhost",
        description="Serve a Leaflet map of the airspace within a radius of a point."
    )
    _add_location_options(map_parser)
    _add_radius_option(map_parser)
    map_parser.add_argument(
        "--tiles", "-t",
        default="street",
        choices=list(TILE_LAYERS),
        help="Starting basemap; all are switchable on the map (default: street)"
    )
    map_parser.add_argument(
        "--port", "-p",
        type=parse_port,
        default=8765,
        help="Port to serve on (default: 8765)"
    )
    map_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open a browser automatically"
    )
    output = map_parser.add_mutually_exclusive_group()
    output.add_argument(
        "--geojson", "-g",
        action="store_true",
        help="Print the map data as GeoJSON instead of serving it"
    )
    output.add_argument(
        "--png",
        metavar="FILE",
        help="Save a screenshot of the map instead of serving it "
             "(needs Chrome, Edge or Chromium)"
    )
    map_parser.add_argument(
        "--size",
        type=parse_size,
        default=DEFAULT_PNG_SIZE,
        metavar="WxH",
        help="Screenshot size for --png (default: {}x{})".format(*DEFAULT_PNG_SIZE)
    )

    return parser


def main(argv: Optional[list] = None) -> int:
    utf8_output()
    parser = build_parser()
    args = parser.parse_args(_normalize_coordinate_args(
        sys.argv[1:] if argv is None else argv, _value_option_strings(parser)))

    commands = {
        "airspace": cmd_airspace,
        "nearby": cmd_nearby,
        "map": cmd_map,
    }
    handler = commands.get(args.command)
    if handler:
        return handler(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
