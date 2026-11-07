from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import AlabamaSAPQuotation, AlabamaSAPQuotationItem


class QuotationPdfTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alabama_x', password='pw')
        q = AlabamaSAPQuotation.objects.create(q_number='126000721', customer_name='ACME & Co',
                                               document_total=Decimal('150.00'), bill_to='Line1\nLine2 <b>')
        AlabamaSAPQuotationItem.objects.create(quotation=q, item_no='A1', description='Pipe <x> & more',
                                               quantity=Decimal('2'), price=Decimal('75'), row_total=Decimal('150'))

    def test_pdf_downloads(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('alabama:export_quotation_pdf', args=['126000721']))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))
        self.assertIn('126000721', r['Content-Disposition'])

    def test_missing_is_404(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('alabama:export_quotation_pdf', args=['nope'])).status_code, 404)

    def test_login_required(self):
        r = self.client.get(reverse('alabama:export_quotation_pdf', args=['126000721']))
        self.assertEqual(r.status_code, 302)

    def test_detail_page_has_button_and_still_works(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('alabama:quotation_detail', args=['126000721']))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'export-pdf')


class QuotationPdfDesignTests(QuotationPdfTests):
    """Same checks again, plus: it is the app's quotation design on the Alabama letterhead, and it falls back safely."""

    def _text(self, content):
        from io import BytesIO
        from PyPDF2 import PdfReader
        return '\n'.join(p.extract_text() or '' for p in PdfReader(BytesIO(content)).pages)

    def test_app_design_content(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse('alabama:export_quotation_pdf', args=['126000721']))
        text = self._text(r.content)
        for needle in ('QUOTATION', 'ACME', '126000721', 'GRAND TOTAL', 'VAT 5%', 'TERMS AND CONDITIONS'):
            self.assertIn(needle, text)
        self.assertNotIn('Profit', text)
        self.assertNotIn('Margin', text)

    def test_falls_back_to_simple_layout_if_app_design_fails(self):
        from unittest import mock
        self.client.force_login(self.user)
        with mock.patch('alabama.quotation_pdf.build_alabama_quotation_pdf', side_effect=RuntimeError('boom')):
            r = self.client.get(reverse('alabama:export_quotation_pdf', args=['126000721']))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b'%PDF'))
