"""Tests for Alabama's own item master (AlabamaItem): the feed importer, the brand list, linking the Excel-upload
history, and that switching the Alabama screens over (ALABAMA_OWN_ITEMS) never changes a quantity / sales figure and
never touches Junaid's so.Items."""
import json
import os
import tempfile
from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from so.models import Customer, Items

from . import item_sync
from .models import (AlabamaDeliveryOrder, AlabamaDeliveryOrderItem, AlabamaFeedBrand, AlabamaItem,
                     AlabamaPurchaseLine, AlabamaSalesLine)


def feed_row(code, brand='KLUDI', desc='A thing', cost=5.0, price=9.0, stock=2.0, upc='U1'):
    return {'ItemCode': code, 'Description': desc, 'ManufacturerName': brand, 'CostPrice': cost, 'SellingPrice': price,
            'StockQuantity': stock, 'FreeStock': 0.0, 'UpcCode': upc, 'WarehouseCode': '1', 'CtnQty': 0.0}


def write_feed(rows):
    fh = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
    json.dump({'ok': True, 'count': len(rows), 'total': len(rows), 'offset': 0, 'items': rows}, fh)
    fh.close()
    return fh.name


def run_import(rows, *extra):
    path = write_feed(rows)
    try:
        out = StringIO()
        call_command('import_items_alabama', '--file', path, *extra, stdout=out)
        return out.getvalue()
    finally:
        os.unlink(path)


class BrandRuleTests(TestCase):
    def setUp(self):
        AlabamaFeedBrand.objects.all().delete()          # the migration seeds the real list; tests set their own

    def rules(self):
        return item_sync.load_brand_rules()

    def test_no_rules_means_every_brand(self):
        self.assertTrue(item_sync.brand_is_quotable('ANYTHING', self.rules()))

    def test_exact_ignores_case_spaces_and_hyphens(self):
        AlabamaFeedBrand.objects.create(brand='ARISTON-CHINA')
        AlabamaFeedBrand.objects.create(brand='vera-solar')
        r = self.rules()
        self.assertTrue(item_sync.brand_is_quotable('ARISTON - CHINA', r))
        self.assertTrue(item_sync.brand_is_quotable('VERA SOLAR', r))
        self.assertFalse(item_sync.brand_is_quotable('ARISTON - ITALY', r))
        self.assertFalse(item_sync.brand_is_quotable('', r))

    def test_prefix_rule_covers_future_brands_but_not_lookalikes(self):
        AlabamaFeedBrand.objects.create(brand='RAKTHERM', match_type='prefix')
        r = self.rules()
        self.assertTrue(item_sync.brand_is_quotable('RAKTHERM', r))
        self.assertTrue(item_sync.brand_is_quotable('RAKTHERM PVC', r))
        self.assertTrue(item_sync.brand_is_quotable('RAKTHERM NEW LINE', r))
        self.assertFalse(item_sync.brand_is_quotable('RAK CERAMIC', r))

    def test_inactive_rule_is_ignored(self):
        AlabamaFeedBrand.objects.create(brand='KLUDI', is_active=False)
        AlabamaFeedBrand.objects.create(brand='GROHE')
        r = self.rules()
        self.assertFalse(item_sync.brand_is_quotable('KLUDI', r))
        self.assertTrue(item_sync.brand_is_quotable('GROHE', r))

    def test_seeded_list_matches_the_agreed_brands(self):
        """The data migration seeds the agreed Alabama brand list."""
        # re-create what the migration seeds (setUp cleared it)
        from importlib import import_module
        mig = import_module('alabama.migrations.0011_seed_alabama_feed_brands')
        for name in mig.EXACT:
            AlabamaFeedBrand.objects.create(brand=name)
        for name in mig.PREFIX:
            AlabamaFeedBrand.objects.create(brand=name, match_type='prefix')
        r = self.rules()
        for ok in ['KLUDI', 'GROHE', 'GROHE.', 'ARISTON - CHINA', 'ARISTON-SOLAR', 'VERA SOLAR', 'VERA-INDIA', 'JOMIX-VALVE',
                   'FRESH', 'RAKTHERM PVC', 'GF-HP', 'GF-UPVC', 'HOME CREATOR', 'LAMBORGINI']:
            self.assertTrue(item_sync.brand_is_quotable(ok, r), ok)
        for bad in ['PLUMBING', 'COSMO - PPR', 'GROHE OLD', 'ARISTON-OLD', 'RAK CERAMIC', 'HEPWORTH-HDPE', 'JOMIX-MIXER']:
            self.assertFalse(item_sync.brand_is_quotable(bad, r), bad)


