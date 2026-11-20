"""Road directions between two points, offline: miles, time, the roads to take and the fuel stations along the way. PROTOTYPE.

Nothing here opens a socket or a file outside the data folder. The route is worked out in this process by Valhalla
(MIT, https://github.com/valhalla/valhalla) from road files built ahead of time from OpenStreetMap:

  <data>/derived/geo/routing/dir/      Valhalla graph tiles (tools/build_routing.py)
  <data>/derived/geo/routing/places.sqlite   optional: fuel stations for a region with no state map installed

LASTBOOK_ROUTING names another folder to read them from. With no road files, or no valhalla module, available() is False
and nothing else changes: the map and the assistant work as before.

The road data is what was mapped on the day it was built. It knows nothing of closures, damage, traffic or weather.
"""
import math
import os
import re
import sqlite3
import threading
from pathlib import Path

EARTH_MILES = 3958.8
STEP = 0.25            # miles between the points fuel stations are measured against: a straight desert highway has few of its own
NEAR_ROAD = 1.0        # miles: a fuel station this close to the route counts as on the way
MAX_LINE = 4000        # points sent to the map for one route
_lock = threading.Lock()
_actor = {'at': None, 'actor': None}


def folder(data):
    """Where the road files are: LASTBOOK_ROUTING, else <data>/derived/geo/routing."""
    env = os.environ.get('LASTBOOK_ROUTING')
    return Path(env) if env else Path(data) / 'derived' / 'geo' / 'routing'


def available(data):
    if not (folder(data) / 'dir').is_dir():
        return False
    try:
        import valhalla  # noqa: F401
    except Exception:
        return False
    return True


def _engine(data):
    """One Valhalla actor for the process, made on the first route. Callers hold _lock."""
    where = folder(data) / 'dir'
    if _actor['at'] != str(where):
        import valhalla
        _actor['actor'], _actor['at'] = valhalla.Actor(valhalla.get_config(tile_extract='', tile_dir=str(where))), str(where)
    return _actor['actor']


# ----------------------------------------------------------------------------------------------------- geometry --
def miles_between(a, b):
    """Great-circle miles between (lat, lon) and (lat, lon)."""
    r = math.pi / 180
    x = math.sin((b[0] - a[0]) * r / 2) ** 2 + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin((b[1] - a[1]) * r / 2) ** 2
    return 2 * EARTH_MILES * math.asin(min(1, math.sqrt(x)))


def decode_line(text, precision=6):
    """An encoded polyline as [(lat, lon), ...]. Valhalla writes six decimal places."""
    out, i, lat, lon, f = [], 0, 0, 0, 10 ** precision
    while i < len(text):
        for which in (0, 1):
            shift = value = 0
            while True:
                b = ord(text[i]) - 63
                i += 1
                value |= (b & 31) << shift
                shift += 5
                if b < 32:
                    break
            d = ~(value >> 1) if value & 1 else value >> 1
            if which == 0:
                lat += d
            else:
                lon += d
        out.append((lat / f, lon / f))
    return out


def along(line):
    """Miles from the start at each point of the line."""
    out, total = [0.0], 0.0
    for a, b in zip(line, line[1:]):
        total += miles_between(a, b)
        out.append(total)
    return out


