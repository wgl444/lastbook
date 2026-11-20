#!/usr/bin/env python3
"""Prototype: build offline road-routing files for a region from OpenStreetMap extracts.

  python -I scripts/build_routing.py OUT_DIR extract.osm.pbf [more.osm.pbf ...] [--driving-only]

Writes OUT_DIR/dir/ (Valhalla graph tiles) and OUT_DIR/config.json, and prints how long it took and how big it is.
Nothing here uses the network: the extracts are read from disk.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import valhalla


def folder_bytes(p):
    return sum(f.stat().st_size for f in Path(p).rglob('*') if f.is_file())


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    driving_only = '--driving-only' in sys.argv
    if len(args) < 2:
        sys.exit(__doc__)
    out, pbfs = Path(args[0]).resolve(), [str(Path(p).resolve(strict=True)) for p in args[1:]]
    tiles = out / 'dir'
    tiles.mkdir(parents=True, exist_ok=True)
    cfg = valhalla.get_config(tile_extract='', tile_dir=str(tiles))
    m = cfg['mjolnir']
    # No admin or timezone databases in this prototype: routes use the defaults (drive on the right, no time-of-day rules).
    m['admin'], m['timezone'] = str(out / 'no-admins.sqlite'), str(out / 'no-timezones.sqlite')
    if driving_only:
        m['include_pedestrian'], m['include_bicycle'] = False, False
    (out / 'config.json').write_text(json.dumps(cfg, indent=1), encoding='utf-8')
    t0 = time.time()
    tool = Path(valhalla.__file__).parent / 'bin' / 'valhalla_build_tiles'     # the build tool the package ships, run directly
    r = subprocess.run([str(tool), '-c', str(out / 'config.json'), *pbfs], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (out / 'build.log').write_text(r.stdout, encoding='utf-8')
    took = time.time() - t0
    files = [f for f in tiles.rglob('*.gph')]
    print(json.dumps({'ok': r.returncode == 0, 'seconds': round(took), 'tile_files': len(files),
                      'tiles_mb': round(folder_bytes(tiles) / 1e6, 1),
                      'input_mb': round(sum(Path(p).stat().st_size for p in pbfs) / 1e6, 1),
                      'driving_only': driving_only, 'log_tail': r.stdout.strip().splitlines()[-3:]}, indent=1))
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