class ImporterTests(TestCase):
    def setUp(self):
        AlabamaFeedBrand.objects.all().delete()
        AlabamaFeedBrand.objects.create(brand='KLUDI')

    def test_imports_all_items_stores_values_as_sent_and_flags_quotable(self):
        run_import([feed_row('100', 'KLUDI', cost=0.0, stock=3.0), feed_row('200', 'PLUMBING', cost=7.5, price=11.0),
                    feed_row('-1', 'X'), feed_row('0', 'X'), feed_row('', 'X')])
        self.assertEqual(AlabamaItem.objects.count(), 2)               # junk codes skipped
        a = AlabamaItem.objects.get(item_code='100')
        self.assertTrue(a.quotable)
        self.assertEqual(a.item_cost, 0.0)                             # cost exactly as the API sends it (even 0)
        self.assertEqual(a.item_stock, 3)
        self.assertEqual(a.total_available_stock, Decimal('3.00'))
        b = AlabamaItem.objects.get(item_code='200')
        self.assertFalse(b.quotable)                                   # not on the brand list
        self.assertEqual((b.item_cost, b.item_price), (7.5, 11.0))

    def test_never_touches_junaid_items(self):
        Items.objects.create(item_code='100', item_description='Junaid text', item_firm='JUNAIDBRAND', item_cost=99)
        run_import([feed_row('100', 'KLUDI', desc='Alabama text', cost=1)])
        j = Items.objects.get(item_code='100')
        self.assertEqual((j.item_description, j.item_firm, j.item_cost), ('Junaid text', 'JUNAIDBRAND', 99))
        a = AlabamaItem.objects.get(item_code='100')
        self.assertEqual((a.item_description, a.item_firm, a.item_cost), ('Alabama text', 'KLUDI', 1))

    def test_same_code_different_brand_in_both_catalogs(self):
        Items.objects.create(item_code='271456', item_description='x', item_firm='FRESH - SPARE PARTS')
        run_import([feed_row('271456', 'FRESH')])
        self.assertEqual(Items.objects.get(item_code='271456').item_firm, 'FRESH - SPARE PARTS')
        self.assertEqual(AlabamaItem.objects.get(item_code='271456').item_firm, 'FRESH')

    def test_second_run_updates_in_place_and_is_idempotent(self):
        run_import([feed_row('100', 'KLUDI', cost=1)])
        run_import([feed_row('100', 'KLUDI', cost=2, desc='new')])
        self.assertEqual(AlabamaItem.objects.count(), 1)
        a = AlabamaItem.objects.get()
        self.assertEqual((a.item_cost, a.item_description), (2, 'new'))

    def test_dry_run_writes_nothing(self):
        out = run_import([feed_row('100')], '--dry-run')
        self.assertIn('DRY RUN', out)
        self.assertEqual(AlabamaItem.objects.count(), 0)

    def test_missing_item_becomes_inactive_but_is_never_deleted(self):
        run_import([feed_row(str(i)) for i in range(1, 11)])
        # next run lists 9 of 10 (>= 80%): the 10th is no longer active but stays
        import time
        time.sleep(0.01)
        run_import([feed_row(str(i)) for i in range(1, 10)])
        self.assertEqual(AlabamaItem.objects.count(), 10)
        active = set(item_sync.active_alabama_items().values_list('item_code', flat=True))
        self.assertEqual(active, {str(i) for i in range(1, 10)})
        self.assertNotIn('10', active)

    def test_a_much_smaller_run_is_not_recorded(self):
        run_import([feed_row(str(i)) for i in range(1, 11)])
        out = run_import([feed_row('1'), feed_row('2')])
        self.assertIn('not recording this run', out)
        self.assertEqual(item_sync.active_alabama_items().count(), 10)   # nothing wrongly marked inactive

    def test_changing_the_brand_list_re_flags_items(self):
        run_import([feed_row('1', 'KLUDI'), feed_row('2', 'GROHE')])
        self.assertEqual(AlabamaItem.objects.filter(quotable=True).count(), 1)
        AlabamaFeedBrand.objects.create(brand='GROHE')
        self.assertEqual(item_sync.refresh_quotable_flags(), 1)
        self.assertEqual(AlabamaItem.objects.filter(quotable=True).count(), 2)
        self.assertEqual(item_sync.quotable_items().count(), 2)

    def test_bad_feed_shape_raises(self):
        from django.core.management.base import CommandError
        fh = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
        fh.write('{"nope": 1}')
        fh.close()
        try:
            with self.assertRaises(CommandError):
                call_command('import_items_alabama', '--file', fh.name, stdout=StringIO())
        finally:
            os.unlink(fh.name)


