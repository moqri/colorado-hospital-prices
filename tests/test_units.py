"""Unit tests for the build steps:  python3 tests/test_units.py"""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import analyze, hospitals

DOC = {
    'reporting_entity_name': 'Acme Health', 'reporting_entity_type': 'Health Insurance Issuer', 'last_updated_on': '2026-07-01', 'version': '2.0.0',
    'provider_references': [{'provider_group_id': 1, 'provider_groups': [{'npi': [1], 'tin': {'type': 'ein', 'value': '123'}}]}],
    'in_network': [{
        'negotiation_arrangement': 'ffs', 'name': 'Visit', 'billing_code': '99213', 'billing_code_type': 'CPT', 'billing_code_type_version': '2026', 'description': 'Visit',
        'negotiated_rates': [{'provider_references': [1], 'negotiated_prices': [
            {'negotiated_type': 'negotiated', 'negotiated_rate': 100, 'expiration_date': '2026-12-31', 'billing_class': 'professional', 'service_code': ['11']},
            {'negotiated_type': 'negotiated', 'negotiated_rate': 200, 'expiration_date': '2026-12-31', 'billing_class': 'institutional'},
            {'negotiated_type': 'percentage', 'negotiated_rate': 50, 'expiration_date': '2026-12-31', 'billing_class': 'institutional'},
            {'negotiated_type': 'negotiated', 'negotiated_rate': 0, 'expiration_date': 'bad', 'billing_class': 'professional'}]}]}],
}


class Analyze(unittest.TestCase):
    def test_counts_and_stats(self):
        comp, pss = analyze.analyze(DOC)
        self.assertEqual((comp['rows'], comp['dollar'], comp['pctRows'], comp['zero']), (4, 2, 1, 1))
        v0 = [r for r in pss if r[0] == 0 and r[1] == 'CPT 99213'][0]       # all classes, dollar rows
        self.assertEqual((v0[3], v0[4], v0[5], v0[6]), (2, 100, 150, 200))
        inst = [r for r in pss if r[0] == 4 and r[1] == 'CPT 99213'][0]     # institutional only
        self.assertEqual(inst[3], 1)

    def test_schema_checks_flag_bad_values(self):
        sc = analyze.schema_checks(DOC)
        bad = {c['label']: c['bad'] for c in sc['list']}
        self.assertEqual(bad['<code>expiration_date</code> is YYYY-MM-DD'], 1)
        self.assertEqual(bad['<code>negotiated_rate</code> is above zero'], 1)
        self.assertEqual(bad['Provider references resolve'], 0)
        self.assertEqual(sc['total'], 19)


class Hospitals(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(hospitals.norm('The Denver Health & Hospital Authority, Inc.'), 'denver health hospital authority')

    def test_clean_name(self):
        self.assertEqual(hospitals.clean_name("Children'S  Hospital Colorado"), "Children's Hospital Colorado")
        self.assertEqual(hospitals.clean_name('Uchealth Grandview Hospital'), 'UCHealth Grandview Hospital')

    def test_filters(self):
        self.assertTrue(hospitals.NOT_HOSPITAL.search('LABORATORY CORPORATION OF AMERICA'))
        self.assertFalse(hospitals.NOT_HOSPITAL.search('DENVER HEALTH AND HOSPITAL AUTHORITY'))
        self.assertTrue(hospitals.LOOKS_LIKE_HOSPITAL.search('CATHOLIC HEALTH INITIATIVES'))


if __name__ == '__main__':
    unittest.main()
