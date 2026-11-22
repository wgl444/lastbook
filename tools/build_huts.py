#!/usr/bin/env python3
"""Build the map's huts and trail shelters (huts.sqlite) from OpenStreetMap.

  build_huts.py --overpass huts.json --out huts.sqlite      an Overpass API result ("out tags center")
  build_huts.py --osm region.osm.pbf --out huts.sqlite      a raw extract (needs the osmium package)

The app reads the result from derived/geo/huts.sqlite in the data folder, or from the file LASTBOOK_HUTS names.
It has the same two tables as the other point layers (resource, spatial), so the map's pins, lists and nearest
search read it with the code they already have.

What is kept, and as what:
  tourism=wilderness_hut                         hut            "Unstaffed hut or cabin"
  tourism=alpine_hut                             hut            "Staffed mountain hut"
  amenity=shelter + shelter_type=basic_hut       hut            "Basic hut"
  amenity=shelter + shelter_type=lean_to         trail_shelter  "Lean-to: three walls and a roof"
  amenity=shelter named hut, cabin, warming, yurt  hut          "Named shelter"
Places tagged access=private or access=no are left out. Bus stops, picnic shelters and shelter_type=weather_shelter
(mostly city shade structures) never match. Geofabrik's simplified layers cannot be used here: their shelter class
has no shelter_type, and wilderness_hut is not one of their classes.

The Overpass query for a whole country (US shown):
  [out:json][timeout:240];
  area["ISO3166-1"="US"]["admin_level"="2"]->.a;
  ( nwr["tourism"~"^(wilderness_hut|alpine_hut)$"](area.a);
    nwr["amenity"="shelter"]["shelter_type"~"^(basic_hut|lean_to)$"](area.a);
    nwr["amenity"="shelter"]["name"~"[Ww]arming|[Hh]ut|[Cc]abin|[Yy]urt"](area.a); );
  out tags center;
"""
import argparse, json, re, sqlite3, sys
from pathlib import Path

SOURCE = 'OpenStreetMap contributors (ODbL)'
HUT_NAME = re.compile(r'\b(warming|huts?|cabins?|yurts?)\b', re.I)
NOT_A_HUT = {'public_transport', 'picnic_shelter', 'weather_shelter', 'gazebo', 'pavilion', 'sun_shelter', 'bicycle_parking'}
ACCESS = {'yes': 'Open to the public', 'public': 'Open to the public', 'permissive': 'Open by permission of the owner',
          'customers': 'Customers only', 'permit': 'Permit needed', 'members': 'Members only'}
RESERVATION = {'required': 'Required', 'yes': 'Taken', 'recommended': 'Recommended', 'no': 'Not needed', 'members_only': 'Members only'}
YES_NO = {'yes': 'Yes', 'no': 'No'}


def classify(tags):
    """(kind, type line) for a hut or trail shelter, or None for anything else."""
    if tags.get('access', '').strip().lower() in ('private', 'no'):
        return None
    tourism, shelter = tags.get('tourism'), tags.get('shelter_type')
    if tourism == 'wilderness_hut':
        return 'hut', 'Unstaffed hut or cabin'
    if tourism == 'alpine_hut':
        return 'hut', 'Staffed mountain hut'
    if tags.get('amenity') == 'shelter':
        if shelter == 'basic_hut':
            return 'hut', 'Basic hut'
        if shelter == 'lean_to':
            return 'trail_shelter', 'Lean-to: three walls and a roof'
        if shelter not in NOT_A_HUT and HUT_NAME.search(tags.get('name', '')):
            return 'hut', 'Named shelter'
    return None


def detail(tags, hut_type, as_of=''):
    """What the map data says about the place, in the words the card shows. Nothing is guessed: where the data
    records no access, booking, fee or lock at all, the card says "Not recorded" in place of leaving the row out."""
    tag = lambda k: tags.get(k, '').strip().lower()
    d = {'hut_type': hut_type, 'run_by': tags.get('operator', '').strip(),
         'access': ACCESS.get(tag('access'), tag('access').replace('_', ' ')),
         'reservation': RESERVATION.get(tag('reservation'), ''), 'fee': YES_NO.get(tag('fee'), ''),
         'locked': YES_NO.get(tag('locked'), ''), 'fireplace': YES_NO.get(tag('fireplace'), ''),
         # the one-line summary says a reservation is needed before it says a place is open to the public
         'booking': {'required': 'Reservation required', 'yes': 'Reservations taken', 'recommended': 'Reservation recommended',
                     'no': 'No reservation needed'}.get(tag('reservation'), '')}
    # access=yes beside a required booking means the public may book it, not walk in: the reservation row says enough
    if tag('access') in ('yes', 'public') and tag('reservation') in ('required', 'yes', 'members_only'):
        d['access'] = ''
    if not any(d[k] for k in ('access', 'reservation', 'fee', 'locked')):
        d['access'] = 'Not recorded'
    ele = re.fullmatch(r'\s*(-?\d+(?:\.\d+)?)\s*m?\s*', tags.get('ele', ''))
    if ele:
        d['elevation_m'] = round(float(ele[1]))
    d['as_of'] = as_of                   # the month the map data was read, shown in the card's caution
    return {k: v for k, v in d.items() if v not in ('', None)}