def densify(line, step=STEP):
    """The same line with no two neighbouring points more than `step` miles apart, and the miles from the start at each."""
    pts, at, total = [line[0]], [0.0], 0.0
    for a, b in zip(line, line[1:]):
        d = miles_between(a, b)
        n = int(d // step)
        for k in range(1, n + 1):
            t = k * step / d
            if t < 1:
                pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
                at.append(total + k * step)
        total += d
        pts.append(b)
        at.append(total)
    return pts, at


def thin(line, most=MAX_LINE):
    """At most `most` points, always with the first and last, for drawing."""
    if len(line) <= most:
        return line
    k = math.ceil(len(line) / most)
    return line[::k] + ([line[-1]] if (len(line) - 1) % k else [])


# ------------------------------------------------------------------------------------------------ fuel stations --
def fuel_sources(data, extra=()):
    """SQLite files with a poi table that may hold fuel stations: the installed maps, then the prototype's own."""
    geo = Path(data) / 'derived' / 'geo'
    return [p for p in (geo / 'map.sqlite', geo / 'national.sqlite', folder(data) / 'places.sqlite', *extra) if Path(p).is_file() and Path(p).stat().st_size > 4096]


def fuel_along(line, sources, within=NEAR_ROAD, cls=('fuel',)):
    """Fuel stations within `within` miles of the line, in the order they come: [{name, lat, lon, mile, off}].
    mile is how far along the route the nearest point is; off is how far the station is from it."""
    if len(line) < 2:
        return []
    pts, at = densify(line)
    cell = 0.02                                          # degrees: about 1.4 miles of latitude
    grid = {}
    for i, (la, lo) in enumerate(pts):
        grid.setdefault((int(la // cell), int(lo // cell)), []).append(i)
    reach = max(1, math.ceil(within / 69 / cell))        # cells to look across for the nearest point
    found = {}
    chunk = max(2, int(25 / STEP))                       # about 25 miles of route per query
    marks = ','.join('?' * len(cls))
    for path in sources:
        try:
            db = sqlite3.connect(f'file:{Path(path).as_posix()}?mode=ro', uri=True)
            cols = {r[1] for r in db.execute('PRAGMA table_info(poi)')}
            if not {'cls', 'name', 'lon', 'lat'} <= cols:
                continue
            name = "COALESCE(NULLIF(name,''), brand, '')" if 'brand' in cols else 'name'
            for s in range(0, len(pts), chunk):
                part = pts[s:s + chunk + 1]
                south, north = min(p[0] for p in part), max(p[0] for p in part)
                west, east = min(p[1] for p in part), max(p[1] for p in part)
                dlat = within / 69
                dlon = within / (69 * max(0.2, math.cos(math.radians((south + north) / 2))))
                for nm, lo, la in db.execute(f'SELECT {name}, lon, lat FROM poi WHERE cls IN ({marks}) AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?',
                                             (*cls, west - dlon, east + dlon, south - dlat, north + dlat)):
                    key = (round(la, 4), round(lo, 4))
                    if key in found:
                        continue
                    best, bi = None, None
                    cy, cx = int(la // cell), int(lo // cell)
                    for y in range(cy - reach, cy + reach + 1):
                        for x in range(cx - reach * 2, cx + reach * 2 + 1):       # a degree of longitude is shorter than one of latitude
                            for i in grid.get((y, x), ()):
                                d = miles_between((la, lo), pts[i])
                                if best is None or d < best:
                                    best, bi = d, i
                    if best is not None and best <= within:
                        found[key] = {'name': (nm or '').strip(), 'lat': la, 'lon': lo, 'mile': round(at[bi], 1), 'off': round(best, 2)}
        except sqlite3.Error:
            continue
    return sorted(found.values(), key=lambda f: f['mile'])


def longest_gap(stations, total):
    """The longest stretch of the route with no fuel station: (miles, from mile, to mile)."""
    marks = [0.0] + [s['mile'] for s in stations] + [total]
    gap, a, b = max(((y - x, x, y) for x, y in zip(marks, marks[1:])), default=(total, 0.0, total))
    return round(gap, 1), round(a, 1), round(b, 1)


def spaced(stations, total, every=60, most=8):
    """A short list for a spoken answer: the station nearest the road in each `every` miles of the route."""
    if not stations:
        return []
    every = max(every, total / most) if total > every * most else every
    picks, edge = [], 0.0
    while edge < total:
        part = [s for s in stations if edge <= s['mile'] < edge + every]
        if part:
            picks.append(min(part, key=lambda s: (s['off'], not s['name'])))
        edge += every
    return picks


# --------------------------------------------------------------------------------------------------------- steps --
def clock(seconds):
    m = int(round(seconds / 60))
    return f'{m} min' if m < 60 else f'{m // 60} h {m % 60:02d} min'


def route(data, start, end, mode='auto', extra_fuel=()):
    """{'miles', 'seconds', 'time', 'steps': [{'text', 'miles', 'roads'}], 'line': [[lat, lon], ...], 'fuel': [...],
    'fuel_count', 'fuel_gap': [miles, from, to]} or {'error': a plain sentence}. start and end are (lat, lon)."""
    if not available(data):
        return {'error': 'Road directions are not installed with this map.', 'code': 'no_roads'}
    ask = {'locations': [{'lat': start[0], 'lon': start[1]}, {'lat': end[0], 'lon': end[1]}], 'costing': mode,
           'units': 'miles', 'directions_options': {'units': 'miles'}}
    try:
        with _lock:
            trip = _engine(data).route(ask)['trip']
    except Exception as e:
        text = str(e)
        if re.search(r'No suitable edges|No path could be found|exceeds the max distance|locations are unreachable|No data found', text, re.I):
            return {'error': 'No road route was found between these two places in the offline road data. One of them may be outside the area this map covers, or not on a road.', 'code': 'no_route'}
        return {'error': f'The route could not be worked out ({type(e).__name__}).', 'code': 'failed'}
    steps, line = [], []
    for leg in trip['legs']:
        line += decode_line(leg['shape'])
        for m in leg['maneuvers']:
            steps.append(step(m.get('instruction', ''), m.get('length', 0), m.get('street_names') or []))
    total = round(trip['summary']['length'], 1)
    fuel = fuel_along(line, fuel_sources(data, extra_fuel))
    return {'miles': total, 'seconds': int(trip['summary']['time']), 'time': clock(trip['summary']['time']), 'steps': steps, 'roads': main_roads(steps, total),
            'line': [[round(a, 5), round(b, 5)] for a, b in thin(line)], 'fuel': fuel, 'fuel_count': len(fuel), 'fuel_gap': list(longest_gap(fuel, total)),
            'straight_miles': round(miles_between(start, end), 1)}


def step(instruction, miles, roads):
    """One turn: what to do, then how far to go and on which road. 'Take exit 144B.' alone does not say the next 145 miles are on I 10."""
    miles = round(miles, 1)
    return {'text': instruction.strip(), 'miles': miles, 'roads': roads, 'then': (f'{miles:g} mi' + (' on ' + ' / '.join(roads[:2]) if roads else '')) if miles >= 0.1 else ''}


def main_roads(steps, total):
    """The long stretches in order, [[road, miles], ...]: the answer to 'which highways'. A stretch counts from 2 miles or 2% of the trip."""
    out = []
    for s in steps:
        if s['roads'] and s['miles'] >= max(2.0, total * 0.02):
            if out and out[-1][0] == s['roads'][0]:
                out[-1][1] = round(out[-1][1] + s['miles'], 1)
            else:
                out.append([s['roads'][0], s['miles']])
    return out
