"""Serves the app, the local data API and refreshes the data every quarter.

Run: python3 server.py   (then open http://localhost:8765/)

Data: data/mrf.sqlite is built by ingest.py from the plan files in data/raw. When the data is older
than REFRESH_DAYS (about a quarter) or missing, this server runs ingest.py again in the background.
"""
import collections, gzip, http.server, json, os, signal, sqlite3, statistics, threading, time, urllib.parse
import analyze, ingest

HERE = os.path.dirname(os.path.abspath(__file__))
DB = ingest.DB
PUBLIC = os.path.join(HERE, 'public')
STATE = {'building': False, 'log': []}


def log(m):
    STATE['log'] = (STATE['log'] + [time.strftime('%H:%M:%S ') + m])[-40:]
    print(m, flush=True)


def manifest():
    try:
        return json.load(open(ingest.MANIFEST))
    except Exception:
        return None


def refresh(force=False):
    if STATE['building']:
        return
    STATE['building'] = True
    try:
        ingest.main(refresh=force, log=log)
    except Exception as e:
        log('refresh failed: %s' % e)
    finally:
        STATE['building'] = False


def watcher():
    while True:
        m = manifest()
        age = (time.time() - m['built_ts']) / 86400 if m else 1e9
        if not os.path.exists(DB):
            threading.Thread(target=refresh, args=(False,), daemon=True).start()
        elif age >= ingest.REFRESH_DAYS:
            log('data is %d days old, refreshing' % age)
            refresh(True)
        time.sleep(6 * 3600)


_tl = threading.local()


