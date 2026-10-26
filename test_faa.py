"""Offline regression tests for faa.py (no package install needed)."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parent / "faa.py"
SPEC = importlib.util.spec_from_file_location("faa_cli_under_test", SCRIPT)
faa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(faa)


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
        self.assertEqual(faa.Locator.parse_coordinates("41.79,-87.74"), (41.79, -87.74))
        self.assertEqual(faa.Locator.parse_coordinates(" -33.86 151.21 "), (-33.86, 151.21))
        self.assertIsNone(faa.Locator.parse_coordinates("Chicago Midway"))

    def test_out_of_range_coordinates_fail(self):
        with self.assertRaises(faa.FAAError):
            faa.Locator.parse_coordinates("91,10")

    def test_negative_latitude_survives_argparse(self):
        parser = faa.build_parser()
        argv = faa._normalize_coordinate_args(
            ["airspace", "-33.86,151.21", "--json"], faa._value_option_strings(parser))
        args = parser.parse_args(argv)
        self.assertEqual(args.location, "-33.86,151.21")
        self.assertTrue(args.json)

    def test_coordinates_skip_geocoding(self):
        with patch.object(faa, "Nominatim"):
            locator = faa.Locator()
        locator._lookup = Mock(side_effect=AssertionError("no lookup expected"))
        location = locator.locate("41.79,-87.74", lookup_address=False)
        self.assertEqual((location["latitude"], location["longitude"]), (41.79, -87.74))
        self.assertIsNone(location["address"])


class GeometryTests(OfflineCase):
    def test_contains_honours_holes(self):
        self.assertTrue(faa.contains(SQUARE, 41.5, -87.5))
        self.assertFalse(faa.contains(DONUT, 41.5, -87.5))
        self.assertTrue(faa.contains(DONUT, 41.1, -87.9))
        self.assertFalse(faa.contains(SQUARE, 43, -87.5))

    def test_distance_is_zero_inside_and_to_edge_outside(self):
        self.assertEqual(faa.distance_nm(SQUARE, 41.5, -87.5), 0.0)
        # One degree of latitude north of the top edge is 60 NM
        self.assertAlmostEqual(faa.distance_nm(SQUARE, 43, -87.5), 60, delta=0.5)

    def test_point_distance_and_bearing(self):
        point = {"type": "Point", "coordinates": [-87.74, 41.89]}
        self.assertAlmostEqual(faa.distance_nm(point, 41.79, -87.74), 6.0, delta=0.05)
        self.assertEqual(faa.bearing_to_cardinal(
            faa.calculate_bearing(41.79, -87.74, 41.89, -87.74)), "N")

    def test_circle_ring_is_closed(self):
        ring = faa.circle_ring(41.79, -87.74, 3)
        self.assertEqual(ring[0], ring[-1])
        self.assertEqual(len(ring), 73)


class InterpretationTests(OfflineCase):
    def test_format_altitude(self):
        self.assertEqual(faa.format_altitude(0, "FT", "SFC"), "SFC")
        self.assertEqual(faa.format_altitude("3600", "FT", "MSL"), "3,600 ft MSL")
        self.assertEqual(faa.format_altitude(600, "FL", "STD"), "FL600")
        self.assertEqual(faa.format_altitude(-9998, None, None), "Class A")

    def test_laanc_entry_names_programs(self):
        entry = faa.laanc_entry(grid_cell(ceiling=200))
        self.assertEqual(entry["ceiling_ft"], 200)
        self.assertEqual(entry["airports"][0]["ident"], "KMDW")
        self.assertIn("Recreational auto approval", entry["airports"][0]["programs"])

    def test_high_sua_floor_is_overhead_not_caution(self):
        moa = {"NAME": "HOWARD EAST MOA", "TYPE_CODE": "MOA", "LOWER_VAL": "9000",
               "LOWER_UOM": "FT", "LOWER_CODE": "MSL"}
        restricted = {**moa, "NAME": "R-1", "TYPE_CODE": "R", "LOWER_VAL": "0", "LOWER_CODE": "SFC"}
        prohibited = {**moa, "NAME": "P-56", "TYPE_CODE": "P"}
        self.assertEqual(faa.sua_entry(moa)["severity"], "overhead")
        self.assertEqual(faa.sua_entry(restricted)["severity"], "caution")
        self.assertEqual(faa.sua_entry(prohibited)["severity"], "no-fly")

    def test_defense_airspace_by_notam_is_caution(self):
        entry = faa.defense_entry({"NAME": "X", "WKHR_CODE": "RMK", "WKHR_RMK": "BY NOTAM"})
        self.assertEqual(entry["severity"], "caution")

    def test_verdicts(self):
        controlled = [faa.class_entry(class_c())]
        laanc = faa.laanc_entry(grid_cell(ceiling=200))
        verdict, notes = faa.assess(controlled, laanc, [], [], [])
        self.assertEqual(verdict, "AUTHORIZATION REQUIRED")
        self.assertIn("200 ft AGL", notes[0])

        verdict, _ = faa.assess([], None, [], [], [])
        self.assertEqual(verdict, "CLEAR")

        overhead = [{"severity": "overhead"}]
        self.assertEqual(faa.assess([], None, overhead, [], [])[0], "CLEAR")
        self.assertEqual(faa.assess([], None, [{"severity": "caution"}], [], [])[0], "CAUTION")
        self.assertEqual(faa.assess(controlled, laanc, [{"severity": "no-fly"}], [], [])[0],
                         "DO NOT FLY")

    def test_zero_ceiling_and_non_laanc_airports(self):
        self.assertIn("0 ft", faa.authorization_note(faa.laanc_entry(grid_cell(0))))
        self.assertIn("FAADroneZone", faa.authorization_note(
            faa.laanc_entry(grid_cell(100, laanc=0))))
        self.assertIn("FAADroneZone", faa.authorization_note(None))

    def test_class_shelf_is_not_surface(self):
        self.assertTrue(faa.class_entry(class_c())["surface"])
        self.assertFalse(faa.class_entry(class_c("1900", "MSL"))["surface"])


class FAAClientTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.client = faa.FAAClient()
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
        with self.assertRaisesRegex(faa.FAAError, "Invalid query"):
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
        self.assertEqual(faa.parse_size("1920x1080"), (1920, 1080))
        with self.assertRaises(faa.argparse.ArgumentTypeError):
            faa.parse_size("big")

    def test_browser_override_wins(self):
        with patch.dict(faa.os.environ, {"FAA_BROWSER": "/opt/chrome"}):
            self.assertEqual(faa.find_browser(), "/opt/chrome")

    def test_missing_browser_fails_clearly(self):
        with patch.object(faa, "find_browser", return_value=None):
            with self.assertRaisesRegex(faa.FAAError, "FAA_BROWSER"):
                faa.screenshot_map("<html>", {}, "x.png", (800, 600))

    def test_runs_headless_browser_against_local_server(self):
        server = Mock(server_address=("127.0.0.1", 54321))

        def browser(command, **kwargs):
            shot = next(a for a in command if a.startswith("--screenshot="))
            Path(shot.split("=", 1)[1]).write_bytes(b"png")
            return Mock(stdout="", stderr="")

        with patch.object(faa, "find_browser", return_value="chrome"), \
                patch.object(faa, "_map_server", return_value=server), \
                patch.object(faa.threading, "Thread"), \
                patch.object(faa.subprocess, "run", side_effect=browser) as run, \
                faa.tempfile.TemporaryDirectory() as out:
            saved = faa.screenshot_map("<html>", {}, str(Path(out) / "m.png"), (800, 600))
            self.assertTrue(Path(saved).is_file())

        command = run.call_args.args[0]
        self.assertEqual(command[-1], "http://127.0.0.1:54321/")
        self.assertIn("--window-size=800,600", command)
        self.assertIn("--headless=new", command)
        server.shutdown.assert_called_once()

    def test_no_file_written_is_an_error(self):
        server = Mock(server_address=("127.0.0.1", 54321))
        with patch.object(faa, "find_browser", return_value="chrome"), \
                patch.object(faa, "_map_server", return_value=server), \
                patch.object(faa.threading, "Thread"), \
                patch.object(faa.subprocess, "run",
                             return_value=Mock(stdout="", stderr="GPU crashed")), \
                faa.tempfile.TemporaryDirectory() as out:
            with self.assertRaisesRegex(faa.FAAError, "GPU crashed"):
                faa.screenshot_map("<html>", {}, str(Path(out) / "m.png"), (800, 600))


class ThrottleTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.client = faa.FAAClient()
        self.client.session = Mock()
        self.sleep = patch.object(faa.time, "sleep").start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def response(status=200, body=None):
        return Mock(status_code=status, json=Mock(return_value=body or {}),
                    raise_for_status=Mock())

    def test_retries_arcgis_throttle_message(self):
        throttled = self.response(body={"error": {"message": "Unable to perform query. Too many requests."}})
        self.client.session.get.side_effect = [throttled, self.response(body={"features": []})]
        self.assertEqual(self.client._get_json("u", {}), {"features": []})
        self.sleep.assert_called_once_with(faa.RETRY_DELAYS[0])

    def test_retries_http_429_then_gives_up(self):
        self.client.session.get.return_value = self.response(status=429)
        with self.assertRaisesRegex(faa.FAAError, "rate limiting"):
            self.client._get_json("u", {})
        self.assertEqual(self.sleep.call_count, len(faa.RETRY_DELAYS))

    def test_other_errors_are_not_retried(self):
        self.client.session.get.return_value = self.response(body={"error": {"message": "Invalid query"}})
        self.assertEqual(self.client._get_json("u", {})["error"]["message"], "Invalid query")
        self.sleep.assert_not_called()


def fake_gather(results):
    def gather(faa, lat, lng, wanted):
        return {name: results.get(name, []) for name in wanted}
    return gather


class CommandTests(OfflineCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(faa, "utf8_output"):
            code = faa.main(list(argv))
        return code, out.getvalue()

    def test_airspace_json_inside_tfr(self):
        results = {
            "laanc": [feature(grid_cell(ceiling=100))],
            "class": [feature(class_c())],
            "tfrs": [feature({"notam": "6/1", "type": "SECURITY", "title": "TEST TFR",
                              "url": "u"}, SQUARE)],
        }
        with patch.object(faa, "gather", fake_gather(results)):
            code, out = self.run_cli("airspace", "41.5,-87.5", "--no-address", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["verdict"], "DO NOT FLY")
        self.assertEqual(report["laanc"]["ceiling_ft"], 100)
        self.assertEqual(report["restrictions"][0]["kind"], "TFR (security)")

    def test_airspace_text_class_g(self):
        with patch.object(faa, "gather", fake_gather({})):
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
        with patch.object(faa, "gather", fake_gather(results)):
            code, out = self.run_cli("map", "41.79,-87.74", "--no-address", "--geojson")
        self.assertEqual(code, 0)
        layers = [f["properties"]["_layer"] for f in json.loads(out)["features"]]
        self.assertEqual(layers, ["laanc", "class", "airports", "radius", "location"])
        known = {key for key, _ in faa.MAP_OVERLAYS}
        self.assertTrue(set(layers) <= known)

    def test_renewed_tfrs_on_one_shape_draw_once(self):
        def tfr(notam, title):
            return feature({"notam": notam, "type": "SECURITY", "title": title,
                            "url": f"https://tfr.faa.gov/tfr3/?page=detail_{notam.replace('/', '_')}"},
                           SQUARE)
        results = {"tfrs": [tfr("5/2809", "Palm Beach, ending"), tfr("6/0100", "Palm Beach, renewal")]}
        with patch.object(faa, "gather", fake_gather(results)):
            code, out = self.run_cli("map", "41.5,-87.5", "--no-address", "--geojson")
        self.assertEqual(code, 0)
        tfrs = [f for f in json.loads(out)["features"] if f["properties"]["_layer"] == "tfrs"]
        self.assertEqual(len(tfrs), 1)
        props = tfrs[0]["properties"]
        self.assertIn("2 NOTAMs", props["title"])
        self.assertEqual([label for label, _ in props["_links"]], ["NOTAM 5/2809", "NOTAM 6/0100"])
        self.assertEqual(props["NOTAM 6/0100"], "Palm Beach, renewal")

    def test_png_screenshots_instead_of_serving(self):
        with patch.object(faa, "gather", fake_gather({})), \
                patch.object(faa, "screenshot_map", return_value="C:/out/map.png") as shot, \
                patch.object(faa, "serve_map") as serve:
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
            faa._MapServer(taken.getsockname(), faa.http.server.BaseHTTPRequestHandler)

    def test_radius_is_bounded(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_cli("nearby", "41.79,-87.74", "--radius", "500")
        self.assertEqual(code, 1)
        self.assertIn("--radius", err.getvalue())


if __name__ == "__main__":
    unittest.main()
