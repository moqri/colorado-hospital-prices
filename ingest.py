"""Downloads the insurer plan files listed in sources.json into data/raw and builds data/mrf.sqlite.

Run:  python3 ingest.py            (download missing files, rebuild the database)
      python3 ingest.py --refresh  (download everything again first)
server.py runs this on its own when the data is older than REFRESH_DAYS (about a quarter).
"""
import analyze, hospitals, gzip, io, json, os, re, sqlite3, sys, time, zipfile, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, 'data', 'raw')
DB = os.path.join(HERE, 'data', 'mrf.sqlite')
MANIFEST = os.path.join(HERE, 'data', 'manifest.json')
REFRESH_DAYS = 90


NPITIN = set()


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def ext_of(url):
    p = url.split('?')[0].lower()
    return '.zip' if p.endswith('.zip') else '.json.gz' if p.endswith('.gz') else '.json'


MIN_PRICES = 100  # plans with fewer negotiated dollar prices than this are left out
MAX_DOWNLOAD = 100 * 1024 * 1024  # no plan file larger than 100 MB is downloaded


def download(src, refresh, log):
    path = os.path.join(RAW, slug(src['n']) + ext_of(src['u']))
    if os.path.exists(path) and not refresh:
        return path
    tmp = path + '.part'
    try:
        req = urllib.request.Request(src['u'], headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, 'wb') as f:
            got = 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                got += len(b)
                if got > MAX_DOWNLOAD:
                    raise ValueError('larger than 100 MB, not downloaded')
                f.write(b)
        os.replace(tmp, path)
        log(f"downloaded {src['n']} ({os.path.getsize(path)//1024} KB)")
    except Exception as e:
        log(f"FAILED {src['n']}: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
        if not os.path.exists(path):
            return None
    return path


def load_json(path):
    if path.endswith('.zip'):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith('.json'))
            return json.load(z.open(name))
    if path.endswith('.gz'):
        with gzip.open(path, 'rb') as f:
            return json.load(f)
    with open(path, 'rb') as f:
        return json.load(f)


SCHEMA = """
CREATE TABLE plans(id INTEGER PRIMARY KEY, label TEXT, insurer TEXT, file TEXT, bytes INTEGER, fetched_at TEXT, last_updated TEXT, items INTEGER, rate_rows INTEGER, source TEXT);
CREATE TABLE items(item_id INTEGER PRIMARY KEY, plan_id INTEGER, type TEXT, code TEXT, descr TEXT, net TEXT, arr TEXT, n_excl INTEGER);
CREATE TABLE compliance(plan_id INTEGER PRIMARY KEY, j TEXT);
CREATE TABLE pss(plan_id INTEGER, v INTEGER, k TEXT, descr TEXT, n INTEGER, mn REAL, med REAL, mx REAL);
CREATE TABLE providers(tin TEXT PRIMARY KEY, name TEXT, npis TEXT, n_ins INTEGER, n_plans INTEGER);
CREATE TABLE refs(plan_id INTEGER, ref_id TEXT, tin TEXT);
CREATE TABLE sets(set_id INTEGER PRIMARY KEY, plan_id INTEGER, code TEXT, nref INTEGER, item_id INTEGER);
CREATE TABLE set_refs(set_id INTEGER, plan_id INTEGER, ref_id TEXT);
CREATE TABLE rates(set_id INTEGER, rate REAL, cls TEXT, type TEXT, arr TEXT, exp TEXT, sc TEXT, modifier TEXT, addl TEXT);
CREATE TABLE services(code TEXT PRIMARY KEY, type TEXT, descr TEXT, n_plans INTEGER);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE npi_tin(npi TEXT, tin TEXT);
"""
INDEXES = """
CREATE INDEX refs_tin ON refs(tin);
CREATE INDEX refs_plan_ref ON refs(plan_id, ref_id);
CREATE INDEX sets_code ON sets(code);
CREATE INDEX set_refs_set ON set_refs(set_id);
CREATE INDEX set_refs_plan_ref ON set_refs(plan_id, ref_id);
CREATE INDEX rates_set ON rates(set_id);
CREATE INDEX prov_name ON providers(name);
CREATE INDEX npi_tin_npi ON npi_tin(npi);
CREATE INDEX svc_descr ON services(descr);
CREATE INDEX sets_item ON sets(item_id);
CREATE INDEX items_plan ON items(plan_id);
CREATE INDEX pss_vk ON pss(v, k);
CREATE INDEX pss_k ON pss(k);
"""