def db():
    c = getattr(_tl, 'c', None)
    if c is None:
        c = sqlite3.connect('file:%s?mode=ro' % DB, uri=True, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only=1')
        c.execute('PRAGMA cache_size=-20000')
        _tl.c = c
    return c


def close_db():
    c = getattr(_tl, 'c', None)
    if c is not None:
        c.close()
        _tl.c = None


def nice(s):
    return s


# everyday words people type -> the wording used in billing descriptions
SYNONYMS = [('broken arm', 'forearm'), ('broken leg', 'tibia'), ('broken', 'fracture'), ('c section', 'cesarean'), ('c-section', 'cesarean'), ('cat scan', 'ct'), ('knee replacement', 'arthroplasty knee'), ('hip replacement', 'arthroplasty hip'),
            ('blood test', 'panel'), ('heart attack', 'myocardial'), ('mammogram', 'mammography'), ('appendix', 'appendectomy'), ('tonsils', 'tonsillectomy'),
            ('gallbladder', 'cholecystectomy'), ('er visit', 'emergency'), ('stitches', 'repair'), ('xray', 'x-ray'), ('ultrasound', 'us ')]


def med(a):
    return statistics.median(a) if a else None


def api(path, q):
    g = lambda k, d='': (q.get(k) or [d])[0]
    if path == '/api/status':
        m = manifest() or {}
        out = {'building': STATE['building'], 'log': STATE['log'][-5:], 'built_at': m.get('built_at')}
        if m:
            out['next_refresh'] = time.strftime('%Y-%m-%d', time.gmtime(m['built_ts'] + ingest.REFRESH_DAYS * 86400))
        if os.path.exists(DB):
            with db() as c:
                out['plans'] = [dict(r) for r in c.execute('SELECT id,label,insurer,bytes,last_updated,items,rate_rows,source FROM plans ORDER BY insurer COLLATE NOCASE, label COLLATE NOCASE')]
                out['providers'] = c.execute('SELECT COUNT(*) FROM providers').fetchone()[0]
                out['rates'] = c.execute('SELECT SUM(rate_rows) FROM plans').fetchone()[0]
                out['hospitals'] = c.execute('SELECT COUNT(*) FROM hospitals WHERE n_rates>0').fetchone()[0]
                r = c.execute("SELECT v FROM meta WHERE k='hospitals_listed'").fetchone()
                out['hospitals_listed'] = int(r[0]) if r else None
        return out
    if not os.path.exists(DB):
        return {'error': 'building'}
    c = db()
    if path == '/api/providers':  # the searchable "providers" are hospitals
        s = g('q').strip().lower().replace('%', '')
        lim = min(int(g('limit', '30')), 200)
        where, args = 'WHERE n_rates>0', ()
        if s:
            like = '%' + s + '%'
            where, args = 'WHERE n_rates>0 AND (lower(name) LIKE ? OR lower(aka) LIKE ? OR lower(city) LIKE ? OR zip LIKE ?)', (like, like, like, s + '%')
        total = c.execute('SELECT COUNT(*) FROM hospitals ' + where, args).fetchone()[0]
        rows = c.execute('SELECT hid tin,name,city,zip,county,n_ins,n_rates,lat,lng FROM hospitals %s ORDER BY lower(name) LIMIT ?' % where, args + (lim,)).fetchall()
        return {'total': total, 'rows': [dict(r) for r in rows]}
    if path == '/api/procedures':
        stats = {r['code']: dict(r) for r in c.execute('SELECT * FROM proc_stats')}
        out = []
        for p in json.load(open(os.path.join(PUBLIC, 'procedures.json'))):
            st = stats.get(p['c'])
            if st:
                out.append(dict(p, h=st['n_hosp'], med=st['med'], mn=st['mn'], mx=st['mx']))
        return {'rows': sorted(out, key=lambda x: x['n'].lower())}
    if path == '/api/hospitals':
        return {'rows': [dict(r) for r in c.execute('SELECT hid tin,name,city,zip,type,n_ins,n_rates,lat,lng FROM hospitals WHERE n_rates>0 ORDER BY lower(name)')]}
    if path == '/api/provider':
        t = g('tin')
        p = c.execute('SELECT * FROM hospitals WHERE hid=?', (t,)).fetchone()
        if not p:
            return {'error': 'not found'}
        plans = c.execute('SELECT DISTINCT p.label,p.insurer,p.last_updated FROM hosp_tins h JOIN refs f ON f.tin=h.tin JOIN plans p ON p.id=f.plan_id WHERE h.hid=? ORDER BY p.insurer,p.label', (t,)).fetchall()
        d = dict(p)
        d['plans'] = [dict(r) for r in plans]
        d['shared'] = json.loads(d.get('shared') or '[]')
        d.pop('npis', None)
        return d
    if path == '/api/services' and g('codes') and g('tin'):  # which of these services does this hospital have a dollar price for?
        codes = [x.upper() for x in g('codes').split(',') if x][:40]
        ok = [k for k in codes if c.execute('''SELECT 1 FROM sets s JOIN set_refs sr ON sr.set_id=s.set_id JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id
            JOIN hosp_tins h ON h.tin=f.tin WHERE s.code=? AND h.hid=? AND s.set_id IN (SELECT set_id FROM rates WHERE rate>0) LIMIT 1''', (k, g('tin'))).fetchone()]
        return {'codes': ok}
    if path == '/api/services':
        raw = g('q').strip().lower()
        lim = min(int(g('limit', '8')), 50)
        if not raw:
            return {'rows': []}
        for k, v in SYNONYMS:
            raw = raw.replace(k, v)
        toks = [t for t in raw.replace('-', ' ').replace('%', '').replace('_', ' ').split() if t][:6]
        cond = ' AND '.join(["replace(lower(descr),'-',' ') LIKE ?"] * len(toks))
        args = ['%' + t + '%' for t in toks]
        hid = g('tin')
        rows = c.execute('''SELECT v.code code,type,descr,n_plans,sh.n_hosp n_hosp,
            CASE WHEN lower(v.code)=? THEN 4 WHEN lower(v.code) LIKE ? THEN 3 WHEN replace(lower(descr),'-',' ') LIKE ? THEN 2 ELSE 1 END sc FROM services v JOIN svc_hosp sh ON sh.code=v.code
            WHERE (lower(v.code) LIKE ? OR (%s)) ORDER BY sc DESC, sh.n_hosp DESC, n_plans DESC LIMIT ?''' % cond,
            [raw, raw + '%', toks[0] + '%' if toks else '%', raw + '%'] + args + [lim * (12 if hid else 1)]).fetchall()
        out = []
        for r in rows:
            if hid and not c.execute('''SELECT 1 FROM sets s JOIN set_refs sr ON sr.set_id=s.set_id JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id
                JOIN hosp_tins h ON h.tin=f.tin WHERE s.code=? AND h.hid=? AND s.set_id IN (SELECT set_id FROM rates WHERE rate>0) LIMIT 1''', (r['code'], hid)).fetchone():
                continue
            out.append(dict(r))
            if len(out) >= lim:
                break
        return {'rows': out}
    if path == '/api/service':
        r = c.execute('SELECT v.code code,type,descr,n_plans,COALESCE(sh.n_hosp,0) n_hosp FROM services v LEFT JOIN svc_hosp sh ON sh.code=v.code WHERE v.code=?', (g('code').upper(),)).fetchone()
        return dict(r) if r else {'error': 'not found'}
    if path == '/api/rates':
        code, tin = g('code').upper(), g('tin')
        sv = c.execute('SELECT code,type,descr,n_plans FROM services WHERE code=?', (code,)).fetchone()
        if tin:
            sql = '''SELECT DISTINCT s.set_id,s.nref,p.label,p.insurer,r.rowid rid,r.rate,r.cls,r.type,r.arr,r.exp,r.sc,r.modifier,r.addl
                FROM hosp_tins h JOIN refs f ON f.tin=h.tin JOIN set_refs sr ON sr.plan_id=f.plan_id AND sr.ref_id=f.ref_id JOIN sets s ON s.set_id=sr.set_id AND s.code=?
                JOIN rates r ON r.set_id=s.set_id JOIN plans p ON p.id=s.plan_id WHERE h.hid=?'''
            args = (code, tin)
        else:
            sql = '''SELECT s.set_id,s.nref,p.label,p.insurer,r.rowid rid,r.rate,r.cls,r.type,r.arr,r.exp,r.sc,r.modifier,r.addl
                FROM sets s JOIN rates r ON r.set_id=s.set_id JOIN plans p ON p.id=s.plan_id WHERE s.code=?'''
            args = (code,)
        rows = [dict(r) for r in c.execute(sql, args)]
        if tin:  # name a few providers that share each rate
            cache = {}
            for r in rows:
                if r['set_id'] not in cache:
                    smp = [dict(x) for x in c.execute('''SELECT DISTINCT f.tin,pr.name FROM set_refs sr JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id
                        JOIN providers pr ON pr.tin=f.tin WHERE sr.set_id=? LIMIT 3''', (r['set_id'],))]
                    nt = c.execute('''SELECT COUNT(DISTINCT f.tin) FROM set_refs sr JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id WHERE sr.set_id=?''', (r['set_id'],)).fetchone()[0]
                    cache[r['set_id']] = (smp, nt)
                r['sample'], r['ntin'] = cache[r['set_id']]
        return {'service': dict(sv) if sv else {'code': code}, 'rows': rows}
    if path == '/api/rank':  # hospitals only
        code = g('code').upper()
        cls = g('cls').lower()
        ins = g('ins')
        sv = c.execute('SELECT code,type,descr,n_plans FROM services WHERE code=?', (code,)).fetchone()
        rows = c.execute('''SELECT DISTINCT r.rowid rid,h.hid tin,r.rate,p.insurer,p.label,hp.name
            FROM sets s JOIN rates r ON r.set_id=s.set_id JOIN set_refs sr ON sr.set_id=s.set_id
            JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id JOIN hosp_tins h ON h.tin=f.tin JOIN hospitals hp ON hp.hid=h.hid
            JOIN plans p ON p.id=s.plan_id WHERE s.code=? AND (?='' OR lower(r.cls)=?) AND (?='' OR p.insurer=?)''', (code, cls, cls, ins, ins)).fetchall()
        by, bypi, allr = {}, {}, {}
        for r in rows:
            by.setdefault(r['tin'], {'name': r['name'], 'a': []})['a'].append(r['rate'])
            k = (r['insurer'], r['tin'])
            e = bypi.setdefault(k, {'a': [], 'plans': set(), 'name': r['name']})
            e['a'].append(r['rate'])
            e['plans'].add(r['label'])
            allr[r['rid']] = r['rate']
        allr = list(allr.values())
        L = sorted(({'tin': t, 'name': v['name'], 'med': med(v['a']), 'n': len(v['a'])} for t, v in by.items()), key=lambda x: x['med'])
        PI = [{'ins': k[0], 'tin': k[1], 'name': v['name'], 'med': med(v['a']), 'min': min(v['a']), 'max': max(v['a']), 'n': len(v['a']), 'plans': sorted(v['plans'])} for k, v in bypi.items()]
        return {'service': dict(sv) if sv else {'code': code}, 'L': L, 'PI': PI,
                'all': {'n': len(allr), 'min': min(allr) if allr else None, 'max': max(allr) if allr else None, 'med': med(allr)}}
    if path == '/api/compliance':
        out = []
        for r in c.execute('SELECT p.id,p.label,p.insurer,p.bytes,p.last_updated,q.j FROM plans p JOIN compliance q ON q.plan_id=p.id ORDER BY p.insurer COLLATE NOCASE, p.label COLLATE NOCASE'):
            d = json.loads(r['j'])
            d.update({'id': r['id'], 'label': r['label'], 'insurer': r['insurer'], 'bytes': r['bytes']})
            out.append(d)
        return {'plans': out, 'key': [{'type': t, 'code': k, 'label': l} for t, k, l in analyze.KEY]}
    if path == '/api/matrix':
        ci = analyze.CLASSES.index(g('cls')) if g('cls') in analyze.CLASSES else 0
        v = ci * 2 + (1 if g('fee') == '1' else 0)
        plans = [dict(r) for r in c.execute('SELECT id,label FROM plans ORDER BY insurer COLLATE NOCASE, label COLLATE NOCASE')]
        if g('key') == '1':
            keys = [t + ' ' + k for t, k, _ in analyze.KEY]
            have = {r['k']: r for r in c.execute('SELECT k,MAX(descr) descr,COUNT(*) have FROM pss WHERE v=? AND k IN (%s) GROUP BY k' % ','.join('?' * len(keys)), (v,) + tuple(keys))}
            ks = [{'k': k, 'descr': l, 'have': have[k]['have'] if k in have else 0} for k, (_, _, l) in zip(keys, analyze.KEY)]
            total = len(ks)
            where, args = 'WHERE v=? AND k IN (%s)' % ','.join('?' * len(keys)), [v] + keys
        else:
            s = g('q').strip().lower().replace('%', '')
            where, args = 'WHERE v=?', [v]
            if s:
                where += ' AND (lower(k) LIKE ? OR lower(descr) LIKE ?)'
                args += ['%' + s + '%', '%' + s + '%']
            lim = 100000 if g('all') == '1' else 400
            total = c.execute('SELECT COUNT(DISTINCT k) FROM pss ' + where, args).fetchone()[0]
            ks = [dict(r) for r in c.execute('SELECT k,MAX(descr) descr,COUNT(*) have FROM pss %s GROUP BY k ORDER BY have DESC, k LIMIT ?' % where, args + [lim])]
        cells = {}
        for i in range(0, len(ks), 500):
            part = [x['k'] for x in ks[i:i + 500]]
            for r in c.execute('SELECT plan_id,k,n,mn,med,mx FROM pss WHERE v=? AND k IN (%s)' % ','.join('?' * len(part)), [v] + part):
                cells.setdefault(r['k'], {})[r['plan_id']] = [r['n'], r['mn'], r['med'], r['mx']]
        for x in ks:
            x['cells'] = [cells.get(x['k'], {}).get(pl['id']) for pl in plans]
        sc = c.execute('SELECT COUNT(*), COALESCE(SUM(n),0) FROM pss ' + where, args).fetchone()
        return {'plans': plans, 'rows': ks, 'total': total, 'cells': sc[0], 'rates': sc[1]}
    if path == '/api/items':
        pid = int(g('plan', '1'))
        s = g('q').strip().lower().replace('%', '')
        where, args = 'WHERE plan_id=?', [pid]
        if s:
            where += ' AND (lower(code||" "||descr) LIKE ?)'
            args.append('%' + s + '%')
        if g('net'):
            where += ' AND net=?'
            args.append(g('net'))
        rows = [dict(r) for r in c.execute('SELECT item_id id,type,code,descr,net,arr FROM items %s LIMIT 200' % where, args)]
        cls = g('cls')
        stats = {}
        for i in range(0, len(rows), 500):
            ids = [r['id'] for r in rows[i:i + 500]]
            sql = 'SELECT s.item_id,r.rate FROM sets s JOIN rates r ON r.set_id=s.set_id WHERE s.item_id IN (%s)' % ','.join('?' * len(ids))
            if cls:
                sql += ' AND r.cls=?'
            for r in c.execute(sql, ids + ([cls] if cls else [])):
                stats.setdefault(r[0], []).append(r[1])
        for r in rows:
            a = stats.get(r['id'])
            r['n'], r['min'], r['med'], r['max'] = (len(a), min(a), med(a), max(a)) if a else (0, None, None, None)
        out = {'rows': rows}
        if g('meta') == '1':
            m = c.execute('SELECT j FROM compliance WHERE plan_id=?', (pid,)).fetchone()
            out['plan'] = json.loads(m[0]) if m else {}
            out['nets'] = [r[0] for r in c.execute('SELECT DISTINCT net FROM items WHERE plan_id=? AND net IS NOT NULL AND net<>"" LIMIT 200', (pid,))]
        return out
    if path == '/api/item':
        iid, cls = int(g('id', '0')), g('cls')
        it = c.execute('SELECT i.*,p.label FROM items i JOIN plans p ON p.id=i.plan_id WHERE item_id=?', (iid,)).fetchone()
        if not it:
            return {'error': 'not found'}
        sql = 'SELECT s.set_id,s.nref,r.rate,r.cls,r.type,r.exp,r.sc,r.modifier,r.addl FROM sets s JOIN rates r ON r.set_id=s.set_id WHERE s.item_id=?'
        rows = [dict(r) for r in c.execute(sql + (' AND r.cls=?' if cls else '') + ' ORDER BY r.rate', (iid, cls) if cls else (iid,))]
        cache = {}
        for r in rows:
            sid = r['set_id']
            if sid not in cache:
                if len(cache) < 600:
                    smp = [dict(x) for x in c.execute('SELECT DISTINCT f.tin,pr.name FROM set_refs sr JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id JOIN providers pr ON pr.tin=f.tin WHERE sr.set_id=? LIMIT 3', (sid,))]
                    nt = c.execute('SELECT COUNT(DISTINCT f.tin) FROM set_refs sr JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id WHERE sr.set_id=?', (sid,)).fetchone()[0]
                    cache[sid] = (smp, nt)
                else:
                    cache[sid] = ([], 0)
            r['sample'], r['ntin'] = cache[sid]
        tsql = 'SELECT COUNT(DISTINCT f.tin) FROM sets s JOIN set_refs sr ON sr.set_id=s.set_id JOIN refs f ON f.plan_id=sr.plan_id AND f.ref_id=sr.ref_id WHERE s.item_id=?'
        if cls:
            tsql += ' AND EXISTS(SELECT 1 FROM rates r WHERE r.set_id=s.set_id AND r.cls=?)'
        providers = c.execute(tsql, (iid, cls) if cls else (iid,)).fetchone()[0]
        v = analyze.CLASSES.index(cls) * 2 if cls in analyze.CLASSES else 0
        cmp = [dict(r) for r in c.execute('SELECT p.id plan_id,p.label,x.n,x.mn,x.med,x.mx FROM pss x JOIN plans p ON p.id=x.plan_id WHERE x.v=? AND x.k=? ORDER BY x.med', (v, '%s %s' % (it['type'], it['code'])))]
        return {'item': dict(it), 'rows': rows, 'providers': providers, 'cmp': cmp}
    return {'error': 'unknown endpoint'}


HOST = os.environ.get('HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', '8765'))
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
       "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data: https://cdnjs.cloudflare.com https://tile.openstreetmap.org; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'none'")
