"""The direct 'Create Customer Order' page (/create/): same company rule and item catalog as the quotation page."""
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from .models import CustomerPrice, OrderItem, SalesOrder
from .tests_alabama_catalog import HOSTS, Base


@override_settings(ALLOWED_HOSTS=HOSTS)
class DirectSalesOrderTests(Base):

    def setUp(self):
        super().setUp()
        self.alabama_admin = self._user('alrole_admin', 'Admin', 'Alabama')       # role Alabama, no "alabama" in the name
        self.junaid_admin = self._user('jadmin', 'Admin', 'Junaid')
        self.alabama_salesman = self._user('alrole_sales', 'Salesman', 'Alabama')  # role Alabama, no "alabama" in the name
        patcher = mock.patch('so.views.send_telegram_message')
        patcher.start()
        self.addCleanup(patcher.stop)

    def post_order(self, items, **extra):
        data = {'customer': self.cust.id, 'salesman': self.salesman.id, 'location': 'L', 'salesman_remarks': 'R'}
        data.update(extra)
        data['item'] = [i[0] for i in items]
        data['quantity'] = [str(i[1]) for i in items]
        data['price'] = [str(i[2]) for i in items]
        data['unit'] = ['pcs'] * len(items)
        return self.client.post(reverse('create_sales_order'), data)

    # ---- switch ON: the quotation rule ---------------------------------------------------------------------------
    @override_settings(ALABAMA_OWN_CATALOG=True)
    def test_who_creates_which_company_when_the_switch_is_on(self):
        cases = [(self.alabama, 'ALABAMA'), (self.alabama_admin, 'ALABAMA'), (self.junaid_admin, 'JUNAID'),
                 (self.junaid, 'JUNAID'), (self.alabama_salesman, 'JUNAID'), (self.so_admin, 'JUNAID')]
        for user, expected in cases:
            self.login(user)
            ctx = self.client.get(reverse('create_sales_order')).context
            self.assertEqual(ctx['default_division'], expected, user.username)
            self.assertEqual(ctx['catalog_division'], 'ALABAMA' if expected == 'ALABAMA' else '', user.username)

    @override_settings(ALABAMA_OWN_CATALOG=True)
    def test_company_dropdown_only_for_so_and_manager(self):
        self.login(self.so_admin)
        resp = self.client.get(reverse('create_sales_order'))
        self.assertTrue(resp.context['show_company_select'])
        self.assertContains(resp, 'id="division"')
        resp = self.client.get(reverse('create_sales_order') + '?division=ALABAMA')
        self.assertEqual(resp.context['catalog_division'], 'ALABAMA')
        self.assertEqual(list(resp.context['firms']), ['KLUDI'])
        for user in (self.alabama, self.alabama_admin, self.junaid, self.junaid_admin):
            self.login(user)
            resp = self.client.get(reverse('create_sales_order'))
            self.assertFalse(resp.context['show_company_select'], user.username)
            self.assertNotContains(resp, 'id="division"')

    @override_settings(ALABAMA_OWN_CATALOG=True)
    def test_alabama_username_user_saves_an_alabama_order_with_alabama_items(self):
        self.login(self.alabama)
        self.post_order([('a:1', 2, 40), ('a:3', 1, 12)])
        order = SalesOrder.objects.get()
        self.assertEqual(order.division, 'ALABAMA')
        self.assertEqual({(l.item_id, l.alabama_item_id) for l in order.items.all()}, {(None, 1), (None, 3)})
        self.assertEqual(order.total_amount, 92)
        self.assertEqual(CustomerPrice.objects.get(customer=self.cust, alabama_item=self.a1).custom_price, 40)
        self.assertFalse(CustomerPrice.objects.filter(item__isnull=False).exists())

    @override_settings(ALABAMA_OWN_CATALOG=True)
    def test_alabama_role_admin_now_creates_an_alabama_order(self):
        self.login(self.alabama_admin)
        self.post_order([('a:1', 1, 33)])
        order = SalesOrder.objects.get()
        self.assertEqual(order.division, 'ALABAMA')
        self.assertEqual(order.items.get().alabama_item_id, 1)
        # and it shows in that admin's own sales-order list (which is limited to ALABAMA orders)
        listing = self.client.get(reverse('view_sales_orders'))
        self.assertContains(listing, order.order_number)

    @override_settings(ALABAMA_OWN_CATALOG=True)
    def test_company_chooser_picks_the_company(self):
        self.login(self.so_admin)
        self.post_order([('a:1', 1, 33)], division='ALABAMA')
        self.assertEqual(SalesOrder.objects.get().division, 'ALABAMA')
        SalesOrder.objects.all().delete()
        self.post_order([('1', 1, 20)], division='JUNAID')
        order = SalesOrder.objects.get()
        self.assertEqual((order.division, order.items.get().item_id), ('JUNAID', 1))
        SalesOrder.objects.all().delete()
        self.post_order([('1', 1, 20)])                                         # no company posted = Junaid
        self.assertEqual(SalesOrder.objects.get().division, 'JUNAID')

    @override_settings(ALABAMA_OWN_CATALOG=True)
    def test_alabama_item_rejected_on_a_junaid_order(self):
        for user, extra in ((self.junaid, {}), (self.junaid_admin, {}), (self.so_admin, {'division': 'JUNAID'})):
            self.login(user)
            self.post_order([('a:1', 1, 33)], **extra)
            self.assertFalse(OrderItem.objects.filter(alabama_item__isnull=False).exists(), user.username)

    @override_settings(ALABAMA_OWN_CATALOG=True)
    def test_junaid_order_is_unchanged_for_junaid_users(self):
        self.login(self.junaid)
        self.post_order([('1', 2, 20), ('2', 1, 9)])
        order = SalesOrder.objects.get()
        self.assertEqual(order.division, 'JUNAID')
        self.assertEqual({(l.item_id, l.alabama_item_id) for l in order.items.all()}, {(1, None), (2, None)})
        self.assertTrue(CustomerPrice.objects.filter(item_id=1).exists())

    @override_settings(ALABAMA_OWN_CATALOG=True)
    def test_new_customer_on_the_page_works_with_alabama_items(self):
        self.login(self.alabama)
        data = {'new_customer_name': 'Walk In LLC', 'salesman': self.salesman.id, 'item': ['a:1'], 'quantity': ['1'],
                'price': ['33'], 'unit': ['pcs']}
        self.client.post(reverse('create_sales_order'), data)
        order = SalesOrder.objects.get()
        self.assertEqual((order.customer.customer_name, order.division), ('Walk In LLC', 'ALABAMA'))

    @override_settings(ALABAMA_OWN_CATALOG=True)
    def test_saved_alabama_order_opens_edits_and_exports(self):
        self.login(self.alabama)
        self.post_order([('a:3', 2, 15)])
        order = SalesOrder.objects.get()
        self.assertContains(self.client.get(reverse('view_sales_order_details', args=[order.id])), 'Alabama costed')
        self.assertContains(self.client.get(reverse('edit_sales_order', args=[order.id])), 'value="a:3" selected')
        self.assertEqual(self.client.get(reverse('export_sales_order_to_excel', args=[order.id])).status_code, 200)

    # ---- switch OFF: exactly the old behaviour --------------------------------------------------------------------
    @override_settings(ALABAMA_OWN_CATALOG=False)
    def test_switch_off_keeps_the_username_rule_and_junaid_items(self):
        for user, expected in ((self.alabama, 'ALABAMA'), (self.alabama_admin, 'JUNAID'), (self.junaid, 'JUNAID'),
                               (self.so_admin, 'JUNAID')):
            self.login(user)
            resp = self.client.get(reverse('create_sales_order'))
            self.assertEqual(resp.context['default_division'], expected, user.username)
            self.assertEqual(resp.context['catalog_division'], '')
            self.assertFalse(resp.context['show_company_select'])
            self.assertEqual(set(resp.context['firms']), {'ARISTON', 'KLUDI'})
        self.login(self.alabama_admin)
        self.post_order([('1', 1, 20)])
        self.assertEqual(SalesOrder.objects.get().division, 'JUNAID')           # unchanged: role alone does not make it Alabama
        SalesOrder.objects.all().delete()
        self.login(self.alabama)
        self.post_order([('1', 1, 20)])
        order = SalesOrder.objects.get()
        self.assertEqual((order.division, order.items.get().item_id), ('ALABAMA', 1))

    @override_settings(ALABAMA_OWN_CATALOG=True)
    def test_page_marks_the_catalog_for_its_scripts(self):
        self.login(self.alabama)
        self.assertContains(self.client.get(reverse('create_sales_order')), "window.QUOTE_CATALOG = 'ALABAMA'")
        self.login(self.junaid)
        self.assertContains(self.client.get(reverse('create_sales_order')), "window.QUOTE_CATALOG = ''")
