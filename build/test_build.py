"""Filter rules. Run: python -m unittest test_build (in this folder)."""
import base64
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import shapely
import zstandard
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import check
import publish
import recall

from build import clearance_values, near_roads, node_hazard, parse_incline, parse_metres, parse_tonnes, steep_spots, way_ends
from build import DEFAULTS, CELL_E6, UNLIMITED, boundary_outline, build_cameras, camera_direction, cameras_inside, camera_limit, cell_id, cut_into_cells, default_byte, default_for, encode_cell, parse_limit, parse_outline, road_limits, side_from_tags, simplify, way_touches


class Cut(unittest.TestCase):
    area = shapely.box(11.0, 46.0, 12.0, 47.0)

    def test_road_crossing_the_border_stays(self):
        self.assertTrue(way_touches(self.area, [(46_500_000, 11_900_000), (46_500_000, 12_100_000)]))
        self.assertTrue(way_touches(self.area, [(46_500_000, 10_900_000), (46_500_000, 12_100_000)]))
        self.assertFalse(way_touches(self.area, [(47_500_000, 11_500_000), (47_600_000, 11_500_000)]))

    def test_cameras_and_sections(self):
        cameras = [("in", 46.5, 11.5), ("out", 47.5, 11.5)]
        sections = [("start", 46.5, 11.5, 47.5, 11.5), ("end", 47.5, 11.5, 46.5, 11.5), ("out", 47.5, 11.5, 47.6, 11.5)]
        inside, parts = cameras_inside(self.area, cameras, sections)
        self.assertEqual(["in"], [c[0] for c in inside])
        self.assertEqual(["start", "end"], [s[0] for s in parts])

    def test_outline_is_lat_lon(self):
        ring = boundary_outline(self.area)[0]
        self.assertEqual({46.0, 47.0}, set(ring[0::2]))
        self.assertEqual({11.0, 12.0}, set(ring[1::2]))


class ParseLimit(unittest.TestCase):
    def test_numbers(self):
        for value, kmh in [("50", 50), ("130", 130), ("50 km/h", 50), ("30 mph", 48)]:
            self.assertEqual(kmh, parse_limit(value), value)

    def test_zones(self):
        for value, kmh in [("IT:urban", 50), ("IT:rural", 90), ("IT:zone30", 30), ("IT:zone:30", 30),
                           ("IT:40", 40), ("DE:rural", 100), ("CH:rural", 80)]:
            self.assertEqual(kmh, parse_limit(value), value)

    def test_nothing_clear(self):
        for value in [None, "sign", "none", "walk", "90;60", "0"]:
            self.assertIsNone(parse_limit(value), value)


class RoadLimits(unittest.TestCase):
    def test_direction_wins(self):
        self.assertEqual((70, 50), road_limits({"maxspeed": "50", "maxspeed:forward": "70"}))

    def test_zone_tags_are_the_fallback(self):
        self.assertEqual((30, 30), road_limits({"maxspeed": "sign", "zone:maxspeed": "DE:30"}))
        self.assertEqual((None, None), road_limits({}))


    def test_no_limit_is_not_unknown(self):
        self.assertEqual((UNLIMITED, UNLIMITED), road_limits({"maxspeed": "none", "source:maxspeed": "DE:rural"}))
        self.assertEqual((UNLIMITED, 80), road_limits({"maxspeed": "none", "maxspeed:backward": "80"}))
        self.assertEqual((None, None), road_limits({"maxspeed": "signals"}))