class HistoryLinkTests(TestCase):
    """Backfill + uploads + the ALABAMA_OWN_ITEMS switch, with Junaid's Items kept untouched."""

    def setUp(self):
        AlabamaFeedBrand.objects.all().delete()
        self.cust = Customer.objects.create(customer_code='C1', customer_name='Cust')
        self.j1 = Items.objects.create(item_code='A1', item_description='Junaid desc', item_firm='COSMO - PPR FITTINGS', item_cost=3)
        self.j2 = Items.objects.create(item_code='A2', item_description='Junaid two', item_firm='JOMIX ANURI', item_cost=4)
        self.s1 = AlabamaSalesLine.objects.create(document_type='Invoice', document_number='D1', posting_date=date(2026, 1, 5),
                                                  customer=self.cust, item=self.j1, quantity=10, net_sales=100, gross_profit=20)
        self.s2 = AlabamaSalesLine.objects.create(document_type='Invoice', document_number='D2', posting_date=date(2026, 2, 5),
                                                  customer=self.cust, item=self.j2, quantity=5, net_sales=50, gross_profit=10)
        self.p1 = AlabamaPurchaseLine.objects.create(document_type='PI', document_number='P1', posting_date=date(2026, 1, 1),
                                                     vendor_code='V', vendor_name='V', item=self.j1, quantity=1, unit_price=1, net_purchase=1)
        do = AlabamaDeliveryOrder.objects.create(do_number='DO1', date=date(2026, 1, 2), customer=self.cust)
        self.d1 = AlabamaDeliveryOrderItem.objects.create(delivery_order=do, item=self.j1, quantity=1, price=1, amount=1)
        run_import([feed_row('A1', 'COSMO - PPR', desc='Alabama desc'), feed_row('A2', 'JOMIX', desc='Alabama two')])

    def test_backfill_links_by_code_without_touching_anything_else(self):
        before_items = list(Items.objects.values_list('id', 'item_code', 'item_description', 'item_firm', 'item_cost').order_by('id'))
        out = StringIO()
        call_command('backfill_alabama_items', stdout=out)
        self.assertEqual(self.s1.__class__.objects.get(pk=self.s1.pk).alabama_item.item_code, 'A1')
        self.assertEqual(AlabamaPurchaseLine.objects.get(pk=self.p1.pk).alabama_item.item_code, 'A1')
        self.assertEqual(AlabamaDeliveryOrderItem.objects.get(pk=self.d1.pk).alabama_item.item_code, 'A1')
        s = AlabamaSalesLine.objects.get(pk=self.s1.pk)
        self.assertEqual((s.item_id, s.quantity, s.net_sales, s.gross_profit), (self.j1.id, 10, 100, 20))   # legacy link + figures intact
        self.assertEqual(before_items, list(Items.objects.values_list('id', 'item_code', 'item_description', 'item_firm', 'item_cost').order_by('id')))
        call_command('backfill_alabama_items', stdout=StringIO())                      # idempotent
        call_command('backfill_alabama_items', '--rollback', stdout=StringIO())        # reversible
        self.assertEqual(AlabamaSalesLine.objects.filter(alabama_item__isnull=False).count(), 0)

    def test_backfill_dry_run_writes_nothing(self):
        call_command('backfill_alabama_items', '--dry-run', stdout=StringIO())
        self.assertEqual(AlabamaSalesLine.objects.filter(alabama_item__isnull=False).count(), 0)

    def test_catalog_item_follows_the_switch(self):
        call_command('backfill_alabama_items', stdout=StringIO())
        line = AlabamaSalesLine.objects.get(pk=self.s1.pk)
        with override_settings(ALABAMA_OWN_ITEMS=False):
            self.assertEqual(line.catalog_item.item_firm, 'COSMO - PPR FITTINGS')      # exactly as before
        with override_settings(ALABAMA_OWN_ITEMS=True):
            self.assertEqual(line.catalog_item.item_firm, 'COSMO - PPR')               # Alabama's own brand

    @override_settings(ALABAMA_OWN_ITEMS=True)
    def test_upload_resolution_creates_nothing_in_junaid_items_when_on(self):
        n = Items.objects.count()
        a, legacy = item_sync.resolve_for_upload('A1', 'x', 'y')            # known in both
        self.assertEqual((a.item_code, legacy.pk), ('A1', self.j1.pk))
        a2, legacy2 = item_sync.resolve_for_upload('NEW-ALABAMA-ONLY', 'New thing', 'SOMEBRAND')
        self.assertIsNone(legacy2)
        self.assertEqual((a2.item_description, a2.item_firm, a2.source), ('New thing', 'SOMEBRAND', AlabamaItem.SOURCE_UPLOAD))
        self.assertEqual(Items.objects.count(), n)                           # Junaid's table untouched

    @override_settings(ALABAMA_OWN_ITEMS=False)
    def test_upload_resolution_keeps_old_behaviour_when_off(self):
        n = Items.objects.count()
        a, legacy = item_sync.resolve_for_upload('BRAND-NEW', 'New', 'B')
        self.assertEqual(Items.objects.count(), n + 1)                       # legacy get_or_create, as before
        self.assertEqual(legacy.item_code, 'BRAND-NEW')
        self.assertEqual(a.item_code, 'BRAND-NEW')

    def test_a_later_feed_run_replaces_an_upload_placeholder(self):
        item_sync.get_or_create_for_upload('Z9', 'from excel', 'EXCELBRAND')
        run_import([feed_row('Z9', 'KLUDI', desc='from feed', cost=3)])
        z = AlabamaItem.objects.get(item_code='Z9')
        self.assertEqual((z.item_description, z.item_firm, z.source, z.item_cost), ('from feed', 'KLUDI', 'feed', 3))

    def _totals(self, client, url):
        return client.get(url).content

    def test_analysis_figures_identical_with_switch_on_and_off(self):
        call_command('backfill_alabama_items', stdout=StringIO())
        User = get_user_model()
        User.objects.create_superuser('admin1', 'a@x.com', 'pw')
        self.client.login(username='admin1', password='pw')
        results = {}
        for flag in (False, True):
            with override_settings(ALABAMA_OWN_ITEMS=flag):
                resp = self.client.get('/alabama/item-analysis/')
                self.assertEqual(resp.status_code, 200, flag)
                ctx = resp.context
                rows = {r['item_code']: r for r in ctx['items']} if 'items' in ctx else {}
                results[flag] = rows
                self.assertEqual(self.client.get('/alabama/credit-note-analysis/').status_code, 200)
                api = self.client.get('/alabama/api/item-analysis-totals/').json()['results']
                results[(flag, 'api')] = {r['item_code']: r['total_qty'] for r in api}
        self.assertEqual(results[(False, 'api')], results[(True, 'api')])          # quantities identical
        self.assertEqual(results[(False, 'api')], {'A1': 10, 'A2': 5})
        self.assertEqual(set(results[False]), set(results[True]))

    def test_item_page_shows_alabama_labels_only_when_on(self):
        call_command('backfill_alabama_items', stdout=StringIO())
        User = get_user_model()
        User.objects.create_superuser('admin2', 'a@x.com', 'pw')
        self.client.login(username='admin2', password='pw')
        with override_settings(ALABAMA_OWN_ITEMS=False):
            body = self.client.get('/alabama/item-analysis/').content.decode()
            self.assertIn('Junaid desc', body)
        with override_settings(ALABAMA_OWN_ITEMS=True):
            body = self.client.get('/alabama/item-analysis/').content.decode()
            self.assertIn('Alabama desc', body)
            self.assertNotIn('Junaid desc', body)


