"""PDF export on the combined quotations pages (Junaid and Alabama): same data as Excel, same user scope."""
import io
from datetime import date
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook
from pypdf import PdfReader

from alabama.models import AlabamaSAPQuotation, AlabamaSAPQuotationItem

from . import combined_quotations_pdf
from .models import Customer, Items, Quotation, QuotationItem, Role, Salesman, SAPQuotation, SAPQuotationItem

PAGES = ('', 'alabama:')


def url(namespace, name):
    return reverse(f'{namespace}export_combined_quotations_{name}')


@override_settings(ALLOWED_HOSTS=['testserver'])
class CombinedQuotationPdfTests(TestCase):
    def setUp(self):
        self.item = Items.objects.create(item_code='A1', item_description='Pipe 2″ <grey> & fittings', item_firm='X')
        salesman = Salesman.objects.create(salesman_name='KADER')
        customer = Customer.objects.create(customer_code='C1', customer_name='App <Customer> & Co', salesman=salesman)
        for division in ('JUNAID', 'ALABAMA'):
            quotation = Quotation.objects.create(customer=customer, salesman=salesman, division=division)
            Quotation.objects.filter(pk=quotation.pk).update(grand_total=150)
            QuotationItem.objects.create(quotation=quotation, item=self.item, quantity=3, price=50, line_total=150)
        for header, line, prefix in ((SAPQuotation, SAPQuotationItem, 'J'), (AlabamaSAPQuotation, AlabamaSAPQuotationItem, 'AL')):
            sap = header.objects.create(q_number=f'{prefix}900', posting_date=date(2026, 3, 4), customer_name='Sap Trading',
                                        salesman_name='OTHERMAN', document_total=1000)
            line.objects.create(quotation=sap, item_no='A1', description='Valve 4″', quantity=2, price=500, row_total=1000)
        self.admin = self._user('boss', 'Admin')
        self.client.force_login(self.admin)

    def _user(self, name, role, company='Junaid'):
        user = User.objects.create_user(name, password='x')
        Role.objects.create(user=user, role=role, company=company)
        return user

    def pdf(self, namespace, name, **params):
        resp = self.client.get(url(namespace, name), params)
        self.assertEqual(resp.status_code, 200, (namespace, name))
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertIn('.pdf"', resp['Content-Disposition'])
        pages = PdfReader(io.BytesIO(resp.content)).pages
        return ' '.join(' '.join(p.extract_text() for p in pages).split())

    def excel_numbers(self, namespace, name, **params):
        resp = self.client.get(url(namespace, name), params)
        rows = list(load_workbook(io.BytesIO(resp.content)).active.iter_rows(values_only=True))
        return [str(r[rows[0].index('Quotation Number')]) for r in rows[1:]]

    def test_summary_and_lines_have_the_same_quotations_as_excel(self):
        for namespace in PAGES:
            for pdf_name, excel_name in (('consolidated_pdf', 'consolidated_excel'), ('items_pdf', 'items_excel')):
                numbers = self.excel_numbers(namespace, excel_name)
                self.assertEqual(len(numbers), 2, (namespace, excel_name))
                text = self.pdf(namespace, pdf_name)
                for number in numbers:
                    self.assertIn(number, text)
                self.assertIn('1,150.00', text)          # 1000 SAP + 150 app
                self.assertIn('App <Customer> & Co', text)

    def test_inch_marks_and_markup_are_printed(self):
        for namespace in PAGES:
            text = self.pdf(namespace, 'items_pdf')
            self.assertIn('Valve 4"', text)
            self.assertIn('Pipe 2" <grey> & fittings', text)

    def test_filters_apply_and_are_shown(self):
        for namespace in PAGES:
            text = self.pdf(namespace, 'consolidated_pdf', source='sap', q='trading')
            self.assertIn('Sap Trading', text)
            self.assertNotIn('App <Customer>', text)
            self.assertIn('Source: sap', text)
            self.assertIn('Search: trading', text)
            self.assertIn('No quotations match', self.pdf(namespace, 'consolidated_pdf', start_date='2030-01-01'))

    def test_salesman_gets_only_own_quotations(self):
        self.client.force_login(self._user('nobody', 'Salesman'))
        for namespace in PAGES:
            for name in ('consolidated_pdf', 'items_pdf'):
                text = self.pdf(namespace, name, salesman_filter='OTHERMAN')
                self.assertNotIn('Sap Trading', text)
                self.assertNotIn('OTHERMAN', text)

    def test_too_many_rows_is_refused_with_a_message(self):
        with mock.patch.object(combined_quotations_pdf, 'SUMMARY_ROW_LIMIT', 1), \
                mock.patch.object(combined_quotations_pdf, 'ITEMS_ROW_LIMIT', 1):
            for namespace in PAGES:
                for name in ('consolidated_pdf', 'items_pdf'):
                    resp = self.client.get(url(namespace, name))
                    self.assertContains(resp, 'too many for one PDF', status_code=400)
                    self.assertEqual(self.client.get(url(namespace, name.replace('pdf', 'excel'))).status_code, 200)

    def test_long_pdf_repeats_the_heading_row(self):
        for i in range(150):
            SAPQuotation.objects.create(q_number=f'L{i:03d}', posting_date=date(2026, 3, 4), customer_name='Bulk ' * 40,
                                        salesman_name='OTHERMAN', document_total=1)
        text = self.pdf('', 'consolidated_pdf')
        self.assertGreater(text.count('Customer name'), 1)
        self.assertIn('L000', text)

    def test_login_is_required(self):
        self.client.logout()
        for namespace in PAGES:
            for name in ('consolidated_pdf', 'items_pdf'):
                self.assertEqual(self.client.get(url(namespace, name)).status_code, 302, (namespace, name))

    def test_pages_have_all_four_buttons(self):
        for namespace in PAGES:
            html = self.client.get(reverse(f'{namespace}combined_quotations_list')).content.decode()
            for button in ('btnCombinedExportSummary', 'btnCombinedExportItems', 'btnCombinedPdfSummary', 'btnCombinedPdfItems'):
                self.assertIn(f'id="{button}"', html)
            for name in ('consolidated_pdf', 'items_pdf'):
                self.assertIn(url(namespace, name), html)
