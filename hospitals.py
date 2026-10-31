"""Colorado hospital list (CMS Care Compare), map coordinates (US Census geocoder) and NPIs (NPPES), matched to the
tax IDs found in the insurer plans. Public lookups are cached in data/hospitals_cache.json and redone on --refresh."""
import json, os, re, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'data', 'hospitals_cache.json')
CMS = 'https://data.cms.gov/provider-data/api/1/datastore/query/xubh-q36u/0?limit=500&conditions%5B0%5D%5Bproperty%5D=state&conditions%5B0%5D%5Bvalue%5D=CO'
UA = {'User-Agent': 'ColoradoHospitalPriceMonitor/1.0 (public data research)'}


def get_json(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=40) as r:
                return json.load(r)
        except Exception:
            time.sleep(1 + i)
    return None


def norm(s):
    s = re.sub(r'[^a-z0-9 ]', ' ', (s or '').lower().replace('&', ' and '))
    return ' '.join(w for w in s.split() if w not in ('the', 'inc', 'llc', 'of', 'and', 'corp', 'co'))


def geocode(h):
    q = urllib.parse.urlencode({'address': '%s, %s, CO %s' % (h['addr'], h['city'], h['zip']), 'benchmark': 'Public_AR_Current', 'format': 'json'})
    j = get_json('https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?' + q)
    try:
        m = j['result']['addressMatches'][0]['coordinates']
        h['lng'], h['lat'] = m['x'], m['y']
        return
    except Exception:
        pass
    z = get_json('https://api.zippopotam.us/us/' + h['zip'][:5])  # fall back to the ZIP code centre
    try:
        h['lat'], h['lng'] = float(z['places'][0]['latitude']), float(z['places'][0]['longitude'])
        h['approx'] = True
    except Exception:
        pass


def is_hospital(r):
    # NPPES taxonomy codes beginning with 28 are hospitals (general acute care, rehabilitation, psychiatric, long-term care ...)
    return any(str(t.get('code', '')).startswith('28') for t in r.get('taxonomies') or [])


def nppes(h):
    names = {h['name']}
    names.add(re.sub(r'\b(hospital|medical center|campus)\b.*', '', h['name'], flags=re.I).strip())
    found = {}
    for nm in [n for n in names if len(n) > 3]:
        q = urllib.parse.urlencode({'version': '2.1', 'organization_name': nm + '*', 'state': 'CO', 'city': h['city'], 'enumeration_type': 'NPI-2', 'limit': 50})
        j = get_json('https://npiregistry.cms.hhs.gov/api/?' + q) or {}
        for r in j.get('results') or []:
            loc = [a for a in r.get('addresses', []) if a.get('address_purpose') == 'LOCATION']
            z = (loc[0].get('postal_code') or '')[:5] if loc else ''
            on = r.get('basic', {}).get('organization_name', '')
            if (z == h['zip'][:5] and is_hospital(r)) or norm(on) == norm(h['name']):
                found[str(r['number'])] = on
    # same street address in the same ZIP code (older or system-level names differ between CMS and NPPES)
    num = re.match(r'\s*(\d+)\s+(\w+)', h['addr'] or '')
    if num:
        j = get_json('https://npiregistry.cms.hhs.gov/api/?' + urllib.parse.urlencode({'version': '2.1', 'postal_code': h['zip'][:5], 'state': 'CO', 'enumeration_type': 'NPI-2', 'limit': 200})) or {}
        for r in j.get('results') or []:
            for a in r.get('addresses', []):
                m = re.match(r'\s*(\d+)\s+(\w+)', a.get('address_1') or '')
                if a.get('address_purpose') == 'LOCATION' and m and m.group(1) == num.group(1) and m.group(2).lower() == num.group(2).lower() and is_hospital(r):
                    found[str(r['number'])] = r.get('basic', {}).get('organization_name', '')
    h['npis'] = sorted(found)