class Defaults(unittest.TestCase):
    RULES = {"urban": {"tertiary": 50}, "rural": {"tertiary": 100}}

    def test_byte_values_do_not_touch_tagged_ones(self):
        self.assertEqual(176, default_byte(50))
        self.assertEqual(201, default_byte(100))
        self.assertEqual(151 + 65, default_byte(130))

    def test_only_listed_classes_and_only_real_gaps(self):
        self.assertEqual(176, default_for(self.RULES, "tertiary", True, False))
        self.assertEqual(201, default_for(self.RULES, "tertiary", False, False))
        self.assertEqual(0, default_for(self.RULES, "unclassified", False, False))
        self.assertEqual(0, default_for(self.RULES, "tertiary", False, True))
        self.assertEqual(0, default_for({}, "tertiary", False, False))

    def test_italy_has_no_rural_default(self):
        rules = DEFAULTS["IT"]
        self.assertEqual(default_for(rules, "primary", True, False), default_byte(50))
        self.assertEqual(default_for(rules, "residential", True, False), default_byte(30))
        self.assertEqual(default_for(rules, "primary", False, False), 0)
        self.assertEqual(default_for(rules, "tertiary", True, False), 0)

    def test_austria_has_no_rural_default(self):
        rules = DEFAULTS["AT"]
        self.assertEqual(default_for(rules, "tertiary", True, False), default_byte(50))
        self.assertEqual(default_for(rules, "primary", False, False), 0)
        self.assertEqual(default_for(rules, "unclassified", True, False), 0)

    def test_switzerland_rural_default(self):
        rules = DEFAULTS["CH"]
        self.assertEqual(default_for(rules, "residential", True, False), default_byte(30))
        self.assertEqual(default_for(rules, "tertiary", False, False), default_byte(80))
        self.assertEqual(default_for(rules, "unclassified", False, False), 0)

    def test_side_from_tags(self):
        self.assertIs(True, side_from_tags("tertiary", {"zone:traffic": "DE:urban"}))
        self.assertIs(False, side_from_tags("tertiary", {"zone:traffic": "DE:rural", "lit": "yes"}))
        self.assertIs(True, side_from_tags("residential", {}))
        self.assertIs(True, side_from_tags("tertiary", {"sidewalk": "both"}))
        self.assertIsNone(side_from_tags("tertiary", {"sidewalk": "no", "lit": "no"}))


class Cameras(unittest.TestCase):
    def test_values(self):
        self.assertEqual(50, camera_limit("50 km/h"))
        self.assertIsNone(camera_limit("DE:urban"))
        self.assertEqual(90.0, camera_direction("90"))
        self.assertEqual(22.5, camera_direction("nne"))
        self.assertIsNone(camera_direction("forward"))

    def test_section_devices_are_no_cameras(self):
        nodes = {1: (48.0, 11.0, {"highway": "speed_camera"}), 2: (48.1, 11.0, {}), 3: (48.2, 11.0, {"highway": "speed_camera", "maxspeed": "60"})}
        relations = [(9, {"enforcement": "average_speed", "maxspeed": "100"}, [(1, "device"), (2, "to")])]
        cameras, sections = build_cameras(relations, nodes)
        self.assertEqual([("osm:9", 48.0, 11.0, 48.1, 11.0, 100)], sections)
        self.assertEqual(["osm:3"], [c[0] for c in cameras])

    def test_relation_gives_limit_and_direction(self):
        nodes = {1: (48.0, 11.0, {"highway": "speed_camera"}), 2: (48.0, 11.0, {}), 3: (49.0, 11.0, {})}
        relations = [(9, {"enforcement": "maxspeed", "maxspeed": "80"}, [(1, "device"), (2, "from"), (3, "to")])]
        cameras, _ = build_cameras(relations, nodes)
        self.assertEqual(80, cameras[0][3])
        self.assertAlmostEqual(0.0, cameras[0][4])


