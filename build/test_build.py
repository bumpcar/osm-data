"""Filter rules. Run: python -m unittest test_build (in this folder)."""
import unittest

import shapely

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


if __name__ == "__main__":
    unittest.main()