def build(refresh=False, log=print):
    cache = None
    if os.path.exists(CACHE) and not refresh:
        try:
            cache = json.load(open(CACHE))
        except Exception:
            cache = None
    if cache is None:
        log('hospitals: reading CMS hospital list')
        j = get_json(CMS)
        if not j:
            return []
        cache = [{'ccn': r['facility_id'], 'name': r['facility_name'].title() if r['facility_name'].isupper() else r['facility_name'], 'addr': r['address'], 'city': r['citytown'].title(),
                  'zip': r['zip_code'], 'county': r['countyparish'].title(), 'phone': r.get('telephone_number'), 'type': r['hospital_type'], 'own': r['hospital_ownership'],
                  'er': r['emergency_services'], 'rating': r['hospital_overall_rating']} for r in j['results']]
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(geocode, cache))
            list(ex.map(nppes, cache))
        json.dump(cache, open(CACHE, 'w'), indent=0)
        log('hospitals: %d hospitals, %d geocoded' % (len(cache), sum(1 for h in cache if 'lat' in h)))
    return cache


def clean_name(n):
    n = re.sub(r'\s+', ' ', n or '').strip()
    n = re.sub(r"'S\b", "'s", n)
    for a, b in (('Adventhealth', 'AdventHealth'), ('Healthone', 'HealthOne'), ('Uchealth', 'UCHealth'), ('Uch-', 'UCHealth '), ('Hca-Healthone Dba ', 'HCA HealthOne '), ('Hca-Healthone', 'HCA HealthOne'), ('Hca ', 'HCA '), ('Va ', 'VA '), ('Centura Health-', 'Centura Health '),
                 (' Llc', ' LLC'), (' Inc', ' Inc.'), ('Ach ', 'ACH ')):
        n = n.replace(a, b)
    return re.sub(r'\s+', ' ', n).strip()


NOT_HOSPITAL = re.compile(r'\b(LAB|LABS|LABORATORY|LABORATORIES|PATHOLOGY|PATHOLOGISTS|DIAGNOSTICS?|RADIOLOGY|RADIOLOGISTS|ANESTHESIA|ANESTHESIOLOGY|QUEST|LABONE|PHYSICIANS?|PHYSIOTHERAPY|THERAPY|THERAPISTS?|SURGEONS|PEDIATRICS|CARDIOLOGY)\b', re.I)
LOOKS_LIKE_HOSPITAL = re.compile(r'HOSPITAL|HEALTH|MEDICAL (CENTER|CTR)|CLINIC|SYSTEM|AUTHORITY|INITIATIVES|DISTRICT|REGIONAL|MEMORIAL|UCH|BANNER|CENTURA|ADVENT|HCA|VAIL|SURGICAL', re.I)


NPI_CACHE = os.path.join(HERE, 'data', 'npi_cache.json')


def npi_orgs(con):
    """Insurers such as Anthem file facility rates under an NPI instead of a tax ID. Look those NPIs up in the public
    NPI registry (cached) so they can be matched to hospitals by street address."""
    try:
        cache = json.load(open(NPI_CACHE))
    except Exception:
        cache = {}
    npis = [r[0] for r in con.execute("""SELECT DISTINCT f.tin FROM refs f JOIN set_refs sr ON sr.plan_id=f.plan_id AND sr.ref_id=f.ref_id JOIN sets s ON s.set_id=sr.set_id
        JOIN rates r ON r.set_id=s.set_id WHERE length(f.tin)=10 AND lower(r.cls) IN ('institutional','both')""")]
    todo = [n for n in npis if n not in cache]

    def one(n):
        j = get_json('https://npiregistry.cms.hhs.gov/api/?version=2.1&number=' + n) or {}
        r = (j.get('results') or [None])[0]
        if not r:
            cache[n] = None
            return
        loc = [a for a in r.get('addresses', []) if a.get('address_purpose') == 'LOCATION']
        a = loc[0] if loc else {}
        cache[n] = {'name': r.get('basic', {}).get('organization_name', ''), 'zip': (a.get('postal_code') or '')[:5], 'addr': a.get('address_1') or '', 'hosp': is_hospital(r)}
    if todo:
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(one, todo))
        json.dump(cache, open(NPI_CACHE, 'w'))
    return {n: cache[n] for n in npis if cache.get(n)}


