"""Road directions (engine/directions.py): the parts that need no road files and no routing engine.

The route itself comes from Valhalla and is checked by hand against real road files (see the proposal). What is checked here
is everything Lastbook adds around it: reading the route's line, finding the fuel stations along it and the longest stretch
with none, naming the main roads, and answering plainly when no road files are installed.

Needs no library, no map and no valhalla module. Never packaged."""
import sqlite3, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine import directions as D   # noqa: E402


def encode(points, precision=6):
    """The encoding decode_line reads, written out the long way."""
    out, last = [], (0, 0)
    for lat, lon in points:
        now = (round(lat * 10 ** precision), round(lon * 10 ** precision))
        for v in (now[0] - last[0], now[1] - last[1]):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        last = now
    return ''.join(out)


def poi_file(folder, rows, brand=False):
    path = Path(folder) / ('brand.sqlite' if brand else 'map.sqlite')
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE poi(id INTEGER PRIMARY KEY, cls TEXT, name TEXT, ' + ('brand TEXT, ' if brand else '') + 'lon REAL, lat REAL)')
    db.executemany('INSERT INTO poi(cls, name, ' + ('brand, ' if brand else '') + 'lon, lat) VALUES (' + ('?,' * (5 if brand else 4))[:-1] + ')', rows)
    db.commit(); db.close()
    return path


# A straight road due east along 34°N: one degree of longitude there is 57.3 miles.
ROAD = [(34.0, -116.0), (34.0, -115.0)]
MILE_LON = 1 / 57.29      # a mile east, in degrees
MILE_LAT = 1 / 69.09      # a mile north


class Line(unittest.TestCase):
    def test_reads_an_encoded_line(self):
        points = [(33.448376, -112.074036), (33.449001, -112.074036), (34.053674, -118.243029), (-0.5, 0.25)]
        back = D.decode_line(encode(points))
        self.assertEqual(len(back), len(points))
        for a, b in zip(points, back):
            self.assertAlmostEqual(a[0], b[0], places=6)
            self.assertAlmostEqual(a[1], b[1], places=6)

    def test_miles(self):
        self.assertAlmostEqual(D.miles_between(*ROAD), 57.3, delta=0.2)
        self.assertEqual(D.along(ROAD)[0], 0)
        self.assertAlmostEqual(D.along(ROAD + [(34.0, -114.0)])[-1], 114.6, delta=0.4)

    def test_fills_in_a_long_straight_stretch(self):
        pts, at = D.densify(ROAD)
        self.assertEqual((pts[0], pts[-1]), (ROAD[0], ROAD[1]))
        self.assertTrue(all(b - a <= D.STEP + 1e-6 for a, b in zip(at, at[1:])))
        self.assertAlmostEqual(at[-1], D.miles_between(*ROAD), places=6)

    def test_thins_for_drawing_and_keeps_both_ends(self):
        line = [(34.0, -116.0 + i / 1000) for i in range(10001)]
        out = D.thin(line, 400)
        self.assertLessEqual(len(out), 402)
        self.assertEqual((out[0], out[-1]), (line[0], line[-1]))
        self.assertEqual(D.thin(line[:50], 400), line[:50])