CACHE = {}
RATE = int(os.environ.get('RATE_LIMIT_PER_MIN', '240'))   # API requests per minute per client; 0 turns the limit off
TRUST_PROXY = os.environ.get('TRUST_PROXY') == '1'          # read the client address from X-Forwarded-For (only behind your own proxy)
HITS = collections.defaultdict(collections.deque)
HITS_LOCK = threading.Lock()


def limited(ip):
    if not RATE:
        return False
    now = time.time()
    with HITS_LOCK:
        q = HITS[ip]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= RATE:
            return True
        q.append(now)
        if len(HITS) > 5000:
            for k in [k for k, v in HITS.items() if not v or v[-1] < now - 60]:
                HITS.pop(k, None)
    return False
CACHE_KEY = [None]


class BadRequest(Exception):
    pass


class NotFound(Exception):
    pass


class Unavailable(Exception):
    pass


def cached_api(path, query):
    m = manifest() or {}
    if CACHE_KEY[0] != m.get('built_ts'):
        CACHE.clear()
        CACHE_KEY[0] = m.get('built_ts')
    key = (path, query)
    if key in CACHE:
        return CACHE[key]
    try:
        out = api(path, urllib.parse.parse_qs(query[:600]))
    except (ValueError, KeyError) as e:
        raise BadRequest(str(e)[:80])
    finally:
        close_db()
    if out.get('error') == 'unknown endpoint':
        raise NotFound()
    if out.get('error') == 'building':
        raise Unavailable()
    if path != '/api/status' and 'error' not in out:
        if len(CACHE) > 400:
            CACHE.clear()
        CACHE[key] = out
    return out


