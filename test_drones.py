"""Offline regression tests for drones.py (no package install needed)."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parent / "drones.py"
SPEC = importlib.util.spec_from_file_location("drones_cli_under_test", SCRIPT)
drones = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drones)


SQUARE = {"type": "Polygon", "coordinates": [[[-88, 41], [-87, 41], [-87, 42], [-88, 42], [-88, 41]]]}
DONUT = {"type": "Polygon", "coordinates": [
    [[-88, 41], [-87, 41], [-87, 42], [-88, 42], [-88, 41]],
    [[-87.6, 41.4], [-87.4, 41.4], [-87.4, 41.6], [-87.6, 41.6], [-87.6, 41.4]],
]}


def grid_cell(ceiling=0, laanc=1, enabled="107-AA,107-FC,44809-AA"):
    return {"CEILING": ceiling, "MAP_EFF": "10/1/2026", "APT1_ICAO": "KMDW",
            "APT1_FAAID": "MDW", "APT1_NAME": "Chicago Midway Intl",
            "APT1_LAANC": laanc, "APT1_Enabled": enabled, "AIRSPACE_1": "C"}


def class_c(lower="0", code="SFC"):
    return {"NAME": "CHICAGO CLASS C", "ICAO_ID": "KMDW", "LOCAL_TYPE": "CLASS_C",
            "LOWER_VAL": lower, "LOWER_UOM": "FT", "LOWER_CODE": code,
            "UPPER_VAL": 3600, "UPPER_UOM": "FT", "UPPER_CODE": "MSL", "WKHR_CODE": "H24"}


def feature(props, geometry=None):
    return {"type": "Feature", "geometry": geometry, "properties": props}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        # Fail closed if a test accidentally reaches an external service.
        guard = patch.object(socket.socket, "connect",
                             side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)


class LocationTests(OfflineCase):
    def test_parses_coordinates_and_rejects_names(self):
        self.assertEqual(drones.Locator.parse_coordinates("41.79,-87.74"), (41.79, -87.74))
        self.assertEqual(drones.Locator.parse_coordinates(" -33.86 151.21 "), (-33.86, 151.21))
        self.assertIsNone(drones.Locator.parse_coordinates("Chicago Midway"))

    def test_out_of_range_coordinates_fail(self):
        with self.assertRaises(drones.DronesError):
            drones.Locator.parse_coordinates("91,10")

    def test_negative_latitude_survives_argparse(self):
        parser = drones.build_parser()
        argv = drones._normalize_coordinate_args(
            ["airspace", "-33.86,151.21", "--json"], drones._value_option_strings(parser))
        args = parser.parse_args(argv)
        self.assertEqual(args.location, "-33.86,151.21")
        self.assertTrue(args.json)

    def test_coordinates_skip_geocoding(self):
        with patch.object(drones, "Nominatim"):
            locator = drones.Locator()
        locator._lookup = Mock(side_effect=AssertionError("no lookup expected"))
        location = locator.locate("41.79,-87.74", lookup_address=False)
        self.assertEqual((location["latitude"], location["longitude"]), (41.79, -87.74))
        self.assertIsNone(location["address"])


class GeometryTests(OfflineCase):
    def test_contains_honours_holes(self):
        self.assertTrue(drones.contains(SQUARE, 41.5, -87.5))
        self.assertFalse(drones.contains(DONUT, 41.5, -87.5))
        self.assertTrue(drones.contains(DONUT, 41.1, -87.9))
        self.assertFalse(drones.contains(SQUARE, 43, -87.5))

    def test_distance_is_zero_inside_and_to_edge_outside(self):
        self.assertEqual(drones.distance_nm(SQUARE, 41.5, -87.5), 0.0)
        # One degree of latitude north of the top edge is 60 NM
        self.assertAlmostEqual(drones.distance_nm(SQUARE, 43, -87.5), 60, delta=0.5)

    def test_point_distance_and_bearing(self):
        point = {"type": "Point", "coordinates": [-87.74, 41.89]}
        self.assertAlmostEqual(drones.distance_nm(point, 41.79, -87.74), 6.0, delta=0.05)
        self.assertEqual(drones.bearing_to_cardinal(
            drones.calculate_bearing(41.79, -87.74, 41.89, -87.74)), "N")

    def test_circle_ring_is_closed(self):
        ring = drones.circle_ring(41.79, -87.74, 3)
        self.assertEqual(ring[0], ring[-1])
        self.assertEqual(len(ring), 73)


class InterpretationTests(OfflineCase):
    def test_format_altitude(self):
        self.assertEqual(drones.format_altitude(0, "FT", "SFC"), "SFC")
        self.assertEqual(drones.format_altitude("3600", "FT", "MSL"), "3,600 ft MSL")
        self.assertEqual(drones.format_altitude(600, "FL", "STD"), "FL600")
        self.assertEqual(drones.format_altitude(-9998, None, None), "Class A")

    def test_laanc_entry_names_programs(self):
        entry = drones.laanc_entry(grid_cell(ceiling=200))
        self.assertEqual(entry["ceiling_ft"], 200)
        self.assertEqual(entry["airports"][0]["ident"], "KMDW")
        self.assertIn("Recreational auto approval", entry["airports"][0]["programs"])

    def test_high_sua_floor_is_overhead_not_caution(self):
        moa = {"NAME": "HOWARD EAST MOA", "TYPE_CODE": "MOA", "LOWER_VAL": "9000",
               "LOWER_UOM": "FT", "LOWER_CODE": "MSL"}
        restricted = {**moa, "NAME": "R-1", "TYPE_CODE": "R", "LOWER_VAL": "0", "LOWER_CODE": "SFC"}
        prohibited = {**moa, "NAME": "P-56", "TYPE_CODE": "P"}
        self.assertEqual(drones.sua_entry(moa)["severity"], "overhead")
        self.assertEqual(drones.sua_entry(restricted)["severity"], "caution")
        self.assertEqual(drones.sua_entry(prohibited)["severity"], "no-fly")

    def test_defense_airspace_by_notam_is_caution(self):
        entry = drones.defense_entry({"NAME": "X", "WKHR_CODE": "RMK", "WKHR_RMK": "BY NOTAM"})
        self.assertEqual(entry["severity"], "caution")

    def test_verdicts(self):
        controlled = [drones.class_entry(class_c())]
        laanc = drones.laanc_entry(grid_cell(ceiling=200))
        verdict, notes = drones.assess(controlled, laanc, [], [])
        self.assertEqual(verdict, "AUTHORIZATION REQUIRED")
        self.assertIn("200 ft AGL", notes[0])

        verdict, _ = drones.assess([], None, [], [])
        self.assertEqual(verdict, "CLEAR")

        overhead = [{"severity": "overhead"}]
        self.assertEqual(drones.assess([], None, overhead, [])[0], "CLEAR")
        self.assertEqual(drones.assess([], None, [{"severity": "caution"}], [])[0], "CAUTION")
        self.assertEqual(drones.assess(controlled, laanc, [{"severity": "no-fly"}], [])[0],
                         "DO NOT FLY")

    def test_zero_ceiling_and_non_laanc_airports(self):
        self.assertIn("0 ft", drones.authorization_note(drones.laanc_entry(grid_cell(0))))
        self.assertIn("FAADroneZone", drones.authorization_note(
            drones.laanc_entry(grid_cell(100, laanc=0))))
        self.assertIn("FAADroneZone", drones.authorization_note(None))

    def test_class_shelf_is_not_surface(self):
        self.assertTrue(drones.class_entry(class_c())["surface"])
        self.assertFalse(drones.class_entry(class_c("1900", "MSL"))["surface"])


class FAAClientTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.client = drones.FAAClient()
        self.client._get_json = Mock()

    def test_query_pages_until_limit_clears(self):
        self.client._get_json.side_effect = [
            {"features": [feature({"CEILING": 0})],
             "properties": {"exceededTransferLimit": True}},
            {"features": [feature({"CEILING": 100})]},
        ]
        features = self.client.query("laanc", 41.79, -87.74, radius_nm=10, geometry=True)
        self.assertEqual([f["properties"]["CEILING"] for f in features], [0, 100])
        first, second = (c.args[1] for c in self.client._get_json.call_args_list)
        self.assertEqual(first["geometry"], "-87.74,41.79")
        self.assertEqual(first["units"], "esriSRUnit_NauticalMile")
        self.assertEqual((first["resultOffset"], second["resultOffset"]), (0, 1))

    def test_point_query_sends_no_distance(self):
        self.client._get_json.return_value = {"features": []}
        self.client.query("class", 41.79, -87.74)
        params = self.client._get_json.call_args.args[1]
        self.assertNotIn("distance", params)
        self.assertIn("CLASS_E2", params["where"])
        self.assertEqual(params["returnGeometry"], "false")

    def test_service_errors_raise(self):
        self.client._get_json.return_value = {"error": {"message": "Invalid query"}}
        with self.assertRaisesRegex(drones.DronesError, "Invalid query"):
            self.client.query("sua", 41.79, -87.74)

    def test_tfrs_extract_notam_and_detail_link(self):
        self.client._get_json.return_value = {"features": [feature(
            {"NOTAM_KEY": "6/9169-1-FDC-F", "LEGAL": "SECURITY", "TITLE": "RALEIGH, NC"},
            SQUARE)]}
        props = self.client.tfrs()[0]["properties"]
        self.assertEqual(props["notam"], "6/9169")
        self.assertEqual(props["url"], "https://tfr.faa.gov/tfr3/?page=detail_6_9169")


class ScreenshotTests(OfflineCase):
    def test_size_parsing(self):
        self.assertEqual(drones.parse_size("1920x1080"), (1920, 1080))
        with self.assertRaises(drones.argparse.ArgumentTypeError):
            drones.parse_size("big")

    def test_browser_override_wins(self):
        with patch.dict(drones.os.environ, {"DRONES_BROWSER": "/opt/chrome"}):
            self.assertEqual(drones.find_browser(), "/opt/chrome")

    def test_missing_browser_fails_clearly(self):
        with patch.object(drones, "find_browser", return_value=None):
            with self.assertRaisesRegex(drones.DronesError, "DRONES_BROWSER"):
                drones.screenshot_map("<html>", {}, "x.png", (800, 600))

    def test_runs_headless_browser_against_local_server(self):
        server = Mock(server_address=("127.0.0.1", 54321))

        def browser(command, **kwargs):
            shot = next(a for a in command if a.startswith("--screenshot="))
            Path(shot.split("=", 1)[1]).write_bytes(b"png")
            return Mock(stdout="", stderr="")

        with patch.object(drones, "find_browser", return_value="chrome"), \
                patch.object(drones, "_map_server", return_value=server), \
                patch.object(drones.threading, "Thread"), \
                patch.object(drones.subprocess, "run", side_effect=browser) as run, \
                drones.tempfile.TemporaryDirectory() as out:
            saved = drones.screenshot_map("<html>", {}, str(Path(out) / "m.png"), (800, 600))
            self.assertTrue(Path(saved).is_file())

        command = run.call_args.args[0]
        self.assertEqual(command[-1], "http://127.0.0.1:54321/")
        self.assertIn("--window-size=800,600", command)
        self.assertIn("--headless=new", command)
        server.shutdown.assert_called_once()

    def test_no_file_written_is_an_error(self):
        server = Mock(server_address=("127.0.0.1", 54321))
        with patch.object(drones, "find_browser", return_value="chrome"), \
                patch.object(drones, "_map_server", return_value=server), \
                patch.object(drones.threading, "Thread"), \
                patch.object(drones.subprocess, "run",
                             return_value=Mock(stdout="", stderr="GPU crashed")), \
                drones.tempfile.TemporaryDirectory() as out:
            with self.assertRaisesRegex(drones.DronesError, "GPU crashed"):
                drones.screenshot_map("<html>", {}, str(Path(out) / "m.png"), (800, 600))


class ThrottleTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.client = drones.FAAClient()
        self.client.session = Mock()
        self.sleep = patch.object(drones.time, "sleep").start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def response(status=200, body=None):
        return Mock(status_code=status, json=Mock(return_value=body or {}),
                    raise_for_status=Mock())

    def test_retries_arcgis_throttle_message(self):
        throttled = self.response(body={"error": {"message": "Unable to perform query. Too many requests."}})
        self.client.session.get.side_effect = [throttled, self.response(body={"features": []})]
        self.assertEqual(self.client._get_json("u", {}), {"features": []})
        self.sleep.assert_called_once_with(drones.RETRY_DELAYS[0])

    def test_retries_http_429_then_gives_up(self):
        self.client.session.get.return_value = self.response(status=429)
        with self.assertRaisesRegex(drones.DronesError, "rate limiting"):
            self.client._get_json("u", {})
        self.assertEqual(self.sleep.call_count, len(drones.RETRY_DELAYS))

    def test_other_errors_are_not_retried(self):
        self.client.session.get.return_value = self.response(body={"error": {"message": "Invalid query"}})
        self.assertEqual(self.client._get_json("u", {})["error"]["message"], "Invalid query")
        self.sleep.assert_not_called()


def fake_gather(results):
    def gather(drones, lat, lng, wanted):
        return {name: results.get(name, []) for name in wanted}
    return gather


ACTIVE_TFR = {"begins": None, "ends": None, "immediate": True, "permanent": True,
              "floor": "SFC", "ceiling": "17,999 ft MSL"}


class CommandTests(OfflineCase):
    def setUp(self):
        super().setUp()
        details = patch.object(drones.FAAClient, "tfr_details", return_value=ACTIVE_TFR)
        details.start()
        self.addCleanup(details.stop)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(drones, "utf8_output"):
            code = drones.main(list(argv))
        return code, out.getvalue()

    def test_airspace_json_inside_tfr(self):
        results = {
            "laanc": [feature(grid_cell(ceiling=100))],
            "class": [feature(class_c())],
            "tfrs": [feature({"notam": "6/1", "type": "SECURITY", "title": "TEST TFR",
                              "url": "u"}, SQUARE)],
        }
        with patch.object(drones, "gather", fake_gather(results)):
            code, out = self.run_cli("airspace", "41.5,-87.5", "--no-address", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["verdict"], "DO NOT FLY")
        self.assertEqual(report["laanc"]["ceiling_ft"], 100)
        self.assertEqual(report["restrictions"][0]["kind"], "TFR (security)")

    def test_airspace_text_class_g(self):
        with patch.object(drones, "gather", fake_gather({})):
            code, out = self.run_cli("airspace", "40.5,-89.9", "--no-address")
        self.assertEqual(code, 0)
        self.assertIn("CLEAR", out)
        self.assertIn("Class G", out)

    def test_map_geojson_tags_every_feature_with_a_layer(self):
        results = {
            "laanc": [feature(grid_cell(ceiling=400), SQUARE)],
            "class": [feature(class_c(), SQUARE)],
            "airports": [feature({"IDENT": "MDW", "NAME": "Midway", "TYPE_CODE": "AD",
                                  "PRIVATEUSE": 0}, {"type": "Point", "coordinates": [-87.75, 41.78]})],
        }
        with patch.object(drones, "gather", fake_gather(results)):
            code, out = self.run_cli("map", "41.79,-87.74", "--no-address", "--geojson")
        self.assertEqual(code, 0)
        layers = [f["properties"]["_layer"] for f in json.loads(out)["features"]]
        self.assertEqual(layers, ["laanc", "class", "airports", "radius", "location"])
        known = {key for key, _ in drones.MAP_OVERLAYS}
        self.assertTrue(set(layers) <= known)

    def test_tfrs_reach_30_nm_past_a_small_radius(self):
        def tfr(notam):
            return feature({"notam": notam, "type": "SECURITY", "title": notam,
                            "url": f"https://tfr.faa.gov/tfr3/?page=detail_{notam.replace('/', '_')}"},
                           SQUARE)
        results = {"tfrs": [tfr("6/0001")]}
        # About 13 NM east of the square, then about 40 NM
        for lng, expected in ((-86.7, 1), (-86.1, 0)):
            with patch.object(drones, "gather", fake_gather(results)):
                code, out = self.run_cli("map", f"41.5,{lng}", "--no-address", "--geojson")
            self.assertEqual(code, 0)
            tfrs = [f for f in json.loads(out)["features"] if f["properties"]["_layer"] == "tfrs"]
            self.assertEqual(len(tfrs), expected, lng)

    def test_renewed_tfrs_on_one_shape_draw_once(self):
        def tfr(notam, title):
            return feature({"notam": notam, "type": "SECURITY", "title": title,
                            "url": f"https://tfr.faa.gov/tfr3/?page=detail_{notam.replace('/', '_')}"},
                           SQUARE)
        results = {"tfrs": [tfr("5/2809", "Palm Beach, ending"), tfr("6/0100", "Palm Beach, renewal")]}
        with patch.object(drones, "gather", fake_gather(results)):
            code, out = self.run_cli("map", "41.5,-87.5", "--no-address", "--geojson")
        self.assertEqual(code, 0)
        tfrs = [f for f in json.loads(out)["features"] if f["properties"]["_layer"] == "tfrs"]
        self.assertEqual(len(tfrs), 1)
        props = tfrs[0]["properties"]
        self.assertIn("2 NOTAMs", props["title"])
        self.assertEqual([label for label, _ in props["_links"]], ["NOTAM 5/2809", "NOTAM 6/0100"])
        self.assertEqual(props["NOTAM 6/0100"], "Palm Beach, renewal")

    def test_png_screenshots_instead_of_serving(self):
        with patch.object(drones, "gather", fake_gather({})), \
                patch.object(drones, "screenshot_map", return_value="C:/out/map.png") as shot, \
                patch.object(drones, "serve_map") as serve:
            code, out = self.run_cli("map", "41.79,-87.74", "--no-address",
                                     "--png", "map.png", "--size", "800x600")
        self.assertEqual(code, 0)
        serve.assert_not_called()
        page, collection, path, size = shot.call_args.args
        self.assertEqual((path, size), ("map.png", (800, 600)))
        self.assertIn("leaflet", page)
        # The map is pinned to the image size so every tile loads before capture
        self.assertIn("#map { width: 800px; height: 600px; }", page)
        self.assertNotIn("__SCREENSHOT_CSS__", page)
        self.assertIn("Saved map to C:/out/map.png", out)

    def test_png_and_geojson_are_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_cli("map", "41.79,-87.74", "--png", "a.png", "--geojson")

    def test_bad_port_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as exit_info:
            self.run_cli("map", "41.79,-87.74", "--port", "70000")
        self.assertEqual(exit_info.exception.code, 2)
        self.assertIn("port must be 0-65535", err.getvalue())

    def test_map_server_refuses_a_port_already_serving(self):
        taken = socket.socket()
        taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        self.addCleanup(taken.close)
        with self.assertRaises(OSError):
            drones._MapServer(taken.getsockname(), drones.http.server.BaseHTTPRequestHandler)

    def test_radius_is_bounded(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_cli("nearby", "41.79,-87.74", "--radius", "500")
        self.assertEqual(code, 1)
        self.assertIn("--radius", err.getvalue())


# Real AWC records (Ft Lauderdale, 2026-10-25), trimmed to the fields used
KFLL_METAR = {
    "icaoId": "KFLL", "obsTime": 1792972380, "temp": 25, "dewp": 20.6,
    "wdir": 40, "wspd": 13, "wgst": 22, "visib": "10+", "fltCat": "VFR",
    "name": "Ft Lauderdale-Hollywood Intl, FL, US",
    "clouds": [{"cover": "SCT", "base": 2600}, {"cover": "BKN", "base": 3600},
               {"cover": "BKN", "base": 9000}],
    "rawOb": "METAR KFLL 252353Z 04013G22KT 10SM SCT026CB BKN036 BKN090 25/21 A2993 "
             "RMK AO2 RAE16B33E43 SLP135 CB ALQDS MOV SW P0001 60003 T02500206 10278 20239 53002 $",
}
KDEN_TAF = {
    "icaoId": "KDEN", "name": "Denver Intl", "issueTime": "2026-10-25T23:33:00.000Z",
    "validTimeFrom": 1792972800, "validTimeTo": 1793080800, "rawTAF": "TAF KDEN ...",
    "fcsts": [
        {"timeFrom": 1792972800, "timeTo": 1792990800, "fcstChange": None, "probability": None,
         "wdir": 330, "wspd": 12, "wgst": None, "visib": "6+", "wxString": None,
         "clouds": [{"cover": "SCT", "base": 6000}, {"cover": "BKN", "base": 9000}]},
        {"timeFrom": 1792976400, "timeTo": 1792990800, "fcstChange": "PROB", "probability": 30,
         "wdir": None, "wspd": None, "wgst": None, "visib": "", "wxString": "-SHRA",
         "clouds": [{"cover": "BKN", "base": 4000}]},
        {"timeFrom": 1793008800, "timeTo": 1793023200, "fcstChange": "TEMPO", "probability": None,
         "wdir": None, "wspd": None, "wgst": None, "visib": "3/4", "wxString": "BR",
         "clouds": [{"cover": "OVC", "base": 200}]},
    ],
}


def station(ident, lat, lon, kinds=("METAR", "TAF"), name=None):
    return {"icaoId": ident, "site": name or ident, "state": "FL", "lat": lat, "lon": lon,
            "siteType": list(kinds), "elev": 3}


class WeatherDecodeTests(OfflineCase):
    def test_metar_decodes_to_plain_language(self):
        report = drones.decode_metar(KFLL_METAR, max_wind=20, altitude_ft=400,
                                  tz=drones.timezone.utc, now=1792972380 + 420)
        self.assertEqual(report["wind"], "NE 40° at 13 kt (15 mph), gusting 22 kt (25 mph)")
        self.assertEqual(report["visibility"], "10+ statute miles")
        self.assertEqual(report["ceiling_ft"], 3600)
        self.assertEqual(report["category"], "VFR")
        self.assertEqual(report["observed"], "11:53 PM UTC (23:53Z)")
        self.assertEqual(report["age_minutes"], 7)
        self.assertEqual(report["temperature"], "25°C (77°F)")

    def test_cumulonimbus_recovered_from_raw_report(self):
        # AWC's JSON drops the CB on SCT026CB; the raw report keeps it
        report = drones.decode_metar(KFLL_METAR, 20, 400)
        self.assertEqual(report["cloud_layers"][0], {"cover": "SCT", "base_ft": 2600, "type": "CB"})
        self.assertIn("scattered 2,600 ft (cumulonimbus)", report["clouds"])
        self.assertEqual(report["hazards"], ["cumulonimbus in all quadrants, moving SW"])
        statuses = dict((text, status) for status, text in report["checks"])
        self.assertEqual(statuses["gusts 22 kt over your 20 kt limit"], "fail")
        self.assertEqual(statuses["cumulonimbus nearby"], "warn")

    def test_wind_limit_margin_and_calm(self):
        def wind_check(speed, gust, limit=20):
            return drones.drone_checks(speed, gust, 10, [], [], [], limit, 400)[0]
        self.assertEqual(wind_check(8, 20)[0], "warn")      # at the limit
        self.assertEqual(wind_check(8, 21)[0], "fail")
        self.assertEqual(wind_check(5, None), ("ok", "wind 5 kt within your 20 kt limit"))
        self.assertEqual(drones.describe_wind(0, 0, None), "calm")
        self.assertEqual(drones.describe_wind("VRB", 4, None), "variable at 4 kt (5 mph)")

    def test_cloud_clearance_uses_altitude_plus_500(self):
        broken = [{"cover": "BKN", "base_ft": 800, "type": None}]
        scattered = [{"cover": "SCT", "base_ft": 800, "type": None}]
        self.assertEqual(drones.drone_checks(5, None, 10, broken, [], [], 20, 400)[2][0], "fail")
        self.assertEqual(drones.drone_checks(5, None, 10, broken, [], [], 20, 200)[2][0], "ok")
        self.assertEqual(drones.drone_checks(5, None, 10, scattered, [], [], 20, 400)[2][0], "warn")
        self.assertEqual(drones.drone_checks(5, None, 2.5, [], [], [], 20, 400)[1][0], "fail")

    def test_present_weather_codes(self):
        self.assertEqual(drones.describe_weather("-SHRA BR VCSH VCTS +TSRA FZFG"), [
            "light rain showers", "mist", "showers in the vicinity",
            "thunderstorm in the vicinity", "heavy thunderstorm with rain", "freezing fog"])
        self.assertEqual(drones.describe_weather(None), [])

    def test_visibility_and_flight_category(self):
        self.assertEqual(drones._number("1 1/2"), 1.5)
        self.assertEqual(drones._number("6+"), 6)
        self.assertIsNone(drones._number(""))
        self.assertEqual(drones.describe_visibility("1/2"), "1/2 statute miles")
        self.assertEqual(drones.flight_category(3600, 10), "VFR")
        self.assertEqual(drones.flight_category(2500, 10), "MVFR")
        self.assertEqual(drones.flight_category(None, 2), "IFR")
        self.assertEqual(drones.flight_category(200, 0.75), "LIFR")

    def test_taf_overlays_are_labelled_not_replacements(self):
        report = drones.decode_taf(KDEN_TAF, 20, 400, tz=drones.timezone.utc)
        base, chance, tempo = report["periods"]
        self.assertIsNone(base["change"])
        self.assertEqual(chance["change"], "30% chance")
        self.assertIsNone(chance["visibility"])  # '' means unchanged, not missing
        self.assertEqual(chance["weather"], ["light rain showers"])
        self.assertEqual(tempo["change"], "temporarily")
        self.assertEqual(tempo["category"], "LIFR")
        # Overlays only list what changes, so no "wind not reported" noise
        self.assertFalse([c for c in chance["checks"] if "not reported" in c[1]])
        self.assertIn(("fail", "visibility 0.75 SM below the 3 SM minimum"), tempo["checks"])


class LiveLayerTests(OfflineCase):
    def test_aircraft_feature_carries_only_what_the_map_draws(self):
        f = drones.aircraft_feature({"hex": "a1b2c3", "flight": "SWA123  ", "lat": 41.8,
                                     "lon": -87.7, "alt_baro": 2400, "track": 310,
                                     "category": "A3", "seen_pos": 1.2, "squawk": "1200"})
        self.assertEqual(f["id"], "a1b2c3")
        self.assertEqual(f["geometry"]["coordinates"], [-87.7, 41.8])
        self.assertEqual(f["properties"], {"label": "SWA123", "alt_ft": 2400,
                                           "track": 310, "rotorcraft": False, "emergency": False,
                                           "color": "#f46d43"})

    def test_ground_emergency_and_stale_aircraft(self):
        self.assertIsNone(drones.aircraft_feature({"hex": "abc", "lat": 41, "lon": -87,
                                                   "alt_baro": "ground"}))
        emergency = drones.aircraft_feature({"hex": "abc", "r": "N12345", "lat": 41, "lon": -87,
                                             "squawk": "7700"})
        self.assertNotIn("alt_ft", emergency["properties"])
        self.assertEqual(emergency["properties"]["label"], "N12345")
        self.assertEqual(emergency["properties"]["color"], drones.NO_ALTITUDE_COLOR)
        self.assertTrue(emergency["properties"]["emergency"])
        self.assertIsNone(drones.aircraft_feature({"hex": "abc", "lat": 41, "lon": -87,
                                                   "seen_pos": 120}))
        self.assertIsNone(drones.aircraft_feature({"hex": "abc"}))

    def test_traffic_client_caps_the_radius_and_reports_throttling(self):
        client = drones.TrafficClient()
        client.session = Mock()
        client.session.get.return_value = Mock(status_code=200, json=lambda: {"ac": [
            {"hex": "a", "lat": 41, "lon": -87, "alt_baro": 900}, {"hex": "b"}]})
        with patch.object(drones.time, "sleep"):
            data = client.nearby(41.123456, -87.654321, 400)
            self.assertEqual([f["id"] for f in data["features"]], ["a"])
            self.assertTrue(client.session.get.call_args.args[0]
                            .endswith("/lat/41.1235/lon/-87.6543/dist/250"))
            client.session.get.return_value = Mock(status_code=429)
            with self.assertRaisesRegex(drones.DronesError, "rate limiting"):
                client.nearby(41, -87, 5)

    def test_traffic_route_validates_the_query(self):
        client = Mock()
        route = drones._traffic_route(client)
        route({"lat": ["41.8"], "lng": ["-87.7"], "dist": ["12.5"]})
        client.nearby.assert_called_once_with(41.8, -87.7, 12.5)
        for bad in ({"lat": ["x"], "lng": ["0"], "dist": ["1"]}, {"lat": ["41"], "lng": ["-87"]},
                    {"lat": ["41"], "lng": ["-87"], "dist": ["900"]}):
            with self.assertRaises(ValueError):
                route(bad)

    def test_radar_times_from_capabilities(self):
        doc = ('<Layer><Dimension name="time" units="ISO8601">2026-10-26T12:02:00Z,'
               '2026-10-26T12:00:00Z, 2026-10-26T12:04:00Z</Dimension></Layer>')
        self.assertEqual(drones.radar_times(doc)[-1], "2026-10-26T12:04:00Z")
        self.assertEqual(drones.radar_times("<Layer/>"), [])

    def test_map_page_carries_live_layers_only_when_asked(self):
        location = {"query": "Midway", "latitude": 41.79, "longitude": -87.74}
        plain = drones.map_page(location, "street")
        self.assertIn("const TRAFFIC = null;", plain)
        self.assertIn("const RADAR = null;", plain)
        live = drones.map_page(location, "street", adsb=True, radar=True)
        self.assertIn(drones.RADAR_LAYER, live)
        self.assertIn('"max_dist_nm": 250', live)
        self.assertNotIn("__", live.split("<script>")[1].split("const LABEL_ZOOM")[0])
        self.assertEqual(set(drones.live_routes(True, True)), {"/traffic.json", "/radar.json"})
        self.assertEqual(drones.live_routes(False, False), {})


class WeatherClientTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.client = drones.WeatherClient()
        self.client._get = Mock()

    def test_stations_drop_sensors_and_sort_by_distance(self):
        self.client._get.return_value = [
            station("KFLL", 26.0787, -80.1622), station("KPMP", 26.2464, -80.1110, ("METAR",)),
            station("PVGF1", 26.092, -80.109, ()), station("KHWO", 25.9996, -80.2412)]
        found = self.client.stations(26.0112, -80.1495, 20)
        self.assertEqual([s["id"] for s in found], ["KFLL", "KHWO", "KPMP"])
        self.assertEqual([s["has_taf"] for s in found], [True, True, False])
        self.assertEqual(self.client._get.call_args.args[0], "stationinfo")

    def test_candidates_widen_until_a_taf_station_appears(self):
        self.client._get.side_effect = [
            [station("KPMP", 26.2464, -80.1110, ("METAR",))],
            [station("KPMP", 26.2464, -80.1110, ("METAR",)), station("KFLL", 26.0787, -80.1622)]]
        found = self.client.candidates(26.0112, -80.1495, need_taf=True)
        self.assertEqual([s["id"] for s in found], ["KFLL"])
        self.assertEqual(self.client._get.call_count, 2)

    def test_no_content_and_throttling(self):
        client = drones.WeatherClient()
        client.session = Mock()
        client.session.get.return_value = Mock(status_code=204)
        self.assertEqual(client._get("metar", ids="XXXX"), [])
        client.session.get.return_value = Mock(status_code=429)
        with patch.object(drones.time, "sleep"), self.assertRaisesRegex(drones.DronesError, "rate limiting"):
            client._get("metar", ids="KFLL")


class WeatherCommandTests(OfflineCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(drones, "utf8_output"):
            code = drones.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_station_ids_keep_the_requested_order(self):
        client = Mock()
        client.metars.return_value = [{**KFLL_METAR, "icaoId": "KDEN"}, KFLL_METAR]
        with patch.object(drones, "WeatherClient", return_value=client):
            code, out, _ = self.run_cli("metar", "kfll,kden", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([r["metar"]["station"] for r in json.loads(out)], ["KFLL", "KDEN"])
        client.metars.assert_called_once_with(["KFLL", "KDEN"])

    def test_place_uses_nearest_station_that_reported(self):
        client = Mock()
        client.candidates.return_value = [
            {"id": "KHWO", "name": "North Perry", "distance_nm": 3.0, "direction": "WSW"},
            {"id": "KFLL", "name": "Ft Lauderdale", "distance_nm": 4.1, "direction": "NNE"}]
        client.tafs.return_value = [{**KDEN_TAF, "icaoId": "KFLL"}]  # KHWO is part time
        with patch.object(drones, "WeatherClient", return_value=client), \
                patch.object(drones, "_locate", return_value={"query": "here", "latitude": 26.0,
                                                           "longitude": -80.2, "address": None}):
            code, out, err = self.run_cli("taf", "Hollywood, FL", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["station"]["id"], "KFLL")
        self.assertIn("KHWO has no current TAF; using KFLL", err)
        client.tafs.assert_called_once_with(["KHWO", "KFLL"])

    def test_bad_limits_fail_before_any_request(self):
        with patch.object(drones, "WeatherClient") as client:
            code, _, err = self.run_cli("metar", "KFLL", "--max-wind", "0")
        self.assertEqual(code, 1)
        self.assertIn("--max-wind", err)
        client.assert_not_called()


class PreflightTests(OfflineCase):
    NOW = drones.datetime(2026, 10, 25, 20, 0, tzinfo=drones.timezone.utc)
    HERE = {"query": "here", "latitude": 26.0, "longitude": -80.2, "address": None}

    def test_parse_when(self):
        now = self.NOW
        self.assertEqual(drones.parse_when("now", now), now)
        self.assertEqual(drones.parse_when("9pm", now).hour, 21)
        self.assertEqual(drones.parse_when("9pm", now).day, 25)
        self.assertEqual(drones.parse_when("7am", now).day, 26)        # passed today -> tomorrow
        self.assertEqual(drones.parse_when("tomorrow 7:30am", now).minute, 30)
        self.assertEqual(drones.parse_when("12am", now).hour, 0)
        self.assertEqual(drones.parse_when("12pm", now).hour, 12)
        self.assertEqual(drones.parse_when("2026-10-26T14:00+00:00", now).hour, 14)
        for bad in ["soon", "25:00", "13pm"]:
            with self.subTest(value=bad), self.assertRaises(drones.DronesError):
                drones.parse_when(bad, now)

    def test_taf_periods_at_picks_prevailing_then_overlays(self):
        periods = drones.taf_periods_at(KDEN_TAF, drones.datetime.fromtimestamp(1792980000, drones.timezone.utc))
        self.assertEqual([p["fcstChange"] for p in periods], [None, "PROB"])
        later = drones.taf_periods_at(KDEN_TAF, drones.datetime.fromtimestamp(1793010000, drones.timezone.utc))
        self.assertEqual([p["fcstChange"] for p in later], ["TEMPO"])

    def test_weather_verdict(self):
        self.assertEqual(drones.weather_verdict([("ok", "fine")], []), ("GO", []))
        self.assertEqual(drones.weather_verdict([("fail", "gusts")], [])[0], "NO-GO")
        self.assertEqual(drones.weather_verdict([("ok", "x")], [("fail", "fog")]),
                         ("CAUTION", ["possible: fog"]))
        self.assertEqual(drones.weather_verdict([("warn", "rain")], [])[0], "CAUTION")

    def run_preflight(self, airspace_report, metar, at=None):
        client = Mock()
        client.candidates.return_value = [{"id": "KFLL", "name": "Ft Lauderdale",
                                           "distance_nm": 4.1, "direction": "NNE"}]
        client.metars.return_value = [metar] if metar else []
        client.tafs.return_value = []
        with patch.object(drones, "check_point", return_value=airspace_report), \
                patch.object(drones, "WeatherClient", return_value=client):
            return drones.preflight(26.0, -80.2, at or self.NOW, 20, 400, now=self.NOW)

    def airspace(self, verdict="CLEAR", ceiling=None, restrictions=()):
        return {"verdict": verdict, "notes": ["LAANC available up to 300 ft"],
                "laanc": {"ceiling_ft": ceiling} if ceiling is not None else None,
                "airspace": [], "restrictions": list(restrictions), "stadiums": []}

    @staticmethod
    def tfr(status):
        return drones.tfr_entry({"type": "VIP", "title": "VIP TFR", "notam": "6/1", "status": status,
                              "begins": "2026-10-27T14:30:00+00:00", "ends": "2026-10-27T20:00:00+00:00"})

    def test_gusts_over_the_limit_are_no_go(self):
        report = self.run_preflight(self.airspace(), KFLL_METAR)
        self.assertEqual(report["verdict"], "NO-GO")
        self.assertIn("gusts 22 kt over your 20 kt limit", report["reasons"])
        self.assertEqual(report["weather"]["source"], "METAR")

    def test_tfr_overrides_good_weather_and_laanc_is_a_requirement(self):
        calm = {**KFLL_METAR, "wspd": 5, "wgst": None, "rawOb": "METAR KFLL 252353Z 04005KT 10SM CLR"}
        report = self.run_preflight(self.airspace("DO NOT FLY", restrictions=[self.tfr("active")]), calm)
        self.assertEqual(report["verdict"], "NO-GO")
        self.assertEqual(report["reasons"][0], "airspace: TFR (vip) - VIP TFR (active until "
                         + drones._local_moment("2026-10-27T20:00:00+00:00") + ")")
        report = self.run_preflight(self.airspace("AUTHORIZATION REQUIRED", ceiling=300), calm)
        self.assertEqual(report["verdict"], "GO")
        self.assertIn("LAANC ceiling 300 ft is below your planned 400 ft", report["requirements"])

    def test_scheduled_tfr_in_controlled_airspace_still_cautions(self):
        calm = {**KFLL_METAR, "wspd": 5, "wgst": None, "rawOb": "METAR KFLL 252353Z 04005KT 10SM CLR"}
        report = self.run_preflight(self.airspace("AUTHORIZATION REQUIRED", ceiling=400,
                                                  restrictions=[self.tfr("scheduled")]), calm)
        self.assertEqual(report["verdict"], "CAUTION")
        self.assertTrue(report["reasons"][0].startswith("airspace: TFR (vip) - VIP TFR (scheduled "))

    def test_time_window_is_checked(self):
        with self.assertRaisesRegex(drones.DronesError, "30 hours"):
            self.run_preflight(self.airspace(), KFLL_METAR, at=self.NOW + drones.timedelta(hours=40))

    def test_no_go_exits_3(self):
        no_go = {"verdict": "NO-GO", "reasons": ["gusts"], "requirements": [], "at": "2026-10-25T20:00+00:00",
                 "airspace": self.airspace(), "weather": {"metar": None, "taf_periods": []}}
        out = io.StringIO()
        with patch.object(drones, "preflight", return_value=no_go), \
                patch.object(drones, "_locate", return_value=self.HERE), \
                patch.object(drones, "utf8_output"), contextlib.redirect_stdout(out):
            code = drones.main(["preflight", "26.0,-80.2", "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out.getvalue())["verdict"], "NO-GO")


NOTAM_TEXT = """<td>Beginning Date and Time     :</td><td>October 27, 2026 at 1430 UTC</td>
<td>Ending Date and Time     :</td><td>October 27, 2026 at 2000 UTC</td>
<p>Area A</p><td>Altitude:</td><td>From the surface  up to and including 4999 feet AGL</td>
<p>Area B</p><td>Altitude:</td><td>From the surface  up to and including 3000 feet AGL</td>"""


class TFRTimeTests(OfflineCase):
    def test_parses_window_and_highest_altitude(self):
        details = drones.parse_tfr_text(NOTAM_TEXT)
        self.assertEqual(details["begins"], drones.datetime(2026, 10, 27, 14, 30, tzinfo=drones.timezone.utc))
        self.assertEqual(details["ends"], drones.datetime(2026, 10, 27, 20, 0, tzinfo=drones.timezone.utc))
        self.assertEqual((details["floor"], details["ceiling"]), ("SFC", "4,999 ft AGL"))

    def test_permanent_and_immediate(self):
        details = drones.parse_tfr_text("<td>Beginning Date and Time :</td><td>Effective Immediately</td>"
                                     "<td>Ending Date and Time :</td><td>Permanent</td>")
        self.assertTrue(details["immediate"] and details["permanent"])
        self.assertEqual(drones.tfr_status(details, drones.datetime.now(drones.timezone.utc)), "active")

    def test_status_at_a_time(self):
        details = drones.parse_tfr_text(NOTAM_TEXT)
        at = lambda h: drones.datetime(2026, 10, 27, h, 0, tzinfo=drones.timezone.utc)
        self.assertEqual(drones.tfr_status(details, at(12)), "scheduled")
        self.assertEqual(drones.tfr_status(details, at(15)), "active")
        self.assertEqual(drones.tfr_status(details, at(21)), "expired")
        self.assertEqual(drones.tfr_status(None, at(15)), "unknown")

    def test_scheduled_tfr_is_caution_and_expired_is_dropped(self):
        client = drones.FAAClient()
        shapes = [feature({"notam": "6/1", "type": "VIP", "title": "VIP TFR"}, SQUARE),
                  feature({"notam": "6/2", "type": "HAZARDS", "title": "Old fire"}, SQUARE)]
        windows = {"6/1": drones.parse_tfr_text(NOTAM_TEXT),
                   "6/2": {**drones.parse_tfr_text(NOTAM_TEXT),
                           "begins": drones.datetime(2026, 10, 25, tzinfo=drones.timezone.utc),
                           "ends": drones.datetime(2026, 10, 26, tzinfo=drones.timezone.utc)}}
        with patch.object(client, "tfr_details", side_effect=windows.get):
            kept = drones.annotate_tfrs(client, shapes, drones.datetime(2026, 10, 27, 12, tzinfo=drones.timezone.utc))
        self.assertEqual([f["properties"]["notam"] for f in kept], ["6/1"])
        entry = drones.tfr_entry(kept[0]["properties"])
        self.assertEqual((entry["status"], entry["severity"]), ("scheduled", "caution"))
        self.assertTrue(entry["hours"].startswith("scheduled "))
        verdict, notes = drones.assess([], None, [entry], [])
        self.assertEqual(verdict, "CAUTION")
        self.assertIn("scheduled but not active", " ".join(notes))

    def test_failed_lookup_stays_no_fly(self):
        client = drones.FAAClient()
        with patch.object(client, "tfr_details", side_effect=drones.DronesError("down")):
            kept = drones.annotate_tfrs(client, [feature({"notam": "6/1", "type": "SECURITY"}, SQUARE)])
        entry = drones.tfr_entry(kept[0]["properties"])
        self.assertEqual((entry["status"], entry["severity"]), ("unknown", "no-fly"))


if __name__ == "__main__":
    unittest.main()
