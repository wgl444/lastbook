#!/usr/bin/env python3
"""Survival Guide — a task-first front end for the offline Survival & Rebuilding Library.

  python3 server.py                 # http://127.0.0.1:8890
  python3 server.py --lan           # also reachable from phones on the same Wi-Fi (no internet needed)
  python3 server.py --data <folder> # data folder holding library-data/ and derived/ (or set LASTBOOK_DATA)

It builds on engine/library.py for every original file, the full-text index, ZIM encyclopedias and video Range
requests, and adds a few read-only endpoints:

  /api/topics                 curated needs and learning routes, with source details resolved from catalog.json
  /api/find?q=&kind=          plain-language search (synonyms, grouping by document, trusted-source ranking)
  /api/doc/<id>               one catalog record (+ linked video/transcript)
  /api/page/<id>/<n>          text of one page;  /api/page/<id>/<n>.png  rendered page image (cached)
  /api/transcript/<video id>  timed transcript cues
  /api/shelves, /api/shelf/<id>   browse the whole collection by plain-language shelf
  /api/map/tile/<z>/<x>/<y>   offline street map vector tile (SoCal OpenStreetMap)
  /api/map/near?lat=&lon=     nearest help points (hospital, pharmacy, water, fire station...)
  /api/places?q=              place finder (ZIP codes, towns, named places)
  /api/battery                this computer's battery (Linux and Mac so far), read when asked; this computer only
  /api/not-found              notes of things the person couldn't find, kept only when they ask; this computer only

Nothing in library-data is written. Caches go to derived/cache.
"""
import argparse, functools, html, io, ipaddress, json, math, os, re, sqlite3, struct, sys, threading, time, traceback, urllib.parse, zlib
from array import array
from pathlib import Path

if __name__ == '__main__':  # --data must be known before the paths below are worked out
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument('--data')
    _data = _pre.parse_known_args()[0].data
    if _data:
        os.environ['LASTBOOK_DATA'] = _data

import engine.paths as lastbook_paths  # noqa: E402  (data folder and program folder can differ in the packaged app)
from engine.paths import ro_uri  # noqa: E402
from engine import saved_pages, address_search, encyclopedias  # noqa: E402
from engine.setup import api as setup_api  # noqa: E402  (the one module that may download: only when the person asks)
HERE = lastbook_paths.DATA  # holds derived/
APP = lastbook_paths.APP  # holds web/ and content/
LIB = lastbook_paths.LIB
WEB = APP / 'web'
CACHE = HERE / 'derived' / 'cache'
MAPDB = HERE / 'derived' / 'geo' / 'map.sqlite'
PLACEDB = HERE / 'derived' / 'geo' / 'places.sqlite'
os.environ.setdefault('SURVIVAL_DISABLE_INDEX_WATCH', '1')
import engine.library as library  # noqa: E402  (provides Handler, search DB path, ZIM reader)
import retrieval, assistant  # noqa: E402
import secrets  # noqa: E402
remote_records = library.remote_records  # videos moved to remote drives by the library's workers; none in the packaged app
INDEX = None  # retrieval.Index, created in main()
LAN_KEY = None
SERVER = None  # the running HTTP server, so the packaged app's launcher can stop it

_pdf_lock = threading.Lock()  # PDFium is not thread-safe

# ---------------------------------------------------------------- catalog ----
_cat = {'mtime': 0, 'by_id': {}, 'by_path': {}, 'video_tx': {}, 'thumbs': {}}
_cat_lock = threading.Lock()


_excl = {'mtime': None, 'ids': set()}


def excluded_videos():
    p = LIB / 'video-exclusions.json'
    try:
        m = p.stat().st_mtime
    except FileNotFoundError:
        return set()
    if m != _excl['mtime']:
        try:
            ids = set(json.loads(p.read_text(encoding='utf-8')).get('videos', {}))
            _excl.update(mtime=m, ids=ids | {'metadata-' + i for i in ids})
            _cat['mtime'] = 0  # re-filter the catalog with the new list
        except (ValueError, OSError):
            pass
    return _excl['ids']


def catalog():
    excluded_videos()
    p = LIB / 'catalog.json'
    m = p.stat().st_mtime
    if m != _cat['mtime']:
        with _cat_lock:
            if m != _cat['mtime']:
                try:
                    recs = json.loads(p.read_text(encoding='utf-8'))
                except ValueError:  # being rewritten by the indexer right now; keep the old copy
                    return _cat
                excl = excluded_videos()
                if excl:  # rejected videos never appear, even if a catalog rebuild brings them back
                    bad_paths = {r['path'] for r in recs if r['id'] in excl}
                    recs = [r for r in recs if r['id'] not in excl and r.get('video_id') not in excl
                            and not (r.get('video_path') and r['video_path'] in bad_paths)]
                by_id = {r['id']: r for r in recs}
                tx, thumbs = {}, {}
                for r in recs:
                    if r['kind'] == 'transcript' and r.get('video_path'):
                        tx[r['video_path']] = r
                    if r['kind'] == 'metadata' and r.get('video_path') and r.get('thumbnail_path'):
                        thumbs[r['video_path']] = r['thumbnail_path']
                _cat.update(mtime=m, by_id=by_id, by_path={r['path']: r for r in recs}, video_tx=tx, thumbs=thumbs)
    return _cat


TRUST = [  # (pattern on publisher+collection, label shown to people, ranking weight)
    (r'World Health|WHO\b|ICRC|IFRC|Red Cross|Médecins|MSF|UNICEF|Sphere|St John Ambulance|CAWST', 'International health & humanitarian guidance', 1.5),
    (r'Hesperian|Where There Is No|Where Women|Book for Midwives|Community Guide to Environmental|Health Actions|Workers\' Guide', 'Hesperian community health guide', 1.45),
    (r'FEMA|Ready\.gov|CDC|Centers for Disease|EPA|Environmental Protection|NOAA|National Weather|USDA|Food Safety|National Park|USGS|Department|Agency|U\.S\.|US Army|Army|Navy|Census|NIOSH|OSHA|NASA|FAA|Cal OES|California|County of|City of', 'Government guidance', 1.4),
    (r'National Center for Home Food Preservation|Extension|University|UC |College|Agricultural|Food Hero|UWyo', 'University / extension service', 1.35),
    (r'MIT|OpenStax|OpenCourseWare|NIST', 'Course or textbook', 1.1),
    (r'Kiwix|Wikipedia|Wikibooks|Appropedia|MDWiki', 'Community-written encyclopedia', 1.0),
    (r'Coalcracker|Corporals Corner|Canterbury|Primitive Technology|creator-', 'Hobbyist demonstration video', 0.8),
]


def trust(r):
    s = f"{r.get('publisher', '')} {r.get('collection', '')} {r.get('category', '')}"
    for rx, label, w in TRUST:
        if re.search(rx, s):
            return label, w
    if r.get('equipment_class') or r.get('model'):
        return 'Manufacturer manual (model-specific)', 1.05
    return 'Reference', 1.0


def kind_label(r):
    k = r['kind']
    return {'pdf': 'PDF', 'html': 'Saved web page', 'video': 'Video', 'transcript': 'Transcript', 'pptx': 'Slides',
            'zim': 'Encyclopedia', 'metadata': 'Video details'}.get(k, k)


def year_of(r):
    for k in ('edition', 'publication_date', 'upload_date'):
        v = str(r.get(k) or '')
        m = re.search(r'(19|20)\d\d', v)
        if m:
            return m.group(0)
    m = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', r.get('title', ''))
    return m.group(0) if m else None


def public(r, full=False):
    """The fields the front end needs, with friendly labels. Never includes local auth or worker state."""
    if not r:
        return None
    t, _ = trust(r)
    out = {'id': r['id'], 'title': clean_title(r), 'kind': r['kind'], 'kindLabel': kind_label(r),
           'publisher': r.get('publisher'), 'collection': r.get('collection'), 'path': r['path'],
           'pages': r.get('pages') or r.get('slides'), 'duration': r.get('duration'), 'year': year_of(r),
           'trust': t, 'bytes': r.get('bytes')}
    if r['kind'] == 'video':
        tx = catalog()['video_tx'].get(r['path'])
        out['transcript'] = bool(tx or r.get('captions'))
        th = catalog()['thumbs'].get(r['path'])
        out['thumb'] = th if th and (LIB / th).is_file() else None
    if (r.get('searchability') or '').startswith('image-only'):
        out['imageOnly'] = True
    rem = remote_records().get(r['path']) if r['kind'] == 'video' else None
    if rem:
        out['remote'] = rem.get('host')
    if full:
        for k in ('notes', 'edition', 'retrieved_at', 'source_url', 'license', 'license_url', 'searchability',
                  'upload_date', 'publication_date', 'model', 'course', 'level', 'caption_type'):
            if r.get(k):
                out[k] = r[k]
        if r['kind'] == 'transcript' and r.get('video_path'):
            v = catalog()['by_path'].get(r['video_path'])
            if v:
                out['videoId'] = v['id']
        if r['kind'] == 'video':
            tx = catalog()['video_tx'].get(r['path'])
            if tx:
                out['transcriptId'] = tx['id']
                out['caption_type'] = tx.get('caption_type')
    return out


def clean_title(r):
    t = r['title']
    t = re.sub(r'^(\d{3}(\s\d+)?)\s+', '', t)          # Hesperian chapter file numbers
    t = re.sub(r'\.pdf$', '', t, flags=re.I)
    if re.fullmatch(r'[\w\-]+', t) and '_' in t:
        t = t.replace('_', ' ')
    return t


# ------------------------------------------------------------------ topics ----
def topics():
    out = []
    for f in sorted((APP / 'content' / 'topics').glob('*.json')):
        try:
            t = json.loads(f.read_text(encoding='utf-8'))
        except ValueError as e:
            out.append({'id': f.stem, 'error': str(e)})
            continue
        out.append(t)
    return out


def zim_names():
    """The encyclopedia files this library really holds. Its folder decides, so one rule covers every size of pack.
    Only files the /zim/ reader would open (engine/safe_paths.py): a link out of collections/ is not offered or cited."""
    return encyclopedias.list_files(LIB)


def item_here(i, zims):
    """Is what this guide item opens in this library? An item that opens nothing is hidden, not sent Home with no message."""
    if i.get('zim'):
        parts = i['zim'].split('/')
        return len(parts) > 1 and parts[1] in zims
    return not i.get('doc') or i['doc'] in catalog()['by_id']


def resolve_item(i):
    i = dict(i)
    if i.get('doc'):
        r = catalog()['by_id'].get(i['doc'])
        i['src'] = public(r, full=False) if r else None
        if r:
            i['src']['notes'] = r.get('notes')
    return i


