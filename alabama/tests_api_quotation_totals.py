from datetime import date

from django.test import TestCase, override_settings

from .models import AlabamaFeedBrand, AlabamaItem, AlabamaSAPQuotation, AlabamaSAPQuotationItem

URL = '/alabama/api/item-quotation-totals/'


@override_settings(PURCHASE_APP_API_KEY='k')
class AlabamaQuotationTotalsApiTests(TestCase):
    def setUp(self):
        AlabamaItem.objects.create(item_code='A1', item_firm='ARISTON - CHINA')
        AlabamaItem.objects.create(item_code='B1', item_firm='OTHER')
        q1 = AlabamaSAPQuotation.objects.create(q_number='1', customer_name='c', posting_date=date(2026, 1, 5))
        q2 = AlabamaSAPQuotation.objects.create(q_number='2', customer_name='c', posting_date=date(2025, 3, 1))
        AlabamaSAPQuotationItem.objects.create(quotation=q1, item_no='A1', description='a', quantity=2, price=10, row_total=25)
        AlabamaSAPQuotationItem.objects.create(quotation=q2, item_no='A1', description='a', quantity=3, price=10)
        AlabamaSAPQuotationItem.objects.create(quotation=q1, item_no='B1', description='b', quantity=1, price=5)

    def get(self, qs='', key='k'):
        return self.client.get(URL + qs, HTTP_X_API_KEY=key)

    def test_auth(self):
        self.assertEqual(self.get(key='bad').status_code, 401)
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_totals(self):
        res = {r['item_code']: r for r in self.get().json()['results']}
        self.assertEqual(res['A1']['quoted_qty'], 5)
        self.assertEqual(res['A1']['quoted_value'], 55)      # 25 (row_total) + 3*10 (fallback)
        self.assertEqual(res['A1']['quotation_count'], 2)
        self.assertEqual(res['A1']['years']['2026']['quoted_qty'], 2)
        self.assertNotIn('cost', str(res['A1']).lower())

    def test_firm_filter_ignores_punctuation(self):
        codes = [r['item_code'] for r in self.get('?firm=ARISTON-CHINA').json()['results']]
        self.assertEqual(codes, ['A1'])

    def test_feed_brands_only(self):
        AlabamaFeedBrand.objects.create(brand='ARISTON', match_type='prefix')
        codes = [r['item_code'] for r in self.get('?feed_brands_only=1').json()['results']]
        self.assertEqual(codes, ['A1'])

    def test_group_by_date_and_year(self):
        data = self.get('?group_by=date&year=2026').json()
        self.assertEqual({(r['item_code'], r['date']) for r in data['results']},
                         {('A1', '2026-01-05'), ('B1', '2026-01-05')})

    def test_post_not_allowed(self):
        self.assertEqual(self.client.post(URL, HTTP_X_API_KEY='k').status_code, 405)