def from_overpass(path):
    """(source id, tags, lat, lon) for each element of an Overpass result written with "out tags center"."""
    for e in json.loads(Path(path).read_text(encoding='utf-8')).get('elements', []):
        at = e.get('center') or e
        if 'lat' in at and 'lon' in at:
            yield f"{e.get('type')}/{e.get('id')}", e.get('tags') or {}, at['lat'], at['lon']


def from_extract(path):
    """The same from a raw .osm.pbf: nodes, and ways placed at their first node."""
    try:
        import osmium
    except ImportError:
        sys.exit('Reading an .osm.pbf needs the osmium package (pip install osmium). An Overpass result needs nothing.')
    found = []

    class Reader(osmium.SimpleHandler):
        def node(self, n):
            if n.tags and classify(dict(n.tags)):
                found.append((f'node/{n.id}', dict(n.tags), n.location.lat, n.location.lon))

        def way(self, w):
            if w.tags and classify(dict(w.tags)):
                try:
                    found.append((f'way/{w.id}', dict(w.tags), w.nodes[0].location.lat, w.nodes[0].location.lon))
                except Exception:       # a way whose first node is outside the extract has no position
                    pass

    Reader().apply_file(str(path), locations=True)
    return found


def build(items, out, as_of=''):
    """Write huts.sqlite from (source id, tags, lat, lon) items; returns {kind: count}. as_of: e.g. "November 2026"."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    db = sqlite3.connect(out)
    db.executescript('''CREATE TABLE resource(id INTEGER PRIMARY KEY, source_id TEXT, kind TEXT, name TEXT, lat REAL, lon REAL, detail TEXT, source TEXT);
      CREATE INDEX resource_kind ON resource(kind);
      CREATE VIRTUAL TABLE spatial USING rtree(id, minx, maxx, miny, maxy);
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);''')
    counts, seen, placed = {}, set(), {}
    # A hut is often mapped twice (a point and its building). Of two with one name within about 200 m, keep the one
    # that says more, so the fuller record is read first.
    items = sorted(items, key=lambda it: -len(detail(it[1], '')))
    for source_id, tags, lat, lon in items:
        found = classify(tags)
        if not found or source_id in seen or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        seen.add(source_id)
        kind, hut_type = found
        name = tags.get('name', '').strip().lower()
        if name:
            spots = placed.setdefault(name, [])
            if any(abs(lat - y) < 0.002 and abs(lon - x) < 0.003 for y, x in spots):
                continue
            spots.append((lat, lon))
        cur = db.execute('INSERT INTO resource(source_id,kind,name,lat,lon,detail,source) VALUES (?,?,?,?,?,?,?)',
                         (source_id, kind, tags.get('name', '').strip(), lat, lon,
                          json.dumps(detail(tags, hut_type, as_of), ensure_ascii=False), SOURCE))   # key order is the card's row order
        db.execute('INSERT INTO spatial VALUES (?,?,?,?,?)', (cur.lastrowid, lon, lon, lat, lat))
        counts[kind] = counts.get(kind, 0) + 1
    db.execute("INSERT INTO meta VALUES ('counts', ?)", (json.dumps(counts, sort_keys=True),))
    db.commit()
    db.close()
    return counts


def main(args):
    ap = argparse.ArgumentParser(description='Build the huts and trail shelters layer from OpenStreetMap.')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--overpass', type=Path, help='an Overpass API result (JSON, "out tags center")')
    src.add_argument('--osm', type=Path, help='a raw OpenStreetMap extract (.osm.pbf); needs the osmium package')
    ap.add_argument('--as-of', dest='as_of', help='when the map data was read, e.g. "November 2026" (taken from an Overpass result if left out)')
    ap.add_argument('--out', type=Path, required=True, help='the file to write, e.g. <data folder>/derived/geo/huts.sqlite')
    a = ap.parse_args(args)
    as_of = a.as_of
    if not as_of and a.overpass:         # an Overpass result carries the time of the data it was read from
        stamp = json.loads(a.overpass.read_text(encoding='utf-8')).get('osm3s', {}).get('timestamp_osm_base', '')
        if re.match(r'\d{4}-\d{2}', stamp):
            as_of = f"{'January February March April May June July August September October November December'.split()[int(stamp[5:7]) - 1]} {stamp[:4]}"
    counts = build(from_overpass(a.overpass) if a.overpass else from_extract(a.osm), a.out, as_of or '')
    print(f"{a.out}: {sum(counts.values()):,} places " + ', '.join(f'{k} {n:,}' for k, n in sorted(counts.items())))


if __name__ == '__main__':
    main(sys.argv[1:])
