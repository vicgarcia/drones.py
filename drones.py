#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "geopy>=2.4.0",
#     "requests>=2.31.0",
# ]
# ///
"""
drones.py - FAA airspace and aviation weather for drone pilots.

Answers "can I fly here?" for a place name or lat/lng: controlled airspace,
LAANC grid ceilings, special use airspace, national security restrictions,
stadiums and TFRs. Can also render the airspace around a point on a local map.

Airspace comes from the FAA's UAS Data Delivery System (the public ArcGIS
layers behind the FAA UAS Facility Map) and tfr.faa.gov. No API key required.
"""

import argparse
import html
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
import urllib.parse
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from geopy.distance import geodesic
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
from geopy.point import Point

# User agent for Nominatim (required) and the FAA services
USER_AGENT = "drones.py-cli/1.0"

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
# NOTAM text for one TFR: effective times and altitudes, which the shape feed lacks
TFR_TEXT_URL = "https://tfr.faa.gov/tfrapi/getWebText"

# NOAA Aviation Weather Center data API (METAR, TAF, station index)
AWC_URL = "https://aviationweather.gov/api/data"
# Widening rings for "nearest station": most US points find a METAR in the
# first, while TAF airports are sparser and can sit 50+ NM away out west
STATION_SEARCH_NM = (15, 30, 60, 120)
DEFAULT_STATION_RADIUS_NM = 25.0
# A TAF officially covers 5 SM around its airport; past this, call it regional
TAF_COVERAGE_NM = 10
DEFAULT_MAX_WIND_KT = 20.0
# Wind at this share of the limit or more is flagged as close
WIND_MARGIN = 0.85

# Live air traffic for the map (--adsb): adsb.fi's open data API, which allows
# one request a second and a radius of up to 250 NM, and asks for attribution
ADSBFI_URL = "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}"
ADSBFI_ATTRIBUTION = 'Traffic &copy; <a href="https://adsb.fi">adsb.fi</a>'
MAX_TRAFFIC_NM = 250
TRAFFIC_MIN_INTERVAL = 1.1
# Positions older than this are dropped rather than drawn where the aircraft was
TRAFFIC_STALE_SECONDS = 60
EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}

# Weather radar for the map (--radar): NOAA's MRMS quality-controlled base
# reflectivity mosaic for the lower 48, a WMS updated every two minutes
RADAR_WMS_URL = "https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows"
RADAR_LAYER = "conus_bref_qcd"
RADAR_ATTRIBUTION = 'Radar &copy; <a href="https://www.nssl.noaa.gov/projects/mrms/">NOAA MRMS</a>'

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
  drones.py airspace "Chicago Midway Airport"
  drones.py airspace 41.79,-87.74
  drones.py airspace "Montrose Beach" --near "Chicago, IL" --within 15
  drones.py airspace "-33.86,151.21" --json

  drones.py nearby "Navy Pier, Chicago"
  drones.py nearby 38.8895,-77.0353 --radius 5

  drones.py map "Chicago Midway Airport"
  drones.py map 41.79,-87.74 --radius 5 --tiles satellite
  drones.py map "Denver, CO" --geojson > denver.geojson
  drones.py map "Navy Pier, Chicago" --radius 2 --png navy-pier.png
  drones.py map 41.79,-87.74 --png midway.png --tiles satellite --size 1920x1080
  drones.py map "Chicago Midway Airport" --adsb --radar

  drones.py metar "Hollywood, FL"
  drones.py metar KFLL,KMIA --max-wind 15
  drones.py taf "Hollywood, FL"
  drones.py taf KMIA --raw
  drones.py stations "Hollywood, FL" --taf-only

  drones.py preflight "Hollywood, FL"
  drones.py preflight "Hollywood Beach, FL" --at "tomorrow 7am" --max-wind 15
  drones.py preflight 41.79,-87.74 --at 2pm --altitude 200 --json

Location:
  Every command takes a place name, address or raw coordinates:
    "41.79,-87.74"         Raw lat,lng (comma or space separated)
    "Chicago Midway"       Place or address (geocoded with OpenStreetMap)
  --near/--within constrain geocoding to an area when a name is ambiguous.

Data:
  FAA UAS Facility Map (LAANC grid), class airspace, special use airspace,
  prohibited areas, national defense airspace, part-time national security
  UAS restrictions, stadiums, airports, and
  TFR shapes from tfr.faa.gov. Distances are nautical miles (NM).

Weather:
  METAR and TAF from NOAA's Aviation Weather Center, decoded to plain
  language. A place uses the nearest station that files the report: any
  METAR station for current conditions, the nearest TAF airport for the
  forecast. Drone checks apply Part 107 minimums (3 SM visibility, 500 ft
  below clouds) and your wind limit (--max-wind, or DRONES_MAX_WIND).
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
    "light": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        "attribution": 'Tiles &copy; <a href="https://www.esri.com/">Esri</a> | &copy; OpenStreetMap contributors',
        "max_zoom": 16,
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

# Aircraft barometric altitude bands, ft MSL, low (red, where drones fly) to high
ALTITUDE_COLORS = [
    (0, "#d7301f"), (1500, "#f46d43"), (3000, "#fdae61"),
    (6000, "#74add1"), (10000, "#4575b4"),
]
# Airborne but not reporting an altitude
NO_ALTITUDE_COLOR = "#9e9e9e"

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
    ("stadiums", "Stadiums"),
    ("laanc", "LAANC grid"),
    ("prohibited", "Prohibited areas"),
    ("tfrs", "TFRs"),
    ("airports", "Airports"),
    ("location", "Location"),
]

# The layer panel lists overlays in draw order, except these, which move up
# to sit just above the overlay named alongside
PANEL_MOVES = [("tfrs", "class"), ("laanc", "stadiums")]

# Overlays drawn for reference only, never intercepting clicks
PASSIVE_OVERLAYS = ["radius"]

# Overlays listed in the layer panel even when none are in range
ALWAYS_LISTED = ["laanc", "tfrs"]

# Overlays always drawn, left out of the layer control
FIXED_OVERLAYS = ["radius", "location"]

