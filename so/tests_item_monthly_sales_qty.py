"""Item Monthly Sales > Sold Qty tab and the proposed 2027 budgets of all three tabs."""
import io
from datetime import date
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from .models import (ItemBudget, Items, Role, SAPARCreditMemo, SAPARCreditMemoItem, SAPARInvoice,
                     SAPARInvoiceItem)

PAGE = reverse('item_monthly_sales')
SAVE = reverse('item_monthly_sales_budget')


@override_settings(ALLOWED_HOSTS=['testserver'])
class SoldQtyTests(TestCase):
    def setUp(self):
        self.a = Items.objects.create(item_code='A1', item_description='Pipe', item_firm='ACME')
        self.b = Items.objects.create(item_code='B1', item_description='Valve', item_firm='ZED')
        self.c = Items.objects.create(item_code='C1', item_description='Never sold', item_firm='ACME')
        self._doc(SAPARInvoice, SAPARInvoiceItem, 'I1', 1, self.a, 10, 'KADER')
        self._doc(SAPARInvoice, SAPARInvoiceItem, 'I2', 3, self.a, 5, 'KADER')
        self._doc(SAPARCreditMemo, SAPARCreditMemoItem, 'CM1', 3, self.a, -2, 'KADER')       # return
        self._doc(SAPARInvoice, SAPARInvoiceItem, 'I3', 4, self.a, 7, 'KADER')
        self._doc(SAPARInvoice, SAPARInvoiceItem, 'I4', 4, self.a, -7, 'KADER')              # cancellation document
        self._doc(SAPARInvoice, SAPARInvoiceItem, 'I5', 5, self.b, 100, 'OTHER')
        self.admin = self._user('boss', 'Admin')
        self.salesman = self._user('kader', 'Salesman')
        self.root = User.objects.create_user('root', password='x', is_superuser=True)        # no Role row
        self.client.force_login(self.admin)

    def _doc(self, head_model, line_model, number, month, item, qty, salesman):
        is_invoice = head_model is SAPARInvoice
        head = head_model.objects.create(
            **{('invoice_number' if is_invoice else 'credit_memo_number'): number},
            posting_date=date(2026, month, 5), customer_name='Cust', salesman_name=salesman, store='HO')
        line_model.objects.create(
            **{('invoice' if is_invoice else 'credit_memo'): head}, item=item, item_code=item.item_code,
            item_description=item.item_description, quantity=D(qty), price=D(2), line_total=D(qty * 2))

    def _user(self, name, role):
        u = User.objects.create_user(name, password='x')
        Role.objects.create(user=u, role=role)
        return u

    def rows(self, **params):
        resp = self.client.get(PAGE, {'view': 'qty', 'year': 2026, **params})
        self.assertEqual(resp.status_code, 200)
        return {r['item_code']: r for r in resp.context['rows']}

    def excel(self, **params):
        resp = self.client.get(PAGE, {'view': 'qty', 'year': 2026, 'export': 'xlsx', **params})
        self.assertEqual(resp.status_code, 200)
        return list(load_workbook(io.BytesIO(resp.content)).worksheets[0].iter_rows(values_only=True))

    def pdf_text(self, **params):
        from pypdf import PdfReader
        resp = self.client.get(PAGE, {'view': 'qty', 'year': 2026, 'export': 'pdf', **params})
        self.assertEqual(resp.status_code, 200)
        return '\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(resp.content)).pages)

    def save(self, item, qty, client=None):
        return (client or self.client).post(SAVE, {'item': item, 'qty': qty})

    def test_net_qty_and_items_without_sales(self):
        rows = self.rows()
        self.assertEqual(list(rows), ['B1', 'A1', 'C1'])    # most sold first, never sold last
        self.assertEqual(rows['A1']['m'][:4], [D(10), D(0), D(3), D(0)])
        self.assertEqual(rows['A1']['total'], D(13))
        self.assertFalse(rows['C1']['sold'])

    def test_filters(self):
        self.assertEqual(set(self.rows(brand='ACME')), {'A1', 'C1'})
        self.assertEqual(set(self.rows(q='never')), {'C1'})
        self.assertEqual(self.rows(q='pipe')['A1']['total'], D(13))
        by_salesman = self.rows(salesman='KADER')
        self.assertFalse(by_salesman['B1']['sold'])         # still listed, without sales
        self.assertEqual(by_salesman['A1']['total'], D(13))

    def test_salesman_sees_only_own_sales_but_the_whole_budget(self):
        self.save('B1', '50')
        self.client.force_login(self.salesman)
        rows = self.rows()
        self.assertFalse(rows['B1']['sold'])
        self.assertEqual(rows['B1']['budget'], 50)
        html = self.client.get(PAGE, {'view': 'qty', 'year': 2026}).content.decode()
        self.assertNotIn('class="budget-input"', html)
        self.assertNotIn(SAVE, html)

    def test_admin_saves_changes_and_clears(self):
        self.assertIn('class="budget-input"', self.client.get(PAGE, {'view': 'qty'}).content.decode())
        self.assertEqual(self.save('C1', '1,500').json(), {'ok': True, 'qty': 1500})
        self.save('C1', '20')
        budget = ItemBudget.objects.get()
        self.assertEqual((budget.item_code, budget.year, budget.qty, budget.updated_by), ('C1', 2027, 20, self.admin))
        self.assertEqual(self.save('C1', '0').json()['qty'], 0)
        self.assertEqual(self.save('C1', '').json(), {'ok': True, 'qty': None})
        self.assertFalse(ItemBudget.objects.exists())

    def test_only_role_admin_can_save(self):
        for user in (self.salesman, self.root):
            self.client.force_login(user)
            self.assertEqual(self.save('A1', '5').status_code, 403, user.username)
        self.assertFalse(ItemBudget.objects.exists())

    def test_bad_requests_are_rejected(self):
        for value in ('-1', '1.5', '1,5', 'abc', '1e3', '1000000000'):
            self.assertEqual(self.save('A1', value).status_code, 400, value)
        self.assertEqual(self.save('NOPE', '5').status_code, 400)
        self.assertEqual(self.client.post(SAVE, {'item': 'A1'}).status_code, 400)   # missing qty never deletes
        self.assertEqual(self.client.get(SAVE).status_code, 405)
        self.assertFalse(ItemBudget.objects.exists())

    def test_save_needs_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(self.save('A1', '5', client).status_code, 403)
        client.get(PAGE, {'view': 'qty'})
        resp = client.post(SAVE, {'item': 'A1', 'qty': '5'}, HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertEqual(resp.status_code, 200)

    def test_excel_has_all_items_and_the_budget_last(self):
        self.save('C1', '40')
        sheet = self.excel()
        self.assertEqual(sheet[0][15:], ('Total Qty', 'Proposed Budget Qty 2027'))
        by_code = {r[0]: r for r in sheet[1:]}
        self.assertEqual(set(by_code), {'A1', 'B1', 'C1'})
        self.assertEqual(by_code['A1'][15:], (13, None))
        self.assertEqual(by_code['C1'][15:], (None, 40))

    def test_pdf_without_brand_lists_only_items_with_sales_or_budget(self):
        text = self.pdf_text()
        self.assertIn('A1', text)
        self.assertNotIn('C1', text)
        self.assertIn('C1', self.pdf_text(brand='ACME'))
        self.save('C1', '4321')
        text = self.pdf_text()
        self.assertIn('C1', text)
        self.assertIn('4,321', text)

    # ---- Sales Amount / Average Unit Selling Price budgets

    LABEL = {'amount': 'Proposed Budget Sales Amount 2027',
             'price': 'Proposed Budget Average Unit Selling Price 2027',
             'qty': 'Proposed Budget Qty 2027'}

    def post(self, item, **value):
        return self.client.post(SAVE, {'item': item, **value})

    def budgets(self, view):
        resp = self.client.get(PAGE, {'view': view, 'year': 2026})
        return {r['item_code']: r['budget'] for r in resp.context['rows']}

    def test_each_tab_has_only_its_own_budget(self):
        self.post('A1', qty='7')
        self.post('A1', amount='1,200')
        self.post('A1', unit_price='2.5')
        self.assertEqual(ItemBudget.objects.count(), 1)
        for view, expected in (('qty', 7), ('amount', 1200), ('price', D('2.50'))):
            self.assertEqual(self.budgets(view)['A1'], expected, view)
            html = self.client.get(PAGE, {'view': view, 'year': 2026}).content.decode()
            for other, label in self.LABEL.items():
                self.assertEqual(label in html, other == view, (view, label))

    def test_clearing_one_budget_keeps_the_others(self):
        self.post('A1', qty='7')
        self.post('A1', amount='1200')
        self.post('A1', unit_price='2.50')
        self.assertEqual(self.post('A1', qty='').json(), {'ok': True, 'qty': None})
        budget = ItemBudget.objects.get()
        self.assertEqual((budget.qty, budget.amount, budget.unit_price), (None, 1200, D('2.50')))
        self.assertIsNone(self.budgets('qty')['A1'])
        self.post('A1', amount='')
        self.assertEqual(ItemBudget.objects.get().unit_price, D('2.50'))
        self.post('A1', unit_price='')
        self.assertFalse(ItemBudget.objects.exists())      # nothing left

    def test_amount_and_price_values(self):
        self.assertEqual(self.post('A1', unit_price='1,234.5').json(), {'ok': True, 'unit_price': '1234.50'})
        self.assertEqual(self.post('A1', amount='999999999999').status_code, 200)
        for bad in ({'amount': '10.5'}, {'amount': '-1'}, {'amount': '1000000000000'}, {'unit_price': '1.234'},
                    {'unit_price': '2,5'}, {'unit_price': '-2'}, {'unit_price': 'NaN'}, {'unit_price': '1000000000'},
                    {'qty': '1', 'amount': '1'}, {'other': '1'}):
            self.assertEqual(self.post('A1', **bad).status_code, 400, bad)
        budget = ItemBudget.objects.get()
        self.assertEqual((budget.qty, budget.amount, budget.unit_price), (None, 999999999999, D('1234.50')))

    def test_salesman_sees_but_cannot_change_amount_and_price(self):
        self.post('A1', amount='1200')
        self.post('A1', unit_price='2.50')
        self.client.force_login(self.salesman)
        for field in ('amount', 'unit_price'):
            self.assertEqual(self.post('A1', **{field: '9'}).status_code, 403, field)
        for view, shown in (('amount', '1,200'), ('price', '2.50')):
            html = self.client.get(PAGE, {'view': view, 'year': 2026}).content.decode()
            self.assertIn(shown, html)
            self.assertNotIn('class="budget-input"', html)
            self.assertNotIn(SAVE, html)

    def test_amount_and_price_tabs_still_list_only_sold_items(self):
        self.post('C1', amount='40')
        self.post('C1', unit_price='4')
        for view in ('amount', 'price'):
            self.assertEqual(set(self.budgets(view)), {'A1', 'B1'}, view)
        self.assertNotIn('C1', self.pdf_text())     # an amount / price budget is not a quantity budget

    def test_amount_and_price_excel_and_pdf_end_with_their_budget(self):
        from pypdf import PdfReader
        self.post('A1', amount='1234567')
        self.post('A1', unit_price='7.25')
        for view, column, value, shown in (('amount', 16, 1234567, '1,234,567'), ('price', 18, 7.25, '7.25')):
            params = {'view': view, 'year': 2026}
            resp = self.client.get(PAGE, {**params, 'export': 'xlsx'})
            sheet = list(load_workbook(io.BytesIO(resp.content)).worksheets[0].iter_rows(values_only=True))
            self.assertEqual((len(sheet[0]), sheet[0][column]), (column + 1, self.LABEL[view]))
            by_code = {r[0]: r for r in sheet[1:]}
            self.assertEqual((by_code['A1'][column], by_code['B1'][column]), (value, None))
            resp = self.client.get(PAGE, {**params, 'export': 'pdf'})
            text = ' '.join(' '.join(p.extract_text() for p in PdfReader(io.BytesIO(resp.content)).pages).split())
            self.assertIn(self.LABEL[view], text)
            self.assertIn(shown, text)