class Hazards(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual("rail", node_hazard({"railway": "level_crossing"}))
        self.assertEqual("animal", node_hazard({"hazard": "animal_crossing"}))
        self.assertIsNone(node_hazard({"railway": "crossing"}))
        self.assertIsNone(node_hazard({"hazard": "low_flying_aircraft"}))

    def test_incline(self):
        self.assertEqual(12.0, parse_incline("12%"))
        self.assertEqual(-8.0, parse_incline("-8 %"))
        self.assertAlmostEqual(17.6, parse_incline("10°"), places=1)
        self.assertIsNone(parse_incline("up"))

    def test_steep_spot_is_the_top_pointing_down(self):
        points = [(48000000, 11000000), (48001000, 11000000)]
        falling = steep_spots(5, {"incline": "-12%"}, points)
        self.assertEqual([("osm:w5:s", 48.0, 11.0, 0.0, 12.0)], falling)
        rising = steep_spots(5, {"incline": "12%"}, points)
        self.assertEqual("osm:w5:e", rising[0][0])
        self.assertAlmostEqual(180.0, rising[0][3])
        self.assertEqual([], steep_spots(5, {"incline": "12%", "oneway": "yes"}, points))
        self.assertEqual([], steep_spots(5, {"incline": "6%"}, points))

    def test_one_way_is_entered_at_one_end(self):
        points = [(48000000, 11000000), (48001000, 11000000)]
        self.assertEqual(2, len(way_ends(1, {}, points)))
        self.assertEqual(["osm:w1:s"], [e[0] for e in way_ends(1, {"oneway": "yes"}, points)])
        self.assertEqual(["osm:w1:e"], [e[0] for e in way_ends(1, {"oneway": "-1"}, points)])
        self.assertEqual(1, len(way_ends(1, {"highway": "motorway"}, points)))

    def test_schools_only_near_roads(self):
        road = [(11.0, 48.0), (11.01, 48.0)]
        schools = [("osm:a1", 48.0003, 11.005), ("osm:a2", 48.01, 11.005)]
        self.assertEqual(["osm:a1"], [h[0] for h in near_roads(schools, road, [0, 0])])


class Clearance(unittest.TestCase):
    def test_metres(self):
        self.assertEqual(3.5, parse_metres("3.5"))
        self.assertEqual(3.5, parse_metres("3,5 m"))
        self.assertEqual(3.51, parse_metres("11'6\""))
        self.assertIsNone(parse_metres("default"))
        self.assertIsNone(parse_metres("none"))
        self.assertIsNone(parse_metres("350"))

    def test_tonnes(self):
        self.assertEqual(7.5, parse_tonnes("7.5"))
        self.assertEqual(3.5, parse_tonnes("3500 kg"))
        self.assertEqual(12.0, parse_tonnes("12 t"))
        self.assertIsNone(parse_tonnes("3 axles"))

    def test_physical_height_is_the_fallback(self):
        self.assertEqual((3.8, None, None), clearance_values({"maxheight": "default", "maxheight:physical": "3.8"}))
        self.assertEqual((None, 7.5, 2.2), clearance_values({"maxweight": "7.5", "maxwidth": "2.2"}))


class Cells(unittest.TestCase):
    def test_straight_points_go_and_bends_stay(self):
        points = [(48000000, 11000000), (48000100, 11000100), (48000200, 11000200)]
        self.assertEqual([points[0], points[2]], simplify(points))
        bend = [(48000000, 11000000), (48001000, 11001000), (48000000, 11002000)]
        self.assertEqual(bend, simplify(bend))

    def test_road_is_cut_at_the_cell_border(self):
        a, b, c = (48010000, 11010000), (48019000, 11010000), (48021000, 11010000)
        cells = cut_into_cells([a, b, c])
        self.assertEqual([[a, b, c]], cells[(48010000 // CELL_E6, 11010000 // CELL_E6)])
        self.assertEqual([[b, c]], cells[(48021000 // CELL_E6, 11010000 // CELL_E6)])

    def test_cell_id_of_negative_longitude(self):
        self.assertEqual((5 << 20) | 0xFFFFF, cell_id(5, -1))

    def test_bytes_the_app_test_expects(self):
        data = encode_cell(2400, 550, [(50, None, [(48000000, 11000000), (48000100, 11000050)]), (None, 30, [(48010000, 11010000), (48010010, 11009990)])])
        self.assertEqual("023200020000140a001e02d00fd00f0201", data.hex())

    def test_bytes_of_no_limit_and_default(self):
        data = encode_cell(2400, 550, [(UNLIMITED, default_byte(100), [(48000000, 11000000), (48000100, 11000050)])])
        self.assertEqual("01ffc902" + "0000140a", data.hex())


class Outline(unittest.TestCase):
    def test_rings_without_holes(self):
        text = "\n".join(["x", "1", " 10.0 50.0", " 10.5 50.0", " 11.0 50.0", " 11.0 51.0", " 10.0 50.0", "END",
                          "!2", " 10.2 50.2", " 10.3 50.2", " 10.2 50.2", "END", "END"])
        self.assertEqual([[50.0, 10.0, 50.0, 11.0, 51.0, 11.0, 50.0, 10.0]], parse_outline(text))


def limits_pack(path, pieces):
    database = Path(str(path) + ".tmp")
    db = sqlite3.connect(database)
    db.execute("CREATE TABLE cell (id INTEGER PRIMARY KEY NOT NULL, data BLOB NOT NULL)")
    db.execute("INSERT INTO cell VALUES (1, ?)", (encode_cell(0, 0, pieces),))
    db.commit()
    db.close()
    Path(path).write_bytes(zstandard.ZstdCompressor().compress(database.read_bytes()))
    database.unlink()


ROAD = [(0, 0), (100, 100)]


class Stats(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual("none", check.limit_bucket(0))
        self.assertEqual("50", check.limit_bucket(50))
        self.assertEqual("unlimited", check.limit_bucket(UNLIMITED))
        self.assertEqual("default 100", check.limit_bucket(default_byte(100)))

    def test_cell_is_read_like_it_was_written(self):
        counts = {}
        far = 5_000_000
        check.cell_counts(encode_cell(0, 0, [(50, None, [(far, far), (far + 100, far)]), (UNLIMITED, 30, [(far, far)])]), counts)
        self.assertEqual({"50": 1, "none": 1, "unlimited": 1, "30": 1}, counts)

    def test_stats_of_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.zst"
            limits_pack(path, [(50, 30, ROAD)])
            stats = check.limits_stats(path)
            self.assertEqual(2, stats["directions"])
            self.assertEqual({"50": 1, "30": 1}, stats["limits"])


class Compare(unittest.TestCase):
    OLD = {"bytes": 1000, "directions": 1000, "limits": {"none": 500, "50": 300, "100": 200}}

    def test_normal_month_passes(self):
        new = {"bytes": 1020, "directions": 1010, "limits": {"none": 500, "50": 310, "100": 200}}
        self.assertEqual([], check.compare_limits(new, self.OLD))

    def test_half_the_roads_are_gone(self):
        new = {"bytes": 1000, "directions": 500, "limits": {"none": 250, "50": 150, "100": 100}}
        self.assertTrue(check.compare_limits(new, self.OLD))

    def test_size_alone_stops_it(self):
        new = {**self.OLD, "bytes": 800}
        self.assertEqual(1, len(check.compare_limits(new, self.OLD)))

    def test_limits_moved_at_the_same_size(self):
        new = {"bytes": 1000, "directions": 1000, "limits": {"none": 500, "50": 200, "100": 300}}
        self.assertEqual(2, len(check.compare_limits(new, self.OLD)))

    def test_cameras_small_numbers_are_noise(self):
        self.assertEqual([], check.compare_cameras({"cameras": 8, "sections": 0}, {"cameras": 3, "sections": 0}))
        self.assertEqual([], check.compare_cameras({"cameras": 1100, "sections": 2}, {"cameras": 1000, "sections": 2}))
        self.assertTrue(check.compare_cameras({"cameras": 500, "sections": 2}, {"cameras": 1000, "sections": 2}))

    def test_point_layers(self):
        self.assertEqual([], check.compare_counts({"rail": 105, "school": 3}, {"rail": 100}))
        self.assertEqual(["rail 1000 -> 500"], check.compare_counts({"rail": 500}, {"rail": 1000}))

    def test_accept(self):
        self.assertIsNone(check.parse_accept("all"))
        self.assertEqual(set(), check.parse_accept(""))
        self.assertEqual({"AT", "DE-BY"}, check.parse_accept(" AT, DE-BY"))


class Hold(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.new = self.root / "new"
        self.old = self.root / "old"
        self.new.mkdir()
        self.old.mkdir()

    def entry(self, name, data, layer="limits"):
        return {"region": "AT", "layer": layer, "file": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "data_date": "2026-10-01T00:00:00Z", "schema_version": 3}

    def run_check(self, new_pieces, accept=frozenset(), recalls=None):
        name = "AT.limits.sqlite.zst"
        limits_pack(self.old / name, [(50, 50, ROAD)] * 100)
        limits_pack(self.new / name, new_pieces)
        old = self.entry(name, (self.old / name).read_bytes())
        new_bytes = (self.new / name).read_bytes()
        report = {"region": "AT", "schema_version": 3, "data_date": "2026-11-01T00:00:00Z", "limits": {"file": name, "bytes": len(new_bytes)}, "cameras_file": {"file": "x"}}
        path = self.new / "AT.report.json"
        path.write_text(json.dumps(report))
        held = {}
        check.check_built(self.new, str(self.old), {("AT", "limits"): old}, recalls or recall.empty(), {"AT": (path, report)}, accept, held)
        return held, report, old, new_bytes

    def test_broken_package_goes_back_to_the_old_one(self):
        held, report, old, _ = self.run_check([(50, 50, ROAD)] * 10)
        self.assertIn("AT limits", held)
        self.assertEqual(old["sha256"], report["limits"]["sha256"])
        self.assertEqual(old["data_date"], report["limits"]["data_date"])
        self.assertEqual((self.old / old["file"]).read_bytes(), (self.new / old["file"]).read_bytes())

    def test_accept_lets_it_through(self):
        for accept in (None, {"AT"}):
            held, _, _, new_bytes = self.run_check([(50, 50, ROAD)] * 10, accept)
            self.assertEqual({}, held)
            self.assertEqual(new_bytes, (self.new / "AT.limits.sqlite.zst").read_bytes())

    def test_recalled_package_is_no_measure(self):
        recalls = {"recalls": [{"region": "AT", "layer": "limits", "valid_from": "2026-10-02"}]}
        held, _, _, _ = self.run_check([(50, 50, ROAD)] * 10, recalls=recalls)
        self.assertEqual({}, held)

    def test_failed_region_keeps_its_packages(self):
        cameras = self.entry("AT.cameras.sqlite.zst", b"c", "cameras")
        limits = self.entry("AT.limits.sqlite.zst", b"l")
        (self.old / cameras["file"]).write_bytes(b"c")
        (self.old / limits["file"]).write_bytes(b"l")
        previous = {("AT", "limits"): limits, ("AT", "cameras"): cameras}
        held = {}
        index = {"regions": [{"code": "AT", "outline": [[1.0, 2.0]]}]}
        check.keep_unbuilt(self.new, str(self.old), index, previous, {}, held)
        self.assertEqual(["no new build"], held["AT"])
        report = json.loads((self.new / "AT.report.json").read_text(encoding="utf-8"))
        self.assertEqual(limits["sha256"], report["limits"]["sha256"])
        self.assertEqual([[1.0, 2.0]], report["outline"])
        self.assertEqual(b"c", (self.new / cameras["file"]).read_bytes())
        self.assertNotIn("hazards_file", report)


class Publish(unittest.TestCase):
    def test_tag_does_not_depend_on_the_day_of_the_run(self):
        self.assertEqual("data-2026-11-04-r14", publish.release_tag("2026-11-04T21:21:03Z", "14"))
        self.assertEqual("data-2026-11-03-r14", publish.existing_tag([{"tagName": "data-2026-11-03-r14"}, {"tagName": "data-2026-11-03-r114"}], "14"))
        self.assertIsNone(publish.existing_tag([{"tagName": "data-2026-11-03-r114"}], "14"))

    def files(self, folder, *names):
        result = []
        for name in names:
            path = Path(folder) / name
            path.write_bytes(b"12345")
            result.append(path)
        return result

    def test_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            a, b, c, d = self.files(folder, "a", "b", "c", "d")
            assets = [{"name": "a", "size": 5, "state": "uploaded"}, {"name": "b", "size": 4, "state": "uploaded"}, {"name": "c", "size": 5, "state": "starter"}]
            self.assertEqual([b, c, d], publish.missing([a, b, c, d], assets))

    def test_upload_retries_only_what_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            a, b = self.files(folder, "a", "b")
            there, calls, waits = [], [], []

            def send(todo):
                calls.append([f.name for f in todo])
                there.append({"name": todo[0].name, "size": 5})
                if len(calls) == 1:
                    raise subprocess.CalledProcessError(1, "gh", stderr="ReleaseAsset.name already exists")

            publish.upload("t", [a, b], assets=lambda tag: there, send=send, sleep=waits.append)
            self.assertEqual([["a", "b"], ["b"]], calls)
            self.assertEqual([30], waits)

    def test_upload_gives_up(self):
        with tempfile.TemporaryDirectory() as folder:
            a = self.files(folder, "a")[0]

            def send(todo):
                raise subprocess.CalledProcessError(1, "gh", stderr="no")

            waits = []
            with self.assertRaises(SystemExit):
                publish.upload("t", [a], assets=lambda tag: [], send=send, sleep=waits.append)
            self.assertEqual([30, 60, 90, 120], waits)


class Recall(unittest.TestCase):
    NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)

    def test_add_replaces_the_same_package(self):
        one = recall.added(recall.empty(), "AT", "limits", "2026-11-04", self.NOW)
        two = recall.added(one, "AT", "limits", "2026-11-05", self.NOW)
        self.assertEqual([{"region": "AT", "layer": "limits", "valid_from": "2026-11-05"}], two["recalls"])
        self.assertGreater(two["issued"], one["issued"])

    def test_remove_gets_a_newer_date(self):
        one = recall.added(recall.empty(), "AT", "limits", "2026-11-04", self.NOW)
        two = recall.removed(one, "AT", "limits", self.NOW)
        self.assertEqual([], two["recalls"])
        self.assertEqual("2026-11-06T12:00:01Z", two["issued"])

    def test_wrong_input_stops(self):
        for region, layer, date in [("XX", "limits", "2026-11-04"), ("AT", "roads", "2026-11-04"), ("AT", "limits", "tomorrow")]:
            with self.assertRaises(SystemExit):
                recall.added(recall.empty(), region, layer, date, self.NOW)
        with self.assertRaises(SystemExit):
            recall.removed(recall.empty(), "AT", "limits", self.NOW)

    def test_older_data_is_recalled(self):
        recalls = recall.added(recall.empty(), "AT", "limits", "2026-11-04", self.NOW)
        self.assertTrue(recall.is_recalled(recalls, "AT", "limits", "2026-11-03T21:00:00Z"))
        self.assertFalse(recall.is_recalled(recalls, "AT", "limits", "2026-11-04T21:00:00Z"))
        self.assertFalse(recall.is_recalled(recalls, "AT", "cameras", "2026-11-03T21:00:00Z"))
        self.assertFalse(recall.is_recalled(recalls, "CH", "limits", "2026-11-03T21:00:00Z"))

    def test_signature(self):
        key = Ed25519PrivateKey.generate()
        data = recall.sign(recall.added(recall.empty(), "AT", "limits", "2026-11-04", self.NOW), key)
        self.assertEqual("AT", recall.parse(data, key.public_key())["recalls"][0]["region"])
        self.assertIsNone(recall.parse(data.replace(b"AT", b"CH"), key.public_key()))
        self.assertIsNone(recall.parse(b"garbage", key.public_key()))
        self.assertLess(len(data), 1024)
        self.assertEqual(64, len(base64.b64decode(data.split(b"\n")[0])))


if __name__ == "__main__":
    unittest.main()
