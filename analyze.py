"""Quarterly precompute: compliance metrics, schema checks and per-service price statistics for one plan.

Ported from the checks the page used to run in the browser, so the page only reads results.
"""
import re, statistics

KEY = [('CPT', '27447', 'Knee replacement'), ('CPT', '59510', 'C-section delivery'), ('CPT', '70553', 'Brain MRI, without and with contrast'),
       ('CPT', '74177', 'CT abdomen + pelvis, with contrast'), ('CPT', '74176', 'CT abdomen + pelvis, without contrast'),
       ('CPT', '43239', 'Upper GI endoscopy with biopsy'), ('CPT', '47562', 'Laparoscopic gallbladder removal'),
       ('CPT', '80053', 'Comprehensive metabolic panel'), ('CPT', '80061', 'Lipid panel')]
KEYSET = {t + ' ' + c for t, c, _ in KEY}
ENUM = {'type': ['CPT', 'HCPCS', 'ICD', 'MS-DRG', 'R-DRG', 'S-DRG', 'APS-DRG', 'AP-DRG', 'APR-DRG', 'APC', 'NDC', 'HIPPS', 'LOCAL', 'EAPG', 'CSTM-ALL', 'RC', 'CDT'],
        'arr': ['ffs', 'bundle', 'capitation'], 'ntype': ['negotiated', 'derived', 'fee schedule', 'percentage', 'per diem'],
        'cls': ['professional', 'institutional', 'both'], 'ent': ['group health plan', 'health insurance issuer', 'third party administrator']}
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
PCT = re.compile('percent', re.I)
FEE = re.compile('fee schedule|negotiated|derived', re.I)
NAN = float('nan')
# variants of the price matrix: class filter x dollar-rate-only filter  -> v = class_index*2 + fee
CLASSES = ['', 'professional', 'institutional']


def num(x):
    if x is None:
        return 0.0
    if isinstance(x, bool):
        return float(x)
    try:
        return float(x)
    except (TypeError, ValueError):
        return NAN


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float('inf')


def prices_of(it):
    for nr in it.get('negotiated_rates') or []:
        for p in nr.get('negotiated_prices') or []:
            yield p, nr


def schema_checks(d):
    items = d.get('in_network') or []
    n = len(items)
    refs = {str(r.get('provider_group_id')): r for r in d.get('provider_references') or []}
    out = []

    def add(label, bad, total, why):
        out.append({'label': label, 'bad': bad, 'total': total, 'why': why})
    allp = [(p, nr) for it in items for p, nr in prices_of(it)]
    npr = len(allp)

    def cnt(f):
        return sum(1 for i in items if f(i))

    def pc(f):
        return sum(1 for p, nr in allp if f(p))
    for k in ('reporting_entity_name', 'reporting_entity_type', 'last_updated_on', 'version'):
        add('Header has <code>%s</code>' % k, 0 if d.get(k) else 1, 1, 'missing')
    add('Entity type is a recognized value', 0 if str(d.get('reporting_entity_type') or '').lower() in ENUM['ent'] else 1, 1, 'unrecognized: ' + str(d.get('reporting_entity_type') or '—'))
    add('<code>last_updated_on</code> is YYYY-MM-DD', 0 if DATE.match(d.get('last_updated_on') or '') else 1, 1, 'bad format')
    req = ['negotiation_arrangement', 'name', 'billing_code', 'billing_code_type', 'billing_code_type_version', 'description']
    add('Items have required fields', cnt(lambda i: not all(i.get(k) for k in req)), n, 'items missing a field')
    add('<code>billing_code_type</code> is an allowed code system', cnt(lambda i: i.get('billing_code_type') not in ENUM['type']), n, 'items with another value')
    add('<code>negotiation_arrangement</code> is ffs, bundle or capitation', cnt(lambda i: i.get('negotiation_arrangement') not in ENUM['arr']), n, 'items with another value')
    add('Every item has negotiated rates', cnt(lambda i: not (i.get('negotiated_rates') or [])), n, 'items without rates')
    add('Every rate points to providers', cnt(lambda i: any(not (r.get('provider_groups') or []) and not (r.get('provider_references') or []) for r in i.get('negotiated_rates') or [])), n, 'items with a rate lacking providers')
    add('Provider references resolve', cnt(lambda i: any(str(x) not in refs for r in i.get('negotiated_rates') or [] for x in r.get('provider_references') or [])), n, 'items with a dangling reference')

    def bad_tin(i):
        for r in i.get('negotiated_rates') or []:
            gs = list(r.get('provider_groups') or [])
            for x in r.get('provider_references') or []:
                gs += (refs.get(str(x)) or {}).get('provider_groups') or []
            for g in gs:
                t = g.get('tin') or {}
                if not t.get('value') or t.get('type') not in ('ein', 'npi'):
                    return True
        return False
    add('Provider groups have a TIN (ein or npi)', cnt(bad_tin), n, 'items with a missing or invalid TIN')
    add('<code>negotiated_type</code> is an allowed value', pc(lambda p: p.get('negotiated_type') not in ENUM['ntype']), npr, 'prices with another value')
    add('<code>negotiated_rate</code> is numeric', pc(lambda p: not is_num(p.get('negotiated_rate'))), npr, 'prices not numeric')
    add('<code>negotiated_rate</code> is above zero', pc(lambda p: is_num(p.get('negotiated_rate')) and p['negotiated_rate'] <= 0), npr, 'prices at or below zero')
    add('<code>billing_class</code> is professional, institutional or both', pc(lambda p: p.get('billing_class') not in ENUM['cls']), npr, 'prices with another value')
    add('<code>expiration_date</code> is YYYY-MM-DD', pc(lambda p: not DATE.match(p.get('expiration_date') or '')), npr, 'prices with a bad date')
    add('Professional prices carry <code>service_code</code>', pc(lambda p: p.get('billing_class') != 'institutional' and not (p.get('service_code') or [])), npr, 'prices without one')
    failed = sum(1 for c in out if c['bad'])
    return {'list': out, 'failed': failed, 'total': len(out)}


