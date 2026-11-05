"""Filter rules. Run: python -m unittest test_build (in this folder)."""
import unittest

from build import build_cameras, camera_direction, camera_limit, parse_limit, road_limits


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


if __name__ == "__main__":
    unittest.main()