class FeedBrandAdminTests(TestCase):
    """The Alabama brand list admin page offers every Alabama brand in a dropdown."""

    def setUp(self):
        AlabamaFeedBrand.objects.all().delete()
        run_import([feed_row('1', 'KLUDI'), feed_row('2', 'KLUDI'), feed_row('3', 'PLUMBING'),
                    feed_row('4', 'RAKTHERM'), feed_row('5', 'RAKTHERM PVC')])
        self.admin_user = get_user_model().objects.create_superuser('root', 'r@x.com', 'x')
        self.client.force_login(self.admin_user)

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_add_page_lists_every_alabama_brand_with_counts(self):
        resp = self.client.get('/admin/alabama/alabamafeedbrand/add/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('<datalist id="brand-suggestions">', body)
        for brand, n in (('KLUDI', 2), ('PLUMBING', 1), ('RAKTHERM', 1), ('RAKTHERM PVC', 1)):
            self.assertIn(f'<option value="{brand}" label="{n} items">', body)

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_adding_a_brand_flags_its_items_and_a_prefix_rule_works(self):
        self.client.post('/admin/alabama/alabamafeedbrand/add/', {'brand': 'kludi', 'match_type': 'exact', 'is_active': 'on'})
        self.client.post('/admin/alabama/alabamafeedbrand/add/', {'brand': 'RAKTHERM', 'match_type': 'prefix', 'is_active': 'on'})
        quotable = set(AlabamaItem.objects.filter(quotable=True).values_list('item_code', flat=True))
        self.assertEqual(quotable, {'1', '2', '4', '5'})

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_duplicate_brand_is_rejected_and_unmatched_brand_only_warns(self):
        self.client.post('/admin/alabama/alabamafeedbrand/add/', {'brand': 'KLUDI', 'match_type': 'exact', 'is_active': 'on'})
        resp = self.client.post('/admin/alabama/alabamafeedbrand/add/', {'brand': 'K-ludi', 'match_type': 'exact', 'is_active': 'on'})
        self.assertContains(resp, 'already in the list')
        self.assertEqual(AlabamaFeedBrand.objects.count(), 1)
        resp = self.client.post('/admin/alabama/alabamafeedbrand/add/', {'brand': 'NOSUCHBRAND', 'match_type': 'exact', 'is_active': 'on'},
                                follow=True)
        self.assertContains(resp, 'does not match any brand in the Alabama API')
        self.assertEqual(AlabamaFeedBrand.objects.count(), 2)

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_list_page_shows_how_many_items_each_rule_matches(self):
        AlabamaFeedBrand.objects.create(brand='RAKTHERM', match_type='prefix')
        resp = self.client.get('/admin/alabama/alabamafeedbrand/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '<td class="field-items_matched">2</td>', html=False)
