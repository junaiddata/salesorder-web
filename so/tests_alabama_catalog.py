"""New Alabama quotations / sales orders use Alabama's own item master (alabama.AlabamaItem) while every existing
quotation, sales order and Junaid flow stays exactly as it was.

Covers: the pickers and lookups (search, brand lists, stock, price), saving (create / edit / convert), showing and
printing, id collisions between the two catalogs, old lines surviving an edit unchanged, and Junaid being untouched."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from alabama.models import AlabamaFeedBrand, AlabamaItem
from . import quotation_conversion_service as conv
from .models import (Customer, CustomerPrice, Items, ItemSyncStatus, OrderItem, Quotation, QuotationItem, Role,
                     SalesOrder, Salesman)

HOSTS = ['testserver']


class Base(TestCase):
    def setUp(self):
        cache.clear()
        AlabamaFeedBrand.objects.all().delete()
        AlabamaFeedBrand.objects.create(brand='KLUDI')
        self.salesman = Salesman.objects.create(salesman_name='KADER')
        self.cust = Customer.objects.create(customer_code='C1', customer_name='Cust One', salesman=self.salesman)

        # Junaid items and Alabama items. The SAME ids on purpose (the collision the 'a:' prefix guards against).
        self.j1 = Items.objects.create(id=1, item_code='A1', item_description='Junaid heater', item_firm='ARISTON',
                                       item_cost=10, item_price=20, item_stock=5, item_upvc='J-UPC')
        self.j2 = Items.objects.create(id=2, item_code='A2', item_description='Junaid mixer', item_firm='KLUDI',
                                       item_cost=4, item_price=9, item_stock=7)
        now = timezone.now()
        for it in (self.j1, self.j2):
            ItemSyncStatus.objects.create(item=it, last_seen_at=now)
        self.a1 = AlabamaItem.objects.create(id=1, item_code='A1', item_description='Alabama heater', item_firm='KLUDI',
                                             item_cost=0, item_price=33, item_stock=2, total_available_stock=Decimal('2'),
                                             item_upvc='A-UPC', quotable=True, last_seen_at=now)
        self.a2 = AlabamaItem.objects.create(id=2, item_code='A2', item_description='Alabama hidden', item_firm='PLUMBING',
                                             item_cost=1, item_price=5, quotable=False, last_seen_at=now)
        self.a3 = AlabamaItem.objects.create(id=3, item_code='A3', item_description='Alabama costed', item_firm='KLUDI',
                                             item_cost=8, item_price=12, item_stock=4, total_available_stock=Decimal('4'),
                                             quotable=True, last_seen_at=now)

        self.alabama = self._user('alabama_sam', 'Admin', 'Alabama')
        self.junaid = self._user('jsam', 'Salesman', 'Junaid')
        self.so_admin = self._user('so', 'Admin', 'Junaid')

    def _user(self, name, role, company):
        u = User.objects.create_user(name, password='x')
        Role.objects.create(user=u, role=role, company=company)
        return u

    def login(self, user):
        self.client.force_login(user)

    def json_ids(self, url, params):
        data = self.client.get(url, params).json()
        rows = data['items'] if isinstance(data, dict) else data
        return {str(r['id']): r for r in rows}

    def post_quote(self, items, division=None, **extra):
        """items: list of (item_ref, qty, price)."""
        data = {'customer': self.cust.id, 'salesman': self.salesman.id, 'remarks': ''}
        if division:
            data['division'] = division
        data.update(extra)
        data['item'] = [i[0] for i in items]
        data['quantity'] = [str(i[1]) for i in items]
        data['price'] = [str(i[2]) for i in items]
        data['unit'] = ['pcs'] * len(items)
        return self.client.post(reverse('create_quotation'), data)


@override_settings(ALABAMA_OWN_CATALOG=False, ALLOWED_HOSTS=HOSTS)
class SwitchOffTests(Base):
    """With the switch off nothing about the pickers changes -- even for an Alabama user."""

    def test_pickers_are_junaid_items_and_no_catalog_marker(self):
        self.login(self.alabama)
        resp = self.client.get(reverse('create_quotation'))
        self.assertEqual(resp.context['catalog_division'], '')
        self.assertEqual(set(resp.context['firms']), {'ARISTON', 'KLUDI'})
        ids = self.json_ids('/api/items-search/', {'q': 'heater', 'division': 'ALABAMA'})
        self.assertEqual(set(ids), {'1'})
        self.assertEqual(ids['1']['item_description'], 'Junaid heater')
        self.assertNotIn('source', ids['1'])

    def test_bare_ids_still_create_junaid_lines_for_alabama(self):
        self.login(self.alabama)
        self.post_quote([('1', 2, 20)])
        q = Quotation.objects.get()
        self.assertEqual(q.division, 'ALABAMA')
        line = q.items.get()
        self.assertEqual((line.item_id, line.alabama_item_id), (1, None))


@override_settings(ALABAMA_OWN_CATALOG=True, ALLOWED_HOSTS=HOSTS)
class PickerTests(Base):
    def test_alabama_search_returns_only_quotable_alabama_items_with_prefixed_ids(self):
        self.login(self.alabama)
        ids = self.json_ids('/api/items-search/', {'q': '', 'division': 'ALABAMA'})
        self.assertEqual(set(ids), {'a:1', 'a:3'})                        # a2 (brand not on the list) is not offered
        self.assertEqual(ids['a:1']['item_description'], 'Alabama heater')
        self.assertEqual(ids['a:1']['source'], 'alabama')
        self.assertEqual(ids['a:1']['item_upvc'], 'A-UPC')

    def test_search_without_division_is_still_junaid(self):
        """The direct sales-order create page and every Junaid screen send no division: unchanged."""
        self.login(self.alabama)
        ids = self.json_ids('/api/items-search/', {'q': 'heater'})
        self.assertEqual(set(ids), {'1'})
        self.assertEqual(ids['1']['item_description'], 'Junaid heater')

    def test_a_junaid_user_cannot_get_the_alabama_catalog(self):
        self.login(self.junaid)
        ids = self.json_ids('/api/items-search/', {'q': 'heater', 'division': 'ALABAMA'})
        self.assertEqual(set(ids), {'1'})

    def test_company_chooser_gets_the_catalog_they_ask_for(self):
        self.login(self.so_admin)
        self.assertEqual(set(self.json_ids('/api/items-search/', {'q': 'heater', 'division': 'ALABAMA'})), {'a:1'})
        self.assertEqual(set(self.json_ids('/api/items-search/', {'q': 'heater', 'division': 'JUNAID'})), {'1'})
        self.assertEqual(set(self.json_ids('/api/items-search/', {'q': 'heater'})), {'1'})

    def test_items_by_firm_alabama_and_cache_separation(self):
        self.login(self.alabama)
        a = self.json_ids('/get_items_by_firm/', {'firms': 'KLUDI', 'division': 'ALABAMA'})
        self.assertEqual(set(a), {'a:1', 'a:3'})
        j = self.json_ids('/get_items_by_firm/', {'firms': 'KLUDI'})
        self.assertEqual(set(j), {'2'})                                    # the Junaid list is not served the Alabama cache
        a2 = self.json_ids('/get_items_by_firm/', {'firms': 'KLUDI', 'division': 'ALABAMA'})
        self.assertEqual(set(a2), {'a:1', 'a:3'})

    def test_create_page_brand_list_is_alabamas_quotable_brands(self):
        self.login(self.alabama)
        resp = self.client.get(reverse('create_quotation'))
        self.assertEqual(resp.context['catalog_division'], 'ALABAMA')
        self.assertEqual(list(resp.context['firms']), ['KLUDI'])
        self.login(self.junaid)
        resp = self.client.get(reverse('create_quotation'))
        self.assertEqual(resp.context['catalog_division'], '')
        self.assertEqual(set(resp.context['firms']), {'ARISTON', 'KLUDI'})

    def test_company_chooser_page_follows_the_chosen_company(self):
        self.login(self.so_admin)
        self.assertEqual(self.client.get(reverse('create_quotation')).context['catalog_division'], '')
        resp = self.client.get(reverse('create_quotation') + '?division=ALABAMA')
        self.assertEqual(resp.context['catalog_division'], 'ALABAMA')
        self.assertEqual(list(resp.context['firms']), ['KLUDI'])

    def test_stock_and_price_use_the_right_catalog_for_the_same_numeric_id(self):
        self.login(self.alabama)
        a = self.client.get('/get_item_stock/', {'item_id': 'a:1'}).json()
        j = self.client.get('/get_item_stock/', {'item_id': '1'}).json()
        self.assertEqual((a['stock'], a['cost'], a['min_selling_price']), (2, 0.0, 33.0))
        self.assertEqual((j['stock'], j['cost'], j['min_selling_price']), (5, 10.0, 20.0))
        pa = self.client.get('/get_item_price/', {'item_id': 'a:1', 'customer_id': self.cust.id}).json()
        pj = self.client.get('/get_item_price/', {'item_id': '1', 'customer_id': self.cust.id}).json()
        self.assertEqual((pa['default_price'], pj['default_price']), (33.0, 20.0))
        pa2 = self.client.get('/get_item_price/', {'item_id': 'a:1', 'customer_id': self.cust.id}).json()   # cached copy
        self.assertEqual(pa2['default_price'], 33.0)

    def test_customer_price_is_looked_up_per_catalog(self):
        CustomerPrice.objects.create(customer=self.cust, item=self.j1, custom_price=25)
        CustomerPrice.objects.create(customer=self.cust, alabama_item=self.a1, custom_price=40)
        self.login(self.alabama)
        pa = self.client.get('/get_item_price/', {'item_id': 'a:1', 'customer_id': self.cust.id}).json()
        pj = self.client.get('/get_item_price/', {'item_id': '1', 'customer_id': self.cust.id}).json()
        self.assertEqual(pa['custom_price'], 40.0)
        self.assertEqual(pj['custom_price'], 25.0)

    def test_unknown_or_junk_ids_are_404(self):
        self.login(self.alabama)
        for bad in ('a:999', 'a:x', 'zzz', ''):
            self.assertEqual(self.client.get('/get_item_stock/', {'item_id': bad}).status_code, 404, bad)


@override_settings(ALABAMA_OWN_CATALOG=True, ALLOWED_HOSTS=HOSTS)
class SaveTests(Base):
    def test_new_alabama_quotation_saves_alabama_lines_and_prices(self):
        self.login(self.alabama)
        self.post_quote([('a:1', 3, 40), ('a:3', 1, 12)])
        q = Quotation.objects.get()
        self.assertEqual(q.division, 'ALABAMA')
        lines = {l.alabama_item_id: l for l in q.items.all()}
        self.assertEqual(set(lines), {1, 3})
        self.assertTrue(all(l.item_id is None for l in lines.values()))
        self.assertEqual(lines[1].line_total, 120)
        self.assertEqual(q.total_amount, 132)
        self.assertEqual(CustomerPrice.objects.get(customer=self.cust, alabama_item=self.a1).custom_price, 40)
        self.assertFalse(CustomerPrice.objects.filter(item__isnull=False).exists())    # no Junaid price was touched

    def test_junaid_user_cannot_post_an_alabama_item(self):
        self.login(self.junaid)
        self.post_quote([('a:1', 1, 10)])
        self.assertFalse(QuotationItem.objects.filter(alabama_item__isnull=False).exists())

    def test_alabama_ref_rejected_when_chooser_picks_junaid(self):
        self.login(self.so_admin)
        self.post_quote([('a:1', 1, 10)], division='JUNAID')
        self.assertFalse(QuotationItem.objects.filter(alabama_item__isnull=False).exists())

    def test_chooser_can_make_an_alabama_quotation_with_alabama_items(self):
        self.login(self.so_admin)
        self.post_quote([('a:1', 1, 10)], division='ALABAMA')
        self.assertEqual(QuotationItem.objects.get().alabama_item_id, 1)

    def test_junaid_quotation_is_unchanged(self):
        self.login(self.junaid)
        self.post_quote([('1', 2, 20), ('2', 1, 9)])
        q = Quotation.objects.get()
        self.assertEqual(q.division, 'JUNAID')
        self.assertEqual({(l.item_id, l.alabama_item_id) for l in q.items.all()}, {(1, None), (2, None)})
        self.assertTrue(CustomerPrice.objects.filter(item_id=1).exists())

    def test_a_line_cannot_have_both_items(self):
        q = Quotation.objects.create(customer=self.cust, salesman=self.salesman, division='ALABAMA')
        with self.assertRaises(IntegrityError), transaction.atomic():
            QuotationItem.objects.create(quotation=q, item=self.j1, alabama_item=self.a1, quantity=1, price=1, line_total=1)


@override_settings(ALABAMA_OWN_CATALOG=True, ALLOWED_HOSTS=HOSTS)
class OldQuotationTests(Base):
    """Existing quotations keep their lines exactly; a mixed quotation (old + new lines) works end to end."""

    def make_old_alabama_quote(self):
        q = Quotation.objects.create(customer=self.cust, salesman=self.salesman, division='ALABAMA',
                                     total_amount=60, grand_total=60, status='Approved')
        QuotationItem.objects.create(quotation=q, item=self.j1, quantity=3, price=20, line_total=60, unit='ctn')
        return q

    def edit_post(self, q, items):
        data = {'customer': self.cust.id, 'salesman': self.salesman.id, 'remarks': 'r'}
        data['item'] = [i[0] for i in items]
        data['quantity'] = [str(i[1]) for i in items]
        data['price'] = [str(i[2]) for i in items]
        data['unit'] = [i[3] if len(i) > 3 else 'pcs' for i in items]
        return self.client.post(reverse('edit_quotation', args=[q.id]), data)

    def test_old_quotation_shows_old_values_when_the_switch_is_on(self):
        q = self.make_old_alabama_quote()
        self.login(self.alabama)
        resp = self.client.get(reverse('view_quotation_details', args=[q.id]))
        self.assertContains(resp, 'Junaid heater')
        self.assertNotContains(resp, 'Alabama heater')

    def test_editing_an_old_quotation_resaves_old_lines_unchanged(self):
        q = self.make_old_alabama_quote()
        self.login(self.alabama)
        resp = self.client.get(reverse('edit_quotation', args=[q.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="1" selected')                     # the old line posts back its bare Junaid id
        self.edit_post(q, [('1', 3, 20, 'ctn')])
        line = q.items.get()
        self.assertEqual((line.item_id, line.alabama_item_id, line.quantity, line.price, line.unit), (1, None, 3, 20, 'ctn'))

    def test_adding_a_new_catalog_line_to_an_old_quotation_gives_a_mixed_quotation(self):
        q = self.make_old_alabama_quote()
        self.login(self.alabama)
        self.edit_post(q, [('1', 3, 20, 'ctn'), ('a:3', 2, 12)])
        lines = list(q.items.order_by('id'))
        self.assertEqual([(l.item_id, l.alabama_item_id) for l in lines], [(1, None), (None, 3)])
        resp = self.client.get(reverse('view_quotation_details', args=[q.id]))
        self.assertContains(resp, 'Junaid heater')                         # old line: old values
        self.assertContains(resp, 'Alabama costed')                        # new line: Alabama values
        self.assertEqual(self.client.get(reverse('edit_quotation', args=[q.id])).status_code, 200)
        self.assertContains(self.client.get(reverse('edit_quotation', args=[q.id])), 'value="a:3" selected')
        q.refresh_from_db()
        self.assertEqual(q.total_amount, 84)

    def test_mixed_quotation_exports_and_converts(self):
        q = self.make_old_alabama_quote()
        QuotationItem.objects.create(quotation=q, alabama_item=self.a3, quantity=2, price=15, line_total=30, unit='pcs')
        self.login(self.alabama)
        self.assertEqual(self.client.get(reverse('export_quotation_to_excel', args=[q.id])).status_code, 200)
        from .quotation_pdf_new import build_so_quotation_pdf
        self.assertTrue(build_so_quotation_pdf(q).startswith(b'%PDF'))
        order = conv.convert_quotation_to_sales_order(q, username='alabama_sam')
        rows = list(order.items.order_by('id'))
        self.assertEqual([(r.item_id, r.alabama_item_id) for r in rows], [(1, None), (None, 3)])
        self.assertEqual(order.division, 'ALABAMA')
        self.assertTrue(CustomerPrice.objects.filter(customer=self.cust, alabama_item=self.a3, custom_price=15).exists())

    def test_old_quotation_converts_exactly_as_before(self):
        q = self.make_old_alabama_quote()
        order = conv.convert_quotation_to_sales_order(q, username='alabama_sam')
        row = order.items.get()
        self.assertEqual((row.item_id, row.alabama_item_id, row.quantity, row.price, row.unit), (1, None, 3, 20, 'ctn'))


@override_settings(ALABAMA_OWN_CATALOG=True, ALLOWED_HOSTS=HOSTS)
class SalesOrderTests(Base):
    def make_order(self):
        order = SalesOrder.objects.create(customer=self.cust, salesman=self.salesman, division='ALABAMA',
                                          total_amount=50, tax=2.5)
        OrderItem.objects.create(order=order, item=self.j1, quantity=1, price=20, unit='pcs')
        OrderItem.objects.create(order=order, alabama_item=self.a3, quantity=2, price=15, unit='pcs')
        return order

    def test_order_pages_show_each_lines_own_values(self):
        order = self.make_order()
        self.login(self.alabama)
        resp = self.client.get(reverse('view_sales_order_details', args=[order.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Junaid heater')
        self.assertContains(resp, 'Alabama costed')
        self.assertEqual(self.client.get(reverse('export_sales_order_to_excel', args=[order.id])).status_code, 200)

    def test_editing_an_order_resaves_both_kinds_of_line(self):
        order = self.make_order()
        self.login(self.alabama)
        resp = self.client.get(reverse('edit_sales_order', args=[order.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="1" selected')
        self.assertContains(resp, 'value="a:3" selected')
        self.client.post(reverse('edit_sales_order', args=[order.id]), {
            'customer': self.cust.id, 'salesman': self.salesman.id,
            'item': ['1', 'a:3'], 'quantity': ['1', '2'], 'price': ['20', '15'], 'unit': ['pcs', 'pcs']})
        rows = list(order.items.order_by('id'))
        self.assertEqual([(r.item_id, r.alabama_item_id) for r in rows], [(1, None), (None, 3)])

    def test_alabama_item_rejected_on_a_junaid_order(self):
        order = SalesOrder.objects.create(customer=self.cust, salesman=self.salesman, division='JUNAID')
        OrderItem.objects.create(order=order, item=self.j1, quantity=1, price=20, unit='pcs')
        self.login(self.so_admin)
        resp = self.client.post(reverse('edit_sales_order', args=[order.id]), {
            'customer': self.cust.id, 'salesman': self.salesman.id,
            'item': ['a:3'], 'quantity': ['1'], 'price': ['15'], 'unit': ['pcs']})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual([(r.item_id, r.alabama_item_id) for r in order.items.all()], [(1, None)])   # rolled back