MAP_PAGE = """\
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/@luomus/leaflet-smooth-wheel-zoom@1.0.0/SmoothWheelZoom.js"></script>
<style>
  html, body, #map { height: 100%; margin: 0; }
  .leaflet-popup-content { font: 13px/1.45 system-ui, sans-serif; }
  .leaflet-control-layers { text-transform: lowercase; }
  .leaflet-popup-content dt { font-weight: 600; margin-top: 4px; }
  .leaflet-popup-content dd { margin: 0; }
  .ceiling {
    background: none; border: none; box-shadow: none; padding: 0;
    font: 600 11px system-ui, sans-serif; color: #222;
    text-shadow: 0 0 2px #fff, 0 0 2px #fff, 0 0 2px #fff;
  }
  /* aircraft: icon rotated to the track, label to its right */
  .ac svg { display: block; overflow: visible; }
  .ac span {
    position: absolute; left: 27px; top: 50%; transform: translateY(-50%);
    white-space: nowrap; padding: 1px 5px 1px 4px;
    background: rgba(255, 255, 255, .92); color: #111;
    border-left: 3px solid; border-radius: 3px;
    box-shadow: 0 1px 2px rgba(0, 0, 0, .3);
    font: 400 11px/1.25 system-ui, sans-serif; font-variant-numeric: tabular-nums;
  }
  .ac span b { font-weight: 650; }
  .ac.emergency svg { filter: drop-shadow(0 0 3px #e7298a) drop-shadow(0 0 3px #e7298a); }
__SCREENSHOT_CSS__
</style>
</head>
<body>
<div id="map"></div>
<script>
const CENTER = __CENTER__;
const OVERLAYS = __OVERLAYS__;
const PANEL_MOVES = __PANEL_MOVES__;
const ALWAYS_LISTED = __ALWAYS_LISTED__;
const BASEMAPS = __BASEMAPS__;
const DEFAULT_BASEMAP = __DEFAULT_BASEMAP__;
const PASSIVE = __PASSIVE__;
const FIXED = __FIXED__;
// Live layers turned on with --adsb and --radar, null when off
const TRAFFIC = __TRAFFIC__;
const RADAR = __RADAR__;
// LAANC ceiling labels only once the grid cells are big enough to read
const LABEL_ZOOM = 14;
const TRAFFIC_REFRESH_MS = 3 * 1000;
const RADAR_REFRESH_MS = 2 * 60 * 1000;
const METERS_PER_NM = 1852;

// Fractional zoom lets fitBounds fill the view with the radius ring instead
// of snapping out to the next whole level, which can leave half the frame empty
// The wheel eases toward its target zoom every frame rather than in steps
const map = L.map('map', {
  preferCanvas: true, zoomSnap: 0.1,
  scrollWheelZoom: false, smoothWheelZoom: true, smoothSensitivity: 1
});
// Credit the tile providers only, without Leaflet's own link and flag
map.attributionControl.setPrefix(false);
const bases = {};
for (const [name, tiles] of Object.entries(BASEMAPS)) {
  // Upscale past a provider's last zoom level rather than going blank
  bases[name] = L.tileLayer(tiles.url, {
    maxNativeZoom: tiles.max_zoom, maxZoom: 19, attribution: tiles.attribution,
    updateWhenZooming: false
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
    return pointStyle(p);
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

// A filled dot with a white ring, unless the feature styles its own outline
function pointStyle(p) {
  return {
    color: p['stroke'] || '#fff',
    weight: p['stroke-width'] !== undefined ? p['stroke-width'] : 2,
    dashArray: p['stroke-dasharray'] || null,
    fillColor: p['marker-color'],
    fillOpacity: p['fill-opacity'] !== undefined ? p['fill-opacity'] : 1
  };
}

function markerFor(feature, latlng) {
  const p = feature.properties || {};
  // simplestyle's sizes, plus a tiny one for the search center
  const sizes = { tiny: 3, small: 5, medium: 7, large: 10 };
  if (!p['marker-color']) return L.marker(latlng);
  // A number is a radius in pixels, for sizes between the named ones
  const size = p['marker-size'];
  const radius = typeof size === 'number' ? size : sizes[size] || 7;
  return L.circleMarker(latlng, { radius, ...pointStyle(p) });
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
  if (p._ceiling !== undefined) ceilingCells.push(layer);
}

// Every label Leaflet holds is repositioned on each zoom step, so only the
// cells in view carry one, and none below LABEL_ZOOM
const ceilingCells = [];
function toggleCeilings() {
  const view = map.getZoom() >= LABEL_ZOOM && map.getBounds().pad(0.2);
  for (const cell of ceilingCells) {
    const show = view && view.intersects(cell.getBounds());
    if (show && !cell.getTooltip()) {
      cell.bindTooltip(String(cell.feature.properties._ceiling), {
        permanent: true, direction: 'center', className: 'ceiling'
      });
    } else if (!show && cell.getTooltip()) {
      cell.unbindTooltip();
    }
  }
}
map.on('moveend', toggleCeilings);

// Weather radar: NOAA's WMS, between the basemap and the airspace. Basemaps sit
// at z-index 1; without the zIndex, switching basemap would cover the radar.
if (RADAR) {
  const radar = L.tileLayer.wms(RADAR.url, {
    layers: RADAR.layer, format: 'image/png', transparent: true, opacity: 0.6,
    zIndex: 10, attribution: RADAR.attribution
  }).addTo(map);
  control.addOverlay(radar, 'Weather radar');
  let latest = null;
  // Pin the newest frame, so a long-open map moves forward with the radar
  // rather than showing whatever frame the browser cached first
  const refreshRadar = () => fetch('radar.json')
    .then(r => r.ok ? r.json() : null)
    .then(info => {
      if (info && info.latest && info.latest !== latest) {
        latest = info.latest;
        radar.setParams({ time: latest });
      }
    })
    .catch(() => {});
  refreshRadar();
  setInterval(refreshRadar, RADAR_REFRESH_MS);
}

// Air traffic: one marker per aircraft, moved in place on every poll
const PLANE = 'M12 2c.8 0 1.3.9 1.3 2v5l7.7 4.5v2l-7.7-2.4v4.6l2.2 1.7v1.6L12 20l-3.5 1v-1.6l2.2-1.7v-4.6L3 15.5v-2L10.7 9V4c0-1.1.5-2 1.3-2z';

function aircraftIcon(p) {
  const shape = p.rotorcraft
    ? '<circle cx="12" cy="12" r="4.5"/><path d="M3 3l18 18M21 3L3 21" stroke-width="2.2" stroke="' + p.color + '"/>'
    : '<path d="' + PLANE + '"/>';
  const rotate = p.track !== undefined ? ' style="transform:rotate(' + p.track + 'deg)"' : '';
  const alt = p.alt_ft !== undefined ? p.alt_ft.toLocaleString() : '';
  return L.divIcon({
    className: 'ac' + (p.emergency ? ' emergency' : ''), iconSize: [24, 24],
    html: '<svg viewBox="0 0 24 24" width="24" height="24"' + rotate + '>' +
      '<g fill="' + p.color + '" stroke="#fff" stroke-width="1">' + shape + '</g></svg>' +
      '<span style="border-left-color:' + p.color + '"><b>' + esc(p.label) + '</b>' +
      (alt ? '<br>' + esc(alt) + ' ft' : '') + '</span>'
  });
}

// Radius (NM) of a circle around the view's center that covers the whole view
function viewRadiusNM() {
  const center = map.getCenter();
  const bounds = map.getBounds();
  return Math.max(map.distance(center, bounds.getNorthEast()),
                  map.distance(center, bounds.getSouthWest())) / METERS_PER_NM;
}

if (TRAFFIC) {
  // Above the airspace and its markers, and display only: clicks pass through
  map.createPane('traffic').style.zIndex = 650;
  map.getPane('traffic').style.pointerEvents = 'none';
  const traffic = L.layerGroup([], { attribution: TRAFFIC.attribution }).addTo(map);
  control.addOverlay(traffic, 'Air traffic');
  const markers = new Map();
  let polling = false;

  const clear = () => { traffic.clearLayers(); markers.clear(); };
  map.on('overlayremove', e => { if (e.layer === traffic) clear(); });

  async function poll() {
    if (polling || !map.hasLayer(traffic) || document.hidden) return;
    const radius = viewRadiusNM();
    if (radius > TRAFFIC.max_dist_nm) { clear(); return; }
    const center = map.getCenter().wrap();
    polling = true;
    const data = await fetch('traffic.json?lat=' + center.lat.toFixed(4) +
      '&lng=' + center.lng.toFixed(4) + '&dist=' + Math.max(1, radius).toFixed(1))
      .then(r => r.ok ? r.json() : null)
      .catch(() => null);
    polling = false;
    if (!data || !map.hasLayer(traffic)) return;

    const current = new Set();
    for (const f of data.features) {
      const p = f.properties;
      const latlng = [f.geometry.coordinates[1], f.geometry.coordinates[0]];
      current.add(f.id);
      let marker = markers.get(f.id);
      if (marker) {
        marker.setLatLng(latlng).setIcon(aircraftIcon(p));
      } else {
        marker = L.marker(latlng, {
          icon: aircraftIcon(p), pane: 'traffic', interactive: false, keyboard: false
        });
        markers.set(f.id, marker);
        traffic.addLayer(marker);
      }
    }
    for (const [id, marker] of markers) {
      if (!current.has(id)) {
        traffic.removeLayer(marker);
        markers.delete(id);
      }
    }
  }
  // The view is only set once data.json lands
  map.whenReady(() => {
    map.on('moveend overlayadd', poll);
    setInterval(poll, TRAFFIC_REFRESH_MS);
    document.addEventListener('visibilitychange', poll);
    poll();
  });
}

fetch('data.json')
  .then(r => r.json())
  .then(data => {
    for (const [key, label] of OVERLAYS) {
      const features = data.features.filter(f => (f.properties || {})._layer === key);
      if (!features.length && !ALWAYS_LISTED.includes(key)) continue;
      const passive = PASSIVE.includes(key);
      layers[key] = L.geoJSON({ type: 'FeatureCollection', features }, {
        style: styleFor, pointToLayer: markerFor,
        onEachFeature: passive ? null : popupFor, interactive: !passive
      }).addTo(map);
    }
    const panel = OVERLAYS.filter(([key]) => layers[key] && !FIXED.includes(key));
    for (const [key, before] of PANEL_MOVES) {
      const from = panel.findIndex(([k]) => k === key);
      if (from === -1) continue;
      const [entry] = panel.splice(from, 1);
      const to = panel.findIndex(([k]) => k === before);
      panel.splice(to === -1 ? from : to, 0, entry);
    }
    for (const [key, label] of panel) control.addOverlay(layers[key], esc(label));
    const bounds = layers.radius && layers.radius.getBounds();
    if (bounds && bounds.isValid()) {
      map.fitBounds(bounds, { padding: [20, 20] });
    } else {
      map.setView(CENTER, 12);
    }
    toggleCeilings();
  })
  .catch(err => console.error('drones.py: failed to load data.json', err));
</script>
</body>
</html>
"""


class DronesError(Exception):
    """Base exception for drones.py errors."""
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
            raise DronesError(f"Invalid coordinates: {', '.join(errors)}")
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
                except DronesError:
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
                raise DronesError(f"No results for '{location}' within "
                                    f"{within_miles} miles of {near}")
            raise DronesError(f"Could not geocode: {location}")

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
            raise DronesError(f"Geocoding failed: {e}")

    def reverse(self, lat: float, lng: float) -> Optional[str]:
        """Reverse geocode coordinates to an address string."""
        try:
            location = self._lookup("reverse", f"{lat}, {lng}")
            return location.address if location else None
        except Exception as e:
            raise DronesError(f"Reverse geocoding failed: {e}")


def viewbox_around(lat: float, lng: float, radius_km: float) -> list:
    """
    Build a Nominatim viewbox around a point.

    Free-form search will happily match a same-named street in the wrong town;
    constraining the search area is the reliable fix.
    """
    if not math.isfinite(radius_km) or radius_km <= 0:
        raise DronesError("Search radius must be a positive finite distance")
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
                raise DronesError(f"Request to {url} failed: {e}") from e
            except ValueError as e:
                raise DronesError(f"{url} did not return JSON") from e

            if delay is None:
                raise DronesError(f"{url} is rate limiting requests; try again in a minute")
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
                raise DronesError(f"FAA {spec['service']} query failed: {message}")

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

    def tfr_details(self, notam: str) -> dict:
        """Effective window and altitudes for one TFR, from its NOTAM text."""
        data = self._get_json(TFR_TEXT_URL, {"notamId": notam})
        record = data[0] if isinstance(data, list) and data else data
        if not isinstance(record, dict) or not record.get("text"):
            raise DronesError(f"No NOTAM text for TFR {notam}")
        return parse_tfr_text(record["text"])