class Fuel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_finds_stations_near_the_road_in_order(self):
        rows = [('fuel', 'At mile 40', -116.0 + 40 * MILE_LON, 34.0 + 0.3 * MILE_LAT),
                ('fuel', 'At mile 10', -116.0 + 10 * MILE_LON, 34.0),
                ('fuel', 'Two miles off the road', -116.0 + 20 * MILE_LON, 34.0 + 2 * MILE_LAT),
                ('fuel', 'Past the end', -115.0 + 3 * MILE_LON, 34.0),
                ('hospital', 'Not fuel', -116.0 + 30 * MILE_LON, 34.0)]
        found = D.fuel_along(ROAD, [poi_file(self.tmp.name, rows)])
        self.assertEqual([f['name'] for f in found], ['At mile 10', 'At mile 40'])
        self.assertAlmostEqual(found[0]['mile'], 10, delta=0.3)
        self.assertAlmostEqual(found[1]['mile'], 40, delta=0.3)
        self.assertAlmostEqual(found[1]['off'], 0.3, delta=0.1)

    def test_a_station_in_two_files_counts_once_and_a_brand_stands_in_for_a_name(self):
        spot = (-116.0 + 25 * MILE_LON, 34.0)
        a = poi_file(self.tmp.name, [('fuel', 'Corner Fuel', *spot)])
        b = poi_file(self.tmp.name, [('fuel', '', 'Shell', *spot), ('fuel', '', 'Chevron', -116.0 + 50 * MILE_LON, 34.0)], brand=True)
        self.assertEqual([f['name'] for f in D.fuel_along(ROAD, [a, b])], ['Corner Fuel', 'Chevron'])

    def test_a_file_without_places_is_passed_over(self):
        other = Path(self.tmp.name) / 'other.sqlite'
        db = sqlite3.connect(other); db.execute('CREATE TABLE meta(key TEXT)'); db.commit(); db.close()
        self.assertEqual(D.fuel_along(ROAD, [other, Path(self.tmp.name) / 'missing.sqlite']), [])

    def test_longest_stretch_without_fuel(self):
        stations = [{'mile': 5.0}, {'mile': 20.0}, {'mile': 78.5}, {'mile': 90.0}]
        self.assertEqual(D.longest_gap(stations, 100.0), (58.5, 20.0, 78.5))
        self.assertEqual(D.longest_gap([], 42.0), (42.0, 0.0, 42.0))
        self.assertEqual(D.longest_gap([{'mile': 2.0}], 100.0), (98.0, 2.0, 100.0))

    def test_a_short_list_spread_along_the_trip(self):
        stations = [{'mile': float(m), 'off': 0.5 if m % 20 else 0.05, 'name': f'S{m}'} for m in range(0, 300, 5)]
        picks = D.spaced(stations, 300.0)
        self.assertEqual([p['name'] for p in picks], ['S0', 'S60', 'S120', 'S180', 'S240'])
        self.assertLessEqual(len(D.spaced(stations, 300.0, every=10, most=8)), 9)
        self.assertEqual(D.spaced([], 300.0), [])


class Steps(unittest.TestCase):
    def test_a_step_says_how_far_and_on_which_road(self):
        self.assertEqual(D.step('Take exit 144B.', 144.63, ['I 10'])['then'], '144.6 mi on I 10')
        self.assertEqual(D.step('Turn left.', 0.3, [])['then'], '0.3 mi')
        self.assertEqual(D.step('Your destination is on the left.', 0, [])['then'], '')

    def test_main_roads_are_the_long_stretches_in_order(self):
        steps = [D.step('a', 0.5, ['Main Street']), D.step('b', 144.6, ['I 10']), D.step('c', 0.2, ['US 95']), D.step('d', 148.5, ['I 10']),
                 D.step('e', 22.8, ['CA 60 West']), D.step('f', 3.0, ['US 101 North'])]
        self.assertEqual(D.main_roads(steps, 320.0), [['I 10', 293.1], ['CA 60 West', 22.8]])

    def test_driving_time_in_words(self):
        self.assertEqual((D.clock(19472), D.clock(1020), D.clock(3600)), ('5 h 25 min', '17 min', '1 h 00 min'))


class NoRoadFiles(unittest.TestCase):
    def test_says_so_and_asks_nothing_of_the_engine(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict('os.environ', {}, clear=False) as env:
            env.pop('LASTBOOK_ROUTING', None)
            self.assertFalse(D.available(tmp))
            r = D.route(tmp, (33.4, -112.0), (34.0, -118.2))
            self.assertEqual(r['code'], 'no_roads')


if __name__ == '__main__':
    unittest.main()
