#!/usr/bin/env python3
"""Prototype: fuel stations from OpenStreetMap extracts, into a small SQLite file.

  python -I tools/build_routing_extras.py OUT.sqlite extract.osm.pbf [more.osm.pbf ...]

Lastbook's own state maps already carry fuel stations for the states they cover. This stands in for them in the
prototype's region, which is not an installed state map on the test computer.
  poi(cls, name, brand, lon, lat)   cls is 'fuel' or 'charging_station'
"""
import sqlite3
import sys
import time
from pathlib import Path

import osmium

KINDS = ('fuel', 'charging_station')


def where(o):
    """lon, lat of a node, or of a way's first node."""
    try:
        loc = o.location if o.is_node() else o.nodes[0].location
        return (loc.lon, loc.lat) if loc.valid() else None
    except Exception:
        return None


def main():
    argv = sys.argv[1:]
    if len(argv) < 2:
        sys.exit(__doc__)
    out, pbfs = Path(argv[0]), [str(Path(p).resolve(strict=True)) for p in argv[1:]]
    out.unlink(missing_ok=True)
    db = sqlite3.connect(out)
    db.execute('CREATE TABLE poi(cls TEXT, name TEXT, brand TEXT, lon REAL, lat REAL)')
    t0, n = time.time(), 0
    for pbf in pbfs:
        for o in osmium.FileProcessor(pbf, osmium.osm.NODE | osmium.osm.WAY).with_locations().with_filter(osmium.filter.KeyFilter('amenity')):
            kind = o.tags.get('amenity')
            p = where(o) if kind in KINDS else None
            if p:
                db.execute('INSERT INTO poi VALUES (?,?,?,?,?)', (kind, o.tags.get('name') or '', o.tags.get('brand') or '', p[0], p[1]))
                n += 1
    db.execute('CREATE INDEX poi_lonlat ON poi(lon, lat)')
    db.commit()
    print({'fuel_and_charging': n, 'seconds': round(time.time() - t0), 'mb': round(out.stat().st_size / 1e6, 1)})


if __name__ == '__main__':
    main()