def parse_tfr_text(text: str) -> dict:
    """
    Effective window and altitudes from a TFR's NOTAM page.

    The page is HTML; its top-level 'Beginning/Ending Date and Time' span every
    area ('Effective Immediately' and 'Permanent' mean open-ended), and each
    area gives an 'Altitude:' line.
    """
    lines = [l.strip() for l in html.unescape(re.sub(r"<[^>]+>", "\n", text or "")).splitlines()]
    lines = [l for l in lines if l]

    def value_after(label):
        for i, line in enumerate(lines[:-1]):
            if line.startswith(label):
                return lines[i + 1]
        return None

    def moment(value):
        match = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4}) at (\d{2})(\d{2}) UTC", value or "")
        if not match:
            return None
        day = datetime.strptime(match.group(1), "%B %d, %Y")
        return day.replace(hour=int(match.group(2)), minute=int(match.group(3)), tzinfo=timezone.utc)

    begins_text, ends_text = value_after("Beginning Date and Time"), value_after("Ending Date and Time")
    floors, ceilings = set(), []
    for i, line in enumerate(lines[:-1]):
        if line != "Altitude:":
            continue
        span = re.sub(r"\s+", " ", lines[i + 1])
        found = re.search(r"From (the surface|[\d,]+ feet (?:MSL|AGL))\s+up to and including ([\d,]+) feet (MSL|AGL)",
                          span)
        if found:
            floors.add("SFC" if found.group(1) == "the surface"
                       else f"{int(found.group(1).split()[0].replace(',', '')):,} ft {found.group(1).split()[-1]}")
            ceilings.append((int(found.group(2).replace(",", "")), found.group(3)))
    ceiling = max(ceilings) if ceilings else None
    return {
        "begins": moment(begins_text),
        "ends": moment(ends_text),
        "immediate": "immediately" in (begins_text or "").lower(),
        "permanent": "permanent" in (ends_text or "").lower(),
        "floor": "SFC" if "SFC" in floors else (sorted(floors)[0] if floors else None),
        "ceiling": f"{ceiling[0]:,} ft {ceiling[1]}" if ceiling else None,
    }


def tfr_status(details: Optional[dict], when: datetime) -> str:
    """active, scheduled or expired at `when`; unknown when the NOTAM text was unavailable."""
    if not details or (details["begins"] is None and not details["immediate"]):
        return "unknown"
    if details["begins"] and when < details["begins"]:
        return "scheduled"
    if details["ends"] and when >= details["ends"]:
        return "expired"
    return "active"


def annotate_tfrs(faa: "FAAClient", features: list, when: Optional[datetime] = None) -> list:
    """
    Add status, window and altitudes to TFR shapes, dropping expired ones.

    Only the TFRs that matter here are looked up, a few at a time. A lookup
    that fails leaves the TFR as 'unknown', which is treated as active.
    """
    when = when or datetime.now(timezone.utc)
    notams = sorted({f["properties"].get("notam") for f in features} - {None})

    def lookup(notam):
        try:
            return notam, faa.tfr_details(notam)
        except DronesError:
            return notam, None

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_QUERIES) as pool:
        details = dict(pool.map(lookup, notams))

    kept = []
    for feature in features:
        props = feature["properties"]
        found = details.get(props.get("notam"))
        props["status"] = tfr_status(found, when)
        if props["status"] == "expired":
            continue
        if found:
            props.update(begins=found["begins"].isoformat() if found["begins"] else None,
                         ends=found["ends"].isoformat() if found["ends"] else None,
                         floor=found["floor"], ceiling=found["ceiling"])
        kept.append(feature)
    return kept


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


