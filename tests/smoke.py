"""Smoke test against a running server:  python3 tests/smoke.py [http://127.0.0.1:8765]"""
import gzip, json, sys, urllib.error, urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8765').rstrip('/')
fails = []


def get(path, expect=200):
    req = urllib.request.Request(BASE + path, headers={'Accept-Encoding': 'gzip'})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        code, body, hdr = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        code, body, hdr = e.code, e.read(), e.headers
    if hdr.get('Content-Encoding') == 'gzip':
        body = gzip.decompress(body)
    if code != expect:
        fails.append('%s: expected %s got %s' % (path, expect, code))
    return body, hdr


def check(cond, msg):
    if not cond:
        fails.append(msg)


j = lambda p, e=200: json.loads(get(p, e)[0])

body, h = get('/')
check(b'Colorado Hospital Prices (reported by payers)' in body, 'home page title')
check('frame-ancestors' in (h.get('Content-Security-Policy') or ''), 'CSP header')
check(j('/healthz')['ok'], 'healthz')
st = j('/api/status')
check(len(st['plans']) >= 20, 'plans loaded: %s' % len(st.get('plans', [])))
hs = j('/api/hospitals')['rows']
check(len(hs) >= 50, 'hospitals: %d' % len(hs))
check(all(h['lat'] and h['lng'] for h in hs), 'every hospital has coordinates')
check(all(37 < h['lat'] < 41.1 and -109.1 < h['lng'] < -102 for h in hs), 'coordinates inside Colorado')
dh = j('/api/providers?q=denver%20health')['rows']
check(dh, 'search finds Denver Health')
pr = j('/api/provider?tin=' + dh[0]['tin'])
check(pr.get('plans'), 'hospital has plans')
rk = j('/api/rank?code=99213')
check(rk['all']['n'] > 100 and rk['L'], 'rank has data')
check(rk['all']['min'] > 0 and rk['all']['max'] < 100000, 'rank values are sane')
rt = j('/api/rates?code=99213&tin=' + rk['L'][0]['tin'])
check(rt['rows'], 'rates for lowest hospital')
check(len(j('/api/services?q=mri')['rows']) > 0, 'service search')
mx = j('/api/matrix?q=mri&fee=1')
check(mx['rows'] and len(mx['rows'][0]['cells']) == len(mx['plans']), 'matrix shape')
cp = j('/api/compliance')
check(len(cp['plans']) == len(st['plans']), 'compliance covers every plan')
it = j('/api/items?plan=%d&q=99213' % cp['plans'][0]['id'])
check('rows' in it, 'items')
# hardening
for p in ('/server.py', '/data/mrf.sqlite', '/data/raw/', '/ingest.py', '/_dev/', '/../server.py', '/sources.json'):
    get(p, 404)
get('/api/items?plan=x', 400)
get('/api/nothing', 404)
if fails:
    print('FAILED:\n  ' + '\n  '.join(fails))
    sys.exit(1)
print('ok: %d plans, %d hospitals' % (len(st['plans']), len(hs)))