class H(http.server.SimpleHTTPRequestHandler):
    timeout = 30
    protocol_version = 'HTTP/1.1'
    server_version = 'PriceMonitor'
    sys_version = ''

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=PUBLIC, **kw)

    def log_message(self, fmt, *a):
        if os.environ.get('ACCESS_LOG'):
            print('%s %s' % (self.address_string(), fmt % a), flush=True)

    def send_common(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)

    def end_headers(self):
        self.send_common()
        if not self.path.startswith('/api/'):
            self.send_header('Cache-Control', 'no-cache' if self.path.split('?')[0] in ('/', '/index.html') else 'public, max-age=86400')
        super().end_headers()

    def reply_json(self, obj, code=200, head=False):
        body = json.dumps(obj, separators=(',', ':')).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        if code == 200 and not self.path.startswith('/api/status'):
            self.send_header('Cache-Control', 'public, max-age=300')
        else:
            self.send_header('Cache-Control', 'no-store')
        if len(body) > 1024 and 'gzip' in (self.headers.get('Accept-Encoding') or ''):
            body = gzip.compress(body, 5)
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Vary', 'Accept-Encoding')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET(True)

    def do_GET(self, head=False):
        u = urllib.parse.urlparse(self.path)
        if u.path == '/healthz':
            return self.reply_json({'ok': os.path.exists(DB), 'building': STATE['building']}, head=head)
        if u.path.startswith('/api/'):
            ip = (self.headers.get('X-Forwarded-For', '').split(',')[0].strip() if TRUST_PROXY else '') or self.client_address[0]
            if limited(ip):
                self.reply_json({'error': 'too many requests, slow down'}, 429, head)
                return
            try:
                self.reply_json(cached_api(u.path, u.query), head=head)
            except Unavailable:
                self.reply_json({'error': 'building'}, 503, head)
            except NotFound:
                self.reply_json({'error': 'not found'}, 404, head)
            except BadRequest as e:
                self.reply_json({'error': 'bad request: ' + str(e)}, 400, head)
            except Exception as e:
                print('api error', u.path, repr(e), flush=True)
                self.reply_json({'error': 'server error'}, 500, head)
            return
        if u.path in ('/', '/index.html'):
            raw = open(os.path.join(PUBLIC, 'index.html'), 'rb').read()
            gz = 'gzip' in (self.headers.get('Accept-Encoding') or '')
            body = gzip.compress(raw, 6) if gz else raw
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            if gz:
                self.send_header('Content-Encoding', 'gzip')
            self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return
        if '.' in os.path.basename(u.path):
            return super().do_HEAD() if head else super().do_GET()
        self.send_error(404)

    def list_directory(self, path):
        self.send_error(404)
        return None


if __name__ == '__main__':
    os.chdir(HERE)
    threading.Thread(target=watcher, daemon=True).start()
    srv = http.server.ThreadingHTTPServer((HOST, PORT), H)
    srv.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *a: threading.Thread(target=srv.shutdown, daemon=True).start())
    print('serving on http://%s:%d/' % (HOST, PORT), flush=True)
    srv.serve_forever()