def _local_moment(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    here = datetime.fromisoformat(iso).astimezone()
    name = here.tzname() or ""
    if " " in name:
        name = "".join(word[0] for word in name.split())
    return f"{here.strftime('%a %b %d %I:%M %p').replace(' 0', ' ')} {name}".strip()


def tfr_window(props: dict) -> str:
    status = props.get("status")
    begins, ends = _local_moment(props.get("begins")), _local_moment(props.get("ends"))
    if status == "scheduled":
        return f"scheduled {begins}" + (f" to {ends}" if ends else ", then permanent")
    if status == "active":
        return f"active until {ends}" if ends else "active, permanent"
    return "times unavailable - treat as active"


def tfr_entry(props: dict) -> dict:
    status = props.get("status") or "unknown"
    return {
        "severity": "caution" if status == "scheduled" else "no-fly",
        "kind": f"TFR ({(props.get('type') or 'unknown').lower()})",
        "name": props.get("title"),
        "notam": props.get("notam"),
        "status": status,
        "begins": props.get("begins"),
        "ends": props.get("ends"),
        "floor": props.get("floor"),
        "ceiling": props.get("ceiling"),
        "hours": tfr_window(props),
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
           stadiums: list) -> tuple[str, list]:
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

    if any(r["severity"] == "caution" and not r.get("kind", "").startswith("TFR") for r in restrictions):
        notes.append("Special use or security airspace overlaps this point - check "
                     "whether it is active before flying.")
    if any(r["severity"] == "caution" and r.get("kind", "").startswith("TFR") for r in restrictions):
        notes.append("A TFR here is scheduled but not active at the time checked - "
                     "see its dates below.")
    if stadiums:
        notes.append("Within 3 NM of a stadium: flight is prohibited below 3,000 ft AGL "
                     "from 1 hour before to 1 hour after major sporting events.")

    return verdict, notes


def check_point(faa: FAAClient, lat: float, lng: float, when: Optional[datetime] = None) -> dict:
    """Everything that applies at one point, with TFRs judged at `when` (default now)."""
    data = gather(faa, lat, lng, {
        "laanc": {},
        "class": {},
        "sua": {},
        "prohibited": {},
        "defense": {},
        "nsufr": {},
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
        + [tfr_entry(f["properties"]) for f in annotate_tfrs(
            faa, [f for f in data["tfrs"] if contains(f["geometry"], lat, lng)], when)],
        ("kind", "name"),
    )
    stadiums = sorted((stadium_entry(f, lat, lng) for f in data["stadiums"]),
                      key=lambda s: s["distance_nm"])

    verdict, notes = assess(controlled, laanc, restrictions, stadiums)
    return {
        "verdict": verdict,
        "notes": notes,
        "airspace": airspace,
        "laanc": laanc,
        "restrictions": restrictions,
        "stadiums": stadiums,
        "tfrs_checked": len(data["tfrs"]),
        "advisory": ADVISORY,
    }


def survey(faa: FAAClient, lat: float, lng: float, radius_nm: float,
           when: Optional[datetime] = None) -> dict:
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

    data["tfrs"] = annotate_tfrs(faa, [f for f in data["tfrs"]
                                       if f["properties"]["_distance_nm"] is not None
                                       and f["properties"]["_distance_nm"] <= radius_nm], when)
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
    }


# ============================================================================
# Aviation weather (NOAA Aviation Weather Center)
# ============================================================================

class WeatherClient:
    """
    Read-only client for aviationweather.gov's data API.

    METAR observations and TAF forecasts, plus the station index that says
    which stations issue which. One narrow request per lookup keeps well under
    the API's 100 requests a minute.
    """

    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def _get(self, endpoint: str, **params) -> list:
        params["format"] = "json"
        for delay in RETRY_DELAYS + (None,):
            try:
                response = self.session.get(f"{AWC_URL}/{endpoint}", params=params,
                                            timeout=self.timeout)
            except requests.RequestException as e:
                raise DronesError(f"Aviation weather request failed: {e}") from e
            if response.status_code == 204:
                return []  # AWC's "nothing matched"
            if response.status_code != 429:
                break
            if delay is None:
                raise DronesError("aviationweather.gov is rate limiting requests; try again in a minute")
            time.sleep(delay)
        try:
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            raise DronesError(f"Aviation weather request failed: {e}") from e
        except ValueError as e:
            raise DronesError("aviationweather.gov did not return JSON") from e
        return data if isinstance(data, list) else []

    def stations(self, lat: float, lng: float, radius_nm: float) -> list:
        """METAR-reporting stations within radius_nm, nearest first, each with has_taf."""
        dlat = radius_nm / 60
        dlng = radius_nm / (60 * max(math.cos(math.radians(lat)), 0.01))
        bbox = f"{lat - dlat:.4f},{lng - dlng:.4f},{lat + dlat:.4f},{lng + dlng:.4f}"
        found = []
        for s in self._get("stationinfo", bbox=bbox):
            kinds = s.get("siteType") or []
            if "METAR" not in kinds or s.get("lat") is None or s.get("lon") is None:
                continue  # buoys and other sensors that file neither report
            distance = geodesic((lat, lng), (s["lat"], s["lon"])).km / KM_PER_NM
            if distance > radius_nm:
                continue  # the bbox corners reach past the circle
            bearing = calculate_bearing(lat, lng, s["lat"], s["lon"])
            found.append({
                "id": s.get("icaoId") or s.get("id"),
                "name": s.get("site"),
                "state": s.get("state"),
                "latitude": s["lat"],
                "longitude": s["lon"],
                "elevation_m": s.get("elev"),
                "has_taf": "TAF" in kinds,
                "distance_nm": round(distance, 1),
                "bearing": bearing,
                "direction": bearing_to_cardinal(bearing),
            })
        return sorted(found, key=lambda s: s["distance_nm"])

    def candidates(self, lat: float, lng: float, need_taf: bool, limit: int = 5) -> list:
        """The closest METAR (or TAF) stations, widening the search until some turn up."""
        for radius in STATION_SEARCH_NM:
            found = [s for s in self.stations(lat, lng, radius) if s["has_taf"] or not need_taf]
            if found:
                return found[:limit]
        return []

    def metars(self, ids: list) -> list:
        return self._get("metar", ids=",".join(ids))

    def tafs(self, ids: list) -> list:
        return self._get("taf", ids=",".join(ids))


CLOUD_COVER = {
    "SKC": "clear", "CLR": "clear", "NCD": "no clouds detected", "NSC": "no significant clouds",
    "CAVOK": "ceiling and visibility OK", "FEW": "few", "SCT": "scattered",
    "BKN": "broken", "OVC": "overcast", "OVX": "sky obscured",
}
CLOUD_TYPES = {"CB": "cumulonimbus", "TCU": "towering cumulus"}
CEILING_COVERS = {"BKN", "OVC", "OVX"}

# METAR/TAF present-weather codes, in the order a group is written
WX_INTENSITY = {"-": "light", "+": "heavy"}
WX_DESCRIPTORS = {
    "MI": "shallow", "PR": "partial", "BC": "patches of", "DR": "low drifting",
    "BL": "blowing", "SH": "showers of", "TS": "thunderstorm with", "FZ": "freezing",
}
WX_PHENOMENA = {
    "DZ": "drizzle", "RA": "rain", "SN": "snow", "SG": "snow grains", "IC": "ice crystals",
    "PL": "ice pellets", "GR": "hail", "GS": "small hail", "UP": "unknown precipitation",
    "BR": "mist", "FG": "fog", "FU": "smoke", "VA": "volcanic ash", "DU": "dust",
    "SA": "sand", "HZ": "haze", "PY": "spray", "PO": "dust whirls", "SQ": "squalls",
    "FC": "funnel cloud", "SS": "sandstorm", "DS": "duststorm",
}
# Descriptors that also stand alone, as in VCSH or VCTS
WX_ALONE = {"SH": "showers", "TS": "thunderstorm"}
WX_GROUP = re.compile(r"^(?P<intensity>[-+]?)(?P<vicinity>VC)?(?P<body>[A-Z]+)$")

FLIGHT_CATEGORIES = {
    "VFR": "visual flight rules",
    "MVFR": "marginal VFR",
    "IFR": "instrument flight rules",
    "LIFR": "low IFR",
}
TAF_CHANGES = {"FM": "from", "TEMPO": "temporarily", "BECMG": "becoming", "PROB": "chance"}


def _number(value) -> Optional[float]:
    """AWC visibility arrives as 10, 0.5, '1/2' or '10+'; ceilings and winds as numbers."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().rstrip("+")
    try:
        if " " in text:
            whole, fraction = text.split(" ", 1)
            return float(whole) + _number(fraction)
        if "/" in text:
            top, bottom = text.split("/", 1)
            return float(top) / float(bottom)
        return float(text)
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def knots_to_mph(knots: float) -> int:
    return round(knots * 1.15078)


def describe_wind(wdir, wspd, wgst) -> str:
    """'NE 40° at 13 kt (15 mph), gusting 22 kt (25 mph)', 'variable at 3 kt' or 'calm'."""
    speed = _number(wspd)
    if speed is None:
        return "not reported"
    if speed == 0:
        return "calm"
    if wdir in (None, "", "VRB"):
        where = "variable"
    else:
        degrees = int(_number(wdir) or 0)
        where = f"{bearing_to_cardinal(degrees)} {degrees}°"
    text = f"{where} at {int(speed)} kt ({knots_to_mph(speed)} mph)"
    gust = _number(wgst)
    if gust:
        text += f", gusting {int(gust)} kt ({knots_to_mph(gust)} mph)"
    return text


def describe_visibility(visib) -> str:
    miles = _number(visib)
    if miles is None:
        return "not reported"
    plus = str(visib).strip().endswith("+")
    shown = f"{miles:g}" if miles >= 1 or not miles else str(visib).strip()
    unit = "statute mile" if miles == 1 and not plus else "statute miles"
    return f"{shown}{'+' if plus else ''} {unit}"


def describe_weather(wx: Optional[str]) -> list:
    """Decode a present-weather string like '-SHRA BR VCTS' into phrases."""
    phrases = []
    for group in (wx or "").split():
        match = WX_GROUP.match(group)
        if not match:
            phrases.append(group)
            continue
        body, words = match["body"], []
        while body:
            code = body[:2]
            if code in WX_DESCRIPTORS and len(body) > 2:
                words.append(WX_DESCRIPTORS[code])
            elif code in WX_PHENOMENA:
                words.append(WX_PHENOMENA[code])
            elif code in WX_ALONE:
                words.append(WX_ALONE[code])
            else:
                words.append(body)
                break
            body = body[2:]
        if words and words[-1].endswith(" of"):
            words[-1] = words[-1][:-3]  # 'showers of' with nothing after reads as 'showers'
        if words and words[-1].endswith(" with"):
            words[-1] = words[-1][:-5]
        phrase = " ".join(words).replace("showers of rain", "rain showers").replace(
            "showers of snow", "snow showers")
        if match["intensity"]:
            phrase = f"{WX_INTENSITY[match['intensity']]} {phrase}"
        if match["vicinity"]:
            phrase += " in the vicinity"
        phrases.append(phrase)
    return phrases


def cloud_types_from_raw(raw: str) -> dict:
    """
    Cumulonimbus/towering cumulus tags by cloud base, from the raw report.

    AWC's METAR JSON drops the CB/TCU suffix (SCT026CB arrives as plain SCT
    2600), and it is the one cloud detail that matters most to a drone pilot.
    """
    report = (raw or "").split(" RMK ")[0]
    return {int(base) * 100: kind for _, base, kind in
            re.findall(r"\b(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)\b", report)}


def cloud_layers(clouds: list, raw: Optional[str] = None, vertical_visibility=None) -> list:
    layers = []
    types = cloud_types_from_raw(raw) if raw else {}
    for c in clouds or []:
        cover = c.get("cover")
        base = _number(c.get("base"))
        kind = c.get("type") or (types.get(int(base)) if base is not None else None)
        layers.append({"cover": cover, "base_ft": None if base is None else int(base), "type": kind})
    vv = _number(vertical_visibility)
    if vv is not None and not any(layer["cover"] == "OVX" for layer in layers):
        layers.append({"cover": "OVX", "base_ft": int(vv), "type": None})
    return layers


def describe_clouds(layers: list) -> str:
    if not layers:
        return "not reported"
    parts = []
    for layer in layers:
        text = CLOUD_COVER.get(layer["cover"], layer["cover"] or "?")
        if layer["base_ft"] is not None and layer["cover"] not in ("SKC", "CLR", "NCD", "NSC"):
            text += f" {layer['base_ft']:,} ft"
        if layer["type"]:
            text += f" ({CLOUD_TYPES.get(layer['type'], layer['type'])})"
        parts.append(text)
    return ", ".join(parts)


def ceiling_ft(layers: list) -> Optional[int]:
    """Lowest broken, overcast or obscured layer; None means no ceiling."""
    bases = [l["base_ft"] for l in layers if l["cover"] in CEILING_COVERS and l["base_ft"] is not None]
    return min(bases) if bases else None


def flight_category(ceiling: Optional[int], visibility: Optional[float]) -> Optional[str]:
    """The standard cutoffs; the worse of ceiling and visibility decides."""
    if visibility is None and ceiling is None:
        return None
    ranks = []
    if ceiling is not None:
        ranks.append(0 if ceiling < 500 else 1 if ceiling < 1000 else 2 if ceiling <= 3000 else 3)
    if visibility is not None:
        ranks.append(0 if visibility < 1 else 1 if visibility < 3 else 2 if visibility <= 5 else 3)
    return ["LIFR", "IFR", "MVFR", "VFR"][min(ranks)]


def remark_hazards(raw: Optional[str]) -> list:
    """Hazards a drone pilot should hear about that live in a METAR's remarks."""
    remarks = (raw or "").split(" RMK ", 1)[1] if " RMK " in (raw or "") else ""
    hazards = []
    cb = re.search(r"\b(CB|TCU)\b(?:\s+(ALQDS|DSNT|VC|OHD|[NSEW]{1,3}(?:-[NSEW]{1,3})*))?"
                   r"(?:\s+MOV\s+([NSEW]{1,3}))?", remarks)
    if cb:
        kind, where, moving = cb.groups()
        text = CLOUD_TYPES[kind]
        text += {"ALQDS": " in all quadrants", "DSNT": " in the distance", "VC": " in the vicinity",
                 "OHD": " overhead", None: " reported"}.get(where, f" to the {where}")
        if moving:
            text += f", moving {moving}"
        hazards.append(text)
    if re.search(r"\bLTG", remarks):
        hazards.append("lightning observed")
    if re.search(r"\bVIRGA\b", remarks):
        hazards.append("virga (rain not reaching the ground)")
    if re.search(r"\bTS\b|TSB\d|TSE\d", remarks) and not any("thunder" in h for h in hazards):
        hazards.append("thunderstorm noted in remarks")
    return hazards


def local_time(epoch: Optional[float], tz=None) -> Optional[str]:
    """'7:53 PM EDT (23:53Z)' in the viewer's time zone, or tz when given."""
    if epoch is None:
        return None
    utc = datetime.fromtimestamp(epoch, tz=timezone.utc)
    here = utc.astimezone(tz)
    name = here.tzname() or ""
    if " " in name:  # Windows says 'Eastern Daylight Time'
        name = "".join(word[0] for word in name.split())
    clock = here.strftime("%I:%M %p").lstrip("0")
    return f"{clock} {name} ({utc:%H:%M}Z)".replace("  ", " ")


def local_span(start: Optional[float], end: Optional[float], tz=None) -> str:
    def short(epoch):
        here = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(tz)
        return here.strftime("%a %I %p").replace(" 0", " ")
    if start is None:
        return "?"
    return f"{short(start)} - {short(end)}" if end else short(start)