def topics_resolved():
    res = []
    try:
        gaps = json.loads((APP / 'content' / 'gaps.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        gaps = {}
    zims = zim_names()

    def here(items):
        return [resolve_item(i) for i in items if item_here(i, zims)]

    def still_useful(step):  # one that had things to open and has none left
        return step.get('goto') or step['read'] or not step['had']

    for t in topics():
        t = dict(t)
        t['gaps'] = gaps.get(t['id'], {})
        for k in ('prepare', 'learn'):
            t[k] = here(t.get(k, []))
        # A warning stands without its source. It only loses the citation that would open nothing.
        t['limits'] = [resolve_item(i) if item_here(i, zims) else {'text': i.get('text')} for i in t.get('limits', [])]
        for k in ('choices', 'route'):
            steps = [dict(c, read=here(c.get('read', [])), had=bool(c.get('read'))) for c in t.get(k, [])]
            t[k] = [{a: b for a, b in c.items() if a != 'had'} for c in steps if still_useful(c)]
        res.append(t)
    return res


# ------------------------------------------------------------------ search ----
STOP = set('a an and are as at be but by can do does for from get got have help how i if in into is it its me my need '
           'no not of on or our please should so some that the their them then there these they this to up us was we '
           'what when where which who why will with without you your'.split())
# Everyday words -> words the manuals actually use. Each group is OR-ed; groups are AND-ed.
SYN = {
    'cut': ['wound', 'wounds', 'laceration', 'bleeding'], 'cuts': ['wound', 'wounds', 'laceration'],
    'bleed': ['bleeding', 'haemorrhage', 'hemorrhage'], 'bleeding': ['haemorrhage', 'hemorrhage', 'blood loss'],
    'blood': ['bleeding'], 'burn': ['burns', 'scald'], 'burned': ['burns'], 'burnt': ['burns'],
    'broken': ['fracture', 'fractures'], 'bone': ['fracture', 'bones'], 'sprain': ['sprains', 'strain', 'twisted'],
    'poop': ['faeces', 'feces', 'excreta', 'latrine'], 'pee': ['urine'], 'toilet': ['latrine', 'toilets', 'sanitation', 'excreta'],
    'sewage': ['sewer', 'wastewater', 'excreta'], 'trash': ['garbage', 'refuse', 'waste', 'rubbish'], 'garbage': ['refuse', 'waste', 'rubbish'],
    'purify': ['purification', 'disinfect', 'disinfection', 'treatment'], 'clean': ['disinfect', 'safe'],
    'drinking': ['potable', 'drinking'], 'filter': ['filtration', 'filters'], 'bleach': ['chlorine', 'hypochlorite'],
    'blackout': ['outage', 'power outage'], 'outage': ['blackout'], 'electricity': ['electric', 'power'],
    'power': ['electricity', 'outage'], 'light': ['lighting', 'flashlight', 'lantern'], 'flashlight': ['torch', 'lantern'],
    'generator': ['generators'], 'solar': ['photovoltaic', 'pv'], 'battery': ['batteries'],
    'cook': ['cooking', 'stove'], 'cooking': ['stove', 'cook'], 'stove': ['stoves', 'cooking'], 'fridge': ['refrigerator', 'refrigerated'],
    'spoiled': ['spoilage', 'unsafe food'], 'rotten': ['spoilage', 'spoiled'],
    'canning': ['canned', 'canner'], 'pickle': ['pickling', 'pickled'],
    'cold': ['hypothermia', 'cold weather', 'winter'], 'freezing': ['hypothermia', 'frostbite'], 'hot': ['heat'],
    'heat': ['heat stroke', 'heat exhaustion', 'hyperthermia', 'extreme heat'], 'heatstroke': ['heat stroke'],
    'smoke': ['air quality', 'wildfire smoke'], 'quake': ['earthquake'], 'earthquake': ['earthquakes', 'seismic'],
    'flood': ['flooding', 'floods'], 'tsunami': ['tsunamis'], 'fire': ['fires', 'wildfire'], 'wildfire': ['wildfires', 'fire'],
    'baby': ['newborn', 'infant', 'babies'], 'pregnant': ['pregnancy'], 'birth': ['childbirth', 'labor', 'delivery'],
    'kid': ['child', 'children'], 'kids': ['children', 'child'], 'old': ['elderly', 'older'],
    'fever': ['temperature', 'fevers'], 'diarrhea': ['diarrhoea', 'dehydration'], 'diarrhoea': ['diarrhea'],
    'vomit': ['vomiting'], 'puke': ['vomiting'], 'sick': ['illness', 'disease'], 'medicine': ['medicines', 'drugs', 'medication'],
    'pills': ['medicines', 'tablets', 'medication'], 'painkiller': ['analgesic', 'paracetamol', 'pain'],
    'heart': ['cardiac', 'heart attack'], 'cpr': ['resuscitation', 'chest compressions'], 'choking': ['choke', 'airway'],
    'unconscious': ['unresponsive', 'recovery position'], 'bite': ['bites'], 'snake': ['snakebite', 'snakes'],
    'sting': ['stings'], 'allergic': ['allergy', 'anaphylaxis'], 'infection': ['infected', 'infections'],
    'teeth': ['tooth', 'dental'], 'tooth': ['teeth', 'dental'], 'stress': ['anxiety', 'mental health'], 'depressed': ['depression'],
    'grow': ['growing', 'planting', 'garden'], 'garden': ['gardening', 'vegetable'], 'seeds': ['seed', 'seed saving'],
    'plant': ['planting', 'sowing'], 'soil': ['compost', 'soils'], 'potato': ['potatoes'], 'potatoes': ['potato'],
    'chickens': ['poultry', 'hens'], 'chicken': ['poultry'], 'goat': ['goats'], 'bees': ['beekeeping', 'honey bee'],
    'radio': ['radios', 'amateur radio', 'ham'], 'ham': ['amateur radio'], 'phone': ['telephone', 'cell phone', 'mobile'],
    'map': ['maps', 'navigation'], 'compass': ['navigation', 'bearing'], 'lost': ['navigation', 'lost'],
    'shelter': ['shelters', 'sheltering'], 'tent': ['tents', 'tarp', 'shelter'], 'warm': ['warmth', 'heating', 'insulation'],
    'fix': ['repair', 'repairs'], 'repair': ['repairs', 'maintenance'], 'engine': ['engines', 'small engine'],
    'weld': ['welding'], 'wood': ['woodworking', 'timber', 'lumber'], 'knife': ['knives', 'sharpening'], 'sharpen': ['sharpening'],
    'knot': ['knots'], 'rope': ['ropes', 'knots'], 'pump': ['pumps'], 'well': ['wells', 'borehole'],
    'rain': ['rainwater'], 'gas': ['natural gas', 'propane', 'gas leak'], 'leak': ['leaks', 'leaking'],
    'carbon': ['monoxide'], 'co': ['carbon monoxide'], 'mold': ['mould'], 'rats': ['rodents', 'rodent'], 'mosquito': ['mosquitoes'],
}
CLINICAL = re.compile(r'surg|clinical|district hospital|essential drugs|tuberculosis|obstetric|cholera epidemic|measles epidemic|anaesth|war surgery', re.I)
GENERAL = re.compile(r'first aid|emergency|basic|household|home|beginner|guide|handbook|where there is no', re.I)
KIND_FILTERS = {'read': ('pdf', 'html', 'pptx'), 'video': ('video', 'transcript'), 'all': None}


def fts_expr(groups):
    def q(t):
        return '"' + t.replace('"', '""') + '"'
    return ' AND '.join('(' + ' OR '.join(q(t) for t in g) + ')' for g in groups)


PHRASES = [  # everyday phrases -> one idea, checked before single words
    (r"power (cut|outage|failure)|no (power|electricity)|lights? (are )?out|blackout", ['outage', 'blackout', 'power outage']),
    (r"(phone|cell|mobile)( is| was)? (dead|flat|out of battery)|(dead|flat) (phone|battery)", [['phone', 'phones', 'cell phone'], ['charge', 'charging', 'charger', 'power bank']]),
    (r"(water|tap) (is )?(off|not working|stopped)|no (running )?water", ['water']),
    (r"toilet (is )?(not working|won'?t flush|broken)|can'?t flush", ['latrine', 'toilet', 'excreta']),
    (r"heart attack", ['heart attack', 'chest pain']),
    (r"(passed|blacked) out|fainted", ['unconscious', 'fainting', 'unresponsive']),
    (r"food (went|gone|is) bad|spoiled food", ['spoilage', 'food safety']),
    (r"can'?t breathe|not breathing|stopped breathing", ['breathing', 'resuscitation', 'airway']),
]


def query_groups(q):
    q = q.lower().replace('\u2019', "'")
    phrase_groups = []
    for rx, g in PHRASES:
        if re.search(rx, q):
            phrase_groups += g if isinstance(g[0], list) else [g]
            q = re.sub(rx, ' ', q)
    words = [w.lower() for w in re.findall(r"[\w'-]+", q)]
    words = [w.strip("'") for w in words if w.strip("'")]
    content = [w for w in words if w not in STOP] or ([] if phrase_groups else words)
    groups = []
    for w in content:
        g = [w] + SYN.get(w, [])
        if w.endswith('s') and len(w) > 4 and w[:-1] not in g:
            g.append(w[:-1])
        groups.append(g)
    groups = phrase_groups + groups
    content = [g[0] for g in phrase_groups] + content
    return content, groups


def search_ids(keep):
    """Doc ids in the search index whose result passes keep (a transcript stands for its video), as a JSON list for
    `doc_id IN (SELECT value FROM json_each(?))`: one parameter however many ids, so a LIMIT counts only those rows."""
    cat = catalog()
    ids = []
    for r in cat['by_id'].values():
        v = cat['by_path'].get(r.get('video_path')) if r['kind'] == 'transcript' else None
        if keep(v or r):
            ids.append(r['id'])
    return json.dumps(ids)


@functools.lru_cache(maxsize=256)
def find(q, kind='all', limit=30):
    content, groups = query_groups(q)
    if not groups:
        return {'q': q, 'results': [], 'expanded': []}
    allowed = KIND_FILTERS.get(kind)
    # Videos are a small share of the index, so a broad word fills the LIMIT with manual pages and the filter below
    # leaves nothing: count only video rows in the query itself. Manual pages are most of the index and need no help,
    # and filtering in the query costs a read of every matching row, so To read and Everything keep the plain query.
    only = (search_ids(lambda r: r['kind'] in allowed),) if kind == 'video' else ()
    con = sqlite3.connect(ro_uri(library.DB), uri=True)
    sql = ("SELECT doc_id,page,snippet(pages,6,'\u0001','\u0002',' … ',28),bm25(pages,0,6,1,1,0,0,1) "
           "FROM pages WHERE pages MATCH ?" + (" AND doc_id IN (SELECT value FROM json_each(?))" if only else "") +
           " ORDER BY bm25(pages,0,6,1,1,0,0,1) LIMIT 600")
    rows, mode = [], 'all words'
    try:
        rows = con.execute(sql, (fts_expr(groups),) + only).fetchall()
        if len(rows) < 8 and len(groups) > 1:
            # relax: any of the ideas, rank documents that have more of them higher
            mode = 'some words'
            rows += con.execute(sql, (' OR '.join('(' + fts_expr([g]) + ')' for g in groups),) + only).fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    cat = catalog()['by_id']
    docs = {}
    for doc_id, page, snip, score in rows:
        r = cat.get(doc_id)
        if not r or r['kind'] == 'metadata':
            continue
        if r['kind'] == 'transcript':  # show the video itself, with the transcript hit
            v = catalog()['by_path'].get(r.get('video_path'))
            if v:
                r, page = v, None
        if allowed and r['kind'] not in allowed:
            continue
        d = docs.get(r['id'])
        if not d:
            d = docs[r['id']] = {'r': r, 'score': 0.0, 'hits': []}
        if len(d['hits']) < 3 and not any(h['page'] == page for h in d['hits']):
            d['hits'].append({'page': page if r['kind'] == 'pdf' else None, 'snippet': snip})
        d['n'] = d.get('n', 0) + 1
        if d['n'] == 1:
            d['score'] = -score
        elif d['n'] <= 4:
            d['score'] += -score * 0.1  # a little credit for a few more matching pages, not for sheer length
    title_words = set(w for g in groups for w in g)
    out = []
    for d in docs.values():
        r = d['r']
        t, w = trust(r)
        title = r['title'].lower()
        tb = sum(1 for g in groups if any(x in title for x in g)) / len(groups)
        s = d['score'] * w * (1 + tb)
        if re.search(r'Spanish|español|\bkit de\b', r['title'] + r.get('notes', ''), re.I):
            s *= 0.5
        if CLINICAL.search(r['title'] + ' ' + r.get('collection', '')) and not CLINICAL.search(q):
            s *= 0.6  # written for surgeons/clinicians; still found, but after general guidance
        if re.search(r'^index$|front matter|table of contents|credits|^contents', clean_title(r), re.I):
            s *= 0.3  # index pages match everything but rarely answer anything
        if GENERAL.search(r['title']):
            s *= 1.25
        out.append((s, d))
    out.sort(key=lambda x: -x[0])
    results = []
    for s, d in out[:limit]:
        p = public(d['r'])
        p['hits'] = [{'page': h['page'], 'snippet': clean_snip(h['snippet'])} for h in d['hits']]
        results.append(p)
    expanded = sorted(set(w for g in groups for w in g[1:]) - set(content))
    return {'q': q, 'mode': mode, 'expanded': expanded, 'results': results}


def clean_snip(s):
    s = html.escape(re.sub(r'\s+', ' ', s or '')).replace('\u0001', '<mark>').replace('\u0002', '</mark>')
    return s


# ------------------------------------------------------------------ shelves ----
SHELVES = [
    ('health', 'Health & first aid', r'medic|first-aid|care|health|dent|mental|obstet|midwi|clinic|surg'),
    ('water', 'Water & sanitation', r'water|rain|sanit|wash|hygien'),
    ('food', 'Food: growing, keeping & cooking', r'agri|food|seed|crop|garden(?!-power)|livestock|dairy|ferment|bee|farm|soil|grow|irrigat|oil-crop|grain'),
    ('safety', 'Disasters, safety & outdoors', r'survival|shelter|disaster|prepar|safety|hazard|wildfire|knot|weather|polar|defen|radiolog|rescue|fire'),
    ('power', 'Power & energy', r'energy|power|solar|generator|offgrid|battery|inverter|charge-controller'),
    ('comms', 'Radio, communication & navigation', r'communic|radio|navigation'),
    ('repair', 'Tools, repair & equipment', r'engine|mower|tractor|vehicle|hvac|compressor|bicycle|aerial|backhoe|bearing|sharpen|weld|repair|equipment|seal|pump|snow|motorcycle|truck|car|marine|forklift|workshop|hand-tool'),
    ('making', 'Making things & materials', r'manufact|craft|glass|ceram|textil|wood|paper|metal|materials|machin|leather|amate|lime|refractor|typeset|mill|mineral|production|raw-material'),
    ('build', 'Building & infrastructure', r'civil|infrastructure|building'),
    ('community', 'Community, teaching & recovery', r'communit|educat|teach|accessib|recover|accountab|organiz|continuity|inventory'),
    ('science', 'Science, maths & engineering courses', r'math|physic|chemistr|biolog|astronom|earth|ecolog|science|engineer|control|metrolog|quality|thermo|electromag|circuit|microelec|comput|digital|space|aviation|aircraft|laborator|measurement'),
    ('creators', 'Bushcraft & primitive-skills videos', r'^creator-'),
]


def shelf_of(r):
    c = r.get('category', '').lower()
    if c.startswith('creator-'):
        return 'creators'
    for sid, _, rx in SHELVES:
        if sid != 'creators' and re.search(rx, c):
            return sid
    return 'other'


def shelves():
    counts = {}
    for r in catalog()['by_id'].values():
        if r['kind'] in ('metadata', 'transcript', 'zim'):
            continue
        counts.setdefault(shelf_of(r), {'read': 0, 'video': 0})
        counts[shelf_of(r)]['video' if r['kind'] == 'video' else 'read'] += 1
    return [{'id': s, 'title': t, **counts.get(s, {'read': 0, 'video': 0})} for s, t, _ in SHELVES] + \
        [{'id': 'other', 'title': 'Other references', **counts.get('other', {'read': 0, 'video': 0})}]


def shelf(sid, kind='all', q='', offset=0, limit=40):
    allowed = KIND_FILTERS.get(kind)
    rs = [r for r in catalog()['by_id'].values()
          if r['kind'] not in ('metadata', 'transcript', 'zim') and shelf_of(r) == sid
          and (not allowed or r['kind'] in allowed)
          and (not q or q.lower() in (r['title'] + ' ' + r.get('collection', '') + ' ' + r.get('publisher', '')).lower())]
    rs.sort(key=lambda r: (-trust(r)[1], r.get('collection', ''), r['title']))
    # group by collection so a 300-chapter manual shows as one entry
    groups = {}
    for r in rs:
        groups.setdefault(r.get('collection') or 'Other', []).append(r)
    items = [{'collection': c, 'count': len(v), 'trust': trust(v[0])[0], 'items': [public(x) for x in v[:6]]}
             for c, v in groups.items()]
    return {'total': len(rs), 'collections': items[offset:offset + limit], 'more': len(items) > offset + limit}


def collection_items(name, offset=0, limit=60):
    rs = sorted([r for r in catalog()['by_id'].values() if r.get('collection') == name and r['kind'] not in ('metadata',)],
                key=lambda r: r['title'])
    return {'total': len(rs), 'items': [public(r) for r in rs[offset:offset + limit]], 'more': len(rs) > offset + limit}


# ------------------------------------------------------------------ videos ----
VROWS = [  # (id, title, exact catalog categories) — practical first, hobby channels and courses after
    ('health', 'First aid & health', 'first-aid medical care-continuity'),
    ('water', 'Water & sanitation', 'water-safety-planning water-treatment rainwater water-sanitation water-sampling sanitary-inspection pumps'),
    ('food', 'Growing & preserving food', 'food-preservation agriculture food-growing seed-saving grain-mill-assembly grain-mill-maintenance grain-mill-operation'),
    ('power', 'Power, generators & solar', 'generator generators offgrid-power'),
    ('outdoors', 'Shelter, fire & outdoor skills', 'field-survival fire-and-shelter knots weather communications-navigation'),
    ('repair', 'Tools, engines & repair', 'small-engine-repair lawn-mower garden-power-equipment snow-blower woodworking welding welding-equipment sharpening workshop milling-layout-and-fit bearing-maintenance seals-installation road-vehicles marine heavy-ag-machinery machining-tool-geometry measurement'),
    ('making', 'Making things by hand', 'craft-manufacturing-including-ceramics-and-textiles glass-manufacturing-processes refractory-installation refractory-process-control typesetting-printing-binding bark-to-amate-paper'),
    ('coalcracker', 'Coalcracker Bushcraft', 'creator-coalcracker-bushcraft'),
    ('corporals', 'Corporals Corner', 'creator-corporals-corner'),
    ('canterbury', 'Dave Canterbury', 'creator-david-canterbury'),
    ('primitive', 'Primitive Technology', 'creator-primitive-technology'),
    ('courses', 'Courses, lectures & more', ''),
]
VROW_NOTE = {'coalcracker': 'Hobbyist channel', 'corporals': 'Hobbyist channel', 'canterbury': 'Hobbyist channel',
             'primitive': 'Hobbyist channel — no narration', 'courses': 'University lectures (MIT and others)'}


def vrow_of(r):
    c = r.get('category', '')
    for vid, _, cats in VROWS:
        if c in cats.split():
            return vid
    return 'courses'


def vcard(r):
    p = public(r)
    return {k: p.get(k) for k in ('id', 'title', 'publisher', 'duration', 'thumb', 'transcript', 'trust', 'year')}


def vsort(r):
    t = trust(r)[1]
    return (-t, not catalog()['video_tx'].get(r['path']) and not r.get('captions'), not catalog()['thumbs'].get(r['path']), r['title'])


def guide_videos():
    out, seen = [], set()
    for t in topics():
        items = [i for c in t.get('choices', []) for i in c.get('read', [])] + t.get('learn', []) + t.get('prepare', []) + \
            [i for st in t.get('route', []) for i in st.get('read', [])]
        for i in items:
            r = catalog()['by_id'].get(i.get('doc') or '')
            if r and r['kind'] == 'video' and r['id'] not in seen:
                seen.add(r['id'])
                out.append(dict(vcard(r), guide=t['id'], guideTitle=t.get('title')))
    return out


def videos_home():
    rows = {}
    for r in catalog()['by_id'].values():
        if r['kind'] == 'video':
            rows.setdefault(vrow_of(r), []).append(r)
    res = [{'id': 'guides', 'title': 'Picked for the guides', 'note': 'Chosen to go with the survival guides', 'count': len(guide_videos()), 'items': guide_videos()[:16]}]
    for vid, title, _ in VROWS:
        rs = sorted(rows.get(vid, []), key=vsort)
        if VROW_NOTE.get(vid, '').startswith('Hobbyist'):
            rs.sort(key=lambda r: r.get('upload_date') or '', reverse=True)  # channels: newest first, not A–Z
        if rs:
            res.append({'id': vid, 'title': title, 'note': VROW_NOTE.get(vid), 'count': len(rs), 'items': [vcard(r) for r in rs[:16]]})
    return res


def videos_list(row, q='', length='', sort='', offset=0, limit=48):
    if row == 'guides':
        rs = [catalog()['by_id'][v['id']] for v in guide_videos()]
    else:
        rs = [r for r in catalog()['by_id'].values() if r['kind'] == 'video' and (row in ('', 'all') or vrow_of(r) == row)]
    if q:
        words = q.lower().split()
        rs = [r for r in rs if all(w in (r['title'] + ' ' + r.get('publisher', '')).lower() for w in words)]
    d = lambda r: r.get('duration') or 0
    if length == 'short':
        rs = [r for r in rs if d(r) and d(r) < 600]
    elif length == 'medium':
        rs = [r for r in rs if 600 <= d(r) < 1800]
    elif length == 'long':
        rs = [r for r in rs if d(r) >= 1800]
    if sort == 'short':
        rs.sort(key=d)
    elif sort == 'new':
        rs.sort(key=lambda r: r.get('upload_date') or '', reverse=True)
    elif sort == 'az':
        rs.sort(key=lambda r: r['title'].lower())
    elif row != 'guides':
        rs.sort(key=vsort)
    title = dict((v[0], v[1]) for v in VROWS).get(row, 'Picked for the guides' if row == 'guides' else 'All videos')
    return {'title': title, 'note': VROW_NOTE.get(row), 'total': len(rs), 'items': [vcard(r) for r in rs[offset:offset + limit]],
            'more': len(rs) > offset + limit}


def videos_related(vid, limit=12):
    v = catalog()['by_id'].get(vid)
    if not v:
        return []
    row = vrow_of(v)
    same_pub = [r for r in catalog()['by_id'].values() if r['kind'] == 'video' and r['id'] != vid and r.get('publisher') == v.get('publisher')]
    same_row = [r for r in catalog()['by_id'].values() if r['kind'] == 'video' and r['id'] != vid and vrow_of(r) == row and r.get('publisher') != v.get('publisher')]
    words = set(w for w in re.findall(r'[a-z]{4,}', v['title'].lower()))
    score = lambda r: -len(words & set(re.findall(r'[a-z]{4,}', r['title'].lower())))
    out = sorted(same_row, key=lambda r: (score(r), vsort(r)))[:limit // 2] + sorted(same_pub, key=lambda r: (score(r), r['title']))[:limit]
    return [vcard(r) for r in out[:limit]]


def prewarm_previews():
    """Render small previews of every curated page in the background so guide pages show them instantly."""
    time.sleep(5)
    for t in topics():
        items = [i for c in t.get('choices', []) for i in c.get('read', [])] + [i for st in t.get('route', []) for i in st.get('read', [])] + t.get('prepare', []) + t.get('learn', [])
        for i in items:
            r = catalog()['by_id'].get(i.get('doc') or '')
            if r and r['kind'] == 'pdf':
                try:
                    render_page(r, i.get('page') or 1, 360)
                except Exception:
                    pass


# ------------------------------------------------------------- pages/text ----
def page_texts(r):
    p = LIB / (r.get('text_path') or f"text/{r['id']}.txt")  # catalog may be mid-rewrite and miss text_path
    if not p.is_file():
        return []
    t = p.read_text(encoding='utf-8', errors='replace')
    return t.split('\n\f\n') if '\n\f\n' in t else t.split('\f')


def png_bytes(rgb):
    """8-bit RGB array to PNG with the standard library, so the package does not need Pillow."""
    h, w = rgb.shape[:2]
    raw = b''.join(b'\0' + rgb[y, :, :3].tobytes() for y in range(h))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def render_page(r, n, width):
    width = max(160, min(1600, width))
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / f"{r['id']}-{n}-{width}.png"
    if not out.exists():
        import pypdfium2
        with _pdf_lock:
            pdf = pypdfium2.PdfDocument(str(LIB / r['path']))
            try:
                page = pdf[n - 1]
                # width is the longer side, as pdftoppm -scale-to did
                bitmap = page.render(scale=width / max(page.get_size()), rev_byteorder=True)
                data = png_bytes(bitmap.to_numpy())
            finally:
                pdf.close()
        tmp = CACHE / f".{r['id']}-{n}-{width}-{threading.get_ident()}.tmp"
        tmp.write_bytes(data)
        try:
            os.replace(tmp, out)
        except PermissionError:  # Windows: another thread is reading the same page right now
            tmp.unlink(missing_ok=True)
        return data
    return out.read_bytes()


def vtt_cues(path):
    cues = []
    for t, lines in saved_pages.caption_blocks((LIB / path).read_bytes()):
        cur = {'t': t, 'text': ''}
        for line in lines:
            txt = html.unescape(re.sub(r'<[^>]+>', '', line)).strip()
            if txt and not cur['text'].endswith(txt):
                cur['text'] = (cur['text'] + ' ' + txt).strip()
        cues.append(cur)
    # auto captions repeat lines across cues; drop repeats and merge into ~sentence chunks
    out, last = [], ''
    for c in cues:
        t = c['text']
        if t.startswith(last) and last:
            t = t[len(last):].strip()
        if not t:
            continue
        last = c['text']
        if out and c['t'] - out[-1]['t'] < 12 and len(out[-1]['text']) < 220:
            out[-1]['text'] += ' ' + t
        else:
            out.append({'t': c['t'], 'text': t})
    return out


def transcript(video_id):
    cat = catalog()
    v = cat['by_id'].get(video_id)
    if not v:
        return None
    tx = cat['video_tx'].get(v['path'])
    vtts = []
    if tx and tx.get('vtt_path'):
        vtts.append(tx['vtt_path'])
    vtts += [c for c in v.get('captions', []) if c.endswith('.vtt')]
    for p in vtts:
        if (LIB / p).is_file():
            return {'source': (tx or {}).get('caption_type') or 'captions', 'notes': (tx or {}).get('notes'), 'cues': vtt_cues(p)}
    if tx and (LIB / tx['path']).is_file():
        return {'source': tx.get('caption_type') or 'transcript', 'notes': tx.get('notes'),
                'cues': [{'t': None, 'text': x} for x in (LIB / tx['path']).read_text(encoding='utf-8', errors='replace').split('\n\n') if x.strip()]}
    return {'source': None, 'cues': []}


# ------------------------------------------------------------------- map ----
_tls = threading.local()


def mapdb():
    if not MAPDB.exists():
        return None
    if not hasattr(_tls, 'map'):
        _tls.map = sqlite3.connect(ro_uri(MAPDB), uri=True, check_same_thread=False)
    return _tls.map


def national_mapdb():
    path = HERE / 'derived/geo/national.sqlite'
    if not path.exists():
        return None
    if not hasattr(_tls, 'national_map'):
        _tls.national_map = sqlite3.connect(ro_uri(path), uri=True, check_same_thread=False)
        _tls.national_map.execute('PRAGMA query_only=ON')
    return _tls.national_map


def national_status():
    path = HERE / 'derived/geo/national-status.json'
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'state':'not-built'}


SOCAL_CELL = 0.05
_socal = {'cells': None}


def socal_cells():
    """0.05° cells holding detailed SoCal features; national rows inside them are duplicates and are skipped."""
    if _socal['cells'] is None:
        cache = HERE / 'derived/geo/socal-cells.json'
        try:
            _socal['cells'] = set(json.loads(cache.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            db = mapdb()
            cells = set() if db is None else {r[0] for r in db.execute(
                'SELECT DISTINCT CAST((minx+180)/? AS INT)*100000+CAST((miny+90)/? AS INT) FROM feat_idx WHERE maxx-minx<?',
                (SOCAL_CELL, SOCAL_CELL, SOCAL_CELL))}
            cache.write_text(json.dumps(sorted(cells)))
            _socal['cells'] = cells
    return _socal['cells']


def in_socal(lon, lat):
    return int((lon + 180) / SOCAL_CELL) * 100000 + int((lat + 90) / SOCAL_CELL) in socal_cells()


def unpack(blob):
    a = array('i')
    a.frombytes(blob)
    parts, i = [], 1
    for _ in range(a[0]):
        n = a[i]; i += 1
        x = y = 0
        pts = []
        for _ in range(n):
            x += a[i]; y += a[i + 1]; i += 2
            pts.append((x / 1e6, y / 1e6))
        parts.append(pts)
    return parts


def tile_bounds(z, x, y):
    n = 2 ** z
    lon0, lon1 = x / n * 360 - 180, (x + 1) / n * 360 - 180
    lat0 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon0, lat0, lon1, lat1


def clip_ring(pts, x0, y0, x1, y1):
    """Sutherland-Hodgman clip of a polygon ring (tile pixel space) to a box."""
    def clip(pts, inside, inter):
        out = []
        for i in range(len(pts)):
            a, b = pts[i - 1], pts[i]
            if inside(b):
                if not inside(a):
                    out.append(inter(a, b))
                out.append(b)
            elif inside(a):
                out.append(inter(a, b))
        return out

    def ix(xv):
        return lambda a, b: (xv, a[1] + (b[1] - a[1]) * (xv - a[0]) / ((b[0] - a[0]) or 1e-9))

    def iy(yv):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (yv - a[1]) / ((b[1] - a[1]) or 1e-9), yv)
    for inside, inter in ((lambda p: p[0] >= x0, ix(x0)), (lambda p: p[0] <= x1, ix(x1)),
                          (lambda p: p[1] >= y0, iy(y0)), (lambda p: p[1] <= y1, iy(y1))):
        pts = clip(pts, inside, inter)
        if not pts:
            break
    return pts


EXT = 512


TERRAINDB = HERE / 'derived/geo/terrain.sqlite'


TERRAIN_DETAIL = HERE / 'derived/geo/terrain-detail.sqlite'


def terrain_tile(z, x, y):
    """Elevation tile (terrain-RGB PNG) or None for open ocean / outside the data: zoom 4-12 nationwide (30 m),
    zoom 13-17 from 1 m lidar around chosen places (tools/build_terrain_detail.py)."""
    path, attr = (TERRAIN_DETAIL, 'terrain_detail') if z >= 13 else (TERRAINDB, 'terrain')
    if not path.exists():
        return None
    db = getattr(_tls, attr, None)
    if db is None:
        db = sqlite3.connect(ro_uri(path), uri=True, check_same_thread=False)
        setattr(_tls, attr, db)
    try:
        row = db.execute('SELECT png FROM tile WHERE z=? AND x=? AND y=?', (z, x, y)).fetchone()
    except sqlite3.OperationalError:  # still being created
        return None
    return row[0] if row else None


def crop_tile(z, x, y):
    """Orchard / vineyard colour overlay (RGBA PNG) or None."""
    if not CROPDB.exists():
        return None
    db = getattr(_tls, 'crops', None)
    if db is None:
        db = _tls.crops = sqlite3.connect(ro_uri(CROPDB), uri=True, check_same_thread=False)
    try:
        row = db.execute('SELECT png FROM tile WHERE z=? AND x=? AND y=?', (z, x, y)).fetchone()
    except sqlite3.OperationalError:  # rebuilding
        return None
    return row[0] if row else None


def data_version():
    """Changes whenever map data changes: every 20 s while the national map builds, and when the water inventory is written."""
    status = national_status()
    v = f"n{int(time.time() // 20)}" if status.get('state') == 'building' else f"n{status.get('features', 0)}"
    for f in (WATERDB, WATERDB.with_name(WATERDB.name + '-wal'), CROPDB, HUTSDB, TERRAINDB, BLDGDB, BLDGDB.with_name(BLDGDB.name + '-wal')):
        try:
            v += f".{int(f.stat().st_mtime) // 60}"
        except OSError:
            pass
    return v


def map_tile(z, x, y):
    version = data_version()
    if z > 9 or national_status().get('state') != 'complete':
        return _map_tile(z, x, y, version)
    # zoomed-out tiles cover huge areas and are slow to assemble: keep them on disk once the data has settled
    path = HERE / f'derived/geo/tilecache/{version}/{z}/{x}/{y}.json'
    try:
        return path.read_bytes()
    except OSError:
        data = _map_tile(z, x, y, version)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f'.{threading.get_ident()}.tmp')
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError:
            pass
        return data


@functools.lru_cache(maxsize=2048)
def _map_tile(z, x, y, version):
    db = mapdb()
    if db is None:
        return b'{"error":"map not built"}'
    lon0, lat0, lon1, lat1 = tile_bounds(z, x, y)
    pad = (lon1 - lon0) * 0.04
    n = 2 ** z

    def proj(lon, lat):
        px = ((lon + 180) / 360 * n - x) * EXT
        s = math.sin(math.radians(max(-85, min(85, lat))))
        py = ((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n - y) * EXT
        return px, py
    rows = db.execute('SELECT f.layer,f.cls,f.name,f.geom FROM feat_idx i JOIN feat f ON f.id=i.id '
                      'WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? AND i.minz<=? ORDER BY f.id',
                      (lon0 - pad, lon1 + pad, lat0 - pad, lat1 + pad, z)).fetchall()
    national = national_mapdb()
    if national is not None and z >= 11:
        c = SOCAL_CELL
        cells = socal_cells()
        if all(i * 100000 + j in cells for i in range(int((lon0 + 180) / c), int((lon1 + 180) / c) + 1)
               for j in range(int((lat0 + 90) / c), int((lat1 + 90) / c) + 1)):
            national = None  # fully covered by the detailed SoCal map
    if national is not None:
        rows += national.execute('SELECT f.layer,f.cls,f.name,f.geom FROM feat_idx i JOIN feat f ON f.id=i.id '
                      'WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? AND i.minz<=? ORDER BY f.id',
                      (lon0 - pad, lon1 + pad, lat0 - pad, lat1 + pad, z)).fetchall()
    if WATERDB.exists():  # NHD lakes, reservoirs, ponds and marsh outlines
        wdb = getattr(_tls, 'water', None) or sqlite3.connect(ro_uri(WATERDB), uri=True, check_same_thread=False)
        _tls.water = wdb
        rows += wdb.execute('SELECT f.layer,f.cls,f.name,f.geom FROM feat_idx i JOIN feat f ON f.id=i.id '
                            'WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? AND i.minz<=?',
                            (lon0 - pad, lon1 + pad, lat0 - pad, lat1 + pad, z)).fetchall()
    layers = {}
    tol = 2.0 if z <= 6 else 1.2 if z <= 9 else 0.7 if z < 15 else 0.4
    for layer, cls, name, geom in rows:
        if layer == 'bldg' and z < 15:
            continue
        parts = unpack(geom)
        polygon = layer in ('water', 'wet', 'land', 'beach', 'bldg', 'ocean', 'island')
        geo = []
        for p in parts:
            pts = [proj(lo, la) for lo, la in p]
            if polygon and len(pts) > 200:
                pts = clip_ring(pts, -16, -16, EXT + 16, EXT + 16)
            q, last = [], None
            for px, py in pts:
                if last is None or abs(px - last[0]) > tol or abs(py - last[1]) > tol:
                    q += [round(px, 1), round(py, 1)]
                    last = (px, py)
            if polygon and len(q) < 6:
                continue
            if not polygon and layer != 'place' and len(q) < 4 and z < 12:
                continue  # sub-pixel line at this zoom
            if layer == 'place':
                q = [round(pts[0][0], 1), round(pts[0][1], 1)]
            if q:
                geo.append(q)
        if geo:
            layers.setdefault(layer, []).append([cls, name, geo])
    if z >= 15:
        ms_buildings(layers, z, x, y, tol, version)
    # Service symbols are not baked into tiles; /api/map/pins serves them clustered and filterable.
    return json.dumps({'z': z, 'layers': layers}, separators=(',', ':')).encode()


BLDGDB = HERE / 'derived/geo/buildings.sqlite'


@functools.lru_cache(maxsize=192)
def ms_bucket(x14, y14, version):
    """Microsoft footprints filed under zoom-14 tile x14/y14: [(minX, maxX, minY, maxY, height_dm, pts)].
    version (data_version()) is only part of the cache key, so a rebuilt database is never served stale."""
    db = getattr(_tls, 'bldg', None)
    if db is None:
        db = _tls.bldg = sqlite3.connect(ro_uri(BLDGDB), uri=True, check_same_thread=False)
    out = []
    for (data,) in db.execute('SELECT data FROM bldg WHERE x=? AND y=?', (x14, y14)):
        a = array('i'); a.frombytes(zlib.decompress(data)); i = 0
        while i < len(a):
            n, h, X, Y = a[i], a[i + 1], a[i + 2], a[i + 3]; i += 4
            pts = [(X, Y)]
            for _ in range(n - 1):
                X += a[i]; Y += a[i + 1]; i += 2; pts.append((X, Y))
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            out.append((min(xs), max(xs), min(ys), max(ys), h, pts))
    return out


AERIALDB = HERE / 'derived/geo/aerial.sqlite'


AERIAL_DETAIL = HERE / 'derived/geo/aerial-detail.sqlite'
AERIAL_COAST = HERE / 'derived/geo/aerial-coast.sqlite'


def aerial_tile(z, x, y):
    """JPEG/PNG bytes of the aerial photo tile, or None: zoom 0-16 from tools/fetch_aerial.py (national),
    zoom 17-19 from tools/build_aerial_coast.py (25 cm NOAA coastal photos) where it has them, otherwise
    tools/fetch_aerial_detail.py (full detail around chosen places)."""
    for path, attr in ((AERIAL_COAST, 'aerial_coast'), (AERIAL_DETAIL, 'aerial_detail')) if z >= 17 else ((AERIALDB, 'aerial'),):
        if not path.exists():
            continue
        db = getattr(_tls, attr, None)
        if db is None:
            db = sqlite3.connect(ro_uri(path), uri=True, check_same_thread=False)
            setattr(_tls, attr, db)
        try:
            r = db.execute('SELECT jpg FROM tile WHERE z=? AND x=? AND y=?', (z, x, y)).fetchone()
        except sqlite3.Error:
            continue
        if r:
            return r[0]
    return None


def aerial_coast_building():
    """Is tools/build_aerial_coast.py writing aerial-coast.sqlite right now (no 'built' stamp yet)?"""
    db = getattr(_tls, 'aerial_coast', None) if AERIAL_COAST.exists() else None
    try:
        return db is not None and db.execute("SELECT 1 FROM meta WHERE key='built'").fetchone() is None
    except sqlite3.Error:
        return False


def bldg3d(x14, y14):
    """Every footprint (with height) centred in zoom-14 tile x14/y14, for the 3D view, as little-endian int32:
    [number of Microsoft ints] + Microsoft records [npts, height_dm, X0, Y0, dX1, dY1, ...] (SoCal included)
    + OpenStreetMap records [npts, height_dm, min_height_dm, is_part, X0, Y0, ...] (tools/build_osm_heights.py)."""
    if not BLDGDB.exists():
        return b''
    db = getattr(_tls, 'bldg', None)
    if db is None:
        db = _tls.bldg = sqlite3.connect(ro_uri(BLDGDB), uri=True, check_same_thread=False)
    def rows(table):
        try:
            return [zlib.decompress(d) for (d,) in db.execute(f'SELECT data FROM {table} WHERE x=? AND y=?', (x14, y14))]
        except sqlite3.OperationalError:  # table not built yet
            return []
    ms = b''.join(rows('bldg') + rows('bldg_socal'))
    return struct.pack('<i', len(ms) // 4) + ms + b''.join(rows('bldg_osm'))


def ms_buildings(layers, z, x, y, tol, version):
    """Add national building footprints (outside the detailed SoCal map) to a z>=15 tile."""
    if not BLDGDB.exists():
        return
    span = 1 << (30 - z); pad = span // 25
    X0, Y0 = x * span - pad, y * span - pad
    X1, Y1 = X0 + span + 2 * pad, Y0 + span + 2 * pad
    k = EXT / span
    out = layers.setdefault('bldg', [])
    reach = 1 << 12  # footprints reaching further than ~150 m into another bucket are also filed there (build_buildings.py)
    seen = set()
    for bx in range((X0 - reach) >> 16, ((X1 + reach) >> 16) + 1):
        for by in range((Y0 - reach) >> 16, ((Y1 + reach) >> 16) + 1):
            try:
                bucket = ms_bucket(bx, by, version)
            except sqlite3.Error:
                return
            for minx, maxx, miny, maxy, h, pts in bucket:
                if maxx < X0 or minx > X1 or maxy < Y0 or miny > Y1 or pts[0] in seen:
                    continue
                seen.add(pts[0])
                q, last = [], None
                for X, Y in pts:
                    px, py = (X - x * span) * k, (Y - y * span) * k
                    if last is None or abs(px - last[0]) > tol or abs(py - last[1]) > tol:
                        q += [round(px, 1), round(py, 1)]; last = (px, py)
                if len(q) >= 6:
                    out.append(['building', None, [q]])


TILE_POI = {'hospital', 'clinic', 'doctors', 'pharmacy', 'chemist', 'fire_station', 'police', 'drinking_water', 'toilet',
            'shelter', 'community_centre', 'supermarket', 'doityourself', 'hardware', 'spring'}


HELP = {'hospital': 'Hospital', 'clinic': 'Clinic', 'doctors': 'Doctor', 'pharmacy': 'Pharmacy', 'chemist': 'Pharmacy',
        'dentist': 'Dentist', 'fire_station': 'Fire station', 'police': 'Police', 'drinking_water': 'Drinking water tap',
        'fountain': 'Fountain (not necessarily drinkable)', 'water_tower': 'Water tower', 'water_works': 'Water works',
        'spring': 'Spring (untested)', 'toilet': 'Public toilet', 'shelter': 'Shelter (structure)',
        'community_centre': 'Community centre', 'town_hall': 'Town hall', 'school': 'School', 'library': 'Library',
        'supermarket': 'Supermarket', 'convenience': 'Convenience store', 'doityourself': 'Hardware / DIY store',
        'hardware': 'Hardware store', 'garden_centre': 'Garden centre', 'post_office': 'Post office',
        'camp_site': 'Campsite', 'veterinary': 'Vet', 'nursing_home': 'Care home', 'fuel': 'Fuel station',
        'car_repair': 'Car repair', 'bicycle_shop': 'Bicycle shop', 'wastewater_plant': 'Wastewater plant',
        'comms_tower': 'Communications tower', 'park': 'Park', 'courthouse': 'Courthouse',
        'college': 'College', 'university': 'University', 'kindergarten': 'Kindergarten', 'beach': 'Beach',
        'embassy': 'Embassy', 'mineral': 'Recorded mineral / mine', 'oil_gas_well': 'Recorded oil / gas well',
        'boat_launch': 'Boating facility', 'fish_stocking': 'Fish-stocking record',
        'dam': 'Dam (National Inventory of Dams)', 'reservoir': 'Reservoir', 'lake': 'Lake or pond', 'pond': 'Pond',
        'orchard': 'Orchard', 'vineyard': 'Vineyard', 'berries': 'Berry field', 'perennial_veg': 'Asparagus field',
        'hut': 'Hut or cabin', 'trail_shelter': 'Trail shelter'}


CROP_KINDS = {'orchard', 'vineyard', 'berries', 'perennial_veg'}
HUT_KINDS = {'hut', 'trail_shelter'}  # mountain huts, cabins and lean-tos from OpenStreetMap (tools/build_huts.py)
RESOURCE_KINDS = {'mineral', 'oil_gas_well', 'boat_launch', 'fish_stocking', 'dam', 'reservoir', 'lake', 'pond'} | CROP_KINDS | HUT_KINDS
WATERDB = HERE / 'derived/geo/water.sqlite'
CROPDB = HERE / 'derived/geo/crops.sqlite'
HUTSDB = Path(os.environ.get('LASTBOOK_HUTS') or HERE / 'derived/geo/huts.sqlite')


def resource_dbs(kinds):
    """Point databases holding these resource kinds: the library's resource atlas and the derived water inventory."""
    water = {'dam', 'reservoir', 'lake', 'pond'}
    out = []
    if set(kinds) - water - CROP_KINDS - HUT_KINDS and (LIB / 'geography/resources/atlas.sqlite').exists():
        out.append(LIB / 'geography/resources/atlas.sqlite')
    if set(kinds) & water and WATERDB.exists():
        out.append(WATERDB)
    if set(kinds) & CROP_KINDS and CROPDB.exists():
        out.append(CROPDB)
    if set(kinds) & HUT_KINDS and HUTSDB.exists():
        out.append(HUTSDB)
    return out


def resource_near(lat, lon, cls, limit):
    out = []
    for path in resource_dbs([cls]):
        with sqlite3.connect(ro_uri(path), uri=True) as db:
            radius = .15
            rows = []
            while radius <= 19.2:
                rows = db.execute("SELECT r.name,r.lat,r.lon,r.detail,r.source FROM spatial s CROSS JOIN resource r ON r.id=s.id WHERE r.kind=? AND s.minx>=? AND s.maxx<=? AND s.miny>=? AND s.maxy<=?", (cls,lon-radius,lon+radius,lat-radius,lat+radius)).fetchall()
                if cls == 'fish_stocking':
                    rows = list({(r[0], round(r[1], 3), round(r[2], 3), json.loads(r[3]).get('FishType')): r for r in rows}.values())
                if len(rows) >= limit:
                    break
                radius *= 2
            for name, y, x, raw, source in rows:
                detail = json.loads(raw)
                summary = _resource_summary(cls, detail) + (' · historical stocking record' if cls == 'fish_stocking' else '')
                dist, brg = haversine(lat, lon, y, x)
                out.append({'cls':cls,'label':HELP[cls],'name':name,'lat':y,'lon':x,'km':round(dist,2),'dir':brg,'detail':summary,'source':source})
    return sorted(out, key=lambda p:p['km'])[:limit]


def resource_view(west, south, east, north, zoom, kinds):
    path = LIB / 'geography/resources/atlas.sqlite'
    if not path.exists():
        return {'points': [], 'records': 0}
    kinds = [k for k in kinds if k in RESOURCE_KINDS]
    if not kinds:
        return {'points': [], 'records': 0}
    west, east = max(-180, west), min(180, east)
    south, north = max(-85, south), min(85, north)
    cell = 180 / (2 ** max(3, min(18, zoom)))
    query = """SELECT r.kind,AVG(r.lat),AVG(r.lon),COUNT(*),r.name,r.detail,r.source,COUNT(DISTINCT CASE WHEN r.kind='fish_stocking' THEN r.name ELSE r.source_id END)
        FROM spatial s CROSS JOIN resource r ON r.id=s.id
        WHERE s.minx>=? AND s.maxx<=? AND s.miny>=? AND s.maxy<=?
        AND r.kind IN (%s)
        GROUP BY r.kind, CAST((r.lon+180)/? AS INTEGER), CAST((r.lat+90)/? AS INTEGER)
        ORDER BY COUNT(*) DESC LIMIT 2001""" % ','.join('?' for _ in kinds)
    db = sqlite3.connect(ro_uri(path), uri=True)
    try:
        rows = db.execute(query, (west,east,south,north,*kinds,cell,cell)).fetchall()
    finally:
        db.close()
    points = [{'cls':k,'lat':lat,'lon':lon,'count':sites,'record_count':n,'name':name if sites==1 else f'{sites:,} locations',
               'label':HELP[k],'detail':json.loads(detail) if sites==1 else {},'source':source}
              for k,lat,lon,n,name,detail,source,sites in rows[:2000]]
    return {'points':points, 'records':sum(p['record_count'] for p in points), 'truncated':len(rows)>2000}


CLUSTER_PX = 46


def _resource_summary(cls, detail):
    keys = {'mineral': ('commod1', 'commod2', 'dev_stat'), 'oil_gas_well': ('WellTypeLabel', 'FieldName', 'API'),
            'fish_stocking': ('FishType', 'Counties'), 'boat_launch': ('Water_Body', 'Facility_Type'),
            'dam': ('purpose', 'river', 'county'), 'reservoir': ('reservoir_type', 'water_type', 'state'),
            'orchard': ('crop',), 'vineyard': ('crop',), 'berries': ('crop',), 'perennial_veg': ('crop',), 'lake': ('water_type', 'state'), 'pond': ('water_type', 'state'),
            'hut': ('hut_type', 'run_by', 'booking'), 'trail_shelter': ('hut_type', 'run_by', 'booking')}.get(cls, ())
    out = [str(detail.get(k)) for k in keys if detail.get(k)]
    if detail.get('not_drinking_water'):
        out.insert(0, 'NOT drinking water')
    if detail.get('area_acres'):
        out.insert(0, f"{detail['area_acres']:,} acres")
    if detail.get('crop_acres'):
        out.append(f"about {round(detail['crop_acres']):,} acres in this block")
    if detail.get('normal_storage_af'):
        out.append(f"{int(float(detail['normal_storage_af'])):,} acre-ft stored")
    return ' · '.join(out)


@functools.lru_cache(maxsize=1)
def socal_classes():
    """Service classes the detailed SoCal map has; for anything else (e.g. fuel) the national points are the only ones."""
    db = mapdb()
    return frozenset(r[0] for r in db.execute('SELECT DISTINCT cls FROM poi')) if db else frozenset()


def _poi_sources(west, south, east, north):
    """(connection, skip-SoCal-duplicates) pairs for service points."""
    out = []
    if mapdb() is not None and east >= -121.4 and west <= -114.1 and north >= 32.5 and south <= 35.9:
        out.append((mapdb(), False))
    nat = national_mapdb()
    if nat is not None:
        dedupe = east >= -121.4 and west <= -114.1 and north >= 32.5 and south <= 35.9 and mapdb() is not None
        if dedupe and not getattr(_tls, 'socal_fn', False):
            nat.create_function('socal', 3, lambda cls, lo, la: 1 if cls in socal_classes() and in_socal(lo, la) else 0, deterministic=True)
            _tls.socal_fn = True
        out.append((nat, dedupe))
    return out


def pins(west, south, east, north, zoom, kinds):
    """Clustered service + resource points for the visible area. Clusters mix kinds and carry their bounds."""
    zoom = round(max(3, min(18, zoom)))
    cx = 360 / 2 ** zoom * (72 if zoom <= 5 else 58 if zoom <= 8 else CLUSTER_PX) / 256  # coarser groups when a whole region is in view
    cy = cx * max(0.2, math.cos(math.radians((south + north) / 2)))
    if zoom >= 17:
        cx = cy = 1e-6  # only exact duplicates group at street level
    else:  # snap to the cluster grid so panning reuses cached answers and clusters don't jump
        cy = round(cy / cx * 8) / 8 * cx
        west, east = math.floor((west + 180) / cx) * cx - 180, math.ceil((east + 180) / cx) * cx - 180
        south, north = math.floor((south + 90) / cy) * cy - 90, math.ceil((north + 90) / cy) * cy - 90
    version = data_version()
    return _pins(max(-180, west), max(-85, south), min(180, east), min(85, north), cx, cy, tuple(sorted(set(kinds))), version)


@functools.lru_cache(maxsize=256)
def _pins(west, south, east, north, cx, cy, kinds, version):
    services = [k for k in kinds if k in HELP and k not in RESOURCE_KINDS]
    resources = [k for k in kinds if k in RESOURCE_KINDS]
    cells = {}

    def add(kind, n, sites, lat, lon, x0, x1, y0, y1, name, detail, source):
        key = (int((lon + 180) / cx), int((lat + 90) / cy))
        c = cells.get(key)
        if c is None:
            cells[key] = c = {'kinds': {}, 'count': 0, 'records': 0, 'lat': 0.0, 'lon': 0.0, 'bbox': [x0, y0, x1, y1],
                              'name': name, 'detail': detail, 'source': source, 'cls': kind}
        c['lat'] += lat * sites; c['lon'] += lon * sites
        c['count'] += sites; c['records'] += n
        c['kinds'][kind] = c['kinds'].get(kind, 0) + sites
        b = c['bbox']; c['bbox'] = [min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)]

    if services:
        q = ','.join('?' * len(services))
        for db, dedupe in _poi_sources(west, south, east, north):
            sql = (f'SELECT cls,COUNT(*),AVG(lat),AVG(lon),MIN(lon),MAX(lon),MIN(lat),MAX(lat),MIN(name) FROM poi '
                   f'WHERE cls IN ({q}) AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?{" AND socal(cls,lon,lat)=0" if dedupe else ""} '
                   f'GROUP BY cls, CAST((lon+180)/? AS INT), CAST((lat+90)/? AS INT)')
            for k, n, la, lo, x0, x1, y0, y1, name in db.execute(sql, (*services, west, east, south, north, cx, cy)):
                add(k, n, n, la, lo, x0, x1, y0, y1, name or '', {}, '')
    for path in resource_dbs(resources):
        rdb = sqlite3.connect(ro_uri(path), uri=True)
        try:
            sql = ("SELECT r.kind,COUNT(*),AVG(r.lat),AVG(r.lon),MIN(r.lon),MAX(r.lon),MIN(r.lat),MAX(r.lat),r.name,r.detail,r.source,"
                   "COUNT(DISTINCT CASE WHEN r.kind='fish_stocking' THEN r.name ELSE r.source_id END) "
                   "FROM spatial s CROSS JOIN resource r ON r.id=s.id WHERE s.minx>=? AND s.maxx<=? AND s.miny>=? AND s.maxy<=? "
                   "AND r.kind IN (%s) GROUP BY r.kind, CAST((r.lon+180)/? AS INT), CAST((r.lat+90)/? AS INT)" % ','.join('?' * len(resources)))
            g = 3 if path == CROPDB and cx > 1e-5 else 1  # farm blocks are dense: group them more coarsely (the colour overlay shows the detail)
            for k, n, la, lo, x0, x1, y0, y1, name, detail, source, sites in rdb.execute(sql, (west, east, south, north, *resources, cx * g, cy * g)):
                add(k, n, sites, la, lo, x0, x1, y0, y1, name or '', json.loads(detail or '{}') if sites == 1 else {}, source or '')
        finally:
            rdb.close()
    out = []
    for c in cells.values():
        c['lat'] /= c['count']; c['lon'] /= c['count']
        single = c['count'] == 1
        if single:
            c['label'] = HELP.get(c['cls'], c['cls'])
            c['summary'] = _resource_summary(c['cls'], c['detail'])
        else:
            c['cls'] = max(c['kinds'], key=c['kinds'].get)
            c['name'] = c['detail'] = c['source'] = None
        # an exact-duplicate group (e.g. one lake stocked many times) still behaves as one place
        c['spot'] = c['count'] > 1 and c['bbox'][2] - c['bbox'][0] < 1e-4 and c['bbox'][3] - c['bbox'][1] < 1e-4
        out.append(c)
    out.sort(key=lambda c: -c['count'])
    total = sum(c['count'] for c in out)
    return {'points': out[:2500], 'sites': total, 'truncated': len(out) > 2500, 'cell': [cx, cy]}


def dedupe_named(rows):
    """The same shop is often mapped twice (a pharmacy node plus a chemist building): drop same-name points within ~80 m."""
    kept = {}
    out = []
    for r in rows:
        if r['name']:
            near_same = kept.setdefault(r['name'].lower(), [])
            if any(abs(r['lat'] - y) < 0.0007 and abs(r['lon'] - x) < 0.0009 for y, x in near_same):
                continue
            near_same.append((r['lat'], r['lon']))
        out.append(r)
    return out


def members(west, south, east, north, kinds, lat, lon, limit=60):
    """Individual records inside a cluster's bounds, nearest to its centre first."""
    services = [k for k in kinds if k in HELP and k not in RESOURCE_KINDS]
    resources = [k for k in kinds if k in RESOURCE_KINDS]
    rows = []
    eps = 1e-4  # the R-tree keeps float32 bounds (~1e-5° error)
    if services:
        q = ','.join('?' * len(services))
        for db, dedupe in _poi_sources(west, south, east, north):
            rows += [{'cls': c, 'label': HELP.get(c, c), 'name': n or '', 'lon': x, 'lat': y, 'summary': '', 'detail': {}, 'source': ''}
                     for c, n, x, y in db.execute(f'SELECT cls,name,lon,lat FROM poi WHERE cls IN ({q}) AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?'
                                                  f'{" AND socal(cls,lon,lat)=0" if dedupe else ""} LIMIT 2000',
                                                  (*services, west - eps, east + eps, south - eps, north + eps))]
    for path in resource_dbs(resources):
        with sqlite3.connect(ro_uri(path), uri=True) as rdb:
            for k, n, y, x, d, src in rdb.execute(
                    "SELECT r.kind,r.name,r.lat,r.lon,r.detail,r.source FROM spatial s CROSS JOIN resource r ON r.id=s.id "
                    "WHERE s.minx>=? AND s.maxx<=? AND s.miny>=? AND s.maxy<=? AND r.kind IN (%s) LIMIT 2000" % ','.join('?' * len(resources)),
                    (west - eps, east + eps, south - eps, north + eps, *resources)):
                detail = json.loads(d or '{}')
                rows.append({'cls': k, 'label': HELP[k], 'name': n or '', 'lat': y, 'lon': x, 'detail': detail,
                             'summary': _resource_summary(k, detail), 'source': src or ''})
    rows = [r for r in rows if r['cls'] in RESOURCE_KINDS] + dedupe_named([r for r in rows if r['cls'] not in RESOURCE_KINDS])
    for r in rows:
        r['km'], r['dir'] = haversine(lat, lon, r['lat'], r['lon'])
    rows.sort(key=lambda r: str(r['detail'].get('WeekOfPlantStart') or ''), reverse=True)  # newest stocking first at one spot
    rows.sort(key=lambda r: round(r['km'], 3))
    return {'items': rows[:limit], 'total': len(rows), 'capped': len(rows) >= 2000}


def near(lat, lon, classes, limit=8):
    db = mapdb()
    limit = max(1, min(limit, 50))
    out = []
    for cls in classes:
        if cls in RESOURCE_KINDS:
            out.extend(resource_near(lat, lon, cls, limit))
            continue
        sources = [d for d in (db, national_mapdb()) if d is not None]
        d = 0.02
        rows = []
        while d < 1.5 and len(rows) < limit:
            rows = []
            for source in sources:
                found = source.execute('SELECT cls,name,lon,lat FROM poi WHERE cls=? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?',
                                       (cls, lon - d, lon + d, lat - d, lat + d)).fetchall()
                if source is not db and db is not None and cls in socal_classes():
                    found = [r for r in found if not in_socal(r[2], r[3])]
                rows.extend(found)
            rows = list({(r[0],r[1],round(r[2],5),round(r[3],5)):r for r in rows}.values())
            d *= 2
        for c, name, x, y in rows:
            dist, brg = haversine(lat, lon, y, x)
            out.append({'cls': c, 'label': HELP.get(c, c), 'name': name, 'lat': y, 'lon': x, 'km': round(dist, 2), 'dir': brg})
    out.sort(key=lambda p: p['km'])
    out = [p for p in out if p['cls'] in RESOURCE_KINDS] + dedupe_named([p for p in out if p['cls'] not in RESOURCE_KINDS])
    out.sort(key=lambda p: p['km'])
    per = {}
    res = []
    for p in out:
        if per.get(p['cls'], 0) < limit:
            per[p['cls']] = per.get(p['cls'], 0) + 1
            res.append(p)
    return res


def haversine(la1, lo1, la2, lo2):
    R = 6371
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * R * math.asin(math.sqrt(a))
    b = (math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360
    return d, ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'][int((b + 22.5) // 45) % 8]


WATER_WORDS = {'lake', 'lakes', 'reservoir', 'res', 'dam', 'pond', 'ponds'}


def near_bonus(near, lat, lon):
    """0-8: places close to where the person is looking rank higher (strongly within ~60 km, gently out to ~600 km)."""
    if not near:
        return 0
    km = haversine(near[0], near[1], lat, lon)[0]
    return 6 * math.exp(-km / 60) + 2 * math.exp(-km / 600)


def word_sets(words):
    """All the words, then (for longer queries) each set with one word dropped — 'echo park lake' still finds 'Echo Lake'."""
    yield words
    if len(words) > 1:
        for i in range(len(words)):
            yield words[:i] + words[i + 1:]


HUT_WORDS = {'hut', 'huts', 'cabin', 'cabins', 'shelter', 'shelters', 'yurt', 'lean'}


def hut_places(q, limit=6, near=None):
    """Named huts, cabins and trail shelters whose name has every word of the query (a few thousand rows: no index needed)."""
    words = [w.lower() for w in re.findall(r'\w+', q)]
    if not words or not HUTSDB.exists():
        return []
    with sqlite3.connect(ro_uri(HUTSDB), uri=True) as db:
        rows = db.execute("SELECT kind,name,lat,lon,detail FROM resource WHERE name!='' AND " + ' AND '.join('name LIKE ?' for _ in words) + ' LIMIT 300',
                          [f'%{w}%' for w in words]).fetchall()
    out = []
    for kind, name, lat, lon, raw in rows:
        d = json.loads(raw or '{}')
        away = ''
        if near:
            km = haversine(near[0], near[1], lat, lon)[0]
            away = f" · {km / 1.609:,.0f} mi away" if km >= 1.6 else ' · nearby'
        score = (name.lower() == q.lower().strip()) * 3 + near_bonus(near, lat, lon)
        out.append((score, {'name': name, 'kind': HELP[kind], 'detail': ' · '.join(x for x in (d.get('run_by') or d.get('hut_type'), d.get('booking')) if x) + away,
                            'lat': lat, 'lon': lon, 'source': 'huts', 'onMap': True}))
    out.sort(key=lambda t: -t[0])
    return [o for _, o in out[:limit]]


def water_places(q, limit=10, near=None):
    """Named lakes, reservoirs and dams matching the query (generic words like 'lake' only help ranking)."""
    if not WATERDB.exists():
        return []
    words = [w.lower() for w in re.findall(r'\w+', q)]
    core = [w for w in words if w not in WATER_WORDS]
    if not core:
        return []
    db = sqlite3.connect(ro_uri(WATERDB), uri=True)
    try:
        try:
            rows, full = [], False
            for ws in word_sets(core):
                if rows and len(ws) < len(core) and full:
                    break  # every word matched: no need to loosen
                if ws:
                    rows += db.execute('SELECT r.kind,r.name,r.lat,r.lon,r.detail FROM water_fts f JOIN resource r ON r.id=f.rowid WHERE water_fts MATCH ? LIMIT 6000',
                                       (' '.join('"%s"*' % w.replace('"', '') for w in ws),)).fetchall()
                    if len(ws) == len(core):
                        full = bool(rows)
            rows = list(dict.fromkeys(rows))
        except sqlite3.OperationalError:  # index not built yet (import still running): slower scan
            cond = ' AND '.join("(r.name LIKE ? OR (r.kind='dam' AND r.detail LIKE ?))" for _ in core)
            rows = db.execute(f"SELECT r.kind,r.name,r.lat,r.lon,r.detail FROM resource r WHERE r.kind IN ('dam','reservoir','lake') AND r.name!='' AND {cond} LIMIT 300",
                              [x for w in core for x in (f'%{w}%', f'%{w}%')]).fetchall()
    finally:
        db.close()
    out = []
    for kind, name, lat, lon, raw in rows:
        d = json.loads(raw or '{}')
        text = (name + ' ' + (d.get('other_names') or '')).lower()
        score = (sum(w in text for w in words) * 2 + (core[0] in text) + (kind != 'dam') + (name.lower() == q.lower().strip()) * 3
                 + math.log10(float(d.get('area_acres') or d.get('surface_acres') or 1) + 1) / 3 + near_bonus(near, lat, lon))
        where = ', '.join(x for x in (d.get('county') and d['county'] + ' County', d.get('state')) if x)
        extra = f" · also called {d['other_names']}" if d.get('other_names') else ''
        away = ''
        if near:
            km = haversine(near[0], near[1], lat, lon)[0]
            away = f" · {km / 1.609:,.0f} mi away" if km >= 1.6 else ' · nearby'
        out.append((score, {'name': name, 'kind': {'dam': 'Dam', 'reservoir': 'Reservoir', 'lake': 'Lake'}[kind],
                            'detail': (where or 'United States') + extra + away, 'lat': lat, 'lon': lon, 'source': 'water', 'onMap': True}))
    out.sort(key=lambda t: -t[0])
    return [o for _, o in out[:limit]]


def places(q, limit=20, near=None):
    if address_search.is_address_query(q):
        return address_search.find_addresses(q, HERE / 'derived' / 'geo', limit=min(limit, 8), near=near)
    if not PLACEDB.exists():
        return []
    db = sqlite3.connect(ro_uri(PLACEDB), uri=True)
    words = re.findall(r'\w+', q)
    if not words:
        return []
    rows = []
    full = False
    for ws in word_sets(words):
        if full and len(ws) < len(words):
            break  # every word matched somewhere: no need to loosen
        if not ws:
            continue
        rows += db.execute('SELECT p.name,p.kind,p.detail,p.lat,p.lon,p.rank,p.source FROM place_fts f JOIN place p ON p.id=f.rowid '
                           'WHERE place_fts MATCH ? ORDER BY p.rank, bm25(place_fts) LIMIT 200',
                           (' '.join('"%s"*' % w.replace('"', '') for w in ws),)).fetchall()
        if len(ws) == len(words):
            full = bool(rows)
    rows = list(dict.fromkeys(rows))
    def norm(t):
        return ' '.join(re.sub(r'\bsaint\b', 'st', re.sub(r'[^\w ]+', ' ', t.lower())).split())
    ql = norm(q)
    nat = national_mapdb()
    size = {}

    def importance(r):
        # GNIS has no population: use OpenStreetMap's city/town tag near the same point so the big Springfield comes first
        if nat is None or norm(r[0]) != ql or len(size) > 80:
            return 3
        k = (round(r[3], 2), round(r[4], 2))
        if k not in size:
            hit = nat.execute("SELECT MIN(CASE f.cls WHEN 'city' THEN 0 WHEN 'town' THEN 1 ELSE 2 END) FROM feat_idx i JOIN feat f ON f.id=i.id "
                              "WHERE i.minx BETWEEN ? AND ? AND i.miny BETWEEN ? AND ? AND i.minz<=10 AND f.layer='place' AND f.name=?",
                              (r[4] - .08, r[4] + .08, r[3] - .08, r[3] + .08, r[0])).fetchone()
            size[k] = 2 if hit[0] is None else hit[0]
        return size[k]
    home = near or home_area()
    rows.sort(key=lambda r: (norm(r[0]) != ql, not norm(r[0]).startswith(ql), r[5] > 2 and norm(r[0]) == ql and importance(r),
                             -round(near_bonus(home, r[3], r[4])), r[5]))
    seen, out = set(), []
    for name, kind, detail, lat, lon, rank, src in rows:
        k = (name.lower(), round(lat, 2), round(lon, 2))
        if k in seen:
            continue
        seen.add(k)
        out.append({'name': name, 'kind': kind, 'detail': detail, 'lat': lat, 'lon': lon, 'source': src,
                    'onMap': bool(pack_info()) or 32.5 < lat < 35.9 and -121.4 < lon < -114.1})
        if len(out) >= limit:
            break
    water = water_places(q, near=near)
    if water:  # a query that names a water word puts lakes and dams first; otherwise towns lead
        out = (water + out) if set(w.lower() for w in re.findall(r'\w+', q)) & WATER_WORDS else (out[:8] + water + out[8:])
    huts = hut_places(q, near=near)
    if huts:  # the same for a hut word, or when the query is a hut's whole name
        first = set(w.lower() for w in re.findall(r'\w+', q)) & HUT_WORDS or any(h['name'].lower() == q.lower().strip() for h in huts)
        out = (huts + out) if first else (out[:8] + huts + out[8:])
    return out[:limit + len(water) + len(huts)]


_cov = {'at': 0}


def coverage():
    """What the assistant can and cannot read — reported, not hidden."""
    if time.time() - _cov['at'] > 300:
        cat = catalog()
        vids = [r for r in cat['by_id'].values() if r['kind'] == 'video']
        with_text = sum(1 for r in vids if cat['video_tx'].get(r['path']) or any(c.endswith('.vtt') for c in r.get('captions', [])))
        pdfs = [r for r in cat['by_id'].values() if r['kind'] == 'pdf']
        img = sum(1 for r in pdfs if (r.get('searchability') or '').startswith('image-only'))
        gaps = 0
        try:
            c = sqlite3.connect(ro_uri(retrieval.IDX / 'semantic.sqlite'), uri=True, timeout=5)
            gaps = c.execute('SELECT count(*) FROM gaps').fetchone()[0]
            c.close()
        except sqlite3.Error:
            pass
        _cov.update(at=time.time(), data={'videos': len(vids), 'videos_with_text': with_text, 'pdfs': len(pdfs), 'pdfs_image_only': img,
                                          'docs_without_text': gaps, 'remote_videos': len(remote_records()),
                                          'videos_excluded': len({i for i in excluded_videos() if i.startswith('video-')})})
    return _cov['data']


def home_area():
    """A household's own opening place, from a local file that is never packaged. None everywhere else. [lat, lon] for ranking place names."""
    try:
        d = json.loads((WEB / 'home-area.json').read_text(encoding='utf-8'))
        return float(d['lat']), float(d['lon'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def pack_info():
    """The open download's own description, or None when this is not a pack. Read once: a pack does not change while it is open."""
    if 'pack' not in _pack:
        try:
            _pack['pack'] = json.loads((HERE / 'pack.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            _pack['pack'] = None
    return _pack['pack']


_pack = {}
SOURCES_PAGE = 'documents/lastbook/sources-and-permissions.html'   # every pack's own credits page, an ordinary record


def has_aerial():
    return any(p.exists() for p in (AERIALDB, AERIAL_DETAIL, AERIAL_COAST))


def _distinct(path, sql):
    if not Path(path).exists():
        return []
    db = sqlite3.connect(ro_uri(path), uri=True)
    try:
        return [r[0] for r in db.execute(sql)]
    except sqlite3.Error:
        return []
    finally:
        db.close()


@functools.lru_cache(maxsize=1)
def map_contents():
    """What a pack's map really holds, decided from its own files: which pin kinds, and the box they fall in. The rest of the map is the plain base."""
    kinds, boxes = set(), []
    for path in (MAPDB, HERE / 'derived/geo/national.sqlite'):
        kinds |= set(_distinct(path, 'SELECT DISTINCT cls FROM poi'))
        if Path(path).exists():
            db = sqlite3.connect(ro_uri(path), uri=True)
            try:
                box = db.execute('SELECT MIN(lon),MIN(lat),MAX(lon),MAX(lat) FROM poi').fetchone()
                if box and box[0] is not None:
                    boxes.append(box)
            except sqlite3.Error:
                pass
            finally:
                db.close()
    for path in (LIB / 'geography/resources/atlas.sqlite', WATERDB, CROPDB, HUTSDB):
        kinds |= set(_distinct(path, 'SELECT DISTINCT kind FROM resource'))
    contours = any(r for r in _distinct(TERRAINDB, 'SELECT 1 FROM tile WHERE z>=11 LIMIT 1'))
    bounds = None
    if boxes:
        bounds = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        if bounds[2] - bounds[0] > 90:  # a map of everything: crossing the date line would show the whole world
            bounds = None
    return {'layers': sorted(kinds), 'bounds': bounds, 'detail': bool(boxes), 'contours': contours}


def map_info():
    db = mapdb()
    meta = dict(db.execute('SELECT key,value FROM meta').fetchall()) if db else {}
    geo = []
    for rel, label, status in [
        ('geography/openstreetmap/socal-latest-free.gpkg.zip', 'Southern California streets, buildings, water and places (OpenStreetMap)', 'Shown on this map'),
        ('geography/openstreetmap/socal-latest.osm.pbf', 'Southern California raw OpenStreetMap data', 'Coastline taken from it for the map; the rest needs conversion'),
        ('geography/openstreetmap/california-latest.osm.pbf', 'All of California raw OpenStreetMap data', 'Covered by the national map'),
        ('geography/openstreetmap/us-latest.osm.pbf', 'United States roads, waterways and places (OpenStreetMap)', ('Shown on this map — ' if national_status().get('state') == 'complete' else 'Converting — ') + format(national_status().get('features',0), ',') + ' features (buildings come from Microsoft, below)'),
        ('geography/openstreetmap/us-virgin-islands-latest.osm.pbf', 'US Virgin Islands raw OpenStreetMap data', 'Raw data — needs conversion before it can be viewed'),
        ('geography/census-gazetteers/2025_Gaz_zcta_national_.zip', '', ''),
    ]:
        p = LIB / rel
        if p.exists() and label:
            geo.append({'label': label, 'status': status, 'path': rel, 'bytes': p.stat().st_size})
    geo.append({'label': 'US place names (USGS GNIS) and Census 2025 ZIP-code and county centres', 'status': 'Used by "Find a place", together with named lakes, reservoirs and dams', 'path': 'geography/geographic-names/'})
    if WATERDB.exists():
        try:
            w = dict(sqlite3.connect(ro_uri(WATERDB), uri=True).execute('SELECT key,value FROM meta').fetchall())
            nid = json.loads(w.get('nid', '{}'))
            states = [k[4:] for k in w if k.startswith('nhd:')]
            if nid:
                geo.append({'label': 'Dams (US Army Corps of Engineers National Inventory of Dams)', 'status': f"{nid['records']:,} dams · data updated {nid['data_last_updated']}"})
            geo.append({'label': 'Lakes, reservoirs and ponds (USGS National Hydrography Dataset, final 2023 edition)',
                        'status': f'{len(states)} of 56 states and territories imported' if states else 'Downloading / importing'})
        except (sqlite3.Error, ValueError):
            pass
    if TERRAINDB.exists():
        geo.append({'label': 'Elevation: shaded relief, contour lines, spot heights and 3D (USGS 3DEP, 30 m)', 'status': 'Whole US and territories'})
    if CROPDB.exists():
        try:
            c = json.loads(dict(sqlite3.connect(ro_uri(CROPDB), uri=True).execute('SELECT key,value FROM meta').fetchall()).get('built', '{}'))
            geo.append({'label': f"Orchards, vineyards & berry fields (USDA Cropland Data Layer {c.get('year', '')})",
                        'status': f"{c.get('cells', 0):,} mapped blocks in {c.get('states', 0)} states (lower 48)"})
        except (sqlite3.Error, ValueError):
            pass
    if BLDGDB.exists():
        try:
            m = dict(sqlite3.connect(ro_uri(BLDGDB), uri=True).execute('SELECT key,value FROM meta').fetchall())
            n, h = m.get('buildings'), m.get('osm_heights')
            geo.append({'label': 'Building outlines for the whole US, plus heights for 3D (Microsoft Global ML Building Footprints, 2026 release, ODbL; OpenStreetMap is used where its outlines are complete)',
                        'status': (f"{int(n):,} buildings, shown from street level zoom" + (f" · {int(h):,} OpenStreetMap buildings with measured heights in 3D" if h else '')) if n else 'Converting'})
        except (sqlite3.Error, ValueError):
            pass
    detail = None
    if AERIAL_DETAIL.exists():
        try:
            m = dict(sqlite3.connect(ro_uri(AERIAL_DETAIL), uri=True).execute('SELECT key,value FROM meta').fetchall())
            detail = [float(v) for v in m['bbox'].split(',')] if m.get('bbox') else None
            geo.append({'label': 'Full-detail aerial photos, 30-60 cm (USGS NAIP Plus: USDA NAIP and high-resolution orthoimagery)',
                        'status': m.get('area', '') + ('' if m.get('built') else ' · downloading')})
        except (sqlite3.Error, ValueError, KeyError):
            pass
    rev = None
    if AERIAL_COAST.exists():
        try:
            m = dict(sqlite3.connect(ro_uri(AERIAL_COAST), uri=True).execute('SELECT key,value FROM meta').fetchall())
            # zoom 17+ photo URLs carry this, so browsers (which keep photo tiles a week) fetch a rebuilt layer afresh
            rev = (m.get('rev') or '') + ('' if m.get('built') else '.building')
            geo.append({'label': 'Sharpest aerial photos, 25 cm, 2024-25 (NOAA coastal mapping, public domain)',
                        'status': m.get('area', '') + ('' if m.get('built') else ' · building')})
        except (sqlite3.Error, ValueError):
            pass
    tdetail = None
    if TERRAIN_DETAIL.exists():
        try:
            m = dict(sqlite3.connect(ro_uri(TERRAIN_DETAIL), uri=True).execute('SELECT key,value FROM meta').fetchall())
            tdetail = [float(v) for v in m['circle'].split(',')] if m.get('circle') else None
            geo.append({'label': 'Detailed elevation from 1 m lidar: relief, 5 ft contours, exact heights (USGS 3DEP)',
                        'status': m.get('area', '') + ('' if m.get('built') else ' · building')})
        except (sqlite3.Error, ValueError, KeyError):
            pass
    if AERIALDB.exists():
        geo.append({'label': 'Aerial photos, whole US (USGS The National Map: USDA NAIP and other orthoimagery)', 'status': 'Zoom 0-15 nationwide, 16 in California and towns · downloading'})
    pack = pack_info()
    extra = {'aerial': has_aerial()}
    if pack:
        extra.update(map_contents())
        geo = pack_datasets(extra)
    return {**extra, 'meta': meta, 'datasets': geo, 'national': {k: v for k, v in national_status().items() if k not in ('source', 'database')}, 'version': data_version(), 'aerial_detail': detail, 'aerial_rev': rev, 'terrain_circle': tdetail}


def pack_datasets(have):
    """The panel beside a pack's map says only what this pack's map files hold."""
    area = 'the area this map details' if have['bounds'] else 'the whole US'
    out = [{'label': 'Main roads, towns and water for the whole US (OpenStreetMap, USGS)', 'status': 'Shown at every zoom level'}]
    if have['detail']:
        out.append({'label': f'Streets, places and help points (OpenStreetMap) for {area}', 'status': 'Street detail stops at the edge of this area.' if have['bounds'] else 'Shown when you zoom in'})
    else:
        out.append({'label': 'No street detail or help points', 'status': 'This download has only the base map. Hospitals, water taps and side streets are not in it.'})
    if PLACEDB.exists():
        out.append({'label': 'Place names (USGS GNIS, Census 2025 ZIP codes and counties)', 'status': 'Used by "Find a town, ZIP or place"'})
    if TERRAINDB.exists():
        out.append({'label': 'Elevation (USGS 3DEP)', 'status': 'Shaded relief. Contour lines only where this map has fine detail.' if have['detail'] else 'Shaded relief at broad zoom levels'})
    if BLDGDB.exists():
        out.append({'label': 'Building outlines (Microsoft Global ML Building Footprints, ODbL; OpenStreetMap)', 'status': f'For {area}, shown from street level zoom'})
    if CROPDB.exists():
        out.append({'label': 'Orchards, vineyards and berry fields (USDA Cropland Data Layer)', 'status': f'For {area}'})
    return out


# --------------------------------------------------------------- handler ----
class Handler(library.Handler):
    def send_json(self, obj, status=200, cache=None):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        if cache:
            self.send_header('Cache-Control', cache)
        self.end_headers()
        self.wfile.write(data)

    def send_static(self, path):
        data = path.read_bytes()
        mime = {'.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml',
                '.json': 'application/manifest+json', '.png': 'image/png', '.webmanifest': 'application/manifest+json', '.woff2': 'font/woff2', '.txt': 'text/plain; charset=utf-8'}.get(path.suffix, 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    # The only parts of the library anyone gets, this computer included: no worker state, logs, tools, sign-in settings or reports.
    SERVED_PREFIXES = ('/ui/', '/api/', '/zim/', '/documents/', '/pages/', '/videos/', '/videos-external/', '/transcripts/', '/metadata/videos/', '/courses/', '/collections/', '/assets/')
    SERVED_ONLY = True  # engine/library.py: a file must really sit in one of these folders, whatever links lead there
    LIBRARY_ROUTES = ('/api/health', '/api/download-status', '/api/collection-search', '/api/search')  # answered in engine/library.py
    LIBRARY_PREFIXES = tuple(p for p in SERVED_PREFIXES if p not in ('/ui/', '/api/', '/zim/'))
    # What a browser runs scripts in when it is opened directly. An <img> never runs them and shows it as before.
    ACTIVE_SUFFIXES = ('', '.html', '.htm', '.shtml', '.xhtml', '.xht', '.svg', '.svgz', '.xml', '.xsl', '.xslt', '.rdf', '.mht', '.mhtml')
    # Library content keeps what it says and runs none of its code: saved web pages, and encyclopedia pages too.
    ACTIVE_TYPES = re.compile(r'(?i)html|xml|xsl|svg|hta|component|rfc822|multipart/related')  # the same, judged by what the browser will be told: the suffix list can't cover what a machine's own type list adds
    # No allow-popups: the one window opens nothing for a page anyway, so all it gave a page was another way to ask. No allow-downloads, no allow-forms.
    # It covers what a sandbox does not: <noscript> frames come alive with scripts off, and <form>, <base> and <object> still load things without any.
    NO_CODE_CSP = ("default-src 'none'; script-src 'none'; connect-src 'none'; worker-src 'none'; object-src 'none'; manifest-src 'none'; base-uri 'none'; form-action 'none'; "
                   "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; media-src 'self'; frame-src 'self'")
    SAVED_PAGE_CSP = 'sandbox; ' + NO_CODE_CSP
    ZIM_CSP = 'sandbox allow-same-origin; ' + NO_CODE_CSP   # same-origin so the reader can follow the entry's title. A page with no scripts can do nothing with it.

    def parse_request(self):
        """Runs before any GET, HEAD or POST handling. Python rewrites a leading '//' in self.path, so read the target as it was sent."""
        if not super().parse_request():
            return False
        words = self.requestline.split()
        target = words[1] if len(words) > 1 else ''
        if not target.startswith('/') or target.startswith('//') or not self.host_valid():
            self.close_connection = True
            self.send_error(400)
            return False
        return True

    def path_ok(self, path):
        if '\0' in path:
            return False
        if path in ('/', '/index.html', '/sw.js'):
            return True
        if path != '/reader.css' and not path.startswith(self.SERVED_PREFIXES):
            return False
        if '/..' in path or path.lower().endswith('.info.json'):  # yt-dlp's .info.json can hold request headers
            return False
        if path.startswith(('/api/', '/zim/')):  # /zim/ file names are checked in engine/library.py
            return True
        if not library.segments_ok(path):
            return False
        return path.startswith('/ui/') or self.file_ok(self.translate_path(self.path), folders=True)

    HOST_SYNTAX = re.compile(r'(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(:\d*)?')

    def host_valid(self):
        """Not a host name at all, as opposed to some other computer's name, which host_ok refuses."""
        host = self.headers.get('Host') or ''
        if not host:
            return True
        try:
            urllib.parse.urlsplit('//' + host)
        except ValueError:
            return False
        return bool(self.HOST_SYNTAX.fullmatch(host))

    def host_ok(self):
        """Only localhost or a bare IP address. A web page using DNS rebinding arrives under its own domain name and is refused."""
        host = urllib.parse.urlsplit('//' + (self.headers.get('Host') or 'localhost')).hostname or ''
        if host == 'localhost':
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def origin_ok(self):
        """A browser POST from any other page, or from a sandboxed saved page, carries a different or null Origin."""
        origin = self.headers.get('Origin')
        try:
            return not origin or urllib.parse.urlsplit(origin).netloc == self.headers.get('Host')
        except ValueError:
            return False

    def list_directory(self, path):
        self.send_error(404)

    def send_header(self, keyword, value):
        if keyword.lower() == 'content-type':
            self.sent_type = value
        super().send_header(keyword, value)

    def send_head(self):
        """Only files get here. /api/ and /zim/ are addresses, never folders, whatever the library folder holds."""
        if urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).startswith(('/api/', '/zim/')):
            self.send_error(404)
            return None
        page = self.saved_page_file()
        if not page:
            return super().send_head()
        data = saved_pages.rewrite(Path(page).read_bytes(), self.saved_page_part)
        self.send_response(200)
        self.send_header('Content-Type', self.guess_type(page))
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        return io.BytesIO(data)

    def saved_page_file(self):
        """The web page this library address shows, if it is one to serve through saved_pages.rewrite."""
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if not path.startswith(self.LIBRARY_PREFIXES):
            return None
        page = self.translate_path(self.path)
        if path.endswith('/'):
            page = os.path.join(page, 'index.html')
        ok = page.lower().endswith(('.html', '.htm')) and os.path.isfile(page) and self.file_ok(page, folders=True)
        return page if ok and os.path.getsize(page) <= saved_pages.MAX_READ else None

    def saved_page_part(self, src):
        """A transcript a saved page names, by the same rules as asking for it directly."""
        target = urllib.parse.urljoin(urllib.parse.urlsplit(self.path).path, src)
        path = urllib.parse.unquote(urllib.parse.urlsplit(target).path)
        if not target.startswith('/') or target.startswith('//') or '/..' in path or not path.startswith(self.LIBRARY_PREFIXES) or not library.segments_ok(path):
            return None
        real = self.translate_path(target)
        if not (os.path.isfile(real) and self.file_ok(real, folders=True) and os.path.getsize(real) <= saved_pages.MAX_TRANSCRIPT):
            return None
        return Path(real).read_bytes()

    def send_data(self, data, mime='application/json', status=200):
        """engine/library.py sends some saved pages from memory, and every encyclopedia page, through here."""
        if status == 200 and 'html' in mime and len(data) <= saved_pages.MAX_PAGE and urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).startswith(self.LIBRARY_PREFIXES):
            data = saved_pages.rewrite(data, self.saved_page_part)
            if '/documents/lastbook/' in urllib.parse.unquote(urllib.parse.urlsplit(self.path).path):
                data = saved_pages.own_look(data)
        elif status == 200 and ('html' in mime or 'svg' in mime) and urllib.parse.urlsplit(self.path).path.startswith('/zim/'):
            data = saved_pages.article(data)
            if 'html' in mime:
                data = saved_pages.article_look(data)
        super().send_data(data, mime, status)

    def end_headers(self):
        sent_type, self.sent_type = getattr(self, 'sent_type', ''), ''
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.sent_type = ''
        try:
            path = urllib.parse.unquote(urllib.parse.urlsplit(getattr(self, 'path', '')).path)  # no self.path yet on a malformed request line
        except ValueError:
            path = ''
        if path.startswith('/zim/'):
            # Every answer under /zim/: pages, redirects and errors alike.
            self.send_header('Content-Security-Policy', self.ZIM_CSP)
            self.send_header('X-DNS-Prefetch-Control', 'off')
        elif path.startswith(self.LIBRARY_PREFIXES) and (Path(path).suffix.lower() in self.ACTIVE_SUFFIXES or self.ACTIVE_TYPES.search(sent_type)):
            self.send_header('Content-Security-Policy', self.SAVED_PAGE_CSP)
            self.send_header('X-DNS-Prefetch-Control', 'off')   # a CSP does not cover the names a browser looks up ahead of a click
        elif path.startswith(self.LIBRARY_PREFIXES) and Path(path).suffix.lower() in ('.woff2', '.woff', '.ttf', '.otf'):
            self.send_header('Access-Control-Allow-Origin', 'null')   # the sandbox above leaves a saved page origin null, and fonts always load by CORS
        super().end_headers()

    def internal_error(self):
        """The details go to the log. What reaches the client names no file path or user."""
        traceback.print_exc()
        return self.send_json({'error': f'Server error ({sys.exc_info()[0].__name__}).'}, 500)

    def lan_ok(self):
        """Loopback is always allowed. Other devices need the LAN access key once (then a cookie)."""
        ip = self.client_address[0]
        if ip in ('127.0.0.1', '::1') or ip.startswith('::ffff:127.'):
            return True
        if not LAN_KEY:
            return False
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        cookie = self.headers.get('Cookie') or ''
        if secrets.compare_digest(qs.get('key', [''])[0], LAN_KEY):
            self.send_response(302)
            self.send_header('Set-Cookie', f'sg_key={LAN_KEY}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000')
            self.send_header('Location', '/')
            self.end_headers()
            return None
        m = re.search(r'(?:^|;\s*)sg_key=([^;]+)', cookie)
        return bool(m and secrets.compare_digest(m.group(1), LAN_KEY))

    def deny(self):
        body = b'<!doctype html><meta name=viewport content="width=device-width"><title>Lastbook</title><p style="font:18px system-ui;padding:24px">This guide is shared on this network only with an access link. Ask the person running it for the link shown when it started.</p>'
        self.send_response(403)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        try:
            if not self.host_ok():
                return self.send_error(403)
            ok = self.lan_ok()
            if ok is None:
                return
            if not ok:
                return self.deny()
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if not self.path_ok(path):
                return self.send_error(404)
            if path in ('/', '/index.html', '/sw.js'):
                return self.send_static(WEB / ('sw.js' if path == '/sw.js' else 'index.html'))
            return super().do_HEAD()
        except library.CLIENT_GONE:
            return
        except Exception:
            traceback.print_exc()
            return self.send_error(500)

    def do_POST(self):
        try:
            return self.handle_post()
        except library.CLIENT_GONE:
            return
        except Exception:
            return self.internal_error()

    def handle_post(self):
        if not self.host_ok() or not self.origin_ok():
            return self.send_json({'error': 'not allowed from this page'}, 403)
        ok = self.lan_ok()
        if ok is None:
            return
        if not ok:
            return self.deny()
        rpath = urllib.parse.urlsplit(self.path).path
        if rpath.startswith('/api/setup/'):  # first-run setup and Library and local AI: engine/setup/api.py, this computer only
            return setup_api.handle(self, 'POST', rpath)
        if rpath.startswith('/api/update/'):  # Check for updates, when the person asks: engine/update.py, this computer only
            from engine import update
            return update.handle(self, 'POST', rpath)
        if rpath == '/api/not-found' or rpath.startswith('/api/not-found/'):  # Things I couldn't find: engine/notes.py, this computer only
            from engine import notes
            return notes.handle(self, 'POST', rpath, [t.get('id') for t in topics()])
        if rpath.startswith('/api/tool/'):  # the assistant's read-only tools, for other local DeepSeek clients
            name = rpath[10:]
            if name not in {t['function']['name'] for t in assistant.TOOLS}:
                return self.send_json({'error': 'unknown tool'}, 404)
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n < 0:
                    raise ValueError
                args = json.loads(self.rfile.read(min(n, 20000)) or b'{}')
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                return self.send_json({'error': 'arguments must be a JSON object'}, 400)
            sess, extra = assistant.Session(), []
            text, summary = assistant.run_tool(name, args, sess, extra)
            images = [p['image_url']['url'] for m in extra for p in m['content'] if isinstance(p, dict) and p.get('type') == 'image_url']
            return self.send_json({'text': text, 'summary': summary, 'sources': list(sess.sources.values()), 'images': images})
        if rpath == '/api/assistant/local':   # the optional local AI: look again for the file, turn it on or off, stop it
            if not assistant.LOCAL:
                return self.send_json({'error': 'no local AI in this version'}, 404)
            try:
                n = int(self.headers.get('Content-Length') or 0)
                action = json.loads(self.rfile.read(min(max(n, 0), 2000)) or b'{}').get('action')
            except (ValueError, AttributeError):
                action = None
            if action == 'rescan':
                threading.Thread(target=assistant.LOCAL.discover, daemon=True).start()
            elif action in ('on', 'off'):
                assistant.LOCAL.set_enabled(action == 'on')
            elif action == 'stop':
                assistant.LOCAL.stop('asked')
            else:
                return self.send_json({'error': 'action is rescan, on, off or stop'}, 400)
            return self.send_json(assistant.LOCAL.status())
        if rpath != '/api/chat':
            return self.send_json({'error': 'not found'}, 404)
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n < 0:
                raise ValueError
            if n > 200_000:
                return self.send_json({'error': 'request too large'}, 413)
            req = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return self.send_json({'error': 'bad request'}, 400)
        msgs = [m for m in (req.get('messages') or []) if isinstance(m, dict)][-12:]
        about = str(req.get('about') or '')[:200] or None
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        gone = [False]

        def emit(ev):
            if gone[0]:
                return
            try:
                self.wfile.write(b'data: ' + json.dumps(ev).encode() + b'\n\n')
                self.wfile.flush()
            except library.CLIENT_GONE:
                gone[0] = True  # the person pressed Stop or left; stop calling the model
        st = assistant.model_status()
        if not st['online']:
            emit({'type': 'error', 'error': 'model_unavailable', 'text': 'The local assistant model is not running right now.'})
            return
        try:
            assistant.chat(msgs, about, emit, lambda: gone[0])
        except Exception as e:
            traceback.print_exc()
            emit({'type': 'error', 'error': 'internal', 'text': f'The assistant stopped on an internal error ({type(e).__name__}).'})

    def do_GET(self):
        try:
            if not self.host_ok():
                return self.send_json({'error': 'not allowed from this page'}, 403)
            ok = self.lan_ok()
            if ok is None:
                return
            if not ok:
                return self.deny()
            url = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(url.path)
            if not self.path_ok(path):
                return self.send_json({'error': 'not found'}, 404)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if path.startswith('/api/setup/'):
                return setup_api.handle(self, 'GET', path)
            if path.startswith('/api/update/'):
                from engine import update
                return update.handle(self, 'GET', path)
            if path == '/api/battery':   # this computer's battery, read when asked: engine/setup/hardware.py, this computer only
                from engine.setup import hardware
                return hardware.handle_battery(self)
            if path == '/api/not-found':   # Things I couldn't find: engine/notes.py, this computer only
                from engine import notes
                return notes.handle(self, 'GET', path)
            if path in ('/', '/index.html'):
                return self.send_static(WEB / 'index.html')
            if path.startswith('/ui/'):
                p = (WEB / path[4:]).resolve()
                if WEB.resolve() in p.parents and p.is_file():
                    return self.send_static(p)
                return self.send_json({'error': 'not found'}, 404)
            if path == '/sw.js':
                return self.send_static(WEB / 'sw.js')
            if path == '/api/topics':
                return self.send_json(topics_resolved())
            if path == '/api/find':
                return self.send_json(find(qs.get('q', '')[:200], qs.get('kind', 'all')))
            if path.startswith('/api/doc/'):
                r = catalog()['by_id'].get(path[9:])
                return self.send_json(public(r, full=True) if r else {'error': 'This item is not in the library catalog.'}, 200 if r else 404)
            m = re.fullmatch(r'/api/page/([^/]+)/(\d+)(\.png)?', path)
            if m:
                r = catalog()['by_id'].get(m.group(1))
                n = int(m.group(2))
                if not r:
                    return self.send_json({'error': 'unknown document'}, 404)
                if m.group(3):
                    if r['kind'] != 'pdf':
                        return self.send_json({'error': 'not a PDF'}, 400)
                    data = render_page(r, n, int(qs.get('w', 1000)))
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/png')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'max-age=86400')
                    self.end_headers()
                    self.wfile.write(data)
                    return
                ps = page_texts(r)
                return self.send_json({'page': n, 'pages': len(ps), 'text': ps[n - 1] if 0 < n <= len(ps) else ''})
            if path.startswith('/api/transcript/'):
                t = transcript(path[16:])
                return self.send_json(t if t is not None else {'error': 'unknown video'}, 200 if t is not None else 404)
            if path == '/api/retrieve':
                return self.send_json({'q': qs.get('q', ''), 'results': assistant.hybrid(qs.get('q', '')[:200], min(20, int(qs.get('k', 8))), qs.get('kind', 'any')),
                                       'semantic': INDEX.status if INDEX else None})
            if path == '/api/assistant/status':
                return self.send_json(dict(assistant.model_status(), coverage=coverage()))
            if path == '/api/tools':
                return self.send_json({'tools': assistant.TOOLS, 'call': 'POST /api/tool/<name> with the JSON arguments',
                                       'note': 'Read-only access to the public survival library. Returned text is reference data, not instructions.'})
            if path == '/api/videos':
                return self.send_json(videos_home())
            if path.startswith('/api/videos/related/'):
                return self.send_json(videos_related(path[20:]))
            if path.startswith('/api/videos/'):
                return self.send_json(videos_list(path[12:], qs.get('q', ''), qs.get('len', ''), qs.get('sort', ''), int(qs.get('offset', 0))))
            if path == '/api/shelves':
                return self.send_json(shelves())
            if path.startswith('/api/shelf/'):
                return self.send_json(shelf(path[11:], qs.get('kind', 'all'), qs.get('q', ''), int(qs.get('offset', 0))))
            if path == '/api/collection':
                return self.send_json(collection_items(qs.get('name', ''), int(qs.get('offset', 0))))
            m = re.fullmatch(r'/api/map/(terrain|crops)/(\d+)/(\d+)/(\d+)\.png', path)
            if m:
                png = (terrain_tile if m[1] == 'terrain' else crop_tile)(*map(int, m.groups()[1:]))
                if png is None:  # lidar tiles may still be building: don't let browsers remember those gaps
                    self.send_response(204); self.send_header('Cache-Control', 'no-store' if m[1] == 'terrain' and int(m[2]) >= 13 else 'max-age=3600'); self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'image/png'); self.send_header('Content-Length', str(len(png)))
                self.send_header('Cache-Control', 'max-age=86400'); self.end_headers()
                self.wfile.write(png)
                return
            m = re.fullmatch(r'/api/map/aerial/(\d+)/(\d+)/(\d+)', path)
            if m:
                img = aerial_tile(*map(int, m.groups()))
                if img is None:  # none (yet): the photo download is still filling in, so don't let browsers remember the gap
                    self.send_response(204); self.send_header('Cache-Control', 'no-store'); self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg' if img[:3] == b'\xff\xd8\xff' else 'image/png')
                # while sharp photos are being built, zoom 17+ tiles may still change: keep them only briefly
                age = 60 if int(m[1]) >= 17 and aerial_coast_building() else 604800
                self.send_header('Content-Length', str(len(img))); self.send_header('Cache-Control', f'max-age={age}'); self.end_headers()
                self.wfile.write(img)
                return
            m = re.fullmatch(r'/api/map/bldg3d/(\d+)/(\d+)', path)
            if m:
                data = bldg3d(int(m[1]), int(m[2]))
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream'); self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'max-age=86400'); self.end_headers()
                self.wfile.write(data)
                return
            m = re.fullmatch(r'/api/map/tile/(\d+)/(\d+)/(\d+)', path)
            if m:
                z, x, y = map(int, m.groups())
                if not 3 <= z <= 18:
                    return self.send_json({'z': z, 'layers': {}})
                return self.send_json(map_tile(z, x, y), cache='max-age=86400' if 'v' in qs else 'max-age=60')  # ?v= carries the data version
            if path == '/api/map/resources':
                return self.send_json(resource_view(float(qs['west']), float(qs['south']), float(qs['east']), float(qs['north']), float(qs.get('zoom', 10)), qs.get('kinds', ','.join(RESOURCE_KINDS)).split(',')))
            if path in ('/api/map/pins', '/api/map/members'):
                box = [float(qs[k]) for k in ('west', 'south', 'east', 'north')]
                kinds = [k for k in qs.get('kinds', '').split(',') if k in HELP]
                if path == '/api/map/pins':
                    return self.send_json(pins(*box, float(qs.get('zoom', 10)), kinds))
                return self.send_json(members(*box, kinds, float(qs['lat']), float(qs['lon'])))
            if path == '/api/map/near':
                cls = [c for c in qs.get('cls', 'hospital,clinic,pharmacy,fire_station,drinking_water').split(',') if c in HELP]
                return self.send_json(near(float(qs['lat']), float(qs['lon']), cls, int(qs.get('limit', 5))))
            if path == '/api/map/info':
                return self.send_json(map_info())
            if path == '/api/places':
                try:
                    near_pt = tuple(map(float, qs['near'].split(',')))[:2] if qs.get('near') else None
                except ValueError:
                    near_pt = None
                return self.send_json(places(qs.get('q', '')[:100], near=near_pt))
            if path == '/api/library':
                s = json.loads((LIB / 'summary.json').read_text(encoding='utf-8'))
                sources = (catalog()['by_path'].get(SOURCES_PAGE) or {}).get('id')
                return self.send_json({**{k: s.get(k) for k in ('resources', 'pdfs', 'pdf_pages', 'web_pages', 'videos', 'collections', 'document_bytes')},
                                       'zims': zim_names(), 'sources': sources, 'pack': bool(pack_info()),
                                       'pins': bool(map_contents()['layers']) if pack_info() else True})
            if path.startswith('/api/') and path not in self.LIBRARY_ROUTES:
                return self.send_json({'error': 'not found'}, 404)
            return super().do_GET()
        except library.CLIENT_GONE:
            return
        except Exception:  # a readable error instead of a dropped connection
            return self.internal_error()


def init(no_index=False):
    """Everything the engine needs before it can answer a request. Opens no socket: the desktop app calls only this."""
    global INDEX
    catalog()
    print(f'onnxruntime telemetry: {retrieval.TELEMETRY}', flush=True)
    INDEX = retrieval.Index(catalog, trust, library.DB)
    assistant.init(sys.modules[__name__], INDEX)
    if not no_index:
        threading.Thread(target=INDEX.run_forever, daemon=True).start()
    threading.Thread(target=prewarm_previews, daemon=True).start()


class HTTPServer(library.HTTPServer):   # queues 128 connections: opening the map asks for 20 or more tiles at once
    if os.name == 'nt':  # SO_REUSEADDR on Windows lets a second copy silently share the port
        allow_reuse_address = False


def serve(port, lan=False):
    """The HTTP server version: binds the port and answers until stopped."""
    global LAN_KEY, SERVER
    if lan:
        kf = HERE / 'derived' / 'lan-key'
        if not kf.exists():
            kf.write_text(secrets.token_urlsafe(18))
            os.chmod(kf, 0o600)
        LAN_KEY = kf.read_text(encoding='utf-8').strip()
    srv = SERVER = HTTPServer(('0.0.0.0' if lan else '127.0.0.1', port), Handler)
    print(f'Lastbook: http://127.0.0.1:{port}/', flush=True)
    if lan:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('10.255.255.255', 1)); ip = s.getsockname()[0]; s.close()
        except OSError:
            ip = '<this-computer-ip>'
        print(f'Phones on this network: http://{ip}:{port}/?key={LAN_KEY}  (keep this link private; delete derived/lan-key to change it)', flush=True)
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8890)
    ap.add_argument('--lan', action='store_true', help='also serve phones on the same network; they need the printed access link')
    ap.add_argument('--no-index', action='store_true', help='do not update the semantic index in the background')
    ap.add_argument('--data', help='data folder holding library-data/ and derived/ (or set LASTBOOK_DATA); default: beside this program')
    a = ap.parse_args()
    init(a.no_index)
    serve(a.port, a.lan)


if __name__ == '__main__':
    main()