def street_key(addr):
    m = re.match(r'\s*(\d+)\s+(\w+)', addr or '')
    return (m.group(1), m.group(2).lower()) if m else None


ALIASES = os.path.join(HERE, 'hospital_aliases.json')


def load_aliases():
    try:
        return {k: v for k, v in json.load(open(ALIASES)).items() if not k.startswith('_')}
    except Exception:
        return {}


def norm2(s):
    """Like norm(), also ignoring words that differ between CMS legal names and insurer names."""
    return ' '.join(w for w in norm(s).split() if w not in ('association', 'district', 'authority', 'assn', 'asn'))


def match(con, hospitals):
    """Fills tables hospitals and hosp_tins from NPIs and names found in the plans."""
    con.executescript('CREATE TABLE hospitals(hid TEXT PRIMARY KEY, name TEXT, addr TEXT, city TEXT, zip TEXT, county TEXT, phone TEXT, type TEXT, own TEXT, er TEXT, rating TEXT, lat REAL, lng REAL, npis TEXT, n_rates INTEGER, n_ins INTEGER, shared TEXT, aka TEXT);'
                      'CREATE TABLE hosp_tins(hid TEXT, tin TEXT);')
    con.execute("INSERT OR REPLACE INTO meta VALUES('hospitals_listed', ?)", (str(len(hospitals)),))
    byname = {}
    for t, n in con.execute('SELECT tin,name FROM providers WHERE name<>""'):
        byname.setdefault(norm2(n), []).append(t)
    aliases = load_aliases()
    orgs = npi_orgs(con)
    for h in hospitals:  # hospital NPIs found in the plans at the hospital's own street address
        k = street_key(h.get('addr'))
        extra = [n for n, o in orgs.items() if k and o['hosp'] and o['zip'] == h['zip'][:5] and street_key(o['addr']) == k]
        h['npis'] = sorted(set(h.get('npis', [])) | set(extra))
    for h in hospitals:
        tins = set()
        for i in range(0, len(h.get('npis', [])), 400):
            part = h['npis'][i:i + 400]
            tins.update(r[0] for r in con.execute('SELECT DISTINCT tin FROM npi_tin WHERE npi IN (%s)' % ','.join('?' * len(part)), part))
        # CMS legal names often read "<entity> dba <hospital>"; insurers file under the entity
        for part in {h['name'], re.split(r'\s+dba\s+', h['name'], flags=re.I)[0]}:
            tins.update(byname.get(norm2(part), []))
        keep = set()
        for t in tins:  # drop tax IDs without prices, and laboratories or physician groups that merely list the hospital's NPI
            n_rates, nm = con.execute('SELECT n_rates, name FROM providers WHERE tin=?', (t,)).fetchone()
            same = norm(nm) == norm(h['name']) or bool(set(norm(nm).split()) & {w for w in norm(h['name']).split() if len(w) > 5})
            if n_rates > 0 and nm and (norm(nm) == norm(h['name']) or (not NOT_HOSPITAL.search(nm) and (LOOKS_LIKE_HOSPITAL.search(nm) or same))):
                keep.add(t)
        # some insurers (Anthem) file a hospital's facility rates under its NPI instead of a tax ID
        for n in h.get('npis', []):
            r = con.execute('SELECT n_rates FROM providers WHERE tin=?', (n,)).fetchone()
            if r and r[0] > 0:
                keep.add(n)
        al = aliases.get(h['ccn'], {})
        for t in al.get('tins', []):  # hand-checked tax IDs (see hospital_aliases.json); they still need prices
            r = con.execute('SELECT n_rates FROM providers WHERE tin=?', (t,)).fetchone()
            if r and r[0] > 0:
                keep.add(t)
        tins = keep
        if not tins:
            continue
        con.execute("INSERT INTO hospitals(hid,name,addr,city,zip,county,phone,type,own,er,rating,lat,lng,npis,n_rates,n_ins,shared,aka) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,'[]',?)", ('H' + h['ccn'], al.get('name') or clean_name(h['name']), h['addr'], h['city'], h['zip'], h['county'], h['phone'], h['type'], h['own'], h['er'],
                    h['rating'], h.get('lat'), h.get('lng'), json.dumps(h.get('npis', [])[:12]), clean_name(h['name']) if al.get('name') else ''))
        con.executemany('INSERT INTO hosp_tins VALUES(?,?)', [('H' + h['ccn'], t) for t in tins])
    # how many hospitals have a dollar price for each service (drives search ranking and the procedure list)
    con.executescript('''CREATE INDEX ht_tin0 ON hosp_tins(tin);
      CREATE TABLE svc_hosp AS SELECT s.code code, COUNT(DISTINCT h.hid) n_hosp FROM sets s JOIN set_refs sr ON sr.set_id=s.set_id
        JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id JOIN hosp_tins h ON h.tin=f.tin
        WHERE s.set_id IN (SELECT set_id FROM rates WHERE rate>0) GROUP BY s.code;
      CREATE INDEX svc_hosp_code ON svc_hosp(code);''')
    con.executescript('''CREATE INDEX ht_tin ON hosp_tins(tin); CREATE INDEX ht_hid ON hosp_tins(hid);
      UPDATE hospitals SET n_rates=COALESCE((SELECT SUM(p.n_rates) FROM hosp_tins t JOIN providers p ON p.tin=t.tin WHERE t.hid=hospitals.hid),0),
        n_ins=COALESCE((SELECT COUNT(DISTINCT p2.insurer) FROM hosp_tins t JOIN refs f ON f.tin=t.tin JOIN plans p2 ON p2.id=f.plan_id WHERE t.hid=hospitals.hid),0);''')
    # hospitals that file prices under the same tax ID (a health system's tax ID covers all of its hospitals)
    for (hid,) in con.execute('SELECT hid FROM hospitals').fetchall():
        names = [r[0] for r in con.execute('SELECT DISTINCT h2.name FROM hosp_tins t JOIN hosp_tins t2 ON t2.tin=t.tin AND t2.hid<>t.hid JOIN hospitals h2 ON h2.hid=t2.hid WHERE t.hid=? ORDER BY h2.name', (hid,))]
        con.execute('UPDATE hospitals SET shared=? WHERE hid=?', (json.dumps(names), hid))
    proc_stats(con)


def proc_stats(con):
    """Median price per common procedure across hospitals (procedures listed in public/procedures.json)."""
    import statistics
    path = os.path.join(HERE, 'public', 'procedures.json')
    con.execute('CREATE TABLE IF NOT EXISTS proc_stats(code TEXT PRIMARY KEY, n_hosp INTEGER, n INTEGER, mn REAL, med REAL, mx REAL)')
    if not os.path.exists(path):
        return
    for p in json.load(open(path)):
        rows = con.execute('''SELECT DISTINCT r.rowid, r.rate FROM sets s JOIN rates r ON r.set_id=s.set_id JOIN set_refs sr ON sr.set_id=s.set_id
            JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id JOIN hosp_tins h ON h.tin=f.tin WHERE s.code=?''', (p['c'],)).fetchall()
        a = [r[1] for r in rows]
        nh = con.execute('SELECT n_hosp FROM svc_hosp WHERE code=?', (p['c'],)).fetchone()
        if a:
            con.execute('INSERT OR REPLACE INTO proc_stats VALUES(?,?,?,?,?,?)', (p['c'], nh[0] if nh else 0, len(a), min(a), statistics.median(a), max(a)))