def drone_checks(wind_kt, gust_kt, visibility, layers, weather: list, hazards: list,
                 max_wind: float, altitude_ft: int) -> list:
    """
    Part 107 weather minimums and the pilot's wind limit, as (status, text) rows.

    Visibility must be 3 statute miles and the aircraft 500 ft below any cloud,
    so a flight to altitude_ft needs cloud bases at altitude_ft + 500 or above.
    """
    checks = []
    peak = max(_number(wind_kt) or 0, _number(gust_kt) or 0)
    kind = "gusts" if (_number(gust_kt) or 0) > (_number(wind_kt) or 0) else "wind"
    if _number(wind_kt) is None:
        checks.append(("warn", "wind not reported"))
    elif peak > max_wind:
        checks.append(("fail", f"{kind} {int(peak)} kt over your {max_wind:g} kt limit"))
    elif peak >= max_wind * WIND_MARGIN:
        checks.append(("warn", f"{kind} {int(peak)} kt near your {max_wind:g} kt limit"))
    else:
        checks.append(("ok", f"{kind} {int(peak)} kt within your {max_wind:g} kt limit"))

    if visibility is None:
        checks.append(("warn", "visibility not reported"))
    elif visibility < 3:
        checks.append(("fail", f"visibility {visibility:g} SM below the 3 SM minimum"))
    else:
        checks.append(("ok", "visibility meets 3 SM"))

    needed = altitude_ft + 500
    low = [l for l in layers if l["base_ft"] is not None and l["cover"] not in ("SKC", "CLR", "NCD", "NSC")]
    lowest = min(low, key=lambda l: l["base_ft"]) if low else None
    if lowest is None or lowest["base_ft"] >= needed:
        checks.append(("ok", f"clouds clear of {altitude_ft} ft + 500 ft"))
    elif lowest["cover"] in CEILING_COVERS:
        checks.append(("fail", f"{CLOUD_COVER[lowest['cover']]} clouds at {lowest['base_ft']:,} ft; "
                               f"a {altitude_ft} ft flight needs {needed:,} ft"))
    else:
        checks.append(("warn", f"{CLOUD_COVER[lowest['cover']]} clouds at {lowest['base_ft']:,} ft; "
                               f"stay 500 ft below them"))

    if any("thunderstorm" in w for w in weather):
        checks.append(("fail", "thunderstorms"))
    if any(l["type"] == "CB" for l in layers) or any("cumulonimbus" in h for h in hazards):
        checks.append(("warn", "cumulonimbus nearby"))
    precipitation = [w for w in weather if any(p in w for p in ("rain", "drizzle", "snow", "hail", "ice"))]
    if precipitation:
        checks.append(("warn", ", ".join(precipitation)))
    if any(w in ("fog", "mist", "haze", "smoke") or "fog" in w for w in weather):
        checks.append(("warn", "reduced visibility (" + ", ".join(
            w for w in weather if w in ("fog", "mist", "haze", "smoke") or "fog" in w) + ")"))
    return checks


def decode_metar(m: dict, max_wind: float, altitude_ft: int, tz=None, now: Optional[float] = None) -> dict:
    """A METAR record from AWC, in plain language plus the drone checks."""
    raw = m.get("rawOb")
    layers = cloud_layers(m.get("clouds"), raw=raw, vertical_visibility=m.get("vertVis"))
    visibility = _number(m.get("visib"))
    weather = describe_weather(m.get("wxString"))
    hazards = remark_hazards(raw)
    ceiling = ceiling_ft(layers)
    observed = m.get("obsTime")
    now = time.time() if now is None else now
    temp, dew = _number(m.get("temp")), _number(m.get("dewp"))
    return {
        "station": m.get("icaoId"),
        "name": m.get("name"),
        "observed": local_time(observed, tz),
        "observed_utc": datetime.fromtimestamp(observed, tz=timezone.utc).isoformat() if observed else None,
        "age_minutes": None if observed is None else max(0, round((now - observed) / 60)),
        "category": m.get("fltCat") or flight_category(ceiling, visibility),
        "wind": describe_wind(m.get("wdir"), m.get("wspd"), m.get("wgst")),
        "wind_kt": _number(m.get("wspd")),
        "gust_kt": _number(m.get("wgst")),
        "visibility": describe_visibility(m.get("visib")),
        "visibility_sm": visibility,
        "weather": weather,
        "clouds": describe_clouds(layers),
        "cloud_layers": layers,
        "ceiling_ft": ceiling,
        "temperature": None if temp is None else f"{temp:g}°C ({round(temp * 9 / 5 + 32)}°F)",
        "dewpoint": None if dew is None else f"{dew:g}°C ({round(dew * 9 / 5 + 32)}°F)",
        "hazards": hazards,
        "checks": drone_checks(m.get("wspd"), m.get("wgst"), visibility, layers, weather,
                               hazards, max_wind, altitude_ft),
        "raw": raw,
    }


def decode_taf(t: dict, max_wind: float, altitude_ft: int, tz=None) -> dict:
    """
    A TAF from AWC as a timeline of periods.

    FM starts a new prevailing period; TEMPO, PROB and BECMG overlay the one
    in force, so they are labelled rather than treated as replacements.
    """
    periods = []
    for f in t.get("fcsts") or []:
        layers = cloud_layers(f.get("clouds"), vertical_visibility=f.get("vertVis"))
        visibility = _number(f.get("visib"))
        weather = describe_weather(f.get("wxString"))
        change = f.get("fcstChange")
        label = None
        if change == "PROB" or f.get("probability"):
            label = f"{f.get('probability') or '?'}% chance"
            if change == "TEMPO":
                label += ", temporarily"
        elif change in ("TEMPO", "BECMG"):
            label = TAF_CHANGES[change]
        ceiling = ceiling_ft(layers)
        has_wind = _number(f.get("wspd")) is not None
        periods.append({
            "from": local_span(f.get("timeFrom"), f.get("timeTo"), tz),
            "from_utc": datetime.fromtimestamp(f["timeFrom"], tz=timezone.utc).isoformat()
                        if f.get("timeFrom") else None,
            "to_utc": datetime.fromtimestamp(f["timeTo"], tz=timezone.utc).isoformat()
                      if f.get("timeTo") else None,
            "change": label,
            "wind": describe_wind(f.get("wdir"), f.get("wspd"), f.get("wgst")) if has_wind else None,
            "wind_dir": f.get("wdir"),
            "wind_kt": _number(f.get("wspd")),
            "gust_kt": _number(f.get("wgst")),
            "visibility": describe_visibility(f.get("visib")) if visibility is not None else None,
            "visibility_sm": visibility,
            "weather": weather,
            "clouds": describe_clouds(layers) if layers else None,
            "cloud_layers": layers,
            "ceiling_ft": ceiling,
            "category": flight_category(ceiling, visibility),
            # Overlay periods only list what changes, so only judge what they say
            "checks": [c for c in drone_checks(f.get("wspd"), f.get("wgst"), visibility, layers,
                                               weather, [], max_wind, altitude_ft)
                       if label is None or "not reported" not in c[1]],
        })
    return {
        "station": t.get("icaoId"),
        "name": t.get("name"),
        "issued": local_time(_epoch(t.get("issueTime")), tz),
        "valid": local_span(t.get("validTimeFrom"), t.get("validTimeTo"), tz),
        "periods": periods,
        "raw": t.get("rawTAF"),
    }