def ingest_plan(con, plan_id, label, insurer, path, provs, svcs, seq):
    d = load_json(path)
    comp, pss = analyze.analyze(d)
    if comp['dollar'] < MIN_PRICES:
        return None
    refs = []
    seen_ref = set()
    refmap = {}
    for r in d.get('provider_references') or []:
        nn = [x for x in ([r.get('network_name')] if not isinstance(r.get('network_name'), list) else r.get('network_name')) if x]
        if nn:
            refmap[str(r.get('provider_group_id'))] = nn[0]
    for r in d.get('provider_references') or []:
        rid = str(r.get('provider_group_id'))
        for g in r.get('provider_groups') or []:
            t = (g.get('tin') or {}).get('value')
            if not t:
                continue
            refs.append((plan_id, rid, t))
            note_provider(provs, g, insurer, label)
    set_rows, setref_rows, rate_rows, item_rows = [], [], [], []
    items = d.get('in_network') or []
    for it in items:
        code = it.get('billing_code')
        if not code:
            continue
        s = svcs.setdefault(code, {'type': it.get('billing_code_type'), 'desc': it.get('description') or it.get('name') or '', 'plans': set()})
        s['plans'].add(plan_id)
        item_slot = len(item_rows)
        item_rows.append(None)
        arr = it.get('negotiation_arrangement')
        seq[1] += 1
        iid = seq[1]
        excl = 0
        net = it.get('provider_network_name')
        for nr in it.get('negotiated_rates') or []:
            ps = []
            for p in nr.get('negotiated_prices') or []:
                excl += 1
                try:
                    v = float(p.get('negotiated_rate'))
                except (TypeError, ValueError):
                    continue
                if v <= 0 or 'percent' in (p.get('negotiated_type') or '').lower():
                    continue
                excl -= 1
                ps.append((v, p.get('billing_class'), p.get('negotiated_type'), arr, p.get('expiration_date'),
                           ','.join(str(m) for m in (p.get('service_code') or []) if m is not None), ','.join(str(m) for m in (p.get('billing_code_modifier') or []) if m is not None), p.get('additional_information')))
            if not net:
                for rid0 in nr.get('provider_references') or []:
                    nn = refmap.get(str(rid0))
                    if nn:
                        net = nn
                        break
            if not ps:
                continue
            seq[0] += 1
            sid = seq[0]
            ids = [str(i) for i in nr.get('provider_references') or []]
            groups = nr.get('provider_groups') or []
            if groups:
                rid = 'i%d' % sid
                ids.append(rid)
                for g in groups:
                    t = (g.get('tin') or {}).get('value')
                    if t:
                        refs.append((plan_id, rid, t))
                        note_provider(provs, g, insurer, label)
            set_rows.append((sid, plan_id, code, len(ids), iid))
            setref_rows.extend((sid, plan_id, i) for i in ids)
            rate_rows.extend((sid,) + p for p in ps)
        item_rows[item_slot] = (iid, plan_id, it.get('billing_code_type'), code, it.get('description') or it.get('name') or '', net, arr, excl)
    con.executemany('INSERT INTO refs VALUES(?,?,?)', refs)
    con.executemany('INSERT INTO items VALUES(?,?,?,?,?,?,?,?)', item_rows)
    con.executemany('INSERT INTO sets VALUES(?,?,?,?,?)', set_rows)
    con.executemany('INSERT INTO set_refs VALUES(?,?,?)', setref_rows)
    con.executemany('INSERT INTO rates VALUES(?,?,?,?,?,?,?,?,?)', rate_rows)
    comp['last_updated_on'] = d.get('last_updated_on')
    con.execute('INSERT INTO compliance VALUES(?,?)', (plan_id, json.dumps(comp)))
    con.executemany('INSERT INTO pss VALUES(?,?,?,?,?,?,?,?)', [(plan_id,) + r for r in pss])
    return d.get('last_updated_on'), len(items), len(rate_rows)