def analyze(d):
    """Returns (compliance dict, list of (v, key, descr, n, min, med, max))."""
    items = d.get('in_network') or []
    rows = dollar = pct_rows = zero = dup = 0
    seen, found = set(), set()
    stats = {}  # key -> [descr, [lists x6]]
    for it in items:
        key = '%s %s' % (it.get('billing_code_type'), it.get('billing_code'))
        for p, nr in prices_of(it):
            rows += 1
            r = num(p.get('negotiated_rate'))
            ty = p.get('negotiated_type') or ''
            bc = (p.get('billing_class') or '')
            if PCT.search(ty):
                pct_rows += 1
            elif not r > 0:
                zero += 1
            else:
                dollar += 1
                if key in KEYSET:
                    found.add(key)
            s = (key, r, bc)
            if s in seen:
                dup += 1
            else:
                seen.add(s)
            if PCT.search(ty) or not r > 0:
                continue
            e = stats.get(key)
            if e is None:
                e = stats[key] = [it.get('description') or it.get('name') or '', [[] for _ in range(6)]]
            fee_ok = bool(FEE.search(ty))
            bl = bc.lower()
            for ci, c in enumerate(CLASSES):
                if c and bl != c and bl != 'both':
                    continue
                e[1][ci * 2].append(r)
                if fee_ok:
                    e[1][ci * 2 + 1].append(r)
    pss, keystats = [], {}
    for key, (desc, lists) in stats.items():
        for v, a in enumerate(lists):
            if a:
                pss.append((v, key, desc, len(a), min(a), statistics.median(a), max(a)))
        if key in KEYSET and lists[0]:
            a = lists[0]
            keystats[key] = {'n': len(a), 'min': min(a), 'med': statistics.median(a), 'max': max(a)}
    plan = d.get('plan_name')
    if plan:
        plan += (' · ' + d['plan_market_type']) if d.get('plan_market_type') else ''
    else:
        nets = []
        for it in items:
            n = it.get('provider_network_name')
            if n and n not in nets:
                nets.append(n)
                if len(nets) == 2:
                    break
        plan = ', '.join(nets)[:70] or '—'
    comp = {'entity': d.get('reporting_entity_name') or '', 'plan': plan, 'plan_name': d.get('plan_name') or '', 'market': d.get('plan_market_type') or '',
            'items': len(items), 'rows': rows, 'dollar': dollar, 'pctRows': pct_rows, 'zero': zero, 'dup': dup, 'found': len(found),
            'sc': schema_checks(d), 'key': keystats}
    return comp, pss