def parse_when(text: Optional[str], now: Optional[datetime] = None) -> datetime:
    """
    '--at' values: now, 2pm, 14:30, 9:15am, tomorrow 7am, or an ISO time.

    A bare clock time that has already passed today means tomorrow.
    """
    now = now or datetime.now().astimezone()
    value = (text or "now").strip().lower()
    if value == "now":
        return now
    try:
        parsed = datetime.fromisoformat(value.upper() if "t" in value else value)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        pass
    match = re.fullmatch(r"(today|tomorrow)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value)
    if not match:
        raise DronesError(f"Could not read --at '{text}': try 'now', '2pm', '14:30' or 'tomorrow 7am'")
    day, hour, minute, meridiem = match.groups()
    hour, minute = int(hour), int(minute or 0)
    if meridiem:
        if not 1 <= hour <= 12:
            raise DronesError(f"Could not read --at '{text}': {hour}{meridiem} is not a time")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        raise DronesError(f"Could not read --at '{text}': no such time")
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day == "tomorrow" or (day is None and when < now - timedelta(minutes=5)):
        when += timedelta(days=1)
    return when


def taf_periods_at(taf: dict, when: datetime) -> list:
    """The prevailing period in force at `when`, followed by any overlays covering it."""
    moment = when.timestamp()
    covering = [f for f in taf.get("fcsts") or []
                if f.get("timeFrom") is not None and f["timeFrom"] <= moment < (f.get("timeTo") or 0)]
    prevailing = [f for f in covering if f.get("fcstChange") in (None, "FM")]
    overlays = [f for f in covering if f.get("fcstChange") not in (None, "FM")]
    return prevailing[-1:] + overlays


def weather_verdict(checks: list, overlay_checks: list) -> tuple[str, list]:
    """GO / CAUTION / NO-GO from the prevailing checks; overlays can only add caution."""
    reasons = [text for status, text in checks if status == "fail"]
    if reasons:
        return "NO-GO", reasons
    possible = [f"possible: {text}" for status, text in overlay_checks if status == "fail"]
    cautions = [text for status, text in checks + overlay_checks if status == "warn"]
    return ("CAUTION", possible + cautions) if possible or cautions else ("GO", [])


def preflight(lat: float, lng: float, when: datetime, max_wind: float, altitude_ft: int,
              now: Optional[datetime] = None) -> dict:
    """Airspace at the point plus the weather for `when`, boiled down to one verdict."""
    now = now or datetime.now().astimezone()
    lead = (when - now).total_seconds() / 3600
    if lead < -1:
        raise DronesError("--at is in the past; preflight looks at now or a coming time")
    if lead > 30:
        raise DronesError("--at is more than 30 hours out, beyond any TAF")

    airspace = check_point(FAAClient(), lat, lng, when)
    client = WeatherClient()
    weather = {"source": None, "station": None, "metar": None, "taf_periods": []}

    if lead <= 1:
        stations = client.candidates(lat, lng, need_taf=False)
        reports = {r.get("icaoId"): r for r in client.metars([s["id"] for s in stations])} if stations else {}
        station = next((s for s in stations if s["id"] in reports), None)
        if station:
            weather.update(source="METAR", station=station,
                           metar=decode_metar(reports[station["id"]], max_wind, altitude_ft))
    if not weather["metar"]:
        stations = client.candidates(lat, lng, need_taf=True)
        reports = {r.get("icaoId"): r for r in client.tafs([s["id"] for s in stations])} if stations else {}
        station = next((s for s in stations if s["id"] in reports), None)
        if station:
            covering = taf_periods_at(reports[station["id"]], when)
            decoded = decode_taf({**reports[station["id"]], "fcsts": covering}, max_wind, altitude_ft)
            weather.update(source="TAF", station=station, taf_periods=decoded["periods"],
                           taf_raw=decoded["raw"])

    if weather["metar"]:
        verdict, reasons = weather_verdict(weather["metar"]["checks"], [])
    elif weather["taf_periods"]:
        base = [p for p in weather["taf_periods"] if not p["change"]]
        overlays = [p for p in weather["taf_periods"] if p["change"]]
        verdict, reasons = weather_verdict(
            [c for p in base for c in p["checks"]], [c for p in overlays for c in p["checks"]])
    else:
        verdict, reasons = "CAUTION", ["no weather report available for this time and place"]

    requirements = []
    # Name the restrictions themselves: an AUTHORIZATION REQUIRED verdict would
    # otherwise hide cautions such as a scheduled TFR inside controlled airspace
    def named(r):
        text = f"airspace: {r['kind']} - {r['name']}"
        return text + f" ({r['hours']})" if r["kind"].startswith("TFR") else text
    no_fly = [named(r) for r in airspace["restrictions"] if r["severity"] == "no-fly"]
    cautions = [named(r) for r in airspace["restrictions"] if r["severity"] == "caution"]
    cautions += [f"airspace: within 3 NM of {s['name']} (event TFRs during major games)"
                 for s in airspace["stadiums"]]
    if no_fly:
        verdict = "NO-GO"
        reasons = no_fly + reasons
    elif cautions and verdict == "GO":
        verdict = "CAUTION"
    reasons += cautions
    if airspace["verdict"] == "AUTHORIZATION REQUIRED":
        requirements.append(airspace["notes"][0])
        ceiling = (airspace["laanc"] or {}).get("ceiling_ft")
        if ceiling is not None and ceiling < altitude_ft:
            requirements.append(f"LAANC ceiling {ceiling} ft is below your planned {altitude_ft} ft")

    return {
        "verdict": verdict,
        "reasons": reasons,
        "requirements": requirements,
        "at": when.isoformat(timespec="minutes"),
        "airspace_verdict": airspace["verdict"],
        "airspace": airspace,
        "weather": weather,
        "limits": {"max_wind_kt": max_wind, "altitude_ft": altitude_ft},
        "advisory": ADVISORY,
    }


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ============================================================================
# Live traffic and radar (adsb.fi, NOAA MRMS)
# ============================================================================

def altitude_color(alt_ft) -> str:
    if alt_ft is None:
        return NO_ALTITUDE_COLOR
    color = ALTITUDE_COLORS[0][1]
    for threshold, candidate in ALTITUDE_COLORS:
        if alt_ft >= threshold:
            color = candidate
    return color


def aircraft_feature(a: dict) -> Optional[dict]:
    """
    One adsb.fi aircraft record -> a map feature, or None when it is on the
    ground or has no fresh position.

    Carries only what the map draws: the label, the altitude under it, and what
    picks the icon, its rotation and colour. The id (the ICAO hex) is how the
    map moves a marker from one poll to the next rather than redrawing it.
    """
    if a.get("lat") is None or a.get("lon") is None:
        return None
    if (a.get("seen_pos") or 0) > TRAFFIC_STALE_SECONDS:
        return None

    if a.get("alt_baro") == "ground":
        return None

    alt = a.get("alt_baro")
    track = next((a[k] for k in ("track", "true_heading", "nav_heading") if a.get(k) is not None),
                 None)
    properties = {
        "label": (a.get("flight") or "").strip() or a.get("r") or a.get("hex", "").upper(),
        "alt_ft": alt,
        "track": track,
        "rotorcraft": a.get("category") == "A7",
        "emergency": a.get("squawk") in EMERGENCY_SQUAWKS
                     or a.get("emergency") not in (None, "none"),
        "color": altitude_color(alt),
    }
    return {
        "type": "Feature",
        "id": a.get("hex"),
        "geometry": {"type": "Point", "coordinates": [a["lon"], a["lat"]]},
        "properties": {k: v for k, v in properties.items() if v is not None},
    }


class TrafficClient:
    """
    Aircraft near a point from adsb.fi, at most one request a second.

    Coverage comes from volunteer ground receivers, so aircraft low and far
    from a feeder can be missing, and aircraft without ADS-B never show. This
    is situational awareness, not see-and-avoid.
    """

    def __init__(self, timeout: float = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._lock = threading.Lock()
        self._last = 0.0

    def nearby(self, lat: float, lng: float, dist_nm: float) -> dict:
        """Aircraft within dist_nm of a point, as a GeoJSON FeatureCollection."""
        dist = max(1, min(MAX_TRAFFIC_NM, math.ceil(dist_nm)))
        url = ADSBFI_URL.format(lat=round(lat, 4), lon=round(lng, 4), dist=dist)
        # One request at a time however many tabs are polling
        with self._lock:
            wait = self._last + TRAFFIC_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                raise DronesError(f"adsb.fi request failed: {e}") from e
            finally:
                self._last = time.monotonic()

        if response.status_code == 429:
            raise DronesError("adsb.fi is rate limiting requests")
        if response.status_code != 200:
            raise DronesError(f"adsb.fi returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as e:
            raise DronesError("adsb.fi did not return JSON") from e
        aircraft = data.get("ac") or data.get("aircraft") or []
        return {"type": "FeatureCollection",
                "features": [f for f in map(aircraft_feature, aircraft) if f]}


TIME_DIMENSION = re.compile(r'<Dimension[^>]*name="time"[^>]*>([^<]*)</Dimension>')


def radar_times(capabilities: str) -> list:
    """The ISO 8601 frame times listed in a WMS GetCapabilities document, oldest first."""
    match = TIME_DIMENSION.search(capabilities)
    if not match:
        return []
    return sorted(t.strip() for t in match.group(1).split(",") if t.strip())


def radar_latest(timeout: float = 30) -> dict:
    """
    The newest MRMS radar frame's time.

    The browser fetches the radar images straight from NOAA; this only tells
    it which frame to ask for, so a long-open map moves forward with the radar.
    """
    try:
        response = requests.get(RADAR_WMS_URL, timeout=timeout,
                                headers={"User-Agent": USER_AGENT},
                                params={"service": "WMS", "version": "1.3.0",
                                        "request": "GetCapabilities"})
        response.raise_for_status()
    except requests.RequestException as e:
        raise DronesError(f"NOAA radar is unavailable: {e}") from e
    times = radar_times(response.text)
    return {"latest": times[-1] if times else None}


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
        scheduled = all(e["status"] == "scheduled" for e in entries)
        props = restriction("tfrs", entries[0], "#fd8d3c" if scheduled else "#d7191c",
                            0.12 if scheduled else 0.25)
        if scheduled:
            props["title"] += " (scheduled)"
            props["stroke-dasharray"] = "6 4"
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

    for f in data["airports"]:
        entry = airport_entry(f, lat, lng)
        features.append(_feature(f["geometry"], {
            "_layer": "airports",
            "title": f"{entry['ident']} - {entry['name']}",
            "description": f"{'Public' if entry['public'] else 'Private'} {entry['type']}",
            "Elevation": f"{entry['elevation_ft']} ft MSL" if entry["elevation_ft"] is not None else None,
            "Distance": f"{entry['distance_nm']} NM {entry['direction']}",
            "marker-color": "#2166ac" if entry["public"] else "#6baed6",
            "marker-size": 8.5 if entry["public"] else 6,
            "stroke-width": 0,
        }))

    features.append(_feature(
        {"type": "Polygon", "coordinates": [circle_ring(lat, lng, radius_nm)]},
        {"_layer": "radius", "title": f"{radius_nm:g} NM radius",
         "stroke": "#737373", "stroke-width": 1.5, "stroke-dasharray": "6 6",
         "fill-opacity": 0}))
    features.append(_feature(
        {"type": "Point", "coordinates": [lng, lat]},
        {"_layer": "location", "title": location["query"],
         "description": location.get("address"),
         "Coordinates": f"{lat:.5f}, {lng:.5f}",
         "marker-color": "#737373", "marker-size": "tiny", "stroke-width": 0}))

    return features


def map_page(location: dict, tiles: str, size: Optional[tuple[int, int]] = None,
             adsb: bool = False, radar: bool = False) -> str:
    """
    Fill the Leaflet page template.

    With a size, the map is pinned to exactly that many pixels for a
    screenshot. Headless Chromium lays the page out in a viewport smaller than
    --window-size and only grows it at capture time, too late for the tiles
    the resize uncovers; a fixed-size map loads every tile up front.

    adsb and radar add the live layers, which the page polls from the local
    server's traffic.json and radar.json.
    """
    traffic_config = {
        "attribution": ADSBFI_ATTRIBUTION,
        "max_dist_nm": MAX_TRAFFIC_NM,
    } if adsb else None
    radar_config = {
        "url": RADAR_WMS_URL,
        "layer": RADAR_LAYER,
        "attribution": RADAR_ATTRIBUTION,
    } if radar else None
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
            .replace("__PANEL_MOVES__", json.dumps(PANEL_MOVES))
            .replace("__ALWAYS_LISTED__", json.dumps(ALWAYS_LISTED))
            .replace("__BASEMAPS__", json.dumps(TILE_LAYERS))
            .replace("__DEFAULT_BASEMAP__", json.dumps(tiles))
            .replace("__PASSIVE__", json.dumps(PASSIVE_OVERLAYS))
            .replace("__FIXED__", json.dumps(FIXED_OVERLAYS))
            .replace("__TRAFFIC__", json.dumps(traffic_config))
            .replace("__RADAR__", json.dumps(radar_config)))


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


def _traffic_route(client: TrafficClient):
    """traffic.json?lat=&lng=&dist= for the page's air traffic poll."""
    def route(query: dict) -> dict:
        try:
            lat, lng, dist = (float(query[k][0]) for k in ("lat", "lng", "dist"))
        except (KeyError, ValueError):
            raise ValueError("traffic.json needs numeric lat, lng and dist")
        if not (-90 <= lat <= 90 and -180 <= lng <= 180 and 0 < dist <= MAX_TRAFFIC_NM):
            raise ValueError("traffic.json lat, lng or dist out of range")
        return client.nearby(lat, lng, dist)
    return route


def live_routes(adsb: bool, radar: bool) -> dict:
    """The map server's live data endpoints for --adsb and --radar."""
    routes = {}
    if adsb:
        routes["/traffic.json"] = _traffic_route(TrafficClient())
    if radar:
        routes["/radar.json"] = lambda query: radar_latest()
    return routes


def _map_server(page: str, data: dict, port: int,
                live: Optional[dict] = None) -> http.server.ThreadingHTTPServer:
    """
    Bind a localhost server for the Leaflet page and its data.

    live maps further paths to functions of the parsed query string that
    fetch fresh JSON on every request.
    """
    routes = {
        "/": ("text/html; charset=utf-8", page.encode()),
        "/data.json": ("application/json", json.dumps(data).encode()),
    }
    live = live or {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path in live:
                self._live(live[path], query)
                return
            if path not in routes:
                self.send_error(404)
                return
            self._send(*routes[path])

        def _live(self, route, query: str):
            try:
                body = json.dumps(route(urllib.parse.parse_qs(query))).encode()
            except ValueError as e:
                self.send_error(400, str(e))
                return
            except DronesError as e:
                self.send_error(502, str(e))
                return
            self._send("application/json", body)

        def _send(self, content_type: str, body: bytes):
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
            raise DronesError(f"Could not start server: {e}")
        print(f"  Port {port} unavailable ({e.strerror}), using a free port instead")
        return _MapServer(("127.0.0.1", 0), Handler)


def serve_map(page: str, data: dict, port: int, open_browser: bool,
              live: Optional[dict] = None) -> int:
    """Serve the Leaflet page on localhost until interrupted."""
    server = _map_server(page, data, port, live)
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
    """Locate Chrome, Edge or Chromium. DRONES_BROWSER overrides the search."""
    override = os.environ.get("DRONES_BROWSER")
    if override:
        return override
    for command in BROWSER_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    return next((path for path in _browser_paths() if os.path.isfile(path)), None)


def screenshot_map(page: str, data: dict, path: str, size: tuple[int, int],
                   wait_ms: int = SCREENSHOT_WAIT_MS, live: Optional[dict] = None) -> str:
    """
    Render the map to a PNG with a headless Chromium browser.

    The page is served on a free localhost port for the duration. Headless
    screenshots fire on a virtual clock, so wait_ms is the page time allowed
    for the basemap tiles and data to land before the capture.
    """
    browser = find_browser()
    if not browser:
        raise DronesError("No Chrome, Edge or Chromium found for --png; "
                            "install one or set DRONES_BROWSER to its path")

    output = os.path.abspath(path)
    if os.path.exists(output):
        os.remove(output)  # so a stale file can't pass for a fresh capture

    server = _map_server(page, data, 0, live)
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
            raise DronesError(f"Browser screenshot failed: {e}") from e
        finally:
            server.shutdown()
            server.server_close()

    if not os.path.isfile(output):
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise DronesError("Browser did not write a screenshot"
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

    print()
    print(_wrap(ADVISORY, "  "))
    print()


CHECK_MARKS = {"ok": "\u2713", "warn": "\u26a0", "fail": "\u2717"}


def _station_line(station: dict, location: Optional[dict]) -> str:
    line = f"{station.get('name') or '?'} ({station.get('id') or station.get('station')})"
    if location and station.get("distance_nm") is not None:
        line += f" \u00b7 {station['distance_nm']:g} NM {station['direction']} of {location['query']}"
    return line


def _print_checks(checks: list, indent: str = "    ") -> None:
    for status, text in checks:
        print(f"{indent}{CHECK_MARKS[status]} {text}")


def print_metar(report: dict, station: dict, location: Optional[dict], args) -> None:
    print(f"\n  {_station_line({**station, 'name': report['name'] or station.get('name')}, location)}")
    age = report["age_minutes"]
    stale = "  \u26a0 over 90 minutes old" if age is not None and age > 90 else ""
    print(f"  Observed {report['observed']} \u00b7 {age} min ago{stale}")
    print("  " + "=" * 58)
    category = report["category"]
    print(f"  Category:    {category} ({FLIGHT_CATEGORIES.get(category, '?')})" if category
          else "  Category:    not reported")
    print(f"  Wind:        {report['wind']}")
    print(f"  Visibility:  {report['visibility']}")
    if report["weather"]:
        print(f"  Weather:     {', '.join(report['weather'])}")
    print(_wrap(f"Clouds:      {report['clouds']}", "  ").replace("\n  ", "\n               "))
    print(f"  Ceiling:     {report['ceiling_ft']:,} ft" if report["ceiling_ft"] is not None
          else "  Ceiling:     none")
    if report["temperature"]:
        print(f"  Temp/dew:    {report['temperature']} / {report['dewpoint']}")
    if report["hazards"]:
        print(f"  Hazards:     {'; '.join(report['hazards'])}")
    print(f"\n  Drone check ({args.altitude} ft AGL, {args.max_wind:g} kt wind limit)")
    _print_checks(report["checks"])
    print(_wrap(f"Raw: {report['raw']}", "  ").replace("\n  ", "\n       "))
    print()


def _compact_period(p: dict) -> str:
    parts = []
    if p["category"]:
        parts.append(f"{p['category']:<4}")
    if p["wind_kt"] is not None:
        gust = f" G{int(p['gust_kt'])}" if p["gust_kt"] else ""
        heading = ("VRB" if p["wind_dir"] in (None, "", "VRB")
                   else bearing_to_cardinal(int(_number(p["wind_dir"]) or 0)))
        parts.append("calm" if not p["wind_kt"] else f"{heading} {int(p['wind_kt'])}{gust} kt")
    if p["visibility"]:
        parts.append(p["visibility"].replace(" statute miles", " SM").replace(" statute mile", " SM"))
    if p["weather"]:
        parts.append(", ".join(p["weather"]))
    if p["clouds"]:
        parts.append(p["clouds"])
    return " \u00b7 ".join(parts)


def print_taf(report: dict, station: dict, location: Optional[dict], args) -> None:
    print(f"\n  TAF {_station_line({**station, 'name': report['name'] or station.get('name')}, location)}")
    print(f"  Issued {report['issued']} \u00b7 valid {report['valid']}")
    print("  " + "=" * 58)
    for p in report["periods"]:
        when = p["from"] if not p["change"] else f"  \u21b3 {p['change']}"
        print(f"  {when:<22} {_compact_period(p)}")
        problems = [c for c in p["checks"] if c[0] != "ok"]
        if problems:
            _print_checks(problems, indent=" " * 25)
    print(f"\n  Drone limits: {args.altitude} ft AGL, {args.max_wind:g} kt wind; "
          "\u2717 and \u26a0 mark periods that break or need watching")
    print(_wrap(f"Raw: {report['raw']}", "  ").replace("\n  ", "\n       "))
    print()


def print_preflight(location: dict, report: dict, args) -> None:
    _header("Preflight", location)
    when = datetime.fromisoformat(report["at"])
    print(f"  When:       {when.strftime('%a %I:%M %p').replace(' 0', ' ')} "
          f"({args.altitude} ft AGL, {args.max_wind:g} kt wind limit)")
    print(f"  Verdict:    {report['verdict']}")
    for reason in report["reasons"]:
        print(_wrap(f"- {reason}", "              "))
    for requirement in report["requirements"]:
        print(_wrap(f"Requires: {requirement}", "              "))

    a = report["airspace"]
    controlled = [x for x in a["airspace"] if x["surface"]]
    print("\n  Airspace")
    print(f"    {a['verdict']}: " + (", ".join(f"{x['class']} {x['name']}" for x in controlled)
                                     or "Class G (uncontrolled)"))
    if a["laanc"]:
        print(f"    LAANC ceiling {a['laanc']['ceiling_ft']} ft AGL")
    for r in a["restrictions"]:
        for line in _restriction_line(r):
            print(line)

    w = report["weather"]
    print("\n  Weather")
    if w["metar"]:
        m = w["metar"]
        print(f"    {_station_line(w['station'], location)}")
        print(_wrap(f"Observed {m['observed']}: {m['category'] or '?'}, wind {m['wind']}, "
                    f"visibility {m['visibility']}, clouds {m['clouds']}", "    "))
        _print_checks(m["checks"], indent="      ")
    elif w["taf_periods"]:
        print(f"    TAF {_station_line(w['station'], location)}")
        for p in w["taf_periods"]:
            when_label = p["from"] if not p["change"] else f"\u21b3 {p['change']}"
            print(f"    {when_label:<22} {_compact_period(p)}")
            _print_checks([c for c in p["checks"] if c[0] != "ok"], indent=" " * 27)
    else:
        print("    No report available")

    print(f"\n  Map:  {_maps_url(location['latitude'], location['longitude'])}")
    print(_wrap(ADVISORY, "  "))
    print()


def print_stations(stations: list, location: dict, radius_nm: float) -> None:
    print(f"\n  Weather stations within {radius_nm:g} NM of {location['query']}")
    print("  " + "=" * 58)
    for s in stations:
        kinds = "METAR + TAF" if s["has_taf"] else "METAR"
        print(f"  {s['id']:<6} {(s['name'] or '?')[:34]:<34} {s['distance_nm']:>5.1f} NM {s['direction']:<3}  {kinds}")
    if not stations:
        print("  None - try a larger --radius")
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
        raise DronesError("--radius must be between 0 and 50 NM")


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
    except DronesError as e:
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
    except DronesError as e:
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
        live = live_routes(args.adsb, args.radar)
        if args.png:
            page = map_page(location, args.tiles, size=args.size,
                            adsb=args.adsb, radar=args.radar)
            saved = screenshot_map(page, collection, args.png, args.size, live=live)
            print(f"\n  Saved map to {saved}\n")
            return 0
        return serve_map(map_page(location, args.tiles, adsb=args.adsb, radar=args.radar),
                         collection, args.port, not args.no_open, live=live)
    except DronesError as e:
        return _fail(str(e))


# A station identifier, or a comma separated list of them (KFLL, KFLL,KMIA)
STATION_IDS = re.compile(r"^[A-Za-z][A-Za-z0-9]{3}(,[A-Za-z][A-Za-z0-9]{3})*$")


def _weather_reports(args, kind: str) -> list:
    """
    (station, location, report) for each requested station.

    Station IDs are tried first; a place name that happens to look like one
    (Rome, Iowa) falls back to geocoding when AWC knows no such station.
    """
    client = WeatherClient()
    fetch = client.metars if kind == "metar" else client.tafs
    query = args.location.strip()
    if STATION_IDS.match(query):
        wanted = query.upper().split(",")
        found = sorted(fetch(wanted), key=lambda r: wanted.index(r.get("icaoId"))
                       if r.get("icaoId") in wanted else len(wanted))
        if found:
            return [({"id": r.get("icaoId"), "name": r.get("name")}, None, r) for r in found]

    location = _locate(args, lookup_address=False)
    lat, lng = location["latitude"], location["longitude"]
    candidates = client.candidates(lat, lng, need_taf=(kind == "taf"))
    if not candidates:
        raise DronesError(f"No {kind.upper()} station within {STATION_SEARCH_NM[-1]:g} NM of {query}")
    # Part-time TAFs and offline sensors: nearest station that actually reported
    reports = {r.get("icaoId"): r for r in fetch([c["id"] for c in candidates])}
    station = next((c for c in candidates if c["id"] in reports), None)
    if not station:
        raise DronesError(f"None of the nearest {kind.upper()} stations "
                       f"({', '.join(c['id'] for c in candidates)}) has a current report")
    skipped = [c["id"] for c in candidates[:candidates.index(station)]]
    if skipped:
        print(f"Note: {', '.join(skipped)} has no current {kind.upper()}; using {station['id']}",
              file=sys.stderr)
    found = [reports[station["id"]]]
    if kind == "taf" and station["distance_nm"] > TAF_COVERAGE_NM:
        print(f"Warning: nearest TAF airport {station['id']} is {station['distance_nm']:g} NM away; "
              "a TAF covers 5 SM around its airport, so treat it as regional",
              file=sys.stderr)
    return [(station, location, found[0])]


def cmd_metar(args: argparse.Namespace) -> int:
    try:
        _check_limits(args)
        results = []
        for station, location, record in _weather_reports(args, "metar"):
            report = decode_metar(record, args.max_wind, args.altitude)
            results.append((station, location, report))
        if args.json:
            out = [{"location": loc, "station": st, "metar": rep} for st, loc, rep in results]
            print(json.dumps(out if len(out) > 1 else out[0], indent=2))
        elif args.raw:
            for _, _, report in results:
                print(report["raw"])
        else:
            for station, location, report in results:
                print_metar(report, station, location, args)
        return 0
    except DronesError as e:
        return _fail(str(e))


def cmd_taf(args: argparse.Namespace) -> int:
    try:
        _check_limits(args)
        results = []
        for station, location, record in _weather_reports(args, "taf"):
            results.append((station, location, decode_taf(record, args.max_wind, args.altitude)))
        if args.json:
            out = [{"location": loc, "station": st, "taf": rep} for st, loc, rep in results]
            print(json.dumps(out if len(out) > 1 else out[0], indent=2))
        elif args.raw:
            for _, _, report in results:
                print(report["raw"])
        else:
            for station, location, report in results:
                print_taf(report, station, location, args)
        return 0
    except DronesError as e:
        return _fail(str(e))


def cmd_preflight(args: argparse.Namespace) -> int:
    try:
        _check_limits(args)
        when = parse_when(args.at)
        location = _locate(args, lookup_address=not args.no_address)
        report = preflight(location["latitude"], location["longitude"], when,
                           args.max_wind, args.altitude)
        if args.json:
            print(json.dumps({"location": location, **report}, indent=2))
        else:
            print_preflight(location, report, args)
        return 0 if report["verdict"] != "NO-GO" else 3
    except DronesError as e:
        return _fail(str(e))


def cmd_stations(args: argparse.Namespace) -> int:
    try:
        _check_radius(args.radius)
        location = _locate(args, lookup_address=False)
        stations = WeatherClient().stations(location["latitude"], location["longitude"], args.radius)
        if args.taf_only:
            stations = [s for s in stations if s["has_taf"]]
        if args.json:
            print(json.dumps({"location": location, "radius_nm": args.radius,
                              "stations": stations}, indent=2))
        else:
            print_stations(stations, location, args.radius)
        return 0
    except DronesError as e:
        return _fail(str(e))


def _check_limits(args: argparse.Namespace) -> None:
    if not math.isfinite(args.max_wind) or args.max_wind <= 0:
        raise DronesError("--max-wind must be a positive number of knots")
    if args.altitude <= 0:
        raise DronesError("--altitude must be a positive number of feet")



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


def _add_limit_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-wind",
        type=float,
        default=float(os.environ.get("DRONES_MAX_WIND", DEFAULT_MAX_WIND_KT)),
        metavar="KT",
        help=f"Your aircraft's wind limit in knots (default: {DEFAULT_MAX_WIND_KT:g}, "
             "or DRONES_MAX_WIND)"
    )
    parser.add_argument(
        "--altitude",
        type=int,
        default=400,
        metavar="FT",
        help="Planned height in ft AGL for the cloud clearance check (default: 400)"
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
        description="drones.py - FAA airspace and aviation weather for drone pilots",
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
        "--adsb",
        action="store_true",
        help="Show live air traffic from adsb.fi, refreshed every 3 seconds"
    )
    map_parser.add_argument(
        "--radar",
        action="store_true",
        help="Show NOAA MRMS weather radar (lower 48), refreshed every 2 minutes"
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

    for name, kind, about in [
        ("metar", "METAR", "Current conditions decoded, with drone checks"),
        ("taf", "TAF", "Forecast timeline decoded, with drone checks"),
    ]:
        weather_parser = subparsers.add_parser(
            name, help=about,
            description=f"{about}. LOCATION is a station ID (KFLL, or KFLL,KMIA) or any "
                        f"place, which uses the nearest {kind}-reporting station."
        )
        _add_location_options(weather_parser)
        _add_limit_options(weather_parser)
        weather_parser.add_argument("--raw", action="store_true", help="Print only the raw report")
        weather_parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Go/no-go: airspace plus weather for when you plan to fly",
        description="Airspace at the point and the weather for --at, as one verdict. "
                    "Within the hour uses the nearest METAR; later uses the nearest TAF. "
                    "Exits 3 on NO-GO."
    )
    _add_location_options(preflight_parser)
    _add_limit_options(preflight_parser)
    preflight_parser.add_argument(
        "--at",
        default="now",
        metavar="TIME",
        help="When you plan to fly: now, 2pm, 14:30, 'tomorrow 7am' or ISO (default: now)"
    )
    preflight_parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")

    stations_parser = subparsers.add_parser(
        "stations",
        help="Weather reporting stations near a point",
        description="METAR stations within a radius, marking which also issue TAFs."
    )
    _add_location_options(stations_parser)
    stations_parser.add_argument(
        "--radius", "-r",
        type=float,
        default=DEFAULT_STATION_RADIUS_NM,
        help=f"Search radius in nautical miles, up to 50 (default: {DEFAULT_STATION_RADIUS_NM:g})"
    )
    stations_parser.add_argument("--taf-only", action="store_true",
                                 help="Only stations that issue TAFs")
    stations_parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")

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
        "metar": cmd_metar,
        "taf": cmd_taf,
        "stations": cmd_stations,
        "preflight": cmd_preflight,
    }
    handler = commands.get(args.command)
    if handler:
        return handler(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