def note_provider(provs, g, insurer, label):
    t = (g.get('tin') or {}).get('value')
    for n in (g.get('npi') if isinstance(g.get('npi'), list) else [g.get('npi')]):
        if n is not None:
            NPITIN.add((str(n), t))
    e = provs.get(t)
    if e is None:
        e = provs[t] = {'name': '', 'npis': [], 'ins': set(), 'plans': set()}
    if not e['name']:
        e['name'] = (g.get('tin') or {}).get('business_name') or ''
    if len(e['npis']) < 12:
        for n in (g.get('npi') if isinstance(g.get('npi'), list) else [g.get('npi')]):
            if n is not None and str(n) not in e['npis'] and len(e['npis']) < 12:
                e['npis'].append(str(n))
    e['ins'].add(insurer)
    e['plans'].add(label)


def main(refresh=False, log=print):
    os.makedirs(RAW, exist_ok=True)
    sources = json.load(open(os.path.join(HERE, 'sources.json')))
    t0 = time.time()
    with ThreadPoolExecutor(4) as ex:
        paths = list(ex.map(lambda s: download(s, refresh, log), sources))
    tmp = DB + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    provs, svcs, seq, status = {}, {}, [0, 0], []
    for i, (src, path) in enumerate(zip(sources, paths), 1):
        if not path:
            status.append({'label': src['n'], 'ok': False})
            continue
        try:
            res = ingest_plan(con, i, src['n'], src['insurer'], path, provs, svcs, seq)
            if res is None:
                status.append({'label': src['n'], 'ok': False, 'error': 'fewer than %d negotiated prices' % MIN_PRICES})
                log(f"skipped {src['n']}: fewer than {MIN_PRICES} negotiated prices")
                continue
            lu, n_items, n_rates = res
            con.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?)', (i, src['n'], src['insurer'], os.path.basename(path), os.path.getsize(path),
                        time.strftime('%Y-%m-%d', time.gmtime(os.path.getmtime(path))), lu, n_items, n_rates, src['u']))
            status.append({'label': src['n'], 'ok': True, 'items': n_items})
            log(f"ingested {src['n']}: {n_items} items, {n_rates} rates")
        except Exception as e:
            status.append({'label': src['n'], 'ok': False, 'error': str(e)[:120]})
            log(f"FAILED to read {src['n']}: {e}")
        con.commit()
    con.executemany('INSERT INTO providers VALUES(?,?,?,?,?)',
                    [(t, e['name'], json.dumps(e['npis']), len(e['ins']), len(e['plans'])) for t, e in provs.items()])
    con.executemany('INSERT INTO services VALUES(?,?,?,?)', [(c, s['type'], s['desc'], len(s['plans'])) for c, s in svcs.items()])
    con.executemany('INSERT INTO npi_tin VALUES(?,?)', NPITIN)
    NPITIN.clear()
    built = time.strftime('%Y-%m-%d', time.gmtime())
    con.execute('INSERT INTO meta VALUES(?,?)', ('built_at', built))
    con.commit()
    con.executescript(INDEXES)
    con.commit()
    # negotiated prices per provider: distinct (rate set, provider) pairs times the prices in each set
    con.executescript('''
      CREATE TEMP TABLE st AS SELECT DISTINCT sr.set_id, f.tin FROM set_refs sr JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id;
      CREATE TEMP TABLE sn AS SELECT set_id, COUNT(*) n FROM rates GROUP BY set_id;
      CREATE INDEX st_set ON st(set_id);
      ALTER TABLE providers ADD COLUMN n_rates INTEGER DEFAULT 0;
      CREATE TEMP TABLE pn AS SELECT st.tin tin, SUM(sn.n) n FROM st JOIN sn ON sn.set_id=st.set_id GROUP BY st.tin;
      CREATE INDEX pn_tin ON pn(tin);
      UPDATE providers SET n_rates=COALESCE((SELECT n FROM pn WHERE pn.tin=providers.tin),0);
    ''')
    con.commit()
    try:
        hospitals.match(con, hospitals.build(refresh, log))
        con.commit()
    except Exception as e:
        log('hospitals failed: %s' % e)
    con.close()
    os.replace(tmp, DB)
    ok = [s for s in status if s['ok']]
    json.dump({'built_at': built, 'built_ts': int(time.time()), 'refresh_days': REFRESH_DAYS, 'plans': status}, open(MANIFEST, 'w'), indent=1)
    log(f"done: {len(ok)} of {len(status)} plans, {len(provs)} providers, {len(svcs)} services in {time.time()-t0:.0f}s")


if __name__ == '__main__':
    main(refresh='--refresh' in sys.argv)
