"""Huts and trail shelters: what tools/build_huts.py keeps from OpenStreetMap, and how the map's code reads it.
Needs nothing but Python: the sample is an Overpass result held in this file. Never packaged."""
import json, os, sqlite3, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import build_huts  # noqa: E402

el = lambda i, lat, lon, **tags: {'type': 'node', 'id': i, 'lat': lat, 'lon': lon, 'tags': tags}
SAMPLE = {'elements': [
    el(1, 39.46, -106.23, tourism='wilderness_hut', name="Example Cabin", operator='Example Hut Association',
       access='yes', fee='yes', reservation='required', fireplace='yes', ele='3520'),
    el(2, 44.33, -71.31, tourism='alpine_hut', name='Example Hut'),
    el(3, 44.10, -73.90, amenity='shelter', shelter_type='lean_to', name='Example Lean-to'),
    el(4, 35.03, -111.93, amenity='shelter', shelter_type='basic_hut'),
    el(5, 45.72, -68.76, amenity='shelter', name='Warming Hut'),
    {'type': 'way', 'id': 6, 'center': {'lat': 61.1, 'lon': -149.5}, 'tags': {'tourism': 'wilderness_hut', 'name': 'Way Cabin'}},
    # left out: the same cabin mapped a second time 100 m away with less said about it
    el(13, 39.4608, -106.2305, tourism='alpine_hut', name='Example Cabin'),
    # left out: private, bus stop, picnic shelter, city shade structure, a shelter with no type, a pizza place
    el(7, 39.0, -106.0, tourism='wilderness_hut', name='Private Cabin', access='private'),
    el(8, 33.44, -112.07, amenity='shelter', shelter_type='public_transport', name='Hut Street stop'),
    el(9, 33.45, -112.07, amenity='shelter', shelter_type='picnic_shelter'),
    el(10, 33.46, -112.07, amenity='shelter', shelter_type='weather_shelter', name='Warming Hut'),
    el(11, 33.47, -112.07, amenity='shelter'),
    el(12, 33.48, -112.07, amenity='restaurant', name='Pizza Hut'),
]}


class Huts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        src = Path(cls.dir.name) / 'huts.json'
        src.write_text(json.dumps(SAMPLE), encoding='utf-8')
        cls.out = Path(cls.dir.name) / 'huts.sqlite'
        cls.counts = build_huts.build(build_huts.from_overpass(src), cls.out)
        cls.db = sqlite3.connect(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.dir.cleanup()

    def test_what_is_kept(self):
        self.assertEqual(self.counts, {'hut': 5, 'trail_shelter': 1})
        names = {n for (n,) in self.db.execute('SELECT name FROM resource')}
        self.assertEqual(names, {'Example Cabin', 'Example Hut', 'Example Lean-to', '', 'Warming Hut', 'Way Cabin'})
        kinds = dict(self.db.execute("SELECT name, kind FROM resource WHERE name!=''"))
        self.assertEqual(kinds['Example Lean-to'], 'trail_shelter')
        self.assertEqual(kinds['Warming Hut'], 'hut')
        self.assertEqual(self.db.execute("SELECT COUNT(*), MIN(source_id) FROM resource WHERE name='Example Cabin'").fetchone(), (1, 'node/1'))

    def test_detail_says_only_what_the_tags_say(self):
        d = json.loads(self.db.execute("SELECT detail FROM resource WHERE name='Example Cabin'").fetchone()[0])
        # access=yes beside a required booking is left out: the public may book it, not walk in
        self.assertEqual(d, {'hut_type': 'Unstaffed hut or cabin', 'run_by': 'Example Hut Association',
                             'fee': 'Yes', 'reservation': 'Required', 'booking': 'Reservation required', 'fireplace': 'Yes', 'elevation_m': 3520})
        bare = json.loads(self.db.execute("SELECT detail FROM resource WHERE name='Example Hut'").fetchone()[0])
        self.assertEqual(bare, {'hut_type': 'Staffed mountain hut', 'access': 'Not recorded'})   # said, not guessed
        lean = json.loads(self.db.execute("SELECT detail FROM resource WHERE name='Example Lean-to'").fetchone()[0])
        self.assertEqual(lean, {'hut_type': 'Lean-to: three walls and a roof', 'access': 'Not recorded'})

    def test_the_month_the_data_was_read_is_kept(self):
        d = build_huts.detail({'tourism': 'wilderness_hut', 'access': 'yes', 'fee': 'no'}, 'Unstaffed hut or cabin', 'November 2026')
        self.assertEqual(d, {'hut_type': 'Unstaffed hut or cabin', 'access': 'Open to the public', 'fee': 'No', 'as_of': 'November 2026'})

    def test_a_way_is_placed_at_its_centre_and_every_row_is_indexed(self):
        self.assertEqual(self.db.execute("SELECT lat, lon, source_id FROM resource WHERE name='Way Cabin'").fetchone(), (61.1, -149.5, 'way/6'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM spatial').fetchone()[0], 6)
        box = self.db.execute('SELECT r.name FROM spatial s JOIN resource r ON r.id=s.id WHERE s.minx>=-107 AND s.maxx<=-106 AND s.miny>=39 AND s.maxy<=40').fetchall()
        self.assertEqual(box, [('Example Cabin',)])

    def test_the_map_reads_it(self):
        """The server's own functions find the huts through the file LASTBOOK_HUTS names, with no library installed."""
        with tempfile.TemporaryDirectory() as data:
            code = ("import json, server\n"
                    "near = server.near(39.4, -106.2, ['hut'], 3)\n"
                    "pins = server.pins(-107, 39, -106, 40, 9, ['hut', 'trail_shelter'])\n"
                    "found = server.hut_places('example cabin')\n"
                    "print(json.dumps({'near': [(p['name'], p['detail']) for p in near], 'sites': pins['sites'],\n"
                    "                  'found': [(p['name'], p['kind'], p['detail']) for p in found],\n"
                    "                  'kinds': sorted(server.HUT_KINDS & server.RESOURCE_KINDS)}))\n")
            import subprocess
            r = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, capture_output=True, text=True,
                               env={**os.environ, 'LASTBOOK_DATA': data, 'LASTBOOK_HUTS': str(self.out)})
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(got['kinds'], ['hut', 'trail_shelter'])
        self.assertEqual(got['near'][0], ['Example Cabin', 'Unstaffed hut or cabin · Example Hut Association · Reservation required'])
        self.assertEqual(got['sites'], 1)
        self.assertEqual(got['found'], [['Example Cabin', 'Hut or cabin', 'Example Hut Association · Reservation required']])


if __name__ == '__main__':
    unittest.main()
