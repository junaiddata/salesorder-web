"""Item Monthly Sales > Sold Qty tab and Proposed Budget 2027."""
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
        self.assertEqual(sheet[0][15:], ('Total Qty', 'Proposed Budget 2027'))
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

    def test_other_tabs_have_no_budget_and_only_sold_items(self):
        self.save('C1', '40')
        for view in ('amount', 'price'):
            resp = self.client.get(PAGE, {'view': view, 'year': 2026})
            self.assertEqual({r['item_code'] for r in resp.context['rows']}, {'A1', 'B1'}, view)
            self.assertNotIn('Proposed Budget', resp.content.decode())
